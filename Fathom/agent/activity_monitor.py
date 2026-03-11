"""
activity_monitor.py — Collects high-level user activity metrics.

Collected data (all non-sensitive):
  - Active window title and application name
  - Keyboard event RATE (events-per-minute — NOT the actual keys)
  - Mouse event RATE (events-per-minute — NOT positions or clicks individually)
  - Idle time in seconds
  - Browser process name + active domain (domain only, NOT full URLs)

PRIVACY GUARANTEES:
  - Keystrokes are NEVER recorded, stored, or transmitted.
  - Mouse positions/clicks are NEVER recorded individually.
  - Full URLs are NEVER transmitted — only the registered domain.
  - Passwords, form data, and clipboard contents are NEVER accessed.
"""

import re
import sys
import time
import threading
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_PLATFORM = sys.platform

# ── Known browsers ────────────────────────────────────────────────────────────

BROWSER_PROCESSES = {
    "chrome", "chromium", "firefox", "msedge", "opera", "brave",
    "safari", "iexplore", "vivaldi",
}

BROWSER_DISPLAY_NAMES = {
    "chrome":    "Google Chrome",
    "chromium":  "Chromium",
    "firefox":   "Mozilla Firefox",
    "msedge":    "Microsoft Edge",
    "opera":     "Opera",
    "brave":     "Brave",
    "safari":    "Safari",
    "iexplore":  "Internet Explorer",
    "vivaldi":   "Vivaldi",
}

# ── Activity rate tracker (pynput path) ───────────────────────────────────────

class ActivityRateTracker:
    """
    Thread-safe EPM counter fed by pynput hooks.
    Uses a 10-second rolling window scaled to per-minute.
    """
    WINDOW_SECONDS   = 10
    MOUSE_THROTTLE_S = 0.1

    def __init__(self):
        self._lock             = threading.Lock()
        self._kb_events:    list[float] = []
        self._mouse_events: list[float] = []
        self._last_event_time: float    = time.monotonic()
        self._last_mouse_move: float    = 0.0

    def record_keyboard_event(self):
        now = time.monotonic()
        with self._lock:
            self._kb_events.append(now)
            self._last_event_time = now

    def record_mouse_click(self):
        now = time.monotonic()
        with self._lock:
            self._mouse_events.append(now)
            self._last_event_time = now

    def record_mouse_move(self):
        now = time.monotonic()
        with self._lock:
            if now - self._last_mouse_move < self.MOUSE_THROTTLE_S:
                return
            self._last_mouse_move = now
            self._mouse_events.append(now)
            self._last_event_time = now

    def _prune(self, events, now):
        cutoff = now - self.WINDOW_SECONDS
        return [t for t in events if t > cutoff]

    def get_rates(self) -> dict:
        now = time.monotonic()
        with self._lock:
            self._kb_events    = self._prune(self._kb_events, now)
            self._mouse_events = self._prune(self._mouse_events, now)
            kb_count  = len(self._kb_events)
            ms_count  = len(self._mouse_events)
            idle      = now - self._last_event_time
        scale = 60.0 / self.WINDOW_SECONDS
        return {
            "keyboard_events_per_min": round(kb_count * scale),
            "mouse_events_per_min":    round(ms_count * scale),
            "idle_seconds":            round(idle, 1),
        }


_tracker = ActivityRateTracker()
_kb_listener    = None
_mouse_listener = None


# ── GetLastInputInfo poller (works elevated + Session 0) ──────────────────────

