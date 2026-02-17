"""
================================================================================
  FRANZ -- A LIVING NARRATIVE AGENT SYSTEM
  README v2
================================================================================


1. OVERVIEW
================================================================================

FRANZ is an experimental agentic system where the AI does not merely produce
outputs -- it IS the output. The central entity is a "story": a mutable text
that the model writes each turn, from which the system extracts and executes
Python function calls, captures the visual consequences, and feeds everything
back so the model can write the next chapter.

There is no planner, no state machine, no reward signal. The story is the
agent's mind, body, and memory. It sees a screen (real or virtual), it acts
through function calls embedded in its prose, and it observes the results in
the next screenshot. What it writes becomes what it remembers.

The system is built entirely from the Python 3.13 standard library plus Win32
ctypes. Zero pip dependencies. Runs on Windows 11 only by design.


2. FILES
================================================================================

  config.py      Hot-reloadable configuration (sampling, modes, resolution)
  panel.py       Reverse proxy + dashboard + logger + pause control
  panel.html     Live browser dashboard (four-quadrant view)
  main.py        Agent loop (the heartbeat) + overlay/canvas lifecycle
  execute.py     Sandboxed code extractor and executor (the hands)
  tools.py       Win32 tool functions (the muscles)
  capture.py     Screenshot or virtual canvas producer (the eyes)
  overlay.py     Persistent transparent overlay window process


3. SYSTEM REQUIREMENTS
================================================================================

  Operating System    Windows 11 only
  Python              3.13
  VLM Backend         LM Studio on localhost:1235 (or any OpenAI-compatible)
  Model               Any vision-language model (tested: qwen3-vl-2b-instruct-1m)
  Dependencies        NONE
  Display             16:9 monitor recommended for dashboard
  Browser             Any modern browser for localhost:1234


4. QUICK START
================================================================================

  1. Start LM Studio with a VLM model on port 1235
  2. Open a terminal in the FRANZ directory
  3. Run:  python panel.py
  4. Open http://localhost:1234/ in a browser
  5. The dashboard shows live turn data as the agent operates

panel.py is the entry point. It starts the HTTP server, launches main.py
as a supervised subprocess, and serves the dashboard. main.py runs the
agent loop. All other processes are spawned from main.py per turn.


5. OPERATING MODES
================================================================================

FRANZ has four distinct operating modes controlled by three boolean flags
in config.py. The flags can be changed while the system is running and
take effect on the next turn (hot-reload).


5.1  PHYSICAL MODE (real desktop control)
--------------------------------------------------------------------------------

  config.py:
    PHYSICAL_EXECUTION = True
    OVERLAY_DEBUG      = False
    VIRTUAL_CANVAS     = False

  Behavior:
    - Agent sees the real screen via BitBlt capture
    - Actions are physically executed via Win32 SendInput
    - Mouse moves smoothly to coordinates, clicks are real, text is typed
    - The agent interacts with whatever applications are on screen
    - No overlay window is created

  Use when:
    You want the agent to actually control the desktop. This is the mode
    for real task execution (opening apps, clicking buttons, drawing, etc.)

  WARNING:
    The agent controls your mouse and keyboard. It can click anything,
    type anything, and interact with any application. Use with caution.


5.2  SIMULATION MODE (observe without acting)
--------------------------------------------------------------------------------

  config.py:
    PHYSICAL_EXECUTION = False
    OVERLAY_DEBUG      = False
    VIRTUAL_CANVAS     = False

  Behavior:
    - Agent sees the real screen
    - Actions are recorded as "OK" in feedback but NOT physically executed
    - No SendInput calls are made
    - No overlay is shown
    - The screen does not change between turns (because nothing is done)

  Use when:
    You want to observe what the model would do without any risk. The model
    receives "click(x,y) -> OK" feedback but the desktop is untouched.


5.3  OVERLAY DEBUG MODE (persistent visual marks)
--------------------------------------------------------------------------------

  config.py:
    PHYSICAL_EXECUTION = False   (forced False automatically)
    OVERLAY_DEBUG      = True
    VIRTUAL_CANVAS     = False

  Behavior:
    - A persistent overlay window covers the entire screen
    - The overlay is TOPMOST and blocks all mouse/keyboard input to the OS
    - Actions are NOT physically executed
    - Action marks are drawn on the overlay and persist across turns:
        white circle  = left click
        green circle  = double click
        blue circle   = right click
        yellow line   = drag path
        red dot       = current cursor position
        faded red dot = previous cursor position
    - The screenshot captures the real screen WITH the overlay visible
    - The VLM sees its own action history as colored marks on the screen
    - The human sees the marks accumulate in real time

  Use when:
    You want to watch the agent's decision-making visually. The marks
    show you where the agent is clicking and dragging, overlaid on the
    real screen. The human cannot accidentally interfere because the
    overlay blocks input.

  How the overlay works:
    - main.py spawns overlay.py as a persistent subprocess at startup
    - overlay.py creates a full-screen layered Win32 window
    - Each turn, capture.py writes marks to marks.json and signals
      overlay.py via a named Win32 event (FranzOverlayRefresh)
    - overlay.py redraws the window with all accumulated marks
    - capture.py then captures the screen (including the overlay)
    - If overlay.py crashes, main.py auto-restarts it next turn

  Architecture:

    main.py
      |
      +-- spawns overlay.py (persistent, full-screen layered window)
      |
      +-- spawns execute.py (per turn)
              |
              +-- spawns capture.py (per turn)
                      |
                      +-- writes marks.json
                      +-- signals FranzOverlayRefresh event
                      +-- waits 150ms for overlay to redraw
                      +-- BitBlt captures screen (overlay included)


5.4  VIRTUAL CANVAS MODE (file-driven fake screen)
--------------------------------------------------------------------------------

  config.py:
    PHYSICAL_EXECUTION = False   (forced False automatically)
    OVERLAY_DEBUG      = any     (ignored -- overlay not started)
    VIRTUAL_CANVAS     = True

  Behavior:
    - At startup, a black image file (virtual_canvas.bmp) is created in
      the run directory at full screen resolution (e.g., 1920x1080 BGRA)
    - The real screen is NEVER captured
    - No overlay window is created
    - No physical input is sent
    - Each turn, action marks are drawn DIRECTLY onto the canvas file
    - The modified canvas is saved back to disk
    - The canvas image is resized and sent to the VLM as the "screenshot"
    - The VLM sees only this canvas -- a black surface with its own marks
    - The dashboard shows the canvas in the screenshot quadrant

  What the VLM experiences:
    Turn 1: Black screen. No marks.
    Turn 2: Model said click(500,300). Now there is a white dot at center.
    Turn 3: Model said drag(100,100,800,800). Now there is a white dot
            and a yellow diagonal line.
    Turn N: The canvas accumulates all marks from all turns. The model
            sees its entire action history as a painting on black.

  Mark colors on canvas (BGRA byte order, same visual result as overlay):
    white  = left click        (B=255, G=255, R=255)
    green  = double click      (B=0,   G=220, R=0)
    blue   = right click       (B=255, G=140, R=80)
    yellow = drag line         (B=0,   G=220, R=255)
    red    = current cursor    (B=0,   G=0,   R=255)
    faded  = previous cursor   (B=0,   G=0,   R=255, alpha=50)

  Use when:
    - Debugging without any screen interference
    - Testing model behavior in a controlled visual environment
    - Studying how the model reacts to seeing its own action history
    - Running the system headless (no monitor needed for the VLM)
    - Archiving: the canvas file is a complete visual history artifact

  The canvas file:
    - Location: <run_dir>/virtual_canvas.bmp
    - Format: Raw BGRA pixel data, no BMP header, screen_w * screen_h * 4 bytes
    - Can be opened with any tool that reads raw pixel data (or converted
      to PNG via the system's own _encode_png function)
    - Persists across turns -- you can stop and restart the system and the
      canvas retains all marks from previous turns
    - You can SEED the canvas by replacing the file with any BGRA image
      of the same dimensions before starting -- the model will see that
      image and its marks will be drawn on top of it


5.5  MODE SUMMARY TABLE
--------------------------------------------------------------------------------

  +-------------------+----------+----------+----------+----------+
  |                   | Physical | Simulate | Overlay  | Canvas   |
  +-------------------+----------+----------+----------+----------+
  | PHYSICAL_EXECUTION|   True   |  False   |  False*  |  False*  |
  | OVERLAY_DEBUG     |   False  |  False   |  True    |  any     |
  | VIRTUAL_CANVAS    |   False  |  False   |  False   |  True    |
  +-------------------+----------+----------+----------+----------+
  | Real screen seen  |   Yes    |  Yes     |  Yes+ovl |  No      |
  | Physical actions  |   Yes    |  No      |  No      |  No      |
  | Overlay window    |   No     |  No      |  Yes     |  No      |
  | Canvas file       |   No     |  No      |  No      |  Yes     |
  | Input blocked     |   No     |  No      |  Yes     |  N/A     |
  | Marks visible to  |   N/A    |  JSON    |  Screen  |  File    |
  | human             |          |  only    |          |          |
  +-------------------+----------+----------+----------+----------+

  * = forced False automatically by the system


6. DATA FLOW PIPELINE
================================================================================

  TURN N
  ======

  +------------------+     +-----------------+     +------------------+
  |    main.py       |     |   execute.py    |     |   capture.py     |
  |  (agent loop)    |     |   (sandbox)     |     |   (eyes)         |
  +------------------+     +-----------------+     +------------------+
  |                  |     |                 |     |                  |
  | 1. Load story    |     |                 |     |                  |
  |    from state    |     |                 |     |                  |
  |                  |     |                 |     |                  |
  | 2. Spawn --------|---->| 3. Receive raw  |     |                  |
  |    execute.py    |     |    story text   |     |                  |
  |                  |     |                 |     |                  |
  |                  |     | 4. Scan story   |     |                  |
  |                  |     |    line by line  |     |                  |
  |                  |     |    with AST      |     |                  |
  |                  |     |    parser        |     |                  |
  |                  |     |                 |     |                  |
  |                  |     | 5. Extract code  |     |                  |
  |                  |     |    from markdown |     |                  |
  |                  |     |    fences (if    |     |                  |
  |                  |     |    any) + bare   |     |                  |
  |                  |     |    function calls|     |                  |
  |                  |     |    from prose    |     |                  |
  |                  |     |                 |     |                  |
  |                  |     | 6. eval() each   |     |                  |
  |                  |     |    extracted     |     |                  |
  |                  |     |    call in safe  |     |                  |
  |                  |     |    namespace     |     |                  |
  |                  |     |                 |     |                  |
  |                  |     | 7. Spawn --------|---->| 8. Branch:       |
  |                  |     |    capture.py   |     |                  |
  |                  |     |                 |     |  VIRTUAL_CANVAS?  |
  |                  |     |                 |     |   Yes: load bmp,  |
  |                  |     |                 |     |   draw marks on   |
  |                  |     |                 |     |   it, save bmp    |
  |                  |     |                 |     |                  |
  |                  |     |                 |     |   No + OVERLAY?   |
  |                  |     |                 |     |   write marks.json|
  |                  |     |                 |     |   signal overlay  |
  |                  |     |                 |     |   wait, BitBlt    |
  |                  |     |                 |     |                  |
  |                  |     |                 |     |   No + No overlay?|
  |                  |     |                 |     |   just BitBlt     |
  |                  |     |                 |     |                  |
  |                  |     |                 |     | 9. Resize BGRA   |
  |                  |     |                 |     |    -> RGBA -> PNG |
  |                  |     |                 |     |    -> base64      |
  |                  |     |                 |     |                  |
  |                  |     | 10. <-----------|<---| Return base64    |
  |                  |     |                 |     +------------------+
  |                  |     | 11. Build       |
  |                  |     |     feedback    |
  |                  |     |     string      |
  |                  |     |                 |
  | 12. <------------|<----| Return JSON     |
  |     feedback +   |     +-----------------+
  |     screenshot   |
  |                  |
  | 13. Track fail   |
  |     streak       |
  |                  |
  | 14. Compose      |
  |     request:     |
  |     system +     |
  |     story +      |
  |     feedback +   |
  |     screenshot   |
  |                  |
  +--------+---------+
           |
           | HTTP POST (JSON + base64 image)
           v
  +------------------+     +------------------+
  |    panel.py      |     |    LM Studio     |
  |  (reverse proxy) |     |    (VLM on GPU)  |
  +------------------+     +------------------+
  |                  |     |                  |
  | 15. Log request  |     |                  |
  |     (full text,  |     |                  |
  |      no truncate)|     |                  |
  |                  |     |                  |
  | 16. Verify SST   |     |                  |
  |                  |     |                  |
  | 17. Forward -----|---->| 18. Encode image |
  |                  |     |     + generate   |
  |                  |     |                  |
  | 19. <------------|<----| Return story     |
  |                  |     +------------------+
  | 20. Log response |
  |     (full text,  |
  |      response_id,|
  |      fingerprint)|
  |                  |
  | 21. SSE broadcast|
  |     to dashboard |
  |                  |
  | 22. Forward -----|----> back to main.py
  +------------------+
           |
           v
  +------------------+
  |    main.py       |
  +------------------+
  | 23. VLM output   |
  |     = NEW STORY  |
  | 24. Save state   |
  | 25. Sleep        |
  | 26. Next turn    |
  +------------------+


7. SUBPROCESS ARCHITECTURE
================================================================================

  panel.py (HTTP server, port 1234)
    |
    +--spawns--> main.py (supervised, auto-restart on crash)
                   |
                   +--spawns--> overlay.py (persistent, only in OVERLAY_DEBUG mode)
                   |              (owns full-screen layered Win32 window)
                   |              (reads marks.json on event signal)
                   |              (stays alive across turns)
                   |
                   +--spawns--> execute.py (per turn, stdin/stdout JSON)
                                  |
                                  +--spawns--> capture.py (per turn, stdin/stdout JSON)

  Communication:
    panel.py <-> main.py       subprocess stdout/stderr piping
    main.py  <-> execute.py    stdin JSON -> stdout JSON
    execute.py <-> capture.py  stdin JSON -> stdout JSON
    main.py  <-> panel.py      HTTP POST localhost:1234
    panel.py <-> LM Studio     HTTP POST localhost:1235
    panel.py <-> browser       HTTP GET + SSE
    capture.py -> overlay.py   marks.json file + named Win32 event
    main.py  -> overlay.py     process lifecycle (start/stop/restart)


8. THE STORY AS ENTITY
================================================================================

In conventional agents, the model produces text that a framework parses
into actions. The model is a function called by the system. In FRANZ,
the model's output IS the system state.

Each turn the VLM writes freely -- prose, observations, plans, function
calls, self-reflection, anything. The executor scans this text with an
AST parser and finds any valid Python function calls targeting known
tools. Those calls are executed. Everything else is preserved verbatim
as the story.

The story is not constrained to be valid Python. It is not constrained
to be structured. It can be:

  "I see a Paint window with the text DRAW A CAT SKETCH. The canvas
   is white. I should start by drawing the head -- a circle in the
   center of the canvas.
   drag(400, 300, 600, 300)
   drag(600, 300, 600, 500)
   I will continue the outline next turn.
   remember(drawing cat head, started with top and right side)"

The executor extracts and runs the two drag() calls and the remember()
call. The prose is preserved. Next turn, the model receives this entire
text as its story, plus the feedback from execution, plus a screenshot
showing where it dragged.

The model discovers what it can do through experimentation. It sees the
consequences of its actions in the next screenshot. It learns that
click() creates white dots, drag() creates yellow lines, and write()
makes text appear. It builds its understanding iteratively.


9. SINGLE SOURCE OF TRUTH
================================================================================

Every piece of data in FRANZ exists in exactly one authoritative location:

  - The story is the truth of what the agent intends
  - The screenshot (or canvas) is the truth of what the world looks like
  - The feedback is the truth of what happened during execution
  - The JSON logs contain complete untruncated data at every stage

The SST principle is enforced:
  - panel.py verifies each prompt contains the previous VLM output
  - The dashboard displays feedback_text_full (never truncated)
  - Logs include response_id and system_fingerprint for cross-referencing
  - No component silently transforms data that another component produced

When SST is violated, the violation is detected, logged, and flagged
in the dashboard -- never hidden.


10. TOOL FUNCTIONS
================================================================================

  +------------------+--------------------------------------------------+
  | Function         | Description                                      |
  +------------------+--------------------------------------------------+
  | click(x, y)      | Left click at normalized coordinates (0-1000)    |
  | right_click(x,y) | Right click at normalized coordinates            |
  | double_click(x,y)| Double click at normalized coordinates           |
  | drag(x1,y1,x2,y2)| Drag from point to point                        |
  | write(text)      | Type text at current cursor position (Unicode)   |
  | remember(text)   | Persist a note to memory.json                    |
  | recall()         | Read all persisted notes                         |
  | help([fn])       | List functions or show help for a function       |
  +------------------+--------------------------------------------------+

Coordinate system: 0-1000 in both axes, top-left origin.
The sandbox provides only safe builtins. No imports, no file I/O,
no network access. The model interacts with the world exclusively
through these 8 functions.

The executor finds these function calls anywhere in the story text
using ast.parse(line, mode="eval"). Lines that are not valid calls
to known functions are silently skipped -- they are narrative, not
errors.


11. PAUSE AND RESUME
================================================================================

The agent can be paused and resumed through multiple mechanisms:

  Automatic pause:
    After 8 consecutive turns with execution errors and zero successful
    actions, main.py creates a PAUSED sentinel file in the run directory
    and blocks. The reason is logged.

  Dashboard pause:
    The browser dashboard has a "Pause Agent" / "Resume Agent" button.
    It sends POST /pause or POST /unpause to panel.py, which creates
    or deletes the PAUSED file. The button reflects current state via
    polling GET /health.

  Manual pause:
    Create a file named PAUSED in the run directory. The agent loop
    checks for this file at the start of each turn and blocks if found.

  Resume:
    Delete the PAUSED file (via dashboard button, filesystem, or script).
    The agent detects the deletion within 2 seconds and resumes.
    The fail_streak counter resets to 0 on resume.


12. DASHBOARD
================================================================================

  http://localhost:1234/

  +-----------------------------------+-----------------------------------+
  |                                   |                                   |
  |  STORY -- NARRATIVE MEMORY        |  FEEDBACK -- EXECUTION RESULTS    |
  |                                   |  (Full untruncated text)          |
  |  Previous VLM output sent as      |                                   |
  |  context. SST badge. Full turn    |  Complete action results, errors, |
  |  metadata (model, tokens, timing, |  hints. Never sliced or shortened |
  |  response_id, fingerprint).       |  per SST principle.               |
  |                                   |                                   |
  +------------------+----------------+-----------------------------------+
  |                  |                |                                   |
  |  VLM RESPONSE    | <-- drag this |  SCREENSHOT / CANVAS              |
  |                  |     crossing  |                                   |
  |  Raw model       |     to resize |  Auto-scaled to fill quadrant     |
  |  output. The     |     all four  |  with preserved aspect ratio.     |
  |  new story.      |     quadrants |  Click to open full size.         |
  |                  |               |  Shows real screen or virtual     |
  |                  |               |  canvas depending on mode.        |
  +-----------------------------------+-----------------------------------+

  Features:
    - Drag the center intersection to resize all four quadrants
    - Arrow keys / A/D to navigate between turns
    - Auto-advance follows incoming turns in real time
    - History overlay with expandable turn cards
    - Pause/Resume button
    - SSE connection with auto-reconnect
    - No external dependencies


13. LOGGING
================================================================================

Every turn produces a JSON log entry containing:

  Request:
    - model, sampling parameters, message count, body size
    - feedback_text (200 char preview) and feedback_text_full (complete)
    - has_image flag

  Response:
    - VLM text (complete), text length, finish reason
    - Token usage (prompt, completion, total)
    - response_id, created timestamp, system_fingerprint
    - Upstream error details if any

  Verification:
    - SST check result (match/violation with detailed diff)

  Timing:
    - Latency in milliseconds (panel-measured)

  Screenshots saved as individual PNG files per turn.
  Logs batched in groups of 15 turns per JSON file.
  All data under panel_log/run_YYYYMMDD_HHMMSS/.


14. ERROR RECOVERY
================================================================================

  Code fence extraction:
    If the VLM wraps output in ```python``` blocks, the executor
    extracts the contents. If no fences, it scans all lines.

  Per-line execution:
    Each extracted function call is eval()'d independently. One
    failing call does not prevent others from executing.

  Fail streak tracking:
    Consecutive turns with errors + zero actions increment the counter.
    Any turn with at least one successful action resets it to zero.
    At streak >= 8, auto-pause triggers.

  Overlay crash recovery:
    If overlay.py crashes, main.py detects it next turn and restarts.
    The new overlay reads existing marks.json and renders immediately.

  Executor timeout:
    120-second hard timeout on execute.py subprocess. 60-second timeout
    on capture.py. Prevents infinite hangs from bad model output.

  VLM retry:
    5 attempts with exponential backoff (1s -> 2s -> 4s -> 8s -> 16s).
    300-second per-request timeout.


15. CONFIGURATION REFERENCE
================================================================================

  TEMPERATURE     float   0.7     VLM sampling temperature
  TOP_P           float   0.9     VLM nucleus sampling threshold
  MAX_TOKENS      int     300     Maximum generation tokens
  MODEL           str     ...     Model identifier for API requests
  WIDTH           int     512     Screenshot resize width (0 = native)
  HEIGHT          int     288     Screenshot resize height (0 = native)
  EXECUTE_ACTIONS bool    True    Master kill switch for all actions
  PHYSICAL_EXECUTION bool False   Send real Win32 input to OS
  OVERLAY_DEBUG   bool    True    Persistent overlay with input blocking
  VIRTUAL_CANVAS  bool    False   File-driven virtual screen mode
  LOOP_DELAY      float   2.0     Minimum seconds between turns
  CAPTURE_DELAY   float   1.0     Delay before screenshot capture

  All values are hot-reloaded by main.py every turn via importlib.reload.
  Changes take effect on the next turn without restarting the system.


16. KNOWN LIMITS
================================================================================

  - Small VLM models (2B) often produce prose instead of action calls.
    The AST-based extractor handles this gracefully but the model may
    take many turns to discover what it can do.

  - Single-turn VLM context. No multi-turn conversation history is sent.
    The model's memory comes only from the story text and remember/recall.

  - Screenshot resolution (512x288 default) loses fine text details.
    Increase WIDTH/HEIGHT for better visual fidelity at the cost of
    slower VLM inference.

  - Virtual canvas is raw BGRA with no header. External viewing requires
    a tool that can import raw pixel data at the correct dimensions.

  - Overlay marks accumulate without limit. Very long runs will have
    cluttered screenshots/canvas.

  - Single monitor only. Multi-monitor captures the primary display.

  - Windows 11 only. Win32 ctypes calls are not portable.

  - The sandbox restricts builtins but exec/eval in Python is not
    fully sandboxed. This is a research system, not production software.


================================================================================
  END OF README
================================================================================
"""
