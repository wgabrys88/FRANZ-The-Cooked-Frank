#!/usr/bin/env python3
"""
FRANZ — single-file autonomous desktop narrative agent.
Requires: Windows 10+, Python 3.12+, LM Studio running on localhost:1235
          with a vision-language model loaded (e.g. qwen3-vl-2b-instruct-1m).
"""
from __future__ import annotations

import ast
import base64
import ctypes
import ctypes.wintypes
import importlib
import json
import logging
import msvcrt
import os
import struct
import subprocess
import sys
import time
import urllib.request
import urllib.error
import zlib
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, List


# ═══════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True, slots=True)
class Config:
    TEMPERATURE: float = 0.7
    TOP_P: float = 0.9
    MAX_TOKENS: int = 300
    MODEL: str = "qwen3-vl-2b-instruct-1m"
    WIDTH: int = 512
    HEIGHT: int = 288
    EXECUTE_ACTIONS: bool = True
    PHYSICAL_EXECUTION: bool = False
    OVERLAY_DEBUG: bool = False
    VIRTUAL_CANVAS: bool = True
    LOOP_DELAY: float = 2.0
    CAPTURE_DELAY: float = 1.0
    SAVE_SCREENSHOTS: bool = True


CONFIG = Config()


def reload_config() -> Config:
    """Attempt hot-reload from a config_overrides.json next to this script."""
    global CONFIG
    try:
        p = Path(__file__).with_name("config_overrides.json")
        if p.exists():
            overrides = json.loads(p.read_text(encoding="utf-8"))
            CONFIG = replace(CONFIG, **{k: v for k, v in overrides.items()
                                        if hasattr(CONFIG, k)})
    except Exception:
        pass
    return CONFIG


# ═══════════════════════════════════════════════════════════════════════════════
#  VLM SYSTEM PROMPT
# ═══════════════════════════════════════════════════════════════════════════════

SYSTEM_PROMPT: Final[str] = r"""You are FRANZ, an autonomous narrative agent who lives on a Windows desktop.
You see a screenshot of the current screen. You MUST:
1. Describe what you observe in 1-3 sentences (the "story").
2. Decide what to do next and emit ONE OR MORE tool calls, each on its own line.

Available tool calls (coordinates are 0-1000, mapped to screen pixels):
  click(x, y)            — left-click at (x, y)
  double_click(x, y)     — double-left-click
  right_click(x, y)      — right-click
  drag(x1, y1, x2, y2)   — click-drag from (x1,y1) to (x2,y2)
  write("text")           — type text via keyboard
  remember("note")        — save a note to persistent memory
  recall()                — retrieve all saved notes

Rules:
- Every response MUST contain at least one tool call line.
- Tool calls must appear as bare Python-style function calls on their own line.
- Do NOT wrap tool calls in markdown code blocks.
- Coordinates: 0=top/left edge, 500=center, 1000=bottom/right edge.
- Be creative, exploratory, and narrate your journey.
"""


# ═══════════════════════════════════════════════════════════════════════════════
#  DRAWING — marks, parsing, rendering
# ═══════════════════════════════════════════════════════════════════════════════

class MarkType(StrEnum):
    CLICK = "click"
    DOUBLE_CLICK = "double_click"
    RIGHT_CLICK = "right_click"
    DRAG = "drag"


@dataclass(slots=True, frozen=True)
class Mark:
    type: MarkType
    x: int
    y: int
    x2: int | None = None
    y2: int | None = None


def norm(v: int, extent: int) -> int:
    return int((max(0, min(1000, v)) / 1000) * extent)