class InputInfoPoller:
    """
    Polls Windows GetLastInputInfo every 0.25s on a background thread.

    This works correctly regardless of:
      - Whether the process is elevated (Run as Admin)
      - Whether it's running as a Windows Service (Session 0)
      - Whether pynput hooks are available

    Derives separate KB and mouse EPM by also checking which type of
    input changed using GetAsyncKeyState sampling and cursor position delta.
    """
    POLL_INTERVAL  = 0.25
    WINDOW_SECONDS = 10

    def __init__(self):
        self._lock              = threading.Lock()
        self._kb_times:    list[float] = []
        self._mouse_times: list[float] = []
        self._last_input_tick:  int   = 0
        self._last_cursor_pos         = (0, 0)
        self._last_event_time: float  = time.monotonic()
        self._running          = False
        self._thread           = None

    def _get_last_input_tick(self) -> int:
        try:
            import ctypes, ctypes.wintypes
            class LASTINPUTINFO(ctypes.Structure):
                _fields_ = [("cbSize", ctypes.wintypes.UINT),
                             ("dwTime", ctypes.wintypes.DWORD)]
            lii = LASTINPUTINFO()
            lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
            ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii))
            return int(lii.dwTime)
        except Exception:
            return 0

    def _get_cursor_pos(self) -> tuple[int, int]:
        try:
            import ctypes, ctypes.wintypes
            pt = ctypes.wintypes.POINT()
            ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
            return (pt.x, pt.y)
        except Exception:
            return (0, 0)

    def _any_key_pressed(self) -> bool:
        """Sample a broad set of common keys to detect keyboard activity."""
        try:
            import ctypes
            # Check a range of virtual key codes (letters, digits, common keys)
            for vk in list(range(0x30, 0x5A)) + [0x20, 0x0D, 0x08, 0x09, 0x10, 0x11]:
                if ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8001:
                    return True
        except Exception:
            pass
        return False

    def _poll_loop(self):
        self._last_input_tick = self._get_last_input_tick()
        self._last_cursor_pos = self._get_cursor_pos()

        while self._running:
            now  = time.monotonic()
            tick = self._get_last_input_tick()

            if tick != self._last_input_tick:
                # Input happened — figure out if it was KB or mouse
                cursor = self._get_cursor_pos()
                moved  = (cursor != self._last_cursor_pos)

                with self._lock:
                    self._last_event_time = now
                    if moved:
                        self._mouse_times.append(now)
                    else:
                        self._kb_times.append(now)
                    self._last_cursor_pos = cursor
                self._last_input_tick = tick

            time.sleep(self.POLL_INTERVAL)

    def start(self):
        self._running = True
        self._thread  = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        logger.info("InputInfoPoller started (works elevated + service mode)")

    def get_rates(self) -> dict:
        now    = time.monotonic()
        cutoff = now - self.WINDOW_SECONDS
        with self._lock:
            self._kb_times    = [t for t in self._kb_times    if t > cutoff]
            self._mouse_times = [t for t in self._mouse_times if t > cutoff]
            kb_count  = len(self._kb_times)
            ms_count  = len(self._mouse_times)
            idle      = now - self._last_event_time

        scale = 60.0 / self.WINDOW_SECONDS
        return {
            "keyboard_events_per_min": round(kb_count * scale),
            "mouse_events_per_min":    round(ms_count * scale),
            "idle_seconds":            round(idle, 1),
        }


# Module-level poller instance
_input_poller: InputInfoPoller | None = None


def start_input_listeners() -> None:
    """
    Start input monitoring. Always uses InputInfoPoller (works in all contexts:
    normal, elevated/admin, and Windows Service). Also tries pynput as a
    supplementary source if available.
    """
    global _input_poller, _kb_listener, _mouse_listener

    # Always start the poller — it works everywhere
    _input_poller = InputInfoPoller()
    _input_poller.start()

    # Also try pynput for richer per-event data (may fail silently if elevated)
    try:
        from pynput import keyboard as _kb_mod, mouse as _mouse_mod

        def on_key(key):
            _tracker.record_keyboard_event()

        def on_click(x, y, button, pressed):
            if pressed:
                _tracker.record_mouse_click()

        def on_move(x, y):
            _tracker.record_mouse_move()

        def on_scroll(x, y, dx, dy):
            _tracker.record_mouse_move()

        _kb_listener = _kb_mod.Listener(on_press=on_key, suppress=False)
        _kb_listener.daemon = True
        _kb_listener.start()

        _mouse_listener = _mouse_mod.Listener(
            on_click=on_click, on_move=on_move, on_scroll=on_scroll
        )
        _mouse_listener.daemon = True
        _mouse_listener.start()
        logger.info("pynput listeners started (supplementary)")
    except Exception as exc:
        logger.info("pynput not available (%s) — using InputInfoPoller only", exc)


