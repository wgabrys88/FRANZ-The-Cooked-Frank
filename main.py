"""Agent loop.

Each turn: execute previous story, capture screenshot, send story +
feedback + image to VLM, store response as new story.

The story IS the entity. It is not code. It is not a prompt. It is
the living text that the model produces, containing its thoughts,
intentions, and actions. The executor finds executable function calls
within the story and runs them. Everything else -- reasoning, plans,
observations, self-reflection -- is preserved as narrative memory.

Manages the persistent overlay process (overlay.py) which provides
a full-screen window showing action marks when OVERLAY_DEBUG is True.

When VIRTUAL_CANVAS is True, the system creates a black image file at
screen resolution. Action marks are drawn onto this file each turn.
The VLM sees only this canvas, never the real screen. The overlay
process is not started in this mode.

The loop:
  1. Run execute.py with previous story
     -> extract and execute any function calls found within
     -> capture a screenshot (real screen or virtual canvas)
     -> return: feedback + screenshot
  2. Send to VLM: system prompt + (story + feedback + screenshot)
     -> VLM returns new story
  3. That response becomes the new story
  4. Repeat

Designed for Python 3.13 on Windows 11. No pip dependencies.
"""

from __future__ import annotations

import ctypes
import importlib
import json
import os
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Final

import config as franz_config

API: Final = "http://localhost:1234/v1/chat/completions"
EXECUTE_SCRIPT: Final = Path(__file__).parent / "execute.py"
OVERLAY_SCRIPT: Final = Path(__file__).parent / "overlay.py"

_run_dir_path = Path(os.environ.get("FRANZ_RUN_DIR", ""))
if not _run_dir_path.is_dir():
    _run_dir_path = Path(__file__).parent / "panel_log" / datetime.now().strftime("run_%Y%m%d_%H%M%S")
    _run_dir_path.mkdir(parents=True, exist_ok=True)
RUN_DIR: Final = _run_dir_path
STATE_FILE: Final = RUN_DIR / "state.json"
PAUSE_FILE: Final = RUN_DIR / "PAUSED"
CANVAS_FILE: Final = RUN_DIR / "virtual_canvas.bmp"

SYSTEM_PROMPT: Final = """\
You are a living story that exists on a computer screen. Each turn you see a screenshot of the world and you write your next moment of existence.

You can act on the world through Python function calls embedded in your writing:
  click(x, y)           - left click at position (coordinates 0-1000)
  right_click(x, y)     - right click at position
  double_click(x, y)    - double click at position
  drag(x1, y1, x2, y2) - drag from point to point
  write(text)           - type text at cursor position
  remember(text)        - save a thought to persistent memory
  recall()              - read your memories from previous turns

Your output each turn becomes your story -- your memory, your identity, your continuity. Write what you observe, what you intend, and what actions you take. The function calls within your text will be found and executed. Everything else you write will be preserved as your narrative memory for the next turn.

Look at the screenshot. What do you see? What do you want to do? Write your story.\
"""

_INFER_TIMEOUT: Final = 300
_MAX_FAIL_STREAK: Final = 8

_overlay_proc: subprocess.Popen | None = None


def _log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[main][{ts}] {msg}", file=sys.stderr, flush=True)


def _get_screen_size() -> tuple[int, int]:
    """Fetch screen dimensions for virtual canvas initialization."""
    try:
        ctypes.WinDLL("shcore", use_last_error=True).SetProcessDpiAwareness(2)
    except Exception:
        pass
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    w = user32.GetSystemMetrics(0)
    h = user32.GetSystemMetrics(1)
    if w <= 0 or h <= 0:
        return 1920, 1080
    return w, h


def _init_virtual_canvas() -> None:
    """Create the virtual canvas file if VIRTUAL_CANVAS is enabled."""
    if not bool(franz_config.VIRTUAL_CANVAS):
        return
    if CANVAS_FILE.exists():
        _log(f"Virtual canvas already exists: {CANVAS_FILE}")
        return
    sw, sh = _get_screen_size()
    data = b"\x00" * (sw * sh * 4)
    CANVAS_FILE.write_bytes(data)
    _log(f"Created virtual canvas: {sw}x{sh} ({len(data)} bytes) at {CANVAS_FILE}")