def parse_marks(text: str) -> List[Mark]:
    marks: List[Mark] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            tree = ast.parse(line, mode="eval")
            if not isinstance(tree.body, ast.Call):
                continue
            func = tree.body.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            args = [
                int(a.value)
                for a in tree.body.args
                if isinstance(a, ast.Constant) and isinstance(a.value, (int, float))
            ]
            match name, len(args):
                case "click", n if n >= 2:
                    marks.append(Mark(MarkType.CLICK, args[0], args[1]))
                case "double_click", n if n >= 2:
                    marks.append(Mark(MarkType.DOUBLE_CLICK, args[0], args[1]))
                case "right_click", n if n >= 2:
                    marks.append(Mark(MarkType.RIGHT_CLICK, args[0], args[1]))
                case "drag", n if n >= 4:
                    marks.append(Mark(MarkType.DRAG, args[0], args[1], args[2], args[3]))
        except Exception:
            continue
    return marks


def _set_pixel(buf: bytearray, w: int, h: int, px: int, py: int,
               r: int, g: int, b: int, a: int = 255) -> None:
    """Set a single pixel in a BGRA buffer (top-down, stride = w*4)."""
    if 0 <= px < w and 0 <= py < h:
        off = (py * w + px) * 4
        buf[off]     = b
        buf[off + 1] = g
        buf[off + 2] = r
        buf[off + 3] = a


def _draw_filled_circle(buf: bytearray, w: int, h: int,
                        cx: int, cy: int, radius: int,
                        r: int, g: int, b: int, a: int = 255) -> None:
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            if dx * dx + dy * dy <= radius * radius:
                _set_pixel(buf, w, h, cx + dx, cy + dy, r, g, b, a)


def _draw_line(buf: bytearray, w: int, h: int,
               x0: int, y0: int, x1: int, y1: int,
               r: int, g: int, b: int, a: int = 255,
               thickness: int = 2) -> None:
    """Bresenham line with thickness via circles at each point."""
    dx = abs(x1 - x0)
    dy = -abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx + dy
    while True:
        _draw_filled_circle(buf, w, h, x0, y0, thickness, r, g, b, a)
        if x0 == x1 and y0 == y1:
            break
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x0 += sx
        if e2 <= dx:
            err += dx
            y0 += sy


# Color scheme: click=green, double=cyan, right=red, drag=yellow
_MARK_COLORS: dict[MarkType, tuple[int, int, int]] = {
    MarkType.CLICK:        (0, 255, 0),
    MarkType.DOUBLE_CLICK: (0, 255, 255),
    MarkType.RIGHT_CLICK:  (255, 0, 0),
    MarkType.DRAG:         (255, 255, 0),
}


def render_marks(buf: bytearray, w: int, h: int,
                 marks: List[Mark], history: List[Mark]) -> None:
    """Draw marks onto a BGRA pixel buffer."""
    # History marks: smaller, semi-transparent
    for m in history:
        col = _MARK_COLORS.get(m.type, (128, 128, 128))
        px, py = norm(m.x, w), norm(m.y, h)
        _draw_filled_circle(buf, w, h, px, py, 4, *col, 120)
        if m.type == MarkType.DRAG and m.x2 is not None and m.y2 is not None:
            px2, py2 = norm(m.x2, w), norm(m.y2, h)
            _draw_line(buf, w, h, px, py, px2, py2, *col, 120, 1)
            _draw_filled_circle(buf, w, h, px2, py2, 4, *col, 120)

    # Current marks: larger, fully opaque
    for m in marks:
        col = _MARK_COLORS.get(m.type, (255, 255, 255))
        px, py = norm(m.x, w), norm(m.y, h)
        _draw_filled_circle(buf, w, h, px, py, 8, *col)
        if m.type == MarkType.DRAG and m.x2 is not None and m.y2 is not None:
            px2, py2 = norm(m.x2, w), norm(m.y2, h)
            _draw_line(buf, w, h, px, py, px2, py2, *col, 255, 2)
            _draw_filled_circle(buf, w, h, px2, py2, 8, *col)


def ppm_from_buffer(w: int, h: int, buf: bytearray) -> bytes:
    header = f"P6\n{w} {h}\n255\n".encode()
    rgb = bytearray()
    for i in range(0, len(buf), 4):
        if i + 2 < len(buf):
            rgb.extend((buf[i + 2], buf[i + 1], buf[i]))
    return header + rgb


