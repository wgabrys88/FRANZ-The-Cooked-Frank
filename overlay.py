"""Persistent overlay window process.

Runs as a long-lived subprocess started by main.py. Maintains a
full-screen layered window that draws action marks and cursor
positions. In OVERLAY_DEBUG mode, the window blocks all mouse and
keyboard input from reaching the OS.

Communication with capture.py is via filesystem + named Win32 event:
  marks.json        - accumulated action marks (read by this process)
  cursor_state.json - current/previous cursor positions (read by this)
  FranzOverlayRefresh - named event signaled by capture.py after
                        updating the JSON files, triggers a redraw

The overlay redraws when signaled and also on a 2-second timer as
a fallback. The window persists between turns, giving the human
observer a continuous view of the agent's action history.

Mark colors:
  white  = left click
  green  = double click
  blue   = right click
  yellow = drag line
  red    = current cursor position
  faded  = previous cursor position

Designed for Python 3.13 on Windows 11. No pip dependencies.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import json
import sys
import time
from pathlib import Path
from typing import Final

_SRCCOPY: Final = 0x00CC0020
_BI_RGB: Final = 0
_DIB_RGB: Final = 0

_WS_EX_LAYERED: Final = 0x00080000
_WS_EX_TRANSPARENT: Final = 0x00000020
_WS_EX_TOPMOST: Final = 0x00000008
_WS_EX_TOOLWINDOW: Final = 0x00000080
_WS_POPUP: Final = 0x80000000
_SW_SHOWNOACTIVATE: Final = 8
_ULW_ALPHA: Final = 0x00000002
_AC_SRC_OVER: Final = 0x00
_AC_SRC_ALPHA: Final = 0x01
_PM_REMOVE: Final = 0x0001
_WM_QUIT: Final = 0x0012
_ERROR_CLASS_ALREADY_EXISTS: Final = 1410
_WAIT_OBJECT_0: Final = 0x00000000
_WAIT_TIMEOUT: Final = 0x00000102
_EVENT_MODIFY_STATE: Final = 0x0002
_SYNCHRONIZE: Final = 0x00100000

_REFRESH_EVENT_NAME = "FranzOverlayRefresh"
_POLL_INTERVAL_MS: Final = 2000

try:
    ctypes.WinDLL("shcore", use_last_error=True).SetProcessDpiAwareness(2)
except Exception:
    pass

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


def _log(msg: str) -> None:
    sys.stderr.write(f"[overlay.py] {msg}\n")
    sys.stderr.flush()


def _get_screen_size() -> tuple[int, int]:
    w = _user32.GetSystemMetrics(0)
    h = _user32.GetSystemMetrics(1)
    if w <= 0 or h <= 0:
        return 1920, 1080
    return w, h


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.wintypes.DWORD), ("biWidth", ctypes.wintypes.LONG),
        ("biHeight", ctypes.wintypes.LONG), ("biPlanes", ctypes.wintypes.WORD),
        ("biBitCount", ctypes.wintypes.WORD), ("biCompression", ctypes.wintypes.DWORD),
        ("biSizeImage", ctypes.wintypes.DWORD), ("biXPelsPerMeter", ctypes.wintypes.LONG),
        ("biYPelsPerMeter", ctypes.wintypes.LONG), ("biClrUsed", ctypes.wintypes.DWORD),
        ("biClrImportant", ctypes.wintypes.DWORD),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", ctypes.wintypes.DWORD * 3)]


class _BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_byte), ("BlendFlags", ctypes.c_byte),
        ("SourceConstantAlpha", ctypes.c_byte), ("AlphaFormat", ctypes.c_byte),
    ]


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


class _MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", ctypes.wintypes.HWND), ("message", ctypes.c_uint),
        ("wParam", ctypes.wintypes.WPARAM), ("lParam", ctypes.wintypes.LPARAM),
        ("time", ctypes.wintypes.DWORD), ("pt", _POINT),
    ]


_WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_long, ctypes.wintypes.HWND, ctypes.c_uint,
    ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM,
)


class _WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", ctypes.c_uint),
        ("lpfnWndProc", _WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", ctypes.wintypes.HINSTANCE),
        ("hIcon", ctypes.wintypes.HICON),
        ("hCursor", ctypes.wintypes.HANDLE),
        ("hbrBackground", ctypes.wintypes.HBRUSH),
        ("lpszMenuName", ctypes.wintypes.LPCWSTR),
        ("lpszClassName", ctypes.wintypes.LPCWSTR),
    ]


def _make_bmi(w: int, h: int) -> _BITMAPINFO:
    bmi = _BITMAPINFO()
    hdr = bmi.bmiHeader
    hdr.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
    hdr.biWidth, hdr.biHeight = w, -h
    hdr.biPlanes, hdr.biBitCount, hdr.biCompression = 1, 32, _BI_RGB
    return bmi


def _pump_messages() -> bool:
    """Drain message queue. Returns False if WM_QUIT received."""
    msg = _MSG()
    while _user32.PeekMessageW(ctypes.byref(msg), 0, 0, 0, _PM_REMOVE):
        if msg.message == _WM_QUIT:
            return False
        _user32.TranslateMessage(ctypes.byref(msg))
        _user32.DispatchMessageW(ctypes.byref(msg))
    return True


def _norm(v: int, extent: int) -> int:
    return int((max(0, min(1000, v)) / 1000.0) * extent)


def _draw_filled_circle(
    buf: ctypes.Array, w: int, h: int,
    px: int, py: int, radius: int,
    r: int, g: int, b: int, a: int,
) -> None:
    r2 = radius * radius
    pa = a / 255.0
    pb, pg, pr = int(b * pa), int(g * pa), int(r * pa)
    for oy in range(-radius, radius + 1):
        yy = py + oy
        if yy < 0 or yy >= h:
            continue
        for ox in range(-radius, radius + 1):
            if ox * ox + oy * oy > r2:
                continue
            xx = px + ox
            if 0 <= xx < w:
                i = (yy * w + xx) * 4
                buf[i] = pb
                buf[i + 1] = pg
                buf[i + 2] = pr
                buf[i + 3] = a


def _draw_line(
    buf: ctypes.Array, w: int, h: int,
    x1: int, y1: int, x2: int, y2: int,
    r: int, g: int, b: int, a: int, thickness: int,
) -> None:
    dx, dy = abs(x2 - x1), abs(y2 - y1)
    sx = 1 if x1 < x2 else -1
    sy = 1 if y1 < y2 else -1
    err = dx - dy
    half = thickness >> 1
    pa = a / 255.0
    pb, pg, pr = int(b * pa), int(g * pa), int(r * pa)
    x, y = x1, y1
    while True:
        for oy in range(-half, half + 1):
            yy = y + oy
            if yy < 0 or yy >= h:
                continue
            for ox in range(-half, half + 1):
                xx = x + ox
                if 0 <= xx < w:
                    i = (yy * w + xx) * 4
                    buf[i] = pb
                    buf[i + 1] = pg
                    buf[i + 2] = pr
                    buf[i + 3] = a
        if x == x2 and y == y2:
            break
        e2 = err << 1
        if e2 > -dy:
            err -= dy
            x += sx
        if e2 < dx:
            err += dx
            y += sy


def _load_json(path: Path, default: object = None) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _render_overlay(
    hwnd: int, sdc: int,
    screen_w: int, screen_h: int,
    marks: list[dict],
    cursor_state: dict,
) -> None:
    """Redraw the overlay window contents."""
    memdc = _gdi32.CreateCompatibleDC(sdc)
    if not memdc:
        return

    bits = ctypes.c_void_p()
    hbmp = _gdi32.CreateDIBSection(
        sdc, ctypes.byref(_make_bmi(screen_w, screen_h)), _DIB_RGB,
        ctypes.byref(bits), None, 0,
    )
    if not hbmp or not bits.value:
        _gdi32.DeleteDC(memdc)
        return

    old = _gdi32.SelectObject(memdc, hbmp)
    buf = (ctypes.c_ubyte * (screen_w * screen_h * 4)).from_address(bits.value)
    ctypes.memset(bits, 0, screen_w * screen_h * 4)

    sw, sh = screen_w, screen_h

    for mark in marks:
        mt = mark.get("type", "")
        match mt:
            case "click":
                px, py = _norm(mark["x"], sw), _norm(mark["y"], sh)
                _draw_filled_circle(buf, sw, sh, px, py, 10, 255, 255, 255, 220)
            case "double_click":
                px, py = _norm(mark["x"], sw), _norm(mark["y"], sh)
                _draw_filled_circle(buf, sw, sh, px, py, 10, 0, 220, 0, 220)
            case "right_click":
                px, py = _norm(mark["x"], sw), _norm(mark["y"], sh)
                _draw_filled_circle(buf, sw, sh, px, py, 10, 80, 140, 255, 220)
            case "drag":
                px1, py1 = _norm(mark["x1"], sw), _norm(mark["y1"], sh)
                px2, py2 = _norm(mark["x2"], sw), _norm(mark["y2"], sh)
                _draw_line(buf, sw, sh, px1, py1, px2, py2, 255, 220, 0, 200, 4)

    prev_x = cursor_state.get("prev_x")
    prev_y = cursor_state.get("prev_y")
    if isinstance(prev_x, int) and isinstance(prev_y, int):
        ppx, ppy = _norm(prev_x, sw), _norm(prev_y, sh)
        _draw_filled_circle(buf, sw, sh, ppx, ppy, 12, 255, 0, 0, 70)

    cur_x = cursor_state.get("last_x")
    cur_y = cursor_state.get("last_y")
    if isinstance(cur_x, int) and isinstance(cur_y, int):
        cpx, cpy = _norm(cur_x, sw), _norm(cur_y, sh)
        _draw_filled_circle(buf, sw, sh, cpx, cpy, 14, 255, 255, 255, 240)
        _draw_filled_circle(buf, sw, sh, cpx, cpy, 10, 255, 0, 0, 220)

    pt_pos = _POINT(0, 0)
    pt_size = _SIZE(screen_w, screen_h)
    blend = _BLENDFUNCTION(_AC_SRC_OVER, 0, 255, _AC_SRC_ALPHA)

    _user32.UpdateLayeredWindow(
        hwnd, sdc, ctypes.byref(pt_pos), ctypes.byref(pt_size),
        memdc, ctypes.byref(pt_pos), 0, ctypes.byref(blend), _ULW_ALPHA,
    )

    _gdi32.SelectObject(memdc, old)
    _gdi32.DeleteObject(hbmp)
    _gdi32.DeleteDC(memdc)


def main() -> None:
    # Parse arguments: run_dir and debug flag
    if len(sys.argv) < 3:
        _log("Usage: overlay.py <run_dir> <debug:0|1>")
        sys.exit(1)

    run_dir = Path(sys.argv[1])
    debug = sys.argv[2] == "1"
    marks_path = run_dir / "marks.json"
    cursor_path = run_dir / "cursor_state.json"

    screen_w, screen_h = _get_screen_size()
    _log(f"Screen: {screen_w}x{screen_h}, debug={debug}, run_dir={run_dir}")

    # Register window class
    wc = _WNDCLASSW()
    wc.lpfnWndProc = _WNDPROC(ctypes.windll.user32.DefWindowProcW)
    wc.hInstance = _user32.GetModuleHandleW(None)
    wc.lpszClassName = "FranzOverlayPersistent"

    atom = _user32.RegisterClassW(ctypes.byref(wc))
    if not atom:
        err_code = ctypes.get_last_error()
        if err_code != _ERROR_CLASS_ALREADY_EXISTS:
            _log(f"RegisterClassW failed: error {err_code}")
            sys.exit(1)

    # Create window
    ex_style = _WS_EX_LAYERED | _WS_EX_TOPMOST | _WS_EX_TOOLWINDOW
    if not debug:
        ex_style |= _WS_EX_TRANSPARENT

    hwnd = _user32.CreateWindowExW(
        ex_style,
        "FranzOverlayPersistent", "", _WS_POPUP,
        0, 0, screen_w, screen_h,
        None, None, _user32.GetModuleHandleW(None), None,
    )
    if not hwnd:
        _log(f"CreateWindowExW failed: error {ctypes.get_last_error()}")
        sys.exit(1)

    _user32.ShowWindow(hwnd, _SW_SHOWNOACTIVATE)
    _pump_messages()
    _log("Overlay window created and shown")

    # Create or open named event for refresh signaling
    h_event = _kernel32.CreateEventW(None, False, False, _REFRESH_EVENT_NAME)
    if not h_event:
        _log(f"CreateEventW failed: error {ctypes.get_last_error()}")
        # Continue without event -- will use polling only
    else:
        _log(f"Refresh event '{_REFRESH_EVENT_NAME}' ready")

    sdc = _user32.GetDC(0)
    if not sdc:
        _log("GetDC(0) failed")
        sys.exit(1)

    # Initial render (empty)
    _render_overlay(hwnd, sdc, screen_w, screen_h, [], {})

    last_marks_mtime = 0.0
    last_cursor_mtime = 0.0

    _log("Entering main loop")
    try:
        while True:
            # Wait on the event or timeout for polling
            if h_event:
                wait_result = _kernel32.WaitForSingleObject(h_event, _POLL_INTERVAL_MS)
            else:
                time.sleep(_POLL_INTERVAL_MS / 1000.0)
                wait_result = _WAIT_TIMEOUT

            # Pump window messages to keep the window responsive
            if not _pump_messages():
                _log("WM_QUIT received, exiting")
                break

            # Check if files changed (by mtime or after event signal)
            needs_redraw = False

            try:
                mt = marks_path.stat().st_mtime if marks_path.exists() else 0.0
                if mt != last_marks_mtime:
                    last_marks_mtime = mt
                    needs_redraw = True
            except OSError:
                pass

            try:
                ct = cursor_path.stat().st_mtime if cursor_path.exists() else 0.0
                if ct != last_cursor_mtime:
                    last_cursor_mtime = ct
                    needs_redraw = True
            except OSError:
                pass

            # Also redraw on event signal regardless of mtime
            if wait_result == _WAIT_OBJECT_0:
                needs_redraw = True

            if needs_redraw:
                marks_data = _load_json(marks_path, [])
                marks = marks_data if isinstance(marks_data, list) else []
                cursor_data = _load_json(cursor_path, {})
                cursor_state = cursor_data if isinstance(cursor_data, dict) else {}
                _render_overlay(hwnd, sdc, screen_w, screen_h, marks, cursor_state)

    except KeyboardInterrupt:
        _log("Interrupted")
    finally:
        _user32.ReleaseDC(0, sdc)
        _user32.DestroyWindow(hwnd)
        _pump_messages()
        if h_event:
            _kernel32.CloseHandle(h_event)
        _log("Overlay process exiting")


if __name__ == "__main__":
    main()
