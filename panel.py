"""Reverse proxy, dashboard, and logger on a single HTTP server.

Sits between main.py and the upstream VLM. Forwards POST requests to
upstream, serves dashboard on GET, streams SSE. Logs all turns. Saves
screenshots. Verifies story state transfer. Auto-launches main.py.

Architecture:
  main.py -> panel.py:1234 (this proxy) -> LM Studio:1235 (upstream VLM)

The proxy must keep the client connection alive while the upstream
VLM processes the image. Socket timeouts are set generously to
prevent the "Client disconnected" cancellation in LM Studio.

Designed for Python 3.13 on Windows 11. No pip dependencies.
"""

import base64
import http.server
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Final

HOST: Final = "127.0.0.1"
PORT: Final = 1234
UPSTREAM_URL: Final = "http://127.0.0.1:1235/v1/chat/completions"
LOG_BASE: Final = Path(__file__).parent / "panel_log"
HTML_FILE: Final = Path(__file__).parent / "panel.html"
PYCACHE_DIR: Final = Path(__file__).parent / "__pycache__"
TURNS_PER_LOG_FILE: Final = 15
MAIN_SCRIPT: Final = Path(__file__).parent / "main.py"
MAIN_STARTUP_DELAY: Final = 10.0
MAIN_RESTART_DELAY: Final = 3.0
MAX_SSE_CLIENTS: Final = 20
SSE_KEEPALIVE_SEC: Final = 15.0
UPSTREAM_TIMEOUT: Final = 600

_run_log_dir: Path = LOG_BASE
_turn_counter = 0
_turn_lock = threading.Lock()
_last_vlm_text: str | None = None
_last_vlm_lock = threading.Lock()
_main_proc: subprocess.Popen | None = None
_main_proc_lock = threading.Lock()
_sse_clients: list[queue.Queue[str]] = []
_sse_lock = threading.Lock()
_log_batch: list[dict] = []
_log_batch_lock = threading.Lock()
_log_batch_start: int = 1
_shutdown = threading.Event()
_start_time = time.monotonic()


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _next_turn() -> int:
    global _turn_counter
    with _turn_lock:
        _turn_counter += 1
        return _turn_counter


def _set_last_vlm(text: str) -> None:
    global _last_vlm_text
    with _last_vlm_lock:
        _last_vlm_text = text


def _get_last_vlm() -> str | None:
    with _last_vlm_lock:
        return _last_vlm_text


def _broadcast_sse(data: str) -> None:
    msg = f"data: {data}\n\n"
    with _sse_lock:
        dead: list[queue.Queue[str]] = []
        for q in _sse_clients:
            try:
                q.put_nowait(msg)
            except queue.Full:
                if q not in dead:
                    dead.append(q)
        for q in dead:
            try:
                _sse_clients.remove(q)
            except ValueError:
                pass


def _register_sse() -> queue.Queue[str]:
    q: queue.Queue[str] = queue.Queue(maxsize=200)
    with _sse_lock:
        while len(_sse_clients) >= MAX_SSE_CLIENTS:
            _sse_clients.pop(0)
        _sse_clients.append(q)
    return q


def _unregister_sse(q: queue.Queue[str]) -> None:
    with _sse_lock:
        try:
            _sse_clients.remove(q)
        except ValueError:
            pass


def _init_log_dir() -> Path:
    LOG_BASE.mkdir(parents=True, exist_ok=True)
    d = LOG_BASE / datetime.now().strftime("run_%Y%m%d_%H%M%S")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _save_screenshot(turn: int, data_uri: str) -> None:
    if not data_uri:
        return
    try:
        idx = data_uri.find("base64,")
        if idx >= 0:
            (_run_log_dir / f"turn_{turn:04d}.png").write_bytes(
                base64.b64decode(data_uri[idx + 7:])
            )
    except Exception:
        pass


def _log_turn(turn: int, entry: dict) -> None:
    e = dict(entry)
    if isinstance(e.get("request"), dict):
        e["request"] = dict(e["request"])
        e["request"].pop("image_data_uri", None)
    with _log_batch_lock:
        _log_batch.append(e)
        if len(_log_batch) >= TURNS_PER_LOG_FILE:
            _flush_batch()