def png_from_bgra(w: int, h: int, buf: bytearray) -> bytes:
    """Encode a top-down BGRA buffer as a PNG file (RGB, no alpha). Pure Python."""

    def _chunk(chunk_type: bytes, data: bytes) -> bytes:
        c = chunk_type + data
        crc = struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
        return struct.pack(">I", len(data)) + c + crc

    # PNG signature
    sig = b"\x89PNG\r\n\x1a\n"

    # IHDR: width, height, bit_depth=8, color_type=2 (RGB)
    ihdr_data = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    ihdr = _chunk(b"IHDR", ihdr_data)

    # IDAT: build raw scanlines with filter byte 0 (None) per row
    raw_rows = bytearray()
    for y in range(h):
        raw_rows.append(0)  # filter: None
        row_off = y * w * 4
        for x in range(w):
            px = row_off + x * 4
            # BGRA → RGB
            raw_rows.append(buf[px + 2])  # R
            raw_rows.append(buf[px + 1])  # G
            raw_rows.append(buf[px])      # B

    compressed = zlib.compress(bytes(raw_rows), 6)
    idat = _chunk(b"IDAT", compressed)

    # IEND
    iend = _chunk(b"IEND", b"")

    return sig + ihdr + idat + iend


# ═══════════════════════════════════════════════════════════════════════════════
#  WIN32 GDI — screen capture helpers
# ═══════════════════════════════════════════════════════════════════════════════

@contextmanager
def screen_dc():
    hdc = ctypes.windll.user32.GetDC(0)
    try:
        yield hdc
    finally:
        ctypes.windll.user32.ReleaseDC(0, hdc)


@contextmanager
def compatible_dc(sdc):
    memdc = ctypes.windll.gdi32.CreateCompatibleDC(sdc)
    try:
        yield memdc
    finally:
        ctypes.windll.gdi32.DeleteDC(memdc)


@contextmanager
def dib_section(sdc, w, h):
    bits = ctypes.c_void_p()
    bmi = _make_bmi(w, h)
    hbmp = ctypes.windll.gdi32.CreateDIBSection(
        sdc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0
    )
    try:
        yield hbmp, bits
    finally:
        ctypes.windll.gdi32.DeleteObject(hbmp)


def _make_bmi(w, h):
    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int),
            ("biHeight", ctypes.c_int), ("biPlanes", ctypes.c_ushort),
            ("biBitCount", ctypes.c_ushort), ("biCompression", ctypes.c_uint32),
            ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int),
            ("biYPelsPerMeter", ctypes.c_int), ("biClrUsed", ctypes.c_uint32),
            ("biClrImportant", ctypes.c_uint32),
        ]
    bmi = BITMAPINFOHEADER()
    bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.biWidth = w
    bmi.biHeight = -h          # top-down DIB
    bmi.biPlanes = 1
    bmi.biBitCount = 32
    bmi.biCompression = 0
    return bmi


SRCCOPY: Final[int] = 0x00CC0020


def capture_screen(w: int = 1920, h: int = 1080) -> bytearray:
    """Capture the entire screen into a BGRA bytearray."""
    with screen_dc() as sdc:
        with compatible_dc(sdc) as memdc:
            with dib_section(sdc, w, h) as (hbmp, bits):
                old = ctypes.windll.gdi32.SelectObject(memdc, hbmp)
                ctypes.windll.gdi32.BitBlt(memdc, 0, 0, w, h, sdc, 0, 0, SRCCOPY)
                ctypes.windll.gdi32.SelectObject(memdc, old)
                size = w * h * 4
                buf = bytearray((ctypes.c_ubyte * size).from_address(bits.value))
    return buf


# ═══════════════════════════════════════════════════════════════════════════════
#  TOOLS — click, drag, write, remember, recall, help
# ═══════════════════════════════════════════════════════════════════════════════

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
    try:
        ctypes.WinDLL("shcore", use_last_error=True).SetProcessDpiAwareness(2)
    except Exception:
        pass
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _screen_w = _user32.GetSystemMetrics(0)
    _screen_h = _user32.GetSystemMetrics(1)
    _user32.SendInput.argtypes = (ctypes.c_uint, ctypes.POINTER(_INPUT), ctypes.c_int)
    _user32.SendInput.restype = ctypes.c_uint


