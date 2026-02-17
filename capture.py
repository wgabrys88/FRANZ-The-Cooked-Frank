"""Screenshot producer.

Takes a screenshot of the full screen (or reads the virtual canvas),
optionally signals the persistent overlay, resizes, encodes as PNG,
returns base64 via stdout JSON.

When VIRTUAL_CANVAS is True, no real screen capture occurs. Instead
a black image file (virtual_canvas.bmp) in the run directory serves
as the screen. Action marks are drawn directly onto this image and
saved back to disk each turn. The VLM sees only this canvas.

When OVERLAY_DEBUG is True (and VIRTUAL_CANVAS is False), the
persistent overlay process (overlay.py) draws action marks on a
full-screen window. This process signals the overlay to refresh
after updating marks, waits briefly, then captures the real screen
(which includes the overlay).

Mark colors:
  white  = left click
  green  = double click
  blue   = right click
  yellow = drag line
  red    = current cursor position (ephemeral)
  faded  = previous cursor position (ephemeral)

Designed for Python 3.13 on Windows 11. No pip dependencies.
"""

from __future__ import annotations

import ast as _ast
import base64
import ctypes
import ctypes.wintypes
import json
import struct
import sys
import time
import zlib
from pathlib import Path
from typing import Final

import config as franz_config

_SRCCOPY: Final = 0x00CC0020
_CAPTUREBLT: Final = 0x40000000
_BI_RGB: Final = 0
_DIB_RGB: Final = 0
_HALFTONE: Final = 4

_EVENT_MODIFY_STATE: Final = 0x0002
_REFRESH_EVENT_NAME = "FranzOverlayRefresh"

try:
    ctypes.WinDLL("shcore", use_last_error=True).SetProcessDpiAwareness(2)
except Exception:
    pass

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)



# ---------------------------------------------------------------------------
# Fix #1: Declare argtypes/restype for all Win32 calls so that 64-bit
# HANDLEs are not truncated to 32-bit c_int (confirmed on this system).
# ---------------------------------------------------------------------------
_user32.GetDC.argtypes = [ctypes.wintypes.HWND]
_user32.GetDC.restype = ctypes.wintypes.HDC

_user32.ReleaseDC.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.HDC]
_user32.ReleaseDC.restype = ctypes.c_int

_user32.GetSystemMetrics.argtypes = [ctypes.c_int]
_user32.GetSystemMetrics.restype = ctypes.c_int

_gdi32.CreateCompatibleDC.argtypes = [ctypes.wintypes.HDC]
_gdi32.CreateCompatibleDC.restype = ctypes.wintypes.HDC

_gdi32.CreateDIBSection.argtypes = [
    ctypes.wintypes.HDC, ctypes.c_void_p, ctypes.wintypes.UINT,
    ctypes.POINTER(ctypes.c_void_p), ctypes.wintypes.HANDLE, ctypes.wintypes.DWORD,
]
_gdi32.CreateDIBSection.restype = ctypes.wintypes.HBITMAP

_gdi32.SelectObject.argtypes = [ctypes.wintypes.HDC, ctypes.wintypes.HGDIOBJ]
_gdi32.SelectObject.restype = ctypes.wintypes.HGDIOBJ

_gdi32.BitBlt.argtypes = [
    ctypes.wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    ctypes.wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.wintypes.DWORD,
]
_gdi32.BitBlt.restype = ctypes.wintypes.BOOL

_gdi32.StretchBlt.argtypes = [
    ctypes.wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    ctypes.wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    ctypes.wintypes.DWORD,
]
_gdi32.StretchBlt.restype = ctypes.wintypes.BOOL

_gdi32.SetStretchBltMode.argtypes = [ctypes.wintypes.HDC, ctypes.c_int]
_gdi32.SetStretchBltMode.restype = ctypes.c_int

_gdi32.SetBrushOrgEx.argtypes = [
    ctypes.wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_void_p,
]
_gdi32.SetBrushOrgEx.restype = ctypes.wintypes.BOOL

_gdi32.DeleteObject.argtypes = [ctypes.wintypes.HGDIOBJ]
_gdi32.DeleteObject.restype = ctypes.wintypes.BOOL

_gdi32.DeleteDC.argtypes = [ctypes.wintypes.HDC]
_gdi32.DeleteDC.restype = ctypes.wintypes.BOOL