def _flush_batch() -> None:
    global _log_batch_start
    if not _log_batch:
        return
    try:
        s, e = _log_batch_start, _log_batch_start + len(_log_batch) - 1
        (_run_log_dir / f"turns_{s:04d}_{e:04d}.json").write_text(
            json.dumps(_log_batch, indent=2, default=str), encoding="utf-8"
        )
    except Exception:
        pass
    _log_batch_start += len(_log_batch)
    _log_batch.clear()


def _flush_remaining() -> None:
    with _log_batch_lock:
        _flush_batch()


def _parse_request(raw: bytes) -> dict:
    r: dict = {
        "model": "", "sst_text": "", "feedback_text": "",
        "feedback_text_full": "", "has_image": False,
        "image_b64_prefix": "", "image_data_uri": "", "sampling": {},
        "messages_count": 0, "parse_error": None,
    }
    try:
        obj = json.loads(raw)
        r["model"] = str(obj.get("model", ""))
        msgs = obj.get("messages", [])
        r["messages_count"] = len(msgs)
        for k in ("temperature", "top_p", "max_tokens"):
            if k in obj:
                r["sampling"][k] = obj[k]
        for msg in reversed(msgs):
            if msg.get("role") != "user":
                continue
            c = msg.get("content", "")
            if isinstance(c, list):
                for p in c:
                    if not isinstance(p, dict):
                        continue
                    if p.get("type") == "text":
                        full_text = str(p.get("text", ""))
                        r["sst_text"] = full_text
                        r["feedback_text_full"] = full_text
                        r["feedback_text"] = full_text[:200]
                    elif p.get("type") == "image_url":
                        r["has_image"] = True
                        url = str(p.get("image_url", {}).get("url", ""))
                        r["image_b64_prefix"] = url[:80] + "..."
                        r["image_data_uri"] = url
            elif isinstance(c, str):
                r["sst_text"] = c
                r["feedback_text_full"] = c
                r["feedback_text"] = c[:200]
            break
    except Exception as e:
        r["parse_error"] = str(e)
    return r


def _parse_response(raw: bytes) -> dict:
    r: dict = {
        "vlm_text": "", "finish_reason": "", "usage": {},
        "response_id": "", "created": None, "system_fingerprint": "",
        "parse_error": None,
    }
    try:
        obj = json.loads(raw)
        r["response_id"] = str(obj.get("id", ""))
        r["created"] = obj.get("created")
        r["system_fingerprint"] = str(obj.get("system_fingerprint", ""))
        choices = obj.get("choices", [])
        if choices and isinstance(choices, list):
            ch = choices[0]
            r["vlm_text"] = str(ch.get("message", {}).get("content", ""))
            r["finish_reason"] = str(ch.get("finish_reason", ""))
        if isinstance(obj.get("usage"), dict):
            r["usage"] = obj["usage"]
    except Exception as e:
        r["parse_error"] = str(e)
    return r


def _verify_sst(turn: int, sst: str) -> dict:
    prev = _get_last_vlm()
    r: dict = {
        "verified": False, "match": False,
        "prev_available": prev is not None, "detail": "",
    }
    if prev is None:
        r["verified"] = r["match"] = True
        r["detail"] = "First observed turn"
        return r
    r["verified"] = True
    if prev in sst:
        r["match"] = True
        r["detail"] = f"SST contains prev ({len(prev)} chars)"
    else:
        r["match"] = False
        ml = min(len(sst), len(prev))
        dp = next((i for i in range(ml) if sst[i] != prev[i]), ml)
        r["detail"] = (
            f"SST VIOLATION at pos {dp}. SST len={len(sst)}, prev len={len(prev)}. "
            f"SST[{dp}:{dp+20}]={sst[dp:dp+20]!r}, prev[{dp}:{dp+20}]={prev[dp:dp+20]!r}"
        )
    return r