def _send_inputs(items: list[_INPUT]) -> None:
    if not items:
        return
    arr = (_INPUT * len(items))(*items)
    if _user32.SendInput(len(items), arr, ctypes.sizeof(_INPUT)) != len(items):
        raise OSError(ctypes.get_last_error())


def _send_mouse(flags: int, abs_x: int | None = None, abs_y: int | None = None) -> None:
    inp = _INPUT(type=_INPUT_MOUSE)
    f, dx, dy = flags, 0, 0
    if abs_x is not None and abs_y is not None:
        dx, dy = abs_x, abs_y
        f = f | _MOUSEEVENTF_ABSOLUTE | _MOUSEEVENTF_MOVE
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


def configure_tools(*, execute: bool, physical: bool, run_dir: str) -> None:
    global _execute, _physical, _executed, _ignored, _run_dir
    _execute = execute
    _physical = physical
    _executed = []
    _ignored = []
    _run_dir = run_dir
    if physical:
        _init_win32()


def _validate_coord(name: str, v: object) -> int:
    if not isinstance(v, (int, float)):
        raise TypeError(f"{name} must be number")
    iv = int(v)
    if not 0 <= iv <= 1000:
        raise ValueError(f"{name}={iv} out of range")
    return iv


def _record(canon: str) -> bool:
    if not _execute:
        _ignored.append(canon)
        return False
    _executed.append(canon)
    return _physical


def click(x: int, y: int) -> None:
    """click(x, y) — left-click at normalized (x, y)."""
    ix, iy = _validate_coord("x", x), _validate_coord("y", y)
    if _record(f"click({ix},{iy})"):
        _phys_click(ix, iy)


def right_click(x: int, y: int) -> None:
    """right_click(x, y) — right-click at normalized (x, y)."""
    ix, iy = _validate_coord("x", x), _validate_coord("y", y)
    if _record(f"right_click({ix},{iy})"):
        _phys_right_click(ix, iy)


def double_click(x: int, y: int) -> None:
    """double_click(x, y) — double-left-click at normalized (x, y)."""
    ix, iy = _validate_coord("x", x), _validate_coord("y", y)
    if _record(f"double_click({ix},{iy})"):
        _phys_double_click(ix, iy)


def drag(x1: int, y1: int, x2: int, y2: int) -> None:
    """drag(x1, y1, x2, y2) — click-drag from (x1,y1) to (x2,y2)."""
    ix1, iy1 = _validate_coord("x1", x1), _validate_coord("y1", y1)
    ix2, iy2 = _validate_coord("x2", x2), _validate_coord("y2", y2)
    if _record(f"drag({ix1},{iy1},{ix2},{iy2})"):
        _phys_drag(ix1, iy1, ix2, iy2)


def write(text: str) -> None:
    """write("text") — type text via keyboard."""
    if not isinstance(text, str):
        raise TypeError("write needs str")
    if _record(f"write({json.dumps(text)})"):
        _send_unicode(text)


def _memory_path() -> Path:
    return Path(_run_dir) / "memory.json" if _run_dir else Path("memory.json")


def remember(text: str) -> None:
    """remember("note") — save a note to persistent memory."""
    if not isinstance(text, str):
        raise TypeError("remember needs str")
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
    """recall() — retrieve all saved notes."""
    p = _memory_path()
    try:
        items = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(items, list) and items:
            return "\n".join(f"- {s}" for s in items)
    except Exception:
        pass
    return "(no memories yet)"


def tool_help(fn: object = None) -> str:
    """help() — show available tool calls."""
    if fn is not None and callable(fn):
        return getattr(fn, "__doc__", "") or f"{getattr(fn, '__name__', '?')}()"
    return "\n".join(filter(None, [
        click.__doc__, right_click.__doc__, double_click.__doc__,
        drag.__doc__, write.__doc__, remember.__doc__,
        recall.__doc__, tool_help.__doc__,
    ]))