# Also export for service wrapper compatibility
start_wts_poller = start_input_listeners


def get_activity_rates() -> dict:
    """
    Return activity rates, preferring InputInfoPoller which works in all contexts.
    Falls back to pynput tracker if poller unavailable.
    """
    if _input_poller is not None:
        return _input_poller.get_rates()
    return _tracker.get_rates()


# ── Active window (uses psutil process list — works elevated) ─────────────────

def _get_active_window_win32() -> tuple[str, str]:
    """
    Get the active window title and process name on Windows.

    When running elevated, GetForegroundWindow may return the wrong window.
    We use a two-step approach:
      1. Try GetForegroundWindow (works in normal sessions)
      2. Fall back to scanning psutil for the most likely foreground app
         by finding the highest-CPU non-system process
    """
    title    = ""
    proc_name = ""

    try:
        import ctypes
        import ctypes.wintypes
        import psutil

        hwnd = ctypes.windll.user32.GetForegroundWindow()

        if hwnd:
            # Get window title
            length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
                title = buf.value or ""

            # Get process from window handle
            pid = ctypes.wintypes.DWORD()
            ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value > 4:   # ignore System (PID 4) and Idle (PID 0)
                try:
                    proc      = psutil.Process(pid.value)
                    proc_name = proc.name().lower().replace(".exe", "")
                except Exception:
                    pass

        # If we got "system idle process" or empty, fall back to process scan
        if proc_name in ("", "system", "idle", "system idle process"):
            title, proc_name = _get_foreground_by_process_scan()

    except Exception as exc:
        logger.debug("GetForegroundWindow failed: %s", exc)
        title, proc_name = _get_foreground_by_process_scan()

    return title, proc_name


def _get_foreground_by_process_scan() -> tuple[str, str]:
    """
    Find the likely active application by scanning visible top-level windows
    and matching them to running processes. Works when elevated.
    """
    try:
        import ctypes
        import ctypes.wintypes
        import psutil

        # Build a map of pid -> process name for non-system processes
        proc_map = {}
        IGNORED  = {"system", "idle", "svchost", "csrss", "smss",
                    "wininit", "services", "lsass", "winlogon", "dwm",
                    "taskhostw", "conhost", "sihost", "fontdrvhost"}
        for proc in psutil.process_iter(['pid', 'name']):
            try:
                name = proc.info['name'].lower().replace('.exe', '')
                if name not in IGNORED and proc.info['pid'] > 4:
                    proc_map[proc.info['pid']] = name
            except Exception:
                continue

        # Enumerate all visible top-level windows and find one with a title
        found_title = ""
        found_proc  = ""

        def enum_callback(hwnd, _):
            nonlocal found_title, found_proc
            if found_title:
                return True   # already found one
            try:
                if not ctypes.windll.user32.IsWindowVisible(hwnd):
                    return True
                length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
                if length < 2:
                    return True
                buf = ctypes.create_unicode_buffer(length + 1)
                ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
                title = buf.value.strip()
                if not title or title in ("Program Manager", ""):
                    return True

                pid = ctypes.wintypes.DWORD()
                ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                proc_name = proc_map.get(pid.value, "")
                if proc_name:
                    found_title = title
                    found_proc  = proc_name
            except Exception:
                pass
            return True

        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
        ctypes.windll.user32.EnumWindows(WNDENUMPROC(enum_callback), 0)
        return found_title, found_proc

    except Exception as exc:
        logger.debug("Process scan fallback failed: %s", exc)
        return "", ""