_kernel32.OpenEventW.argtypes = [ctypes.wintypes.DWORD, ctypes.wintypes.BOOL, ctypes.wintypes.LPCWSTR]
_kernel32.OpenEventW.restype = ctypes.wintypes.HANDLE

_kernel32.SetEvent.argtypes = [ctypes.wintypes.HANDLE]
_kernel32.SetEvent.restype = ctypes.wintypes.BOOL

_kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
_kernel32.CloseHandle.restype = ctypes.wintypes.BOOL
# ---------------------------------------------------------------------------


def _get_screen_size() -> tuple[int, int]:
    w = _user32.GetSystemMetrics(0)
    h = _user32.GetSystemMetrics(1)
    if w <= 0 or h <= 0:
        _log("GetSystemMetrics returned invalid size, defaulting 1920x1080")
        return 1920, 1080
    return w, h


def _log(msg: str) -> None:
    sys.stderr.write(f"[capture.py] {msg}\n")
    sys.stderr.flush()


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


def _make_bmi(w: int, h: int) -> _BITMAPINFO:
    bmi = _BITMAPINFO()
    hdr = bmi.bmiHeader
    hdr.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
    hdr.biWidth, hdr.biHeight = w, -h
    hdr.biPlanes, hdr.biBitCount, hdr.biCompression = 1, 32, _BI_RGB
    return bmi


def _capture_bgra(w: int, h: int) -> bytes | None:
    sdc = _user32.GetDC(0)
    if not sdc:
        _log("GetDC(0) failed")
        return None

    memdc = _gdi32.CreateCompatibleDC(sdc)
    if not memdc:
        _log("CreateCompatibleDC failed")
        _user32.ReleaseDC(0, sdc)
        return None

    bits = ctypes.c_void_p()
    hbmp = _gdi32.CreateDIBSection(
        sdc, ctypes.byref(_make_bmi(w, h)), _DIB_RGB,
        ctypes.byref(bits), None, 0,
    )
    if not hbmp or not bits.value:
        _log(f"CreateDIBSection failed: hbmp={hbmp}, bits={bits.value}")
        _gdi32.DeleteDC(memdc)
        _user32.ReleaseDC(0, sdc)
        return None

    old = _gdi32.SelectObject(memdc, hbmp)
    _gdi32.BitBlt(memdc, 0, 0, w, h, sdc, 0, 0, _SRCCOPY | _CAPTUREBLT)

    try:
        result = bytes((ctypes.c_ubyte * (w * h * 4)).from_address(bits.value))
    except Exception as exc:
        _log(f"Failed to read DIB bits: {exc}")
        result = None

    _gdi32.SelectObject(memdc, old)
    _gdi32.DeleteObject(hbmp)
    _gdi32.DeleteDC(memdc)
    _user32.ReleaseDC(0, sdc)
    return result


# def _resize_bgra(src: bytes, sw: int, sh: int, dw: int, dh: int) -> bytes | None:
#     sdc = _user32.GetDC(0)
#     if not sdc:
#         _log("GetDC(0) failed in resize")
#         return None

#     src_dc = _gdi32.CreateCompatibleDC(sdc)
#     dst_dc = _gdi32.CreateCompatibleDC(sdc)
#     if not src_dc or not dst_dc:
#         _log("CreateCompatibleDC failed in resize")
#         if src_dc:
#             _gdi32.DeleteDC(src_dc)
#         if dst_dc:
#             _gdi32.DeleteDC(dst_dc)
#         _user32.ReleaseDC(0, sdc)
#         return None

#     src_bmp = _gdi32.CreateCompatibleBitmap(sdc, sw, sh)
#     if not src_bmp:
#         _log("CreateCompatibleBitmap failed in resize")
#         _gdi32.DeleteDC(src_dc)
#         _gdi32.DeleteDC(dst_dc)
#         _user32.ReleaseDC(0, sdc)
#         return None

#     old_src = _gdi32.SelectObject(src_dc, src_bmp)

#     dst_bits = ctypes.c_void_p()
#     dst_bmp = _gdi32.CreateDIBSection(
#         sdc, ctypes.byref(_make_bmi(dw, dh)), _DIB_RGB,
#         ctypes.byref(dst_bits), None, 0,
#     )
#     if not dst_bmp or not dst_bits.value:
#         _log(f"CreateDIBSection failed in resize: dst_bmp={dst_bmp}")
#         _gdi32.SelectObject(src_dc, old_src)
#         _gdi32.DeleteObject(src_bmp)
#         _gdi32.DeleteDC(src_dc)
#         _gdi32.DeleteDC(dst_dc)
#         _user32.ReleaseDC(0, sdc)
#         return None