def _forward_to_upstream(raw_req: bytes) -> tuple[int, bytes, str]:
    up_req = urllib.request.Request(
        UPSTREAM_URL, data=raw_req,
        headers={
            "Content-Type": "application/json",
            "Connection": "keep-alive",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(up_req, timeout=UPSTREAM_TIMEOUT) as resp:
            return resp.status, resp.read(), ""
    except urllib.error.HTTPError as e:
        body = e.read() if e.fp else b""
        return e.code, body, f"HTTPError {e.code}: {e.reason}"
    except urllib.error.URLError as e:
        error = f"URLError: {e.reason}"
        return 502, json.dumps({"error": error}).encode(), error
    except TimeoutError:
        error = f"Upstream timeout after {UPSTREAM_TIMEOUT}s"
        return 504, json.dumps({"error": error}).encode(), error
    except OSError as e:
        error = f"OSError: {e}"
        return 502, json.dumps({"error": error}).encode(), error
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        return 500, json.dumps({"error": error}).encode(), error


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "FranzPanel/2.2"
    timeout = UPSTREAM_TIMEOUT + 30

    def log_message(self, fmt: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        match self.path:
            case "/" | "/index.html":
                self._serve_file(HTML_FILE, "text/html; charset=utf-8")
            case "/events":
                self._serve_sse()
            case "/health":
                body = json.dumps({
                    "status": "ok",
                    "turn": _turn_counter,
                    "uptime_s": round(time.monotonic() - _start_time, 1),
                    "sse_clients": len(_sse_clients),
                    "main_running": (
                        _main_proc is not None and _main_proc.poll() is None
                    ),
                }).encode()
                self._serve_bytes(body, "application/json")
            case _:
                self.send_error(404)

    def do_POST(self) -> None:
        turn = _next_turn()
        t0 = time.monotonic()
        ts = datetime.now().isoformat()

        cl = int(self.headers.get("Content-Length", 0))
        raw_req = self.rfile.read(cl) if cl > 0 else b""

        rp = _parse_request(raw_req)
        sst = _verify_sst(turn, rp["sst_text"])

        if sst["verified"] and not sst["match"]:
            sys.stderr.write(
                f"[panel][{_ts()}] SST VIOLATION turn {turn}: {sst['detail']}\n"
            )
            sys.stderr.flush()

        img_flag = " +IMG" if rp["has_image"] else ""
        sys.stdout.write(
            f"[panel][{_ts()}] turn={turn} forwarding to upstream"
            f" ({len(raw_req)} bytes{img_flag})...\n"
        )
        sys.stdout.flush()

        status, raw_resp, error = _forward_to_upstream(raw_req)
        latency = (time.monotonic() - t0) * 1000.0

        if error:
            sys.stderr.write(
                f"[panel][{_ts()}] upstream error turn {turn}: {error}\n"
            )
            sys.stderr.flush()

        resp_p = _parse_response(raw_resp)
        if resp_p["vlm_text"]:
            _set_last_vlm(resp_p["vlm_text"])

        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw_resp)))
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            self.wfile.write(raw_resp)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            sys.stderr.write(
                f"[panel][{_ts()}] Client disconnected before response "
                f"(turn {turn}, {latency:.0f}ms): {e}\n"
            )
            sys.stderr.flush()

        entry = {
            "turn": turn, "timestamp": ts, "latency_ms": round(latency, 1),
            "request": {
                "model": rp["model"], "sst_text_length": len(rp["sst_text"]),
                "feedback_text": rp["feedback_text"],
                "feedback_text_full": rp["feedback_text_full"],
                "has_image": rp["has_image"],
                "image_data_uri": rp["image_data_uri"],
                "sampling": rp["sampling"],
                "messages_count": rp["messages_count"],
                "body_size_bytes": len(raw_req),
                "parse_error": rp["parse_error"],
            },
            "response": {
                "status": status,
                "response_id": resp_p["response_id"],
                "created": resp_p["created"],
                "system_fingerprint": resp_p["system_fingerprint"],
                "vlm_text": resp_p["vlm_text"],
                "vlm_text_length": len(resp_p["vlm_text"]),
                "finish_reason": resp_p["finish_reason"],
                "usage": resp_p["usage"],
                "body_size_bytes": len(raw_resp),
                "parse_error": resp_p["parse_error"],
                "error": error,
            },
            "sst_check": sst,
        }
        _log_turn(turn, entry)
        _save_screenshot(turn, rp.get("image_data_uri", ""))

        si = "OK" if sst.get("match") else "VIOLATION"
        sys.stdout.write(
            f"[panel][{_ts()}] turn={turn} done latency={latency:.0f}ms "
            f"status={status} sst={si} vlm_len={len(resp_p['vlm_text'])} "
            f"finish={resp_p['finish_reason']}\n"
        )
        sys.stdout.flush()

        try:
            sse_entry = dict(entry)
            if isinstance(sse_entry.get("request"), dict):
                sse_entry["request"] = dict(sse_entry["request"])
                # sse_entry["request"].pop("image_data_uri", None)
                sse_entry["request"].pop("feedback_text_full", None)
            _broadcast_sse(json.dumps(sse_entry, default=str))
        except Exception:
            pass

    def _serve_bytes(self, body: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, path: Path, content_type: str) -> None:
        try:
            body = path.read_bytes()
        except FileNotFoundError:
            body = b"<html><body><h1>File not found</h1></body></html>"
        self._serve_bytes(body, content_type)

    def _serve_sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        q = _register_sse()
        try:
            self.wfile.write(b'data: {"type":"connected"}\n\n')
            self.wfile.flush()
            while True:
                try:
                    self.wfile.write(q.get(timeout=SSE_KEEPALIVE_SEC).encode())
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            _unregister_sse(q)