def _get_active_window_darwin() -> tuple[str, str]:
    try:
        import subprocess
        script = (
            'tell application "System Events" to get '
            '{name, title of front window} of first application process '
            'whose frontmost is true'
        )
        result = subprocess.run(["osascript", "-e", script],
                                capture_output=True, text=True, timeout=2)
        parts     = result.stdout.strip().split(", ", 1)
        proc_name = parts[0].strip().lower() if parts else ""
        title     = parts[1].strip() if len(parts) > 1 else ""
        return title, proc_name
    except Exception:
        return "", ""


def _get_active_window_linux() -> tuple[str, str]:
    try:
        import subprocess
        result = subprocess.run(
            ["xdotool", "getactivewindow", "getwindowname"],
            capture_output=True, text=True, timeout=2
        )
        title = result.stdout.strip()
        pid_result = subprocess.run(
            ["xdotool", "getactivewindow", "getwindowpid"],
            capture_output=True, text=True, timeout=2
        )
        proc_name = ""
        if pid_result.returncode == 0:
            import psutil
            try:
                proc      = psutil.Process(int(pid_result.stdout.strip()))
                proc_name = proc.name().lower()
            except Exception:
                pass
        return title, proc_name
    except Exception:
        return "", ""


def get_active_window() -> tuple[str, str]:
    if _PLATFORM == "win32":
        return _get_active_window_win32()
    elif _PLATFORM == "darwin":
        return _get_active_window_darwin()
    else:
        return _get_active_window_linux()


# ── Browser detection ─────────────────────────────────────────────────────────

def get_running_browser() -> str:
    try:
        import psutil
        for proc in psutil.process_iter(['name']):
            try:
                name = proc.info['name'].lower().replace('.exe', '')
                if name in BROWSER_PROCESSES:
                    return BROWSER_DISPLAY_NAMES.get(name, name)
            except Exception:
                continue
    except Exception:
        pass
    return ""


_STRIP_BROWSER_BRAND = re.compile(
    r'\s*[-–—|]\s*(?:Google Chrome|Chromium|Mozilla Firefox|Firefox|'
    r'Microsoft Edge|Opera|Brave|Safari|Internet Explorer)\s*$',
    re.IGNORECASE,
)

_DOMAIN_RE = re.compile(
    r'\b((?:[a-zA-Z0-9](?:[a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?\.)'
    r'+(?:com|org|net|edu|gov|io|co|app|dev|ai|uk|de|fr|ca|au|jp|'
    r'ru|br|cn|in|me|tv|info|biz|xyz|tech|cloud|online|store|shop))\b'
)


def _extract_domain_from_title(title: str) -> str:
    if not title:
        return ""
    cleaned  = _STRIP_BROWSER_BRAND.sub("", title).strip()
    segments = re.split(r'[-–—|·•]\s*', cleaned)
    for segment in reversed(segments):
        segment = segment.strip()
        if '.' in segment:
            match = _DOMAIN_RE.search(segment)
            if match:
                candidate = match.group(1).lower()
                if candidate not in ("e.g", "i.e"):
                    return candidate
    matches = _DOMAIN_RE.findall(cleaned)
    return matches[-1].lower() if matches else ""


# ── Public API ────────────────────────────────────────────────────────────────

def get_activity_metrics() -> dict:
    title, proc_name = get_active_window()
    rates            = get_activity_rates()

    clean_proc      = proc_name.lower().replace(".exe", "")
    browser_process = ""
    browser_domain  = ""

    if clean_proc in BROWSER_PROCESSES:
        browser_process = BROWSER_DISPLAY_NAMES.get(clean_proc, clean_proc)
        browser_domain  = _extract_domain_from_title(title)
    else:
        running = get_running_browser()
        if running:
            browser_process = f"{running} (background)"

    return {
        "active_window_title":     title[:120] if title else "",
        "active_app":              proc_name[:64] if proc_name else "",
        "keyboard_events_per_min": rates["keyboard_events_per_min"],
        "mouse_events_per_min":    rates["mouse_events_per_min"],
        "idle_seconds":            rates["idle_seconds"],
        "browser_process":         browser_process,
        "browser_domain":          browser_domain,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