#     old_dst = _gdi32.SelectObject(dst_dc, dst_bmp)
#     _gdi32.SetDIBits(sdc, src_bmp, 0, sh, src, ctypes.byref(_make_bmi(sw, sh)), _DIB_RGB)
#     _gdi32.SetStretchBltMode(dst_dc, _HALFTONE)
#     _gdi32.SetBrushOrgEx(dst_dc, 0, 0, None)
#     _gdi32.StretchBlt(dst_dc, 0, 0, dw, dh, src_dc, 0, 0, sw, sh, _SRCCOPY)

#     try:
#         result = bytes((ctypes.c_ubyte * (dw * dh * 4)).from_address(dst_bits.value))
#     except Exception as exc:
#         _log(f"Failed to read resized DIB bits: {exc}")
#         result = None

#     _gdi32.SelectObject(dst_dc, old_dst)
#     _gdi32.SelectObject(src_dc, old_src)
#     _gdi32.DeleteObject(dst_bmp)
#     _gdi32.DeleteObject(src_bmp)
#     _gdi32.DeleteDC(dst_dc)
#     _gdi32.DeleteDC(src_dc)
#     _user32.ReleaseDC(0, sdc)
#     return result

def _resize_bgra(src: bytes, sw: int, sh: int, dw: int, dh: int) -> bytes | None:
    sdc = _user32.GetDC(0)
    if not sdc:
        _log("GetDC(0) failed in resize")
        return None

    src_dc = _gdi32.CreateCompatibleDC(sdc)
    dst_dc = _gdi32.CreateCompatibleDC(sdc)
    if not src_dc or not dst_dc:
        _log("CreateCompatibleDC failed in resize")
        if src_dc: _gdi32.DeleteDC(src_dc)
        if dst_dc: _gdi32.DeleteDC(dst_dc)
        _user32.ReleaseDC(0, sdc)
        return None

    # ── Source: DIB section (top-down, matches our captured data) ──
    src_bits = ctypes.c_void_p()
    src_bmp = _gdi32.CreateDIBSection(
        sdc, ctypes.byref(_make_bmi(sw, sh)), _DIB_RGB,
        ctypes.byref(src_bits), None, 0,
    )
    if not src_bmp or not src_bits.value:
        _log("Source CreateDIBSection failed in resize")
        _gdi32.DeleteDC(src_dc)
        _gdi32.DeleteDC(dst_dc)
        _user32.ReleaseDC(0, sdc)
        return None

    # Copy source pixels directly into the DIB section memory
    # No format ambiguity — both are top-down BGRA, same layout
    ctypes.memmove(src_bits.value, src, sw * sh * 4)

    old_src = _gdi32.SelectObject(src_dc, src_bmp)

    # ── Destination: DIB section ──
    dst_bits = ctypes.c_void_p()
    dst_bmp = _gdi32.CreateDIBSection(
        sdc, ctypes.byref(_make_bmi(dw, dh)), _DIB_RGB,
        ctypes.byref(dst_bits), None, 0,
    )
    if not dst_bmp or not dst_bits.value:
        _log("Dest CreateDIBSection failed in resize")
        _gdi32.SelectObject(src_dc, old_src)
        _gdi32.DeleteObject(src_bmp)
        _gdi32.DeleteDC(src_dc)
        _gdi32.DeleteDC(dst_dc)
        _user32.ReleaseDC(0, sdc)
        return None

    old_dst = _gdi32.SelectObject(dst_dc, dst_bmp)

    _gdi32.SetStretchBltMode(dst_dc, _HALFTONE)
    _gdi32.SetBrushOrgEx(dst_dc, 0, 0, None)
    _gdi32.StretchBlt(dst_dc, 0, 0, dw, dh, src_dc, 0, 0, sw, sh, _SRCCOPY)

    try:
        result = bytes((ctypes.c_ubyte * (dw * dh * 4)).from_address(dst_bits.value))
    except Exception as exc:
        _log(f"Failed to read resized DIB bits: {exc}")
        result = None

    _gdi32.SelectObject(dst_dc, old_dst)
    _gdi32.SelectObject(src_dc, old_src)
    _gdi32.DeleteObject(dst_bmp)
    _gdi32.DeleteObject(src_bmp)
    _gdi32.DeleteDC(dst_dc)
    _gdi32.DeleteDC(src_dc)
    _user32.ReleaseDC(0, sdc)
    return result