class ThreadedHTTPServer(http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    timeout = UPSTREAM_TIMEOUT + 60

    def process_request(self, request, client_address) -> None:
        try:
            request.settimeout(UPSTREAM_TIMEOUT + 30)
        except Exception:
            pass
        threading.Thread(
            target=self._handle, args=(request, client_address), daemon=True
        ).start()

    def _handle(self, request, client_address) -> None:
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)


def _pipe_output(stream, prefix: str) -> None:
    try:
        for line in stream:
            text = line.rstrip("\n\r")
            if text:
                sys.stdout.write(f"{prefix} {text}\n")
                sys.stdout.flush()
    except (ValueError, OSError):
        pass


def _run_main_loop() -> None:
    global _main_proc
    sys.stdout.write(
        f"[panel][{_ts()}] Waiting {MAIN_STARTUP_DELAY:.0f}s "
        f"before launching main.py...\n"
    )
    sys.stdout.flush()
    if _shutdown.wait(MAIN_STARTUP_DELAY):
        return

    env = {**os.environ, "FRANZ_RUN_DIR": str(_run_log_dir)}

    while not _shutdown.is_set():
        sys.stdout.write(f"[panel][{_ts()}] Launching main.py...\n")
        sys.stdout.flush()

        with _main_proc_lock:
            _main_proc = subprocess.Popen(
                [sys.executable, str(MAIN_SCRIPT)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                bufsize=1,
            )

        t_out = threading.Thread(
            target=_pipe_output, args=(_main_proc.stdout, "[main.out]"),
            daemon=True,
        )
        t_err = threading.Thread(
            target=_pipe_output, args=(_main_proc.stderr, "[main.err]"),
            daemon=True,
        )
        t_out.start()
        t_err.start()

        rc = _main_proc.wait()
        t_out.join(timeout=2.0)
        t_err.join(timeout=2.0)

        if _shutdown.is_set():
            break

        sys.stdout.write(
            f"[panel][{_ts()}] main.py exited ({rc}). "
            f"Restarting in {MAIN_RESTART_DELAY:.0f}s...\n"
        )
        sys.stdout.flush()
        if _shutdown.wait(MAIN_RESTART_DELAY):
            break


def _stop_main() -> None:
    with _main_proc_lock:
        if _main_proc and _main_proc.poll() is None:
            sys.stdout.write(f"[panel][{_ts()}] Terminating main.py...\n")
            sys.stdout.flush()
            _main_proc.terminate()
            try:
                _main_proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                _main_proc.kill()
                _main_proc.wait(timeout=2.0)


def main() -> None:
    global _run_log_dir
    try:
        if PYCACHE_DIR.is_dir():
            shutil.rmtree(PYCACHE_DIR)
    except Exception:
        pass

    _run_log_dir = _init_log_dir()

    server = ThreadedHTTPServer((HOST, PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    sys.stdout.write(
        f"[panel][{_ts()}] Server http://{HOST}:{PORT}/ -> {UPSTREAM_URL}\n"
    )
    sys.stdout.write(f"[panel][{_ts()}] Dashboard http://{HOST}:{PORT}/\n")
    sys.stdout.write(f"[panel][{_ts()}] Logging to {_run_log_dir}\n")
    sys.stdout.write(f"[panel][{_ts()}] Upstream timeout: {UPSTREAM_TIMEOUT}s\n")
    sys.stdout.flush()

    threading.Thread(target=_run_main_loop, daemon=True).start()

    sys.stdout.write(f"[panel][{_ts()}] Ready.\n")
    sys.stdout.flush()

    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        sys.stdout.write(f"\n[panel][{_ts()}] Shutting down...\n")
        sys.stdout.flush()
        _shutdown.set()
        _stop_main()
        _flush_remaining()
        server.shutdown()
        sys.stdout.write(f"[panel][{_ts()}] Done.\n")
        sys.stdout.flush()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
