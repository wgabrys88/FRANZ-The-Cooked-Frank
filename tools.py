"""Tool functions for the VLM agent.

Coordinates 0-1000 inclusive. Physical Win32 input via SendInput when
PHYSICAL_EXECUTION is True. Simulation mode records actions as OK but
sends no Win32 calls.

Designed for Python 3.13 on Windows 11. No pip dependencies.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import json
import time
from pathlib import Path
from typing import Final

_INPUT_MOUSE: Final = 0
_INPUT_KEYBOARD: Final = 1
_MOUSEEVENTF_MOVE: Final = 0x0001
_MOUSEEVENTF_LEFTDOWN: Final = 0x0002
_MOUSEEVENTF_LEFTUP: Final = 0x0004
_MOUSEEVENTF_RIGHTDOWN: Final = 0x0008
_MOUSEEVENTF_RIGHTUP: Final = 0x0010
_MOUSEEVENTF_ABSOLUTE: Final = 0x8000
_KEYEVENTF_KEYUP: Final = 0x0002
_KEYEVENTF_UNICODE: Final = 0x0004
_MOVE_STEPS: Final = 20
_STEP_DELAY: Final = 0.01
_CLICK_DELAY: Final = 0.12

_ULONG_PTR = ctypes.c_size_t


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long), ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong), ("dwExtraInfo", _ULONG_PTR),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
        ("dwExtraInfo", _ULONG_PTR),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("u", _INPUTUNION)]


_user32: ctypes.WinDLL | None = None
_screen_w: int = 0
_screen_h: int = 0

_execute: bool = True
_physical: bool = False
_executed: list[str] = []
_ignored: list[str] = []
_run_dir: str = ""


def _init_win32() -> None:
    global _user32, _screen_w, _screen_h
    if _user32 is not None:
        return
    ctypes.WinDLL("shcore", use_last_error=True).SetProcessDpiAwareness(2)
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _screen_w = _user32.GetSystemMetrics(0)
    _screen_h = _user32.GetSystemMetrics(1)
    _user32.SendInput.argtypes = (ctypes.c_uint, ctypes.POINTER(_INPUT), ctypes.c_int)
    _user32.SendInput.restype = ctypes.c_uint


def _send_inputs(items: list[_INPUT]) -> None:
    assert _user32 is not None
    if not items:
        return
    arr = (_INPUT * len(items))(*items)
    if _user32.SendInput(len(items), arr, ctypes.sizeof(_INPUT)) != len(items):
        raise OSError(ctypes.get_last_error())


def _send_mouse(flags: int, abs_x: int | None = None, abs_y: int | None = None) -> None:
    inp = _INPUT(type=_INPUT_MOUSE)
    f, dx, dy = flags, 0, 0
    if abs_x is not None and abs_y is not None:
        dx, dy, f = abs_x, abs_y, f | _MOUSEEVENTF_ABSOLUTE | _MOUSEEVENTF_MOVE
    inp.u.mi = _MOUSEINPUT(dx, dy, 0, f, 0, 0)
    _send_inputs([inp])


def _send_unicode(text: str) -> None:
    items: list[_INPUT] = []
    for ch in text:
        if ch == "\r":
            continue
        code = 0x000D if ch == "\n" else ord(ch)
        for fl in (_KEYEVENTF_UNICODE, _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP):
            inp = _INPUT(type=_INPUT_KEYBOARD)
            inp.u.ki = _KEYBDINPUT(0, code, fl, 0, 0)
            items.append(inp)
    _send_inputs(items)


def _to_px(v: int, dim: int) -> int:
    return int((v / 1000) * dim)


def _to_abs(x_px: int, y_px: int) -> tuple[int, int]:
    return (
        max(0, min(65535, int((x_px / max(1, _screen_w - 1)) * 65535))),
        max(0, min(65535, int((y_px / max(1, _screen_h - 1)) * 65535))),
    )


def _smooth_move(tx: int, ty: int) -> None:
    assert _user32 is not None
    pt = ctypes.wintypes.POINT()
    _user32.GetCursorPos(ctypes.byref(pt))
    sx, sy = pt.x, pt.y
    ddx, ddy = tx - sx, ty - sy
    for i in range(_MOVE_STEPS + 1):
        t = i / _MOVE_STEPS
        t = t * t * (3.0 - 2.0 * t)
        _send_mouse(0, *_to_abs(int(sx + ddx * t), int(sy + ddy * t)))
        time.sleep(_STEP_DELAY)


def _mouse_click(down: int, up: int) -> None:
    _send_mouse(down)
    time.sleep(0.02)
    _send_mouse(up)


def _phys_click(x: int, y: int) -> None:
    _smooth_move(_to_px(x, _screen_w), _to_px(y, _screen_h))
    time.sleep(_CLICK_DELAY)
    _mouse_click(_MOUSEEVENTF_LEFTDOWN, _MOUSEEVENTF_LEFTUP)


def _phys_right_click(x: int, y: int) -> None:
    _smooth_move(_to_px(x, _screen_w), _to_px(y, _screen_h))
    time.sleep(_CLICK_DELAY)
    _mouse_click(_MOUSEEVENTF_RIGHTDOWN, _MOUSEEVENTF_RIGHTUP)


def _phys_double_click(x: int, y: int) -> None:
    _phys_click(x, y)
    time.sleep(0.06)
    _phys_click(x, y)


def _phys_drag(x1: int, y1: int, x2: int, y2: int) -> None:
    _smooth_move(_to_px(x1, _screen_w), _to_px(y1, _screen_h))
    time.sleep(0.08)
    _send_mouse(_MOUSEEVENTF_LEFTDOWN)
    time.sleep(0.06)
    _smooth_move(_to_px(x2, _screen_w), _to_px(y2, _screen_h))
    time.sleep(0.06)
    _send_mouse(_MOUSEEVENTF_LEFTUP)


def configure(*, execute: bool, physical: bool, run_dir: str) -> None:
    global _execute, _physical, _executed, _ignored, _run_dir
    _execute = execute
    _physical = physical
    _executed = []
    _ignored = []
    _run_dir = run_dir
    if physical:
        _init_win32()


def get_results() -> tuple[list[str], list[str]]:
    return list(_executed), list(_ignored)


def _validate_coord(name: str, v: object) -> int:
    if not isinstance(v, int | float):
        raise TypeError(f"{name} must be a number, got {type(v).__name__}")
    iv = int(v)
    if not 0 <= iv <= 1000:
        raise ValueError(f"{name}={iv} outside valid range 0-1000")
    return iv


def _record(canon: str) -> bool:
    if not _execute:
        _ignored.append(canon)
        return False
    _executed.append(canon)
    return _physical


def click(x: int, y: int) -> None:
    """click(x, y) -- Left-click at (x, y). Coordinates 0-1000."""
    ix, iy = _validate_coord("x", x), _validate_coord("y", y)
    if _record(f"click({ix}, {iy})"):
        _phys_click(ix, iy)


def right_click(x: int, y: int) -> None:
    """right_click(x, y) -- Right-click at (x, y). Coordinates 0-1000."""
    ix, iy = _validate_coord("x", x), _validate_coord("y", y)
    if _record(f"right_click({ix}, {iy})"):
        _phys_right_click(ix, iy)


def double_click(x: int, y: int) -> None:
    """double_click(x, y) -- Double-click at (x, y). Coordinates 0-1000."""
    ix, iy = _validate_coord("x", x), _validate_coord("y", y)
    if _record(f"double_click({ix}, {iy})"):
        _phys_double_click(ix, iy)


def drag(x1: int, y1: int, x2: int, y2: int) -> None:
    """drag(x1, y1, x2, y2) -- Drag from (x1,y1) to (x2,y2). Coordinates 0-1000."""
    ix1, iy1 = _validate_coord("x1", x1), _validate_coord("y1", y1)
    ix2, iy2 = _validate_coord("x2", x2), _validate_coord("y2", y2)
    if _record(f"drag({ix1}, {iy1}, {ix2}, {iy2})"):
        _phys_drag(ix1, iy1, ix2, iy2)


def write(text: str) -> None:
    """write(text) -- Type text at current cursor position."""
    if not isinstance(text, str):
        raise TypeError(f"write() requires str, got {type(text).__name__}")
    if _record(f"write({json.dumps(text)})"):
        _send_unicode(text)


def _memory_path() -> Path:
    return Path(_run_dir) / "memory.json" if _run_dir else Path("memory.json")


def remember(text: str) -> None:
    """remember(text) -- Store a learning for future turns and sessions."""
    if not isinstance(text, str):
        raise TypeError(f"remember() requires str, got {type(text).__name__}")
    p = _memory_path()
    items: list[str] = []
    try:
        items = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    items.append(text)
    p.write_text(json.dumps(items, indent=2), encoding="utf-8")
    _record(f"remember({json.dumps(text)})")


def recall() -> str:
    """recall() -- Read all stored learnings. Returns a string."""
    p = _memory_path()
    try:
        items = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(items, list) and items:
            return "\n".join(f"- {s}" for s in items)
    except Exception:
        pass
    return "(no memories yet)"


def help(fn: object = None) -> str:
    """help() -- List functions. help(fn) -- Show help for fn."""
    if fn is not None and callable(fn):
        return getattr(fn, "__doc__", None) or f"{getattr(fn, '__name__', '?')}()"
    return "\n".join(f.__doc__ for f in _PUBLIC if f.__doc__)


_PUBLIC: Final[tuple[object, ...]] = (
    click, right_click, double_click, drag, write, remember, recall, help,
)

TOOL_NAMES: Final[tuple[str, ...]] = (
    "click", "right_click", "double_click", "drag", "write",
    "remember", "recall", "help",
)