def _bgra_to_rgba(bgra: bytes) -> bytearray:
    n = len(bgra)
    out = bytearray(n)
    out[0::4] = bgra[2::4]
    out[1::4] = bgra[1::4]
    out[2::4] = bgra[0::4]
    out[3::4] = b"\xff" * (n // 4)
    return out


def _encode_png(rgba: bytes, w: int, h: int) -> bytes:
    stride = w * 4
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        raw.extend(rgba[y * stride:(y + 1) * stride])

    def chunk(tag: bytes, body: bytes) -> bytes:
        crc = zlib.crc32(tag + body) & 0xFFFFFFFF
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", crc)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + chunk(b"IEND", b"")
    )


def _norm(v: int, extent: int) -> int:
    return int((max(0, min(1000, v)) / 1000.0) * extent)


def _parse_action_coords(line: str) -> tuple[str, list[int]]:
    s = line.strip()
    try:
        node = _ast.parse(s, mode="eval").body
    except SyntaxError:
        return ("", [])
    if not isinstance(node, _ast.Call) or not isinstance(node.func, _ast.Name):
        return ("", [])
    name = node.func.id
    args = [
        int(a.value) for a in node.args
        if isinstance(a, _ast.Constant) and isinstance(a.value, int | float)
    ]
    return (name, args)


def _state_load(path: Path) -> dict[str, int | None]:
    result: dict[str, int | None] = {
        "last_x": None, "last_y": None, "prev_x": None, "prev_y": None,
    }
    try:
        o = json.loads(path.read_text(encoding="utf-8"))
        for key in result:
            v = o.get(key)
            if isinstance(v, int):
                result[key] = v
    except Exception:
        pass
    return result


def _atomic_write(path: Path, data: str) -> None:
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(data, encoding="utf-8")
        tmp.replace(path)
    except Exception:
        tmp.unlink(missing_ok=True)


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_bytes(data)
        tmp.replace(path)
    except Exception:
        tmp.unlink(missing_ok=True)


def _actions_to_marks(actions: list[str]) -> list[dict]:
    new_marks: list[dict] = []
    for line in actions:
        name, args = _parse_action_coords(line)
        match name:
            case "click" if len(args) >= 2:
                new_marks.append({"type": "click", "x": args[0], "y": args[1]})
            case "double_click" if len(args) >= 2:
                new_marks.append({"type": "double_click", "x": args[0], "y": args[1]})
            case "right_click" if len(args) >= 2:
                new_marks.append({"type": "right_click", "x": args[0], "y": args[1]})
            case "drag" if len(args) >= 4:
                new_marks.append({"type": "drag", "x1": args[0], "y1": args[1],
                                  "x2": args[2], "y2": args[3]})
    return new_marks


def _update_cursor_state(actions: list[str], state_path: Path) -> dict[str, int | None]:
    st = _state_load(state_path)
    st["prev_x"], st["prev_y"] = st["last_x"], st["last_y"]
    for line in actions:
        name, args = _parse_action_coords(line)
        if name in ("click", "right_click", "double_click") and len(args) >= 2:
            st["last_x"], st["last_y"] = args[0], args[1]
        elif name == "drag" and len(args) >= 4:
            st["last_x"], st["last_y"] = args[2], args[3]
    _atomic_write(state_path, json.dumps(st))
    return st


def _signal_overlay() -> None:
    h_event = _kernel32.OpenEventW(
        _EVENT_MODIFY_STATE, False, _REFRESH_EVENT_NAME
    )
    if h_event:
        _kernel32.SetEvent(h_event)
        _kernel32.CloseHandle(h_event)
        _log("Signaled overlay refresh")
    else:
        _log("Overlay refresh event not found (overlay process may not be running)")


# ---------------------------------------------------------------------------
# Virtual canvas -- file-driven fake screen
# ---------------------------------------------------------------------------

def _canvas_path(run_dir: Path) -> Path:
    return run_dir / "virtual_canvas.bmp"