TOOL_NAMES: Final[tuple[str, ...]] = (
    "click", "right_click", "double_click", "drag",
    "write", "remember", "recall", "help",
)

# Map tool names to the actual functions in this module
_TOOL_FUNCS: dict[str, Any] = {
    "click": click, "right_click": right_click,
    "double_click": double_click, "drag": drag,
    "write": write, "remember": remember,
    "recall": recall, "help": tool_help,
}


# ═══════════════════════════════════════════════════════════════════════════════
#  PERSISTENCE
# ═══════════════════════════════════════════════════════════════════════════════

class FranzPersistence:
    def __init__(self, base_dir: str = "runs"):
        self.run_dir = Path(base_dir) / datetime.now().strftime("%Y%m%d_%H%M%S")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "screenshots").mkdir(exist_ok=True)
        self.story_path = self.run_dir / "mind_story.txt"
        self.jsonl_path = self.run_dir / "turns.jsonl"
        self.log_path = self.run_dir / "session.log"
        self.state_path = self.run_dir / "state.json"
        self.memory_path = self.run_dir / "memory.json"
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s | %(levelname)s | %(message)s",
            handlers=[
                logging.FileHandler(self.log_path, encoding="utf-8"),
                logging.StreamHandler(),
            ],
        )
        self.logger = logging.getLogger("franz")
        self.turn = 0
        self.paused = False
        self._load_state()

    def _load_state(self) -> None:
        if self.state_path.exists():
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.turn = data.get("turn", 0)
            self.paused = data.get("paused", False)

    def save_state(self) -> None:
        self.state_path.write_text(
            json.dumps({
                "turn": self.turn,
                "paused": self.paused,
                "last_update": datetime.now().isoformat(),
            }, indent=2),
            encoding="utf-8",
        )

    def new_turn(self, story_chunk: str, actions: list[str],
                 feedback: dict, screenshot_b64: str | None = None) -> None:
        self.turn += 1
        ts = datetime.now().isoformat()
        with self.story_path.open("a", encoding="utf-8") as f:
            f.write(f"\n\n=== TURN {self.turn} [{ts}] ===\n{story_chunk.strip()}\n")
        entry: dict[str, Any] = {
            "turn": self.turn, "timestamp": ts, "actions": actions,
            "feedback": feedback, "story_chars": len(story_chunk),
        }
        with self.jsonl_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        self.logger.info(
            f"Turn {self.turn} | Actions: {len(actions)} | "
            f"Status: {feedback.get('status', 'OK')}"
        )
        if screenshot_b64 and CONFIG.SAVE_SCREENSHOTS:
            try:
                raw = screenshot_b64.split(",")[-1] if "," in screenshot_b64 else screenshot_b64
                data = base64.b64decode(raw)
                (self.run_dir / "screenshots" / f"turn_{self.turn:03d}.png").write_bytes(data)
            except Exception:
                pass
        self.save_state()

    def toggle_pause(self) -> None:
        self.paused = not self.paused
        self.save_state()
        self.logger.info(f"Agent {'PAUSED' if self.paused else 'RESUMED'}")

    def get_full_story(self) -> str:
        return self.story_path.read_text(encoding="utf-8") if self.story_path.exists() else ""


# ═══════════════════════════════════════════════════════════════════════════════
#  SUB-COMMAND: --execute   (runs as subprocess, reads JSON from stdin)
# ═══════════════════════════════════════════════════════════════════════════════

def _subcmd_execute() -> None:
    req = json.loads(sys.stdin.read())
    raw = req.get("raw", "")
    run_dir = req.get("run_dir", ".")

    configure_tools(
        execute=CONFIG.EXECUTE_ACTIONS,
        physical=CONFIG.PHYSICAL_EXECUTION,
        run_dir=run_dir,
    )

    executable: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            tree = ast.parse(line, mode="eval")
            if isinstance(tree.body, ast.Call):
                func = tree.body.func
                name = func.id if isinstance(func, ast.Name) else ""
                if name in TOOL_NAMES:
                    executable.append(line)
        except Exception:
            continue

    for line in executable:
        try:
            exec(line, {"__builtins__": {}}, _TOOL_FUNCS)
        except Exception:
            pass

    print(json.dumps({
        "executed": _executed.copy(),
        "ignored": _ignored.copy(),
        "feedback": "OK",
    }))


