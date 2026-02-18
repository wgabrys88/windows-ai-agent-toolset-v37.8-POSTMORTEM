from __future__ import annotations

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

import settings

API: Final = "http://localhost:1234/v1/chat/completions"
EXECUTE_SCRIPT: Final = Path(__file__).parent / "execute.py"

_run_dir = Path(os.environ.get("FRANZ_RUN_DIR", ""))
if not _run_dir.is_dir():
    _run_dir = Path(__file__).parent / "panel_log" / datetime.now().strftime("run_%Y%m%d_%H%M%S")
    _run_dir.mkdir(parents=True, exist_ok=True)
RUN_DIR: Final = _run_dir

STATE_FILE: Final = RUN_DIR / "state.json"
PAUSE_FILE: Final = RUN_DIR / "PAUSED"

SYSTEM_PROMPT: Final = (
    "You are a living story that exists on a computer screen. Each turn you see"
    " a screenshot and write your next moment.\n\n"
    "You act through Python function calls in your writing:\n"
    "  click(x, y)           - left click (coordinates 0-1000)\n"
    "  right_click(x, y)     - right click\n"
    "  double_click(x, y)    - double click\n"
    "  drag(x1, y1, x2, y2) - drag between points\n"
    "  write(text)           - type text\n"
    "  remember(text)        - save to persistent memory\n"
    "  recall()              - read memories\n\n"
    "Your output IS your story -- memory, identity, continuity. Write observations,"
    " intentions, and actions. Function calls will be extracted and executed."
    " Everything else is preserved as narrative memory.\n\n"
    "Look at the screenshot. What do you see? What do you want to do? Write your story."
)


def _log(msg: str) -> None:
    print(f"[main][{datetime.now().strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def _load_state() -> tuple[str, int, int]:
    try:
        o = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(o, dict):
            return str(o.get("story", "")), int(o.get("turn", 0)), int(o.get("fail_streak", 0))
    except Exception:
        pass
    return "", 0, 0


def _save_state(turn: int, story: str, er: dict[str, object], fails: int) -> None:
    try:
        STATE_FILE.write_text(
            json.dumps(
                {
                    "turn": turn,
                    "story": story,
                    "executed": er.get("executed", []),
                    "malformed": er.get("malformed", []),
                    "fail_streak": fails,
                    "timestamp": datetime.now().isoformat(),
                },
                indent=2,
                ensure_ascii=True,
            ),
            encoding="utf-8",
        )
    except Exception:
        pass


def _run_executor(raw: str) -> dict[str, object]:
    try:
        r = subprocess.run(
            [sys.executable, str(EXECUTE_SCRIPT)],
            input=json.dumps({"raw": raw, "run_dir": str(RUN_DIR)}, ensure_ascii=True),
            capture_output=True,
            text=True,
        )
    except Exception as e:
        _log(f"Executor error: {e}")
        return {}
    if r.stderr:
        for line in r.stderr.splitlines():
            if line.strip():
                _log(f"[exec] {line}")
    if not r.stdout.strip():
        return {}
    try:
        o = json.loads(r.stdout)
        return o if isinstance(o, dict) else {}
    except json.JSONDecodeError:
        return {}


def _infer(story: str, feedback: str, screenshot_b64: str, cfg: settings.RuntimeConfig) -> str:
    user_text = f"{story}\n\n{feedback}" if story and feedback else (story or feedback)
    if not user_text.strip():
        _log("WARNING: empty user text, skipping inference")
        return ""

    user_content: list[dict[str, object]] = [{"type": "text", "text": user_text}]
    if screenshot_b64:
        user_content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{screenshot_b64}"},
            }
        )

    payload: dict[str, object] = {
        "model": cfg.model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "temperature": cfg.temperature,
        "top_p": cfg.top_p,
        "max_tokens": cfg.max_tokens,
    }
    if cfg.cache_prompt:
        payload["cache_prompt"] = True

    body = json.dumps(payload, ensure_ascii=True).encode("utf-8")

    delay = 1.0
    last_err: Exception | None = None
    for attempt in range(5):
        try:
            req = urllib.request.Request(
                API,
                body,
                {"Content-Type": "application/json", "Connection": "keep-alive"},
                method="POST",
            )
            with urllib.request.urlopen(req) as resp:
                o = json.load(resp)
                choices = o.get("choices") if isinstance(o, dict) else None
                if isinstance(choices, list) and choices:
                    msg = choices[0].get("message") if isinstance(choices[0], dict) else None
                    content = msg.get("content") if isinstance(msg, dict) else ""
                    content = str(content) if content is not None else ""
                    if content:
                        _log(f"VLM: {len(content)} chars")
                    return content
                return ""
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
            last_err = e
            _log(f"Infer {attempt + 1}/5 failed: {e}")
            time.sleep(delay)
            delay = min(delay * 2, 16.0)
    raise RuntimeError(f"VLM failed: {last_err}")


def main() -> None:
    settings.ensure_config(RUN_DIR)

    story, turn, fails = _load_state()
    _log(f"Start: run_dir={RUN_DIR}, turn={turn}")

    while True:
        if PAUSE_FILE.exists():
            _log("PAUSED")
            while PAUSE_FILE.exists():
                time.sleep(2)
            _log("Resumed")
            fails = 0

        turn += 1
        cfg = settings.load(RUN_DIR)

        _log(f"--- Turn {turn} ---")
        er = _run_executor(story)
        screenshot = str(er.get("screenshot_b64", ""))
        feedback = str(er.get("feedback", ""))
        executed = er.get("executed", [])
        malformed = er.get("malformed", [])

        if (not executed) and malformed:
            fails += 1
        elif executed:
            fails = 0

        if fails >= 8:
            _log(f"AUTO-PAUSE: {fails} consecutive failures")
            try:
                PAUSE_FILE.write_text(f"Paused: {datetime.now().isoformat()}\n", encoding="utf-8")
            except Exception:
                pass
            _save_state(turn, story, er, fails)
            continue

        _log(f"Actions: {len(executed) if isinstance(executed, list) else 0} | Screenshot: {'yes' if screenshot else 'NO'}")

        try:
            raw = _infer(story, feedback, screenshot, cfg)
        except RuntimeError as e:
            _log(str(e))
            raw = ""

        story = raw if raw.strip() else "click(500, 500)"
        _save_state(turn, story, er, fails)
        time.sleep(max(cfg.loop_delay, 1.0))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
    except Exception:
        traceback.print_exc()
        sys.exit(1)