def _create_canvas(w: int, h: int, path: Path) -> None:
    """Create a black BGRA raw file at the given path."""
    data = b"\x00" * (w * h * 4)
    path.write_bytes(data)
    _log(f"Created virtual canvas: {w}x{h} at {path}")


def _load_canvas(w: int, h: int, path: Path) -> bytearray:
    """Load the canvas BGRA buffer. Creates if missing or wrong size."""
    expected = w * h * 4
    try:
        data = path.read_bytes()
        if len(data) == expected:
            return bytearray(data)
        _log(f"Canvas size mismatch: got {len(data)}, expected {expected}")
    except FileNotFoundError:
        pass
    except Exception as exc:
        _log(f"Canvas load error: {exc}")
    # Recreate
    buf = bytearray(expected)
    path.write_bytes(bytes(buf))
    return buf


def _save_canvas(buf: bytearray, path: Path) -> None:
    """Save canvas buffer back to disk."""
    _atomic_write_bytes(path, bytes(buf))


def _draw_circle_bgra(
    buf: bytearray, w: int, h: int,
    px: int, py: int, radius: int,
    b: int, g: int, r: int, a: int,
) -> None:
    """Draw a filled circle onto a BGRA buffer (pre-multiplied not needed for opaque)."""
    r2 = radius * radius
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
                if a >= 255:
                    buf[i] = b
                    buf[i + 1] = g
                    buf[i + 2] = r
                    buf[i + 3] = 255
                else:
                    # Alpha blend over existing
                    fa = a / 255.0
                    inv = 1.0 - fa
                    buf[i] = int(b * fa + buf[i] * inv)
                    buf[i + 1] = int(g * fa + buf[i + 1] * inv)
                    buf[i + 2] = int(r * fa + buf[i + 2] * inv)
                    buf[i + 3] = min(255, int(a + buf[i + 3] * inv))


def _draw_line_bgra(
    buf: bytearray, w: int, h: int,
    x1: int, y1: int, x2: int, y2: int,
    b: int, g: int, r: int, a: int, thickness: int,
) -> None:
    """Draw a line onto a BGRA buffer using Bresenham's algorithm."""
    dx, dy = abs(x2 - x1), abs(y2 - y1)
    sx = 1 if x1 < x2 else -1
    sy = 1 if y1 < y2 else -1
    err = dx - dy
    half = thickness >> 1
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
                    fa = a / 255.0
                    inv = 1.0 - fa
                    buf[i] = int(b * fa + buf[i] * inv)
                    buf[i + 1] = int(g * fa + buf[i + 1] * inv)
                    buf[i + 2] = int(r * fa + buf[i + 2] * inv)
                    buf[i + 3] = min(255, int(a + buf[i + 3] * inv))
        if x == x2 and y == y2:
            break
        e2 = err << 1
        if e2 > -dy:
            err -= dy
            x += sx
        if e2 < dx:
            err += dx
            y += sy


def _draw_marks_on_canvas(
    buf: bytearray, w: int, h: int,
    marks: list[dict],
    cursor_state: dict[str, int | None],
) -> None:
    """Draw new marks and cursor onto the canvas buffer (BGRA byte order)."""
    for mark in marks:
        mt = mark.get("type", "")
        match mt:
            case "click":
                px, py = _norm(mark["x"], w), _norm(mark["y"], h)
                _draw_circle_bgra(buf, w, h, px, py, 10, 255, 255, 255, 255)
            case "double_click":
                px, py = _norm(mark["x"], w), _norm(mark["y"], h)
                _draw_circle_bgra(buf, w, h, px, py, 10, 0, 220, 0, 255)
            case "right_click":
                px, py = _norm(mark["x"], w), _norm(mark["y"], h)
                _draw_circle_bgra(buf, w, h, px, py, 10, 255, 140, 80, 255)
            case "drag":
                px1, py1 = _norm(mark["x1"], w), _norm(mark["y1"], h)
                px2, py2 = _norm(mark["x2"], w), _norm(mark["y2"], h)
                _draw_line_bgra(buf, w, h, px1, py1, px2, py2, 0, 220, 255, 220, 4)

    # Draw ephemeral cursor indicators (these get overwritten each turn
    # because the canvas is persistent -- we draw them as semi-transparent)
    prev_x = cursor_state.get("prev_x")
    prev_y = cursor_state.get("prev_y")
    if isinstance(prev_x, int) and isinstance(prev_y, int):
        ppx, ppy = _norm(prev_x, w), _norm(prev_y, h)
        _draw_circle_bgra(buf, w, h, ppx, ppy, 12, 0, 0, 255, 50)

    cur_x = cursor_state.get("last_x")
    cur_y = cursor_state.get("last_y")
    if isinstance(cur_x, int) and isinstance(cur_y, int):
        cpx, cpy = _norm(cur_x, w), _norm(cur_y, h)
        _draw_circle_bgra(buf, w, h, cpx, cpy, 14, 255, 255, 255, 180)
        _draw_circle_bgra(buf, w, h, cpx, cpy, 10, 0, 0, 255, 200)