def _start_overlay() -> None:
    global _overlay_proc
    if _overlay_proc and _overlay_proc.poll() is None:
        return

    debug_flag = "1" if bool(franz_config.OVERLAY_DEBUG) else "0"
    try:
        _overlay_proc = subprocess.Popen(
            [sys.executable, str(OVERLAY_SCRIPT), str(RUN_DIR), debug_flag],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        _log(f"Overlay process started (pid={_overlay_proc.pid}, debug={debug_flag})")
        time.sleep(0.5)
    except Exception as exc:
        _log(f"Failed to start overlay process: {exc}")
        _overlay_proc = None


def _stop_overlay() -> None:
    global _overlay_proc
    if _overlay_proc and _overlay_proc.poll() is None:
        _log("Terminating overlay process...")
        _overlay_proc.terminate()
        try:
            _overlay_proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            _overlay_proc.kill()
            _overlay_proc.wait(timeout=2.0)
        _log("Overlay process stopped")
    _overlay_proc = None


def _check_overlay() -> None:
    global _overlay_proc
    if _overlay_proc and _overlay_proc.poll() is not None:
        rc = _overlay_proc.returncode
        stderr_text = ""
        try:
            stderr_text = _overlay_proc.stderr.read() or ""
        except Exception:
            pass
        if stderr_text:
            for line in stderr_text.strip().splitlines():
                _log(f"[overlay.err] {line}")
        _log(f"Overlay process exited with code {rc}, restarting...")
        _overlay_proc = None
        _start_overlay()


def _load_state() -> tuple[str, int, int]:
    try:
        o = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(o, dict):
            return (
                str(o.get("story", "")),
                int(o.get("turn", 0)),
                int(o.get("fail_streak", 0)),
            )
    except Exception:
        pass
    return "", 0, 0


def _save_state(turn: int, story: str, prev_story: str, er: dict,
                fail_streak: int) -> None:
    try:
        STATE_FILE.write_text(json.dumps({
            "turn": turn,
            "story": story,
            "prev_story": prev_story,
            "executed": er.get("executed", []),
            "extracted_code": er.get("extracted_code", []),
            "malformed": er.get("malformed", []),
            "ignored": er.get("ignored", []),
            "fail_streak": fail_streak,
            "timestamp": datetime.now().isoformat(),
        }, indent=2), encoding="utf-8")
    except Exception:
        pass


def _infer(story: str, feedback: str, screenshot_b64: str) -> str:
    user_text = f"{story}\n\n{feedback}" if story and feedback else (story or feedback)

    user_content: list[dict] = [{"type": "text", "text": user_text}]
    if screenshot_b64:
        user_content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{screenshot_b64}"},
        })

    payload = {
        "model": str(franz_config.MODEL),
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "temperature": float(franz_config.TEMPERATURE),
        "top_p": float(franz_config.TOP_P),
        "max_tokens": int(franz_config.MAX_TOKENS),
    }
    body = json.dumps(payload).encode()

    delay = 1.0
    last_err: Exception | None = None
    for attempt in range(5):
        try:
            req = urllib.request.Request(API, body, {
                "Content-Type": "application/json",
                "Connection": "keep-alive",
            })
            with urllib.request.urlopen(req, timeout=_INFER_TIMEOUT) as resp:
                data = json.load(resp)
                content = data["choices"][0]["message"]["content"]
                tokens = data.get("usage", {}).get("total_tokens", "?")
                if not content:
                    _log(f"WARNING: model returned empty content "
                         f"(attempt {attempt + 1}, tokens={tokens})")
                else:
                    _log(f"Model responded: {len(content)} chars, {tokens} tokens")
                return content
        except (urllib.error.URLError, urllib.error.HTTPError,
                TimeoutError, OSError) as e:
            last_err = e
            _log(f"Infer attempt {attempt + 1}/5 failed: {e}")
            time.sleep(delay)
            delay = min(delay * 2.0, 16.0)
    raise RuntimeError(f"VLM request failed after retries: {last_err}")