# ═══════════════════════════════════════════════════════════════════════════════
#  SUB-COMMAND: --capture   (runs as subprocess, reads JSON from stdin)
# ═══════════════════════════════════════════════════════════════════════════════

def _subcmd_capture() -> None:
    req = json.loads(sys.stdin.read())
    actions = req.get("actions", [])
    run_dir = Path(req.get("run_dir", "."))
    marks = parse_marks("\n".join(actions))

    sw, sh = 1920, 1080

    if CONFIG.VIRTUAL_CANVAS:
        canvas = run_dir / "virtual_canvas.bmp"
        if not canvas.exists():
            canvas.write_bytes(b"\x00" * (sw * sh * 4))
        buf = bytearray(canvas.read_bytes())
        # Ensure correct size
        expected = sw * sh * 4
        if len(buf) != expected:
            buf = bytearray(expected)
        render_marks(buf, sw, sh, marks, [])
        canvas.write_bytes(buf)
    else:
        buf = capture_screen(sw, sh)
        render_marks(buf, sw, sh, marks, [])

    # Encode as proper PNG
    png_bytes = png_from_bgra(sw, sh, buf)
    b64 = base64.b64encode(png_bytes).decode()
    print(json.dumps({"screenshot_b64": f"data:image/png;base64,{b64}"}))


# ═══════════════════════════════════════════════════════════════════════════════
#  VLM CALLER
# ═══════════════════════════════════════════════════════════════════════════════

def call_vlm(story: str, screenshot_b64: str) -> str:
    """Call the local LM Studio VLM endpoint and return the response text."""
    url = "http://localhost:1235/v1/chat/completions"

    # Build message content — truncate story to last 4000 chars to stay in context
    text_content = (
        SYSTEM_PROMPT
        + "\n\n--- STORY SO FAR ---\n"
        + (story[-4000:] if len(story) > 4000 else story)
    )

    image_url = screenshot_b64 or (
        "data:image/png;base64,"
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGAoK1v0QAAAABJRU5ErkJggg=="
    )

    payload = json.dumps({
        "model": CONFIG.MODEL,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": text_content},
                {"type": "image_url", "image_url": {"url": image_url}},
            ],
        }],
        "temperature": CONFIG.TEMPERATURE,
        "max_tokens": CONFIG.MAX_TOKENS,
    }).encode("utf-8")

    try:
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            chunk = data["choices"][0]["message"]["content"]
            return chunk
    except Exception as e:
        return f"\n\n[VLM call failed: {type(e).__name__}: {e}. The agent pauses to think...]"


# ═══════════════════════════════════════════════════════════════════════════════
#  FRANZ CONSOLE — main interactive dashboard
# ═══════════════════════════════════════════════════════════════════════════════

