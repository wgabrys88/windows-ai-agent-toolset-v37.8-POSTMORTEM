# panel.py
from __future__ import annotations

import base64
import http.server
import json
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import dataclasses
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Final

import capture
import settings

HOST: Final = "127.0.0.1"
PORT: Final = 1234
LOG_BASE: Final = Path(__file__).parent / "panel_log"
HTML_FILE: Final = Path(__file__).parent / "panel.html"
MAIN_SCRIPT: Final = Path(__file__).parent / "main.py"
EXECUTE_SCRIPT: Final = Path(__file__).parent / "execute.py"

ALL_TOOLS: Final = ("click", "right_click", "double_click", "drag", "write", "remember", "recall")


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _json_bytes(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=True, separators=(",", ":"), default=str).encode("utf-8")


def _read_json(raw: bytes) -> dict[str, Any] | None:
    try:
        obj = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def _replace_last_user_text(raw: bytes, user_text: str) -> bytes:
    obj = _read_json(raw)
    if not isinstance(obj, dict):
        return raw
    msgs = obj.get("messages")
    if not isinstance(msgs, list):
        return raw
    for i in range(len(msgs) - 1, -1, -1):
        m = msgs[i]
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        content = m.get("content")
        if isinstance(content, list):
            updated = False
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    part["text"] = user_text
                    updated = True
                    break
            if not updated:
                content.insert(0, {"type": "text", "text": user_text})
        elif isinstance(content, str):
            m["content"] = user_text
        else:
            m["content"] = user_text
        break
    return _json_bytes(obj)


def _safe_send(handler: http.server.BaseHTTPRequestHandler, code: int, body: bytes, ct: str) -> None:
    try:
        handler.send_response(code)
        handler.send_header("Content-Type", ct)
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-cache")
        handler.end_headers()
        handler.wfile.write(body)
        handler.wfile.flush()
    except Exception:
        pass