def _capture_virtual_canvas(
    actions: list[str],
    run_dir: Path,
    screen_w: int, screen_h: int,
) -> bytes:
    """Draw marks on virtual canvas and return the BGRA buffer."""
    cp = _canvas_path(run_dir)

    # Load existing canvas
    buf = _load_canvas(screen_w, screen_h, cp)

    # Get cursor state
    state_path = run_dir / "cursor_state.json"
    st = _update_cursor_state(actions, state_path)

    # Get new marks from this turn's actions
    new_marks = _actions_to_marks(actions)
    if new_marks:
        _log(f"Drawing {len(new_marks)} marks on virtual canvas")

    # Draw onto the canvas
    _draw_marks_on_canvas(buf, screen_w, screen_h, new_marks, st)

    # Save the modified canvas back to disk
    _save_canvas(buf, cp)

    return bytes(buf)


# ---------------------------------------------------------------------------
# Main capture logic
# ---------------------------------------------------------------------------

def capture(actions: list[str], run_dir: str) -> str:
    """Capture screenshot. Returns base64 PNG string, or '' on failure."""
    screen_w, screen_h = _get_screen_size()
    width = int(franz_config.WIDTH)
    height = int(franz_config.HEIGHT)
    delay = float(franz_config.CAPTURE_DELAY)
    debug = bool(franz_config.OVERLAY_DEBUG)
    virtual = bool(franz_config.VIRTUAL_CANVAS)

    rd = Path(run_dir) if run_dir else Path(".")

    if virtual:
        # Virtual canvas mode -- all drawing happens on the image file
        bgra = _capture_virtual_canvas(actions, rd, screen_w, screen_h)
    else:
        # Real screen capture mode
        state_path = rd / "cursor_state.json"
        marks_path = rd / "marks.json"

        _update_cursor_state(actions, state_path)

        if debug:
            marks: list[dict] = []
            try:
                data = json.loads(marks_path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    marks = data
            except Exception:
                pass
            marks.extend(_actions_to_marks(actions))
            _atomic_write(marks_path, json.dumps(marks))
            _signal_overlay()
            time.sleep(0.15)

        if delay > 0:
            time.sleep(delay)

        bgra_result = _capture_bgra(screen_w, screen_h)
        if bgra_result is None:
            _log("Screen capture returned None -- returning empty base64")
            return ""
        bgra = bgra_result

    # Resize if needed
    dw = screen_w if width <= 0 else width
    dh = screen_h if height <= 0 else height
    if (dw, dh) != (screen_w, screen_h):
        resized = _resize_bgra(bgra, screen_w, screen_h, dw, dh)
        if resized is None:
            _log("Resize failed -- using original resolution")
            dw, dh = screen_w, screen_h
        else:
            bgra = resized

    rgba = _bgra_to_rgba(bgra)
    png = _encode_png(bytes(rgba), dw, dh)
    return base64.b64encode(png).decode("ascii")


def main() -> None:
    try:
        req = json.loads(sys.stdin.read() or "{}")
        raw_actions = req.get("actions", [])
        actions = [str(a) for a in raw_actions] if isinstance(raw_actions, list) else []
        run_dir = str(req.get("run_dir", ""))
        b64 = capture(actions, run_dir)
        if not b64:
            _log("WARNING: capture() returned empty string")
        sys.stdout.write(json.dumps({"screenshot_b64": b64, "applied": actions}))
        sys.stdout.flush()
    except Exception as exc:
        _log(f"FATAL: {exc}")
        sys.stdout.write(json.dumps({"screenshot_b64": "", "applied": [], "error": str(exc)}))
        sys.stdout.flush()


if __name__ == "__main__":
    main()