class FranzConsole:
    def __init__(self) -> None:
        self.p = FranzPersistence()
        self.running = True
        self.paused = False

        print("=" * 90)
        print(" FRANZ — Autonomous Desktop Narrative Agent")
        print(" Press P to pause/resume | Q to quit")
        print("=" * 90)

        self.story = (
            self.p.get_full_story()
            or "You are FRANZ. Begin a new interactive story by describing "
               "the desktop and making your first tool call."
        )

        self._loop()

    def _clear(self) -> None:
        os.system("cls")

    def _print_status(self) -> None:
        status = "PAUSED" if self.paused else "RUNNING"
        self._clear()
        print("═" * 90)
        print(f" FRANZ CONSOLE DASHBOARD  |  Turn: {self.p.turn}  |  Status: {status}")
        print(f" Run dir: {self.p.run_dir}")
        print("═" * 90)
        print("LIVE STORY (last 25 lines):")
        print("─" * 90)
        if self.p.story_path.exists():
            lines = self.p.story_path.read_text(encoding="utf-8").splitlines()[-25:]
        else:
            lines = ["(waiting for first turn...)"]
        for line in lines:
            print(line[:130])
        print("─" * 90)
        print("\nRECENT LOGS:")
        print("─" * 90)
        if self.p.log_path.exists():
            logs = self.p.log_path.read_text(encoding="utf-8").splitlines()[-12:]
        else:
            logs = []
        for line in logs:
            print(line.strip()[:130])
        print("─" * 90)
        print("\n  P = Pause/Resume    Q = Quit")
        print("═" * 90)

    def _agent_step(self) -> None:
        if self.paused:
            return

        this_file = os.path.abspath(__file__)

        print(f"\n--- TURN {self.p.turn + 1} START ---")

        # ── Execute actions ──
        try:
            er = subprocess.run(
                [sys.executable, this_file, "--execute"],
                input=json.dumps({"raw": self.story, "run_dir": str(self.p.run_dir)}),
                capture_output=True, text=True, timeout=15,
            )
            feedback = json.loads(er.stdout) if er.stdout.strip() else {"executed": [], "feedback": "OK"}
        except Exception as e:
            feedback = {"executed": [], "feedback": f"execute error: {e}"}
            self.p.logger.warning(f"Execute subprocess failed: {e}")

        executed = feedback.get("executed", [])
        print(f"  Executed {len(executed)} actions: {executed[:5]}")

        # ── Capture screenshot ──
        try:
            cr = subprocess.run(
                [sys.executable, this_file, "--capture"],
                input=json.dumps({"actions": executed, "run_dir": str(self.p.run_dir)}),
                capture_output=True, text=True, timeout=10,
            )
            cap = json.loads(cr.stdout) if cr.stdout.strip() else {"screenshot_b64": ""}
        except Exception as e:
            cap = {"screenshot_b64": ""}
            self.p.logger.warning(f"Capture subprocess failed: {e}")

        screenshot = cap.get("screenshot_b64", "")

        # ── Save turn ──
        self.p.new_turn(self.story, executed, feedback, screenshot)

        # ── Call VLM ──
        print(f"  Calling VLM ({CONFIG.MODEL})...")
        new_chunk = call_vlm(self.story, screenshot)
        self.story += "\n\n" + new_chunk
        print(f"  VLM returned {len(new_chunk)} chars")
        print(f"--- TURN {self.p.turn} COMPLETE ---\n")

    def _loop(self) -> None:
        while self.running:
            reload_config()

            self._agent_step()
            self._print_status()

            # Non-blocking keyboard check during delay
            deadline = time.monotonic() + CONFIG.LOOP_DELAY
            while time.monotonic() < deadline:
                if msvcrt.kbhit():
                    key = msvcrt.getch().lower()
                    if key == b"p":
                        self.paused = not self.paused
                        self.p.paused = self.paused
                        self.p.save_state()
                        print(f"\n  >>> Agent {'PAUSED' if self.paused else 'RESUMED'} <<<")
                    elif key == b"q":
                        self.running = False
                        print("\n  Shutting down FRANZ...")
                        break
                time.sleep(0.05)


# ═══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    if "--execute" in sys.argv:
        _subcmd_execute()
    elif "--capture" in sys.argv:
        _subcmd_capture()
    elif "--help" in sys.argv:
        print("FRANZ — Autonomous Desktop Narrative Agent")
        print("Usage:")
        print(f"  python {Path(__file__).name}              # Start interactive console")
        print(f"  python {Path(__file__).name} --execute     # (internal) execute actions from stdin")
        print(f"  python {Path(__file__).name} --capture     # (internal) capture screenshot from stdin")
        print()
        print("Config overrides: create config_overrides.json next to this file.")
        print(json.dumps({k: getattr(CONFIG, k) for k in Config.__dataclass_fields__}, indent=2))
    else:
        FranzConsole()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nShutdown by Ctrl+C")
