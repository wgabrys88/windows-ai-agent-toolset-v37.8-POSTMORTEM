# settings.py
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

CONFIG_FILENAME: Final = "config.json"

DEFAULT_CONFIG: Final[dict[str, Any]] = {
    "model": "huihui-qwen3-vl-2b-instruct-abliterated",
    "temperature": 0.5,
    "top_p": 0.8,
    "max_tokens": 300,
    "cache_prompt": True,
    "width": 512,
    "height": 288,
    "physical_execution": True,
    "loop_delay": 2.0,
    "capture_delay": 1.0,
    "firewall_enabled": False,
    "auto_approve": True,
    "stream_to_panel": False,
    "upstream_url": "http://127.0.0.1:1235/v1/chat/completions",
    "full_fidelity_logs": True,
}


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    model: str
    temperature: float
    top_p: float
    max_tokens: int
    cache_prompt: bool
    width: int
    height: int
    physical_execution: bool
    loop_delay: float
    capture_delay: float
    firewall_enabled: bool
    auto_approve: bool
    stream_to_panel: bool
    upstream_url: str
    full_fidelity_logs: bool


def _coerce_bool(v: Any, default: bool) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)) and v in (0, 1):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("1", "true", "yes", "y", "on"):
            return True
        if s in ("0", "false", "no", "n", "off"):
            return False
    return default


def _coerce_int(v: Any, default: int) -> int:
    try:
        return int(v)
    except Exception:
        return default


def _coerce_float(v: Any, default: float) -> float:
    try:
        return float(v)
    except Exception:
        return default


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def config_path(run_dir: Path) -> Path:
    return run_dir / CONFIG_FILENAME


def ensure_config(run_dir: Path) -> dict[str, Any]:
    run_dir.mkdir(parents=True, exist_ok=True)
    p = config_path(run_dir)
    cur = _read_json(p)
    if cur is None:
        cur = {}
    changed = False
    for k, v in DEFAULT_CONFIG.items():
        if k not in cur:
            cur[k] = v
            changed = True
    for k in list(cur.keys()):
        if k not in DEFAULT_CONFIG:
            cur.pop(k, None)
            changed = True
    if changed or not p.is_file():
        p.write_text(json.dumps(cur, indent=2, ensure_ascii=True), encoding="utf-8")
    return cur


def load(run_dir: Path) -> RuntimeConfig:
    cfg = ensure_config(run_dir)
    return RuntimeConfig(
        model=str(cfg.get("model", DEFAULT_CONFIG["model"])),
        temperature=_coerce_float(cfg.get("temperature"), float(DEFAULT_CONFIG["temperature"])),
        top_p=_coerce_float(cfg.get("top_p"), float(DEFAULT_CONFIG["top_p"])),
        max_tokens=_coerce_int(cfg.get("max_tokens"), int(DEFAULT_CONFIG["max_tokens"])),
        cache_prompt=_coerce_bool(cfg.get("cache_prompt"), bool(DEFAULT_CONFIG["cache_prompt"])),
        width=_coerce_int(cfg.get("width"), int(DEFAULT_CONFIG["width"])),
        height=_coerce_int(cfg.get("height"), int(DEFAULT_CONFIG["height"])),
        physical_execution=_coerce_bool(cfg.get("physical_execution"), bool(DEFAULT_CONFIG["physical_execution"])),
        loop_delay=_coerce_float(cfg.get("loop_delay"), float(DEFAULT_CONFIG["loop_delay"])),
        capture_delay=_coerce_float(cfg.get("capture_delay"), float(DEFAULT_CONFIG["capture_delay"])),
        firewall_enabled=_coerce_bool(cfg.get("firewall_enabled"), bool(DEFAULT_CONFIG["firewall_enabled"])),
        auto_approve=_coerce_bool(cfg.get("auto_approve"), bool(DEFAULT_CONFIG["auto_approve"])),
        stream_to_panel=_coerce_bool(cfg.get("stream_to_panel"), bool(DEFAULT_CONFIG["stream_to_panel"])),
        upstream_url=str(cfg.get("upstream_url", DEFAULT_CONFIG["upstream_url"])),
        full_fidelity_logs=_coerce_bool(cfg.get("full_fidelity_logs"), bool(DEFAULT_CONFIG["full_fidelity_logs"])),
    )


def update(run_dir: Path, updates: dict[str, Any]) -> dict[str, Any]:
    cfg = ensure_config(run_dir)
    for k, v in updates.items():
        if k in DEFAULT_CONFIG:
            cfg[k] = v
    config_path(run_dir).write_text(json.dumps(cfg, indent=2, ensure_ascii=True), encoding="utf-8")
    return cfg