def _extract_user(messages: list[Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"user_text": "", "has_image": False, "image_data_uri": ""}
    for msg in reversed(messages):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    out["user_text"] = str(part.get("text", ""))
                elif part.get("type") == "image_url":
                    out["has_image"] = True
                    out["image_data_uri"] = str(part.get("image_url", {}).get("url", ""))
        elif isinstance(content, str):
            out["user_text"] = content
        break
    return out


def _parse_req(raw: bytes) -> dict[str, Any]:
    parsed: dict[str, Any] = {
        "parse_error": "",
        "model": "",
        "sampling": {},
        "messages_count": 0,
        "user_text": "",
        "has_image": False,
        "image_data_uri": "",
    }
    obj = _read_json(raw)
    if obj is None:
        parsed["parse_error"] = "Bad JSON"
        return parsed
    parsed["model"] = str(obj.get("model", ""))
    messages = obj.get("messages", [])
    if isinstance(messages, list):
        parsed["messages_count"] = len(messages)
        parsed.update(_extract_user(messages))
    for k in ("temperature", "top_p", "max_tokens", "cache_prompt"):
        if k in obj:
            parsed["sampling"][k] = obj[k]
    return parsed


def _parse_resp(raw: bytes) -> dict[str, Any]:
    parsed: dict[str, Any] = {
        "parse_error": "",
        "response_id": "",
        "created": None,
        "system_fingerprint": "",
        "vlm_text": "",
        "finish_reason": "",
        "usage": {},
    }
    obj = _read_json(raw)
    if obj is None:
        parsed["parse_error"] = "Bad JSON"
        return parsed
    parsed["response_id"] = str(obj.get("id", ""))
    parsed["created"] = obj.get("created")
    parsed["system_fingerprint"] = str(obj.get("system_fingerprint", ""))
    choices = obj.get("choices", [])
    if isinstance(choices, list) and choices:
        parsed["vlm_text"] = str(choices[0].get("message", {}).get("content", ""))
        parsed["finish_reason"] = str(choices[0].get("finish_reason", ""))
    usage = obj.get("usage")
    if isinstance(usage, dict):
        parsed["usage"] = usage
    return parsed


def _sst_check(prev: str | None, current_user_text: str) -> dict[str, Any]:
    if prev is None:
        return {"verified": True, "match": True, "prev_available": False, "detail": "First turn"}
    if current_user_text.startswith(prev):
        return {"verified": True, "match": True, "prev_available": True, "detail": f"Prefix match ({len(prev)} chars)"}
    ml = min(len(prev), len(current_user_text))
    pos = next((i for i in range(ml) if prev[i] != current_user_text[i]), ml)
    return {
        "verified": True,
        "match": False,
        "prev_available": True,
        "detail": f"VIOLATION pos {pos}. current={len(current_user_text)} prev={len(prev)}",
    }


def _forward(raw: bytes, upstream_url: str) -> tuple[int, bytes, str]:
    req = urllib.request.Request(
        upstream_url,
        data=raw,
        headers={"Content-Type": "application/json", "Connection": "keep-alive"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read(), ""
    except urllib.error.HTTPError as e:
        return e.code, (e.read() if e.fp else b""), f"HTTP {e.code}: {e.reason}"
    except urllib.error.URLError as e:
        err = f"URLError: {e.reason}"
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    return 502, _json_bytes({"error": err}), err


def _stream_delta_from_obj(obj: dict[str, Any]) -> tuple[str, str, str, dict[str, Any] | None, str]:
    cid = str(obj.get("id", "")) if "id" in obj else ""
    model = str(obj.get("model", "")) if "model" in obj else ""
    finish = ""
    usage = obj.get("usage") if isinstance(obj.get("usage"), dict) else None
    choices = obj.get("choices")
    if isinstance(choices, list) and choices:
        c0 = choices[0] if isinstance(choices[0], dict) else {}
        fr = c0.get("finish_reason")
        if isinstance(fr, str) and fr:
            finish = fr
        delta = c0.get("delta")
        if isinstance(delta, dict):
            t = delta.get("content")
            if isinstance(t, str) and t:
                return cid, model, finish, usage, t
        msg = c0.get("message")
        if isinstance(msg, dict):
            t = msg.get("content")
            if isinstance(t, str) and t:
                return cid, model, finish, usage, t
    return cid, model, finish, usage, ""


def _enable_stream_request(raw: bytes) -> bytes:
    obj = _read_json(raw)
    if not isinstance(obj, dict):
        return raw
    obj["stream"] = True
    if "stream_options" not in obj:
        obj["stream_options"] = {"include_usage": True}
    return _json_bytes(obj)


def _forward_streaming(raw: bytes, upstream_url: str, req_id: str) -> tuple[int, bytes, str]:
    raw2 = _enable_stream_request(raw)
    req = urllib.request.Request(
        upstream_url,
        data=raw2,
        headers={"Content-Type": "application/json", "Connection": "keep-alive"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            ct = str(resp.headers.get("Content-Type", ""))
            if "text/event-stream" not in ct.lower():
                return resp.status, resp.read(), ""

            full_text = ""
            rid = ""
            model = ""
            finish_reason = ""
            usage: dict[str, Any] | None = None

            def broadcast_delta(delta: str, done: bool) -> None:
                if delta or done:
                    STATE.broadcast(
                        {
                            "type": "stream_delta",
                            "id": req_id,
                            "delta": delta,
                            "done": bool(done),
                            "chars": len(full_text),
                        }
                    )
                with STATE.lock:
                    p = STATE.pending.get(req_id)
                    if p and p.stage == "response":
                        p.parsed_response = {
                            "vlm_text": full_text,
                            "finish_reason": finish_reason,
                            "usage": usage or {},
                            "parse_error": "",
                            "streaming": True,
                            "done": bool(done),
                        }
                        p.raw_response = _completion(full_text, model=str(p.parsed_request.get("model", "panel")))

            event_lines: list[bytes] = []
            for raw_line in resp:
                line = raw_line.rstrip(b"\r\n")
                if not line:
                    if not event_lines:
                        continue
                    data_lines: list[bytes] = []
                    for l in event_lines:
                        if l.startswith(b"data:"):
                            data_lines.append(l[5:].lstrip())
                    event_lines.clear()
                    for payload in data_lines:
                        if payload == b"[DONE]":
                            broadcast_delta("", True)
                            final = _completion(
                                full_text,
                                model=model or str((_read_json(raw2) or {}).get("model", "panel")),
                            )
                            return resp.status, final, ""
                        try:
                            obj = json.loads(payload.decode("utf-8", "replace"))
                        except Exception:
                            continue
                        if not isinstance(obj, dict):
                            continue
                        cid, mdl, fr, u, delta = _stream_delta_from_obj(obj)
                        if cid:
                            rid = cid
                        if mdl:
                            model = mdl
                        if fr:
                            finish_reason = fr
                        if isinstance(u, dict):
                            usage = u
                        if delta:
                            full_text += delta
                            broadcast_delta(delta, False)
                    continue
                if line.startswith(b":"):
                    continue
                event_lines.append(line)

            broadcast_delta("", True)
            final2 = _completion(full_text, model=model or "panel")
            return resp.status, final2, ""
    except urllib.error.HTTPError as e:
        return e.code, (e.read() if e.fp else b""), f"HTTP {e.code}: {e.reason}"
    except urllib.error.URLError as e:
        err = f"URLError: {e.reason}"
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    return 502, _json_bytes({"error": err}), err


def _completion(content: str, model: str = "panel") -> bytes:
    return _json_bytes(
        {
            "id": f"panel-{int(time.time())}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
        }
    )


def _out(msg: str) -> None:
    print(f"[{_ts()}] {msg}", flush=True)


@dataclass(slots=True)
class Decision:
    action: str = "hold"
    message: str = ""
    raw_request: bytes | None = None
    raw_response: bytes | None = None


@dataclass(slots=True)
class PendingItem:
    request_id: str
    created: str
    path: str
    stage: str
    raw_request: bytes
    parsed_request: dict[str, Any]
    raw_response: bytes
    parsed_response: dict[str, Any]
    decision: Decision = field(default_factory=Decision)
    event: threading.Event = field(default_factory=threading.Event)


@dataclass(slots=True)
class TurnItem:
    turn_id: str
    timestamp: str
    path: str
    latency_ms: float
    request_raw: str
    response_raw: str
    request: dict[str, Any]
    response: dict[str, Any]
    sst_check: dict[str, Any]
    status: str


@dataclass(slots=True)
class State:
    run_dir: Path
    turns: dict[str, TurnItem] = field(default_factory=dict)
    turn_index: list[dict[str, Any]] = field(default_factory=list)
    pending: dict[str, PendingItem] = field(default_factory=dict)
    last_vlm_text: str | None = None

    lock: threading.Lock = field(default_factory=threading.Lock)
    shutdown: threading.Event = field(default_factory=threading.Event)

    sse_lock: threading.Lock = field(default_factory=threading.Lock)
    sse_clients: list[queue.Queue[str]] = field(default_factory=list)

    main_proc: subprocess.Popen[str] | None = None

    def broadcast(self, payload: dict[str, Any]) -> None:
        msg = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
        with self.sse_lock:
            clients = list(self.sse_clients)
        dead: list[queue.Queue[str]] = []
        for q in clients:
            try:
                q.put_nowait(msg)
            except Exception:
                dead.append(q)
        if dead:
            with self.sse_lock:
                self.sse_clients = [q for q in self.sse_clients if q not in dead]

    def sse_register(self) -> queue.Queue[str]:
        q: queue.Queue[str] = queue.Queue()
        with self.sse_lock:
            self.sse_clients.append(q)
        return q

    def sse_unregister(self, q: queue.Queue[str]) -> None:
        with self.sse_lock:
            if q in self.sse_clients:
                self.sse_clients.remove(q)

    def write_turn(self, item: TurnItem) -> None:
        p = self.run_dir / "turns.jsonl"
        line = json.dumps(dataclasses.asdict(item), ensure_ascii=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


def _make_run_dir() -> Path:
    LOG_BASE.mkdir(parents=True, exist_ok=True)
    name = datetime.now().strftime("run_%Y%m%d_%H%M%S")
    d = LOG_BASE / name
    d.mkdir(parents=True, exist_ok=True)
    return d


STATE = State(run_dir=_make_run_dir())


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "franz-panel/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        try:
            self._do_GET()
        except Exception:
            traceback.print_exc()
            _safe_send(self, 500, b"Internal Error", "text/plain")

    def _do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path

        if path == "/":
            html = HTML_FILE.read_bytes()
            _safe_send(self, 200, html, "text/html; charset=utf-8")
            return

        if path == "/events":
            self._handle_sse()
            return

        if path == "/health":
            with STATE.lock:
                paused = (STATE.run_dir / "PAUSED").exists()
                pending_count = len(STATE.pending)
                turns_count = len(STATE.turn_index)
                main_running = STATE.main_proc is not None
            _safe_send(
                self,
                200,
                _json_bytes(
                    {
                        "run_dir": str(STATE.run_dir),
                        "paused": paused,
                        "pending": pending_count,
                        "turns": turns_count,
                        "main_running": main_running,
                    }
                ),
                "application/json",
            )
            return

        if path == "/index":
            with STATE.lock:
                idx = list(STATE.turn_index)
            _safe_send(self, 200, _json_bytes({"index": idx}), "application/json")
            return

        if path.startswith("/turn/"):
            tid = path.split("/")[-1]
            with STATE.lock:
                t = STATE.turns.get(tid)
            if not t:
                _safe_send(self, 404, _json_bytes({"error": "not_found"}), "application/json")
                return
            _safe_send(self, 200, _json_bytes(dataclasses.asdict(t)), "application/json")
            return

        if path == "/pending":
            with STATE.lock:
                items = []
                for pid, p in STATE.pending.items():
                    items.append(
                        {
                            "id": pid,
                            "created": p.created,
                            "path": p.path,
                            "stage": p.stage,
                        }
                    )
            _safe_send(self, 200, _json_bytes({"pending": items}), "application/json")
            return

        if path.startswith("/pending/"):
            pid = path.split("/")[-1]
            with STATE.lock:
                p = STATE.pending.get(pid)
            if not p:
                _safe_send(self, 404, _json_bytes({"error": "not_found"}), "application/json")
                return
            _safe_send(
                self,
                200,
                _json_bytes(
                    {
                        "id": p.request_id,
                        "created": p.created,
                        "path": p.path,
                        "stage": p.stage,
                        "raw_request": p.raw_request.decode("utf-8", "replace"),
                        "raw_response": p.raw_response.decode("utf-8", "replace"),
                        "parsed_request": p.parsed_request,
                        "parsed_response": p.parsed_response,
                    }
                ),
                "application/json",
            )
            return

        if path == "/preview":
            b64 = capture.preview_b64(960)
            _safe_send(self, 200, _json_bytes({"data_uri": b64}), "application/json")
            return

        if path == "/crop":
            obj = _read_run_json("crop.json", default={})
            _safe_send(self, 200, _json_bytes(obj), "application/json")
            return

        if path == "/allowed_tools":
            obj = _read_run_json("allowed_tools.json", default={"allowed": list(ALL_TOOLS)})
            _safe_send(self, 200, _json_bytes(obj), "application/json")
            return

        if path == "/config":
            cfg = settings.ensure_config(STATE.run_dir)
            _safe_send(self, 200, _json_bytes(cfg), "application/json")
            return

        _safe_send(self, 404, _json_bytes({"error": "not_found"}), "application/json")

    def do_POST(self) -> None:
        try:
            self._do_POST()
        except Exception:
            traceback.print_exc()
            _safe_send(self, 500, b"Internal Error", "text/plain")

    def _do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path

        if path == "/pause":
            (STATE.run_dir / "PAUSED").write_text("1", encoding="utf-8")
            STATE.broadcast({"type": "paused"})
            _safe_send(self, 200, _json_bytes({"ok": True}), "application/json")
            return

        if path == "/unpause":
            try:
                (STATE.run_dir / "PAUSED").unlink()
            except Exception:
                pass
            STATE.broadcast({"type": "unpaused"})
            _safe_send(self, 200, _json_bytes({"ok": True}), "application/json")
            return

        if path == "/crop":
            raw = self._read_body()
            obj = _read_json(raw) or {}
            if not isinstance(obj, dict):
                obj = {}
            _write_run_json("crop.json", obj)
            STATE.broadcast({"type": "crop"})
            _safe_send(self, 200, _json_bytes({"ok": True}), "application/json")
            return

        if path == "/allowed_tools":
            raw = self._read_body()
            obj = _read_json(raw) or {}
            allowed = obj.get("allowed")
            if not isinstance(allowed, list):
                allowed = list(ALL_TOOLS)
            allowed2 = [str(x) for x in allowed if str(x) in ALL_TOOLS]
            _write_run_json("allowed_tools.json", {"allowed": allowed2})
            STATE.broadcast({"type": "allowed_tools"})
            _safe_send(self, 200, _json_bytes({"ok": True}), "application/json")
            return

        if path == "/config":
            raw = self._read_body()
            obj = _read_json(raw) or {}
            if not isinstance(obj, dict):
                obj = {}
            cfg = settings.update(STATE.run_dir, obj)
            STATE.broadcast({"type": "config"})
            _safe_send(self, 200, _json_bytes(cfg), "application/json")
            return

        if path == "/debug/execute":
            raw = self._read_body()
            obj = _read_json(raw) or {}
            text = str(obj.get("raw", ""))
            out = _run_debug_executor(text)
            _safe_send(self, 200, _json_bytes(out), "application/json")
            return

        if path.startswith("/pending/") and path.endswith("/approve"):
            pid = path.split("/")[-2]
            self._pending_decide(pid, "approve")
            _safe_send(self, 200, _json_bytes({"ok": True}), "application/json")
            return

        if path.startswith("/pending/") and path.endswith("/reject"):
            pid = path.split("/")[-2]
            raw = self._read_body()
            obj = _read_json(raw) or {}
            msg = str(obj.get("message", "rejected"))
            self._pending_decide(pid, "reject", message=msg)
            _safe_send(self, 200, _json_bytes({"ok": True}), "application/json")
            return

        if path.startswith("/pending/") and path.endswith("/edit_request"):
            pid = path.split("/")[-2]
            raw = self._read_body()
            obj = _read_json(raw) or {}
            raw_request = str(obj.get("raw_request", ""))
            self._pending_decide(pid, "edit_request", raw_request=raw_request)
            _safe_send(self, 200, _json_bytes({"ok": True}), "application/json")
            return

        if path.startswith("/pending/") and path.endswith("/edit_response"):
            pid = path.split("/")[-2]
            raw = self._read_body()
            obj = _read_json(raw) or {}
            raw_response = str(obj.get("raw_response", ""))
            self._pending_decide(pid, "edit_response", raw_response=raw_response)
            _safe_send(self, 200, _json_bytes({"ok": True}), "application/json")
            return

        if path.startswith("/pending/") and path.endswith("/inject_response"):
            pid = path.split("/")[-2]
            raw = self._read_body()
            obj = _read_json(raw) or {}
            raw_response = str(obj.get("raw_response", ""))
            self._pending_decide(pid, "inject_response", raw_response=raw_response)
            _safe_send(self, 200, _json_bytes({"ok": True}), "application/json")
            return

        if path.startswith("/v1/"):
            self._proxy_request(path)
            return

        _safe_send(self, 404, _json_bytes({"error": "not_found"}), "application/json")

    def _read_body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or "0")
        if n <= 0:
            return b"{}"
        return self.rfile.read(n)

    def _handle_sse(self) -> None:
        q = STATE.sse_register()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while not STATE.shutdown.is_set():
                try:
                    msg = q.get(timeout=15)
                    data = ("data: " + msg + "\n\n").encode("utf-8")
                    self.wfile.write(data)
                    self.wfile.flush()
                except queue.Empty:
                    try:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                    except Exception:
                        break
        finally:
            STATE.sse_unregister(q)

    def _pending_decide(
        self,
        pid: str,
        action: str,
        message: str = "",
        raw_request: str = "",
        raw_response: str = "",
    ) -> None:
        with STATE.lock:
            p = STATE.pending.get(pid)
            if not p:
                return
            p.decision.action = action
            if message:
                p.decision.message = message
            if raw_request:
                p.decision.raw_request = raw_request.encode("utf-8", "replace")
            if raw_response:
                p.decision.raw_response = raw_response.encode("utf-8", "replace")
            p.event.set()
        STATE.broadcast({"type": "pending_updated", "id": pid})

    def _proxy_request(self, path: str) -> None:
        raw_req = self._read_body()
        parsed_req = _parse_req(raw_req)

        cfg = settings.load(STATE.run_dir)

        req_id = f"{int(time.time() * 1000)}"
        t0 = time.monotonic()

        with STATE.lock:
            prev = STATE.last_vlm_text
        sst = _sst_check(prev, parsed_req.get("user_text", ""))

        STATE.broadcast({"type": "turn_started", "id": req_id})

        if cfg.firewall_enabled and not cfg.auto_approve:
            pending = PendingItem(
                request_id=req_id,
                created=_now_iso(),
                path=path,
                stage="request",
                raw_request=raw_req,
                parsed_request=parsed_req,
                raw_response=b"",
                parsed_response={},
            )
            with STATE.lock:
                STATE.pending[req_id] = pending
                STATE.turn_index.insert(0, {"id": req_id, "kind": "pending", "stage": "request", "timestamp": pending.created})
            STATE.broadcast({"type": "pending_created", "id": req_id, "stage": "request"})
            pending.event.wait()
            with STATE.lock:
                STATE.pending.pop(req_id, None)
            dec = pending.decision
            if dec.action == "reject":
                status, raw_resp, error = 200, _completion(dec.message, model="panel-reject"), "rejected_request"
            else:
                if dec.action == "edit_request" and dec.raw_request:
                    raw_sent = dec.raw_request
                else:
                    raw_sent = raw_req
                parsed_sent = _parse_req(raw_sent)
                with STATE.lock:
                    prev2 = STATE.last_vlm_text
                sst = _sst_check(prev2, parsed_sent.get("user_text", ""))

                if cfg.stream_to_panel:
                    pending_resp = PendingItem(
                        request_id=req_id,
                        created=_now_iso(),
                        path=path,
                        stage="response",
                        raw_request=raw_sent,
                        parsed_request=parsed_sent,
                        raw_response=b"",
                        parsed_response={},
                    )
                    with STATE.lock:
                        STATE.pending[req_id] = pending_resp
                        for i, e in enumerate(STATE.turn_index):
                            if e.get("id") == req_id:
                                STATE.turn_index[i] = {**e, "kind": "pending", "stage": "response"}
                                break
                    STATE.broadcast({"type": "pending_created", "id": req_id, "stage": "response"})

                    status, raw_resp, error = _forward_streaming(raw_sent, cfg.upstream_url, req_id)
                    pending_resp.raw_response = raw_resp
                    pending_resp.parsed_response = _parse_resp(raw_resp)

                    pending_resp.event.wait()
                    with STATE.lock:
                        STATE.pending.pop(req_id, None)

                    dec2 = pending_resp.decision
                    if dec2.action == "reject":
                        status, raw_resp, error = 200, _completion(dec2.message, model="panel-reject"), "rejected_response"
                    elif dec2.action in ("edit_response", "inject_response") and dec2.raw_response:
                        status, raw_resp, error = 200, dec2.raw_response, "edited_response"
                    else:
                        status, raw_resp, error = status, raw_resp, error
                else:
                    status, raw_resp, error = _forward(raw_sent, cfg.upstream_url)

                    parsed_up = _parse_resp(raw_resp)
                    pending_resp = PendingItem(
                        request_id=req_id,
                        created=_now_iso(),
                        path=path,
                        stage="response",
                        raw_request=raw_sent,
                        parsed_request=parsed_sent,
                        raw_response=raw_resp,
                        parsed_response=parsed_up,
                    )
                    with STATE.lock:
                        STATE.pending[req_id] = pending_resp
                        for i, e in enumerate(STATE.turn_index):
                            if e.get("id") == req_id:
                                STATE.turn_index[i] = {**e, "kind": "pending", "stage": "response"}
                                break
                    STATE.broadcast({"type": "pending_created", "id": req_id, "stage": "response"})
                    pending_resp.event.wait()
                    with STATE.lock:
                        STATE.pending.pop(req_id, None)

                    dec2 = pending_resp.decision
                    if dec2.action == "reject":
                        status, raw_resp, error = 200, _completion(dec2.message, model="panel-reject"), "rejected_response"
                    elif dec2.action in ("edit_response", "inject_response") and dec2.raw_response:
                        status, raw_resp, error = 200, dec2.raw_response, "edited_response"
                    else:
                        status, raw_resp, error = status, raw_resp, error
        else:
            if cfg.stream_to_panel:
                status, raw_resp, error = _forward_streaming(raw_req, cfg.upstream_url, req_id)
            else:
                status, raw_resp, error = _forward(raw_req, cfg.upstream_url)

        resp_parsed = _parse_resp(raw_resp)

        if resp_parsed.get("vlm_text"):
            with STATE.lock:
                STATE.last_vlm_text = str(resp_parsed["vlm_text"])

        latency_ms = (time.monotonic() - t0) * 1000.0

        item = TurnItem(
            turn_id=req_id,
            timestamp=_now_iso(),
            path=path,
            latency_ms=round(latency_ms, 1),
            request_raw=raw_req.decode("utf-8", "replace"),
            response_raw=raw_resp.decode("utf-8", "replace"),
            request=parsed_req,
            response={
                **resp_parsed,
                "status": status,
                "error": error,
                "body_size": len(raw_resp),
            },
            sst_check=sst,
            status="completed",
        )

        with STATE.lock:
            STATE.turns[req_id] = item
            for idx, e in enumerate(STATE.turn_index):
                if e.get("id") == req_id:
                    STATE.turn_index[idx] = {**e, "kind": "turn", "status": status}
                    break
            else:
                STATE.turn_index.insert(0, {"id": req_id, "kind": "turn", "timestamp": item.timestamp})
        try:
            STATE.write_turn(item)
        except Exception as e:
            _out(f"log write failed: {e}")

        if parsed_req.get("image_data_uri"):
            _save_screenshot(req_id, str(parsed_req.get("image_data_uri", "")))

        STATE.broadcast({"type": "turn_completed", "id": req_id})

        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw_resp)))
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            self.wfile.write(raw_resp)
            self.wfile.flush()
        except Exception:
            pass


def _save_screenshot(req_id: str, uri: str) -> None:
    if not uri:
        return
    i = uri.find("base64,")
    if i < 0:
        return
    try:
        png = base64.b64decode(uri[i + 7 :])
        (STATE.run_dir / f"turn_{req_id}.png").write_bytes(png)
    except Exception:
        pass


def _run_debug_executor(raw_text: str) -> dict[str, Any]:
    try:
        r = subprocess.run(
            [sys.executable, str(EXECUTE_SCRIPT)],
            input=_json_bytes({"raw": raw_text, "run_dir": str(STATE.run_dir)}).decode("utf-8"),
            capture_output=True,
            text=True,
        )
    except Exception as e:
        return {"error": str(e)}
    stderr_lines: list[str] = []
    if r.stderr:
        for line in r.stderr.splitlines():
            stderr_lines.append(line)
    if not r.stdout.strip():
        return {"error": "Empty stdout", "stderr": stderr_lines}
    try:
        obj = json.loads(r.stdout)
        if isinstance(obj, dict):
            obj["stderr"] = stderr_lines
            return obj
    except json.JSONDecodeError:
        return {"error": "Bad JSON from executor", "raw_stdout": r.stdout, "stderr": stderr_lines}
    return {"error": "Unknown", "stderr": stderr_lines}


def _write_run_json(name: str, data: object) -> bool:
    try:
        (STATE.run_dir / name).write_text(json.dumps(data, ensure_ascii=True, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


def _read_run_json(name: str, default: object = None) -> object:
    try:
        return json.loads((STATE.run_dir / name).read_text(encoding="utf-8"))
    except Exception:
        return default


def _pipe(stream, prefix: str) -> None:
    try:
        for line in stream:
            t = line.rstrip("\n\r")
            if t:
                _out(f"{prefix} {t}")
    except Exception:
        pass


def _run_main() -> None:
    env = {**os.environ, "FRANZ_RUN_DIR": str(STATE.run_dir)}
    while not STATE.shutdown.is_set():
        try:
            with STATE.lock:
                STATE.main_proc = subprocess.Popen(
                    [sys.executable, str(MAIN_SCRIPT)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env,
                    bufsize=1,
                )
            _out("main.py started")
            threads = [
                threading.Thread(target=_pipe, args=(STATE.main_proc.stdout, "[main.out]"), daemon=True),
                threading.Thread(target=_pipe, args=(STATE.main_proc.stderr, "[main.err]"), daemon=True),
            ]
            for t in threads:
                t.start()
            rc = STATE.main_proc.wait()
            _out(f"main.py exited ({rc})")
        except Exception as e:
            _out(f"main.py supervisor error: {e}")
        finally:
            with STATE.lock:
                STATE.main_proc = None


def _stop_main() -> None:
    with STATE.lock:
        p = STATE.main_proc
    if not p:
        return
    try:
        p.terminate()
    except Exception:
        pass
    try:
        p.kill()
    except Exception:
        pass


def _heartbeat() -> None:
    while not STATE.shutdown.is_set():
        STATE.broadcast({"type": "ping", "ts": _now_iso()})
        STATE.shutdown.wait(10)


def main() -> None:
    _out(f"Run dir: {STATE.run_dir}")
    srv = http.server.ThreadingHTTPServer((HOST, PORT), Handler)
    threading.Thread(target=_run_main, daemon=True).start()
    threading.Thread(target=_heartbeat, daemon=True).start()
    _out(f"Dashboard http://{HOST}:{PORT}/")
    _out(f"Proxy http://{HOST}:{PORT}/v1/... -> config.upstream_url")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        STATE.shutdown.set()
        _stop_main()
        try:
            srv.shutdown()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