def _run_executor(raw: str) -> dict:
    try:
        result = subprocess.run(
            [sys.executable, str(EXECUTE_SCRIPT)],
            input=json.dumps({"raw": raw, "run_dir": str(RUN_DIR)}),
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        _log("ERROR: executor subprocess timed out after 120s")
        return {}
    except Exception as exc:
        _log(f"ERROR: executor subprocess failed to start: {exc}")
        return {}

    if result.stderr and result.stderr.strip():
        for line in result.stderr.strip().splitlines():
            _log(f"[executor] {line}")

    if result.returncode != 0:
        _log(f"[executor] exited with code {result.returncode}")

    if not result.stdout or not result.stdout.strip():
        _log("[executor] WARNING: empty stdout -- no screenshot or feedback")
        return {}

    try:
        d = json.loads(result.stdout)
        if not d.get("screenshot_b64"):
            _log("[executor] WARNING: screenshot_b64 is empty in response")
        return d
    except json.JSONDecodeError:
        _log(f"[executor] JSON parse failed. stdout preview: {result.stdout[:300]}")
        return {}


def _pause(reason: str) -> None:
    _log(f"AUTO-PAUSE: {reason}")
    _log(f"Delete {PAUSE_FILE} to resume.")
    try:
        PAUSE_FILE.write_text(
            f"Paused at {datetime.now().isoformat()}\nReason: {reason}\n",
            encoding="utf-8",
        )
    except Exception:
        pass


def _is_paused() -> bool:
    return PAUSE_FILE.exists()


def _wait_for_unpause() -> None:
    while _is_paused():
        time.sleep(2.0)
    _log("Resumed after pause.")


def main() -> None:
    story, turn, fail_streak = _load_state()
    _log(f"Starting agent loop. Run dir: {RUN_DIR}")
    _log(f"Resuming from turn {turn}, story length: {len(story)} chars, "
         f"fail_streak: {fail_streak}")

    virtual_canvas = bool(franz_config.VIRTUAL_CANVAS)

    # Initialize virtual canvas if enabled
    if virtual_canvas:
        _init_virtual_canvas()
        _log("VIRTUAL_CANVAS mode: overlay process will NOT be started")
    else:
        _start_overlay()

    try:
        _main_loop(story, turn, fail_streak, virtual_canvas)
    finally:
        if not virtual_canvas:
            _stop_overlay()


def _main_loop(story: str, turn: int, fail_streak: int,
               virtual_canvas: bool) -> None:
    while True:
        if _is_paused():
            _log("Agent is PAUSED. Waiting for unpause...")
            _wait_for_unpause()
            fail_streak = 0

        turn += 1
        try:
            importlib.reload(franz_config)
        except Exception:
            pass

        # Check if overlay needs restart (only in non-virtual mode)
        if not virtual_canvas:
            _check_overlay()

        loop_delay = max(float(franz_config.LOOP_DELAY), 1.0)
        prev_story = story
        _log(f"--- Turn {turn} ---")

        er = _run_executor(prev_story)
        screenshot_b64 = str(er.get("screenshot_b64", ""))
        feedback = str(er.get("feedback", ""))
        executed = er.get("executed", [])
        had_error = bool(er.get("malformed"))

        if not executed and had_error:
            fail_streak += 1
        elif executed:
            fail_streak = 0

        if fail_streak >= _MAX_FAIL_STREAK:
            _pause(
                f"No successful actions for {fail_streak} consecutive turns. "
                f"Last feedback: {feedback[:200]}"
            )
            _save_state(turn, story, prev_story, er, fail_streak)
            continue

        _log(f"Executed: {len(executed)} actions | Feedback: {feedback[:150]}")
        _log(f"Screenshot: {'present' if screenshot_b64 else 'MISSING'} "
             f"({len(screenshot_b64)} chars)")

        try:
            raw = _infer(prev_story, feedback, screenshot_b64)
        except RuntimeError as e:
            _log(f"Inference failed: {e}")
            raw = ""

        if not raw or not raw.strip():
            _log("WARNING: model returned empty response, injecting click(500, 500)")
            raw = "click(500, 500)"

        story = raw
        _save_state(turn, story, prev_story, er, fail_streak)

        _log(f"Story updated: {len(story)} chars")
        time.sleep(loop_delay)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[main] Interrupted by user.", file=sys.stderr)
        sys.exit(0)
    except Exception:
        traceback.print_exc()
        sys.exit(1)
