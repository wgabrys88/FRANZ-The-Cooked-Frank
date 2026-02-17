"""Hot-reloadable configuration. Reloaded by main.py every turn.

PHYSICAL_EXECUTION controls whether Win32 SendInput calls are made.
When False, all tool functions record success and cursor overlay marks
are drawn, but no physical mouse or keyboard input is sent to the OS.

OVERLAY_DEBUG isolation debug mode. The transparent overlay window
becomes opaque to input events (blocks all clicks/keys from reaching
the OS) and draws persistent marks for all actions across turns.
Different action types are drawn in different colors:
  white  = left click
  green  = double click
  blue   = right click
  yellow = drag line
  red    = current cursor position
  faded  = previous cursor position
PHYSICAL_EXECUTION is forced False when OVERLAY_DEBUG is True.

VIRTUAL_CANVAS mode. When True, the system creates a black image file
(virtual_canvas.bmp) at screen resolution in the run directory. Each
turn, action marks are drawn onto this image instead of captured from
the real screen. The VLM sees only this canvas. The real screen is
never captured or affected. PHYSICAL_EXECUTION is forced False when
VIRTUAL_CANVAS is True.
"""

TEMPERATURE: float = 0.7
TOP_P: float = 0.9
MAX_TOKENS: int = 300
MODEL: str = "qwen3-vl-2b-instruct-1m"
WIDTH: int = 512
HEIGHT: int = 288
EXECUTE_ACTIONS: bool = True
PHYSICAL_EXECUTION: bool = False
OVERLAY_DEBUG: bool = True
VIRTUAL_CANVAS: bool = True
LOOP_DELAY: float = 2.0
CAPTURE_DELAY: float = 1.0
