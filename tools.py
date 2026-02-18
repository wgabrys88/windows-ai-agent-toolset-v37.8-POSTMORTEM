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


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("u", _INPUTUNION)]


_user32: ctypes.WinDLL | None = None
_screen_w: int = 0
_screen_h: int = 0

_physical: bool = False
_executed: list[str] = []

_run_dir: str = ""

_crop_x1: int = 0
_crop_y1: int = 0
_crop_x2: int = 0
_crop_y2: int = 0
_crop_active: bool = False


def _init_win32() -> None:
    global _user32, _screen_w, _screen_h
    if _user32 is not None:
        return
    try:
        ctypes.WinDLL("shcore", use_last_error=True).SetProcessDpiAwareness(2)
    except Exception:
        pass
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _screen_w = int(_user32.GetSystemMetrics(0))
    _screen_h = int(_user32.GetSystemMetrics(1))
    _user32.SendInput.argtypes = (ctypes.c_uint, ctypes.POINTER(_INPUT), ctypes.c_int)
    _user32.SendInput.restype = ctypes.c_uint
    _user32.GetCursorPos.argtypes = (ctypes.POINTER(ctypes.wintypes.POINT),)
    _user32.GetCursorPos.restype = ctypes.wintypes.BOOL


def _send_inputs(items: list[_INPUT]) -> None:
    assert _user32 is not None
    if not items:
        return
    arr = (_INPUT * len(items))(*items)
    sent = int(_user32.SendInput(len(items), arr, ctypes.sizeof(_INPUT)))
    if sent != len(items):
        raise OSError(ctypes.get_last_error())


def _to_abs(x_px: int, y_px: int) -> tuple[int, int]:
    x = int((x_px / max(1, _screen_w - 1)) * 65535)
    y = int((y_px / max(1, _screen_h - 1)) * 65535)
    return max(0, min(65535, x)), max(0, min(65535, y))


def _send_mouse(flags: int, x_abs: int | None = None, y_abs: int | None = None) -> None:
    inp = _INPUT(type=_INPUT_MOUSE)
    f = int(flags)
    dx = 0
    dy = 0
    if x_abs is not None and y_abs is not None:
        f |= _MOUSEEVENTF_ABSOLUTE | _MOUSEEVENTF_MOVE
        dx = int(x_abs)
        dy = int(y_abs)
    inp.u.mi = _MOUSEINPUT(dx, dy, 0, f, 0, 0)
    _send_inputs([inp])


def _send_unicode(text: str) -> None:
    items: list[_INPUT] = []
    for ch in text:
        if ch == "\r":
            continue
        code = 0x000D if ch == "\n" else ord(ch)
        down = _INPUT(type=_INPUT_KEYBOARD)
        down.u.ki = _KEYBDINPUT(0, code, _KEYEVENTF_UNICODE, 0, 0)
        up = _INPUT(type=_INPUT_KEYBOARD)
        up.u.ki = _KEYBDINPUT(0, code, _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP, 0, 0)
        items.append(down)
        items.append(up)
    _send_inputs(items)


def _smooth_move(tx: int, ty: int) -> None:
    assert _user32 is not None
    pt = ctypes.wintypes.POINT()
    if not _user32.GetCursorPos(ctypes.byref(pt)):
        _send_mouse(0, *_to_abs(tx, ty))
        return
    sx, sy = int(pt.x), int(pt.y)
    dx, dy = tx - sx, ty - sy
    for i in range(21):
        t = i / 20.0
        t = t * t * (3.0 - 2.0 * t)
        x = int(sx + dx * t)
        y = int(sy + dy * t)
        _send_mouse(0, *_to_abs(x, y))
        time.sleep(0.01)


def _remap_x(v: int) -> int:
    if _crop_active:
        return _crop_x1 + int((v / 1000.0) * (_crop_x2 - _crop_x1))
    return int((v / 1000.0) * _screen_w)


def _remap_y(v: int) -> int:
    if _crop_active:
        return _crop_y1 + int((v / 1000.0) * (_crop_y2 - _crop_y1))
    return int((v / 1000.0) * _screen_h)


_CLICK_BUTTONS: Final[dict[str, tuple[int, int, bool]]] = {
    "click": (_MOUSEEVENTF_LEFTDOWN, _MOUSEEVENTF_LEFTUP, False),
    "right_click": (_MOUSEEVENTF_RIGHTDOWN, _MOUSEEVENTF_RIGHTUP, False),
    "double_click": (_MOUSEEVENTF_LEFTDOWN, _MOUSEEVENTF_LEFTUP, True),
}


def _phys_click(kind: str, x: int, y: int) -> None:
    down, up, dbl = _CLICK_BUTTONS[kind]
    _smooth_move(_remap_x(x), _remap_y(y))
    time.sleep(0.12)
    _send_mouse(down)
    time.sleep(0.02)
    _send_mouse(up)
    if dbl:
        time.sleep(0.06)
        _send_mouse(down)
        time.sleep(0.02)
        _send_mouse(up)


def _phys_drag(x1: int, y1: int, x2: int, y2: int) -> None:
    _smooth_move(_remap_x(x1), _remap_y(y1))
    time.sleep(0.08)
    _send_mouse(_MOUSEEVENTF_LEFTDOWN)
    time.sleep(0.06)
    _smooth_move(_remap_x(x2), _remap_y(y2))
    time.sleep(0.06)
    _send_mouse(_MOUSEEVENTF_LEFTUP)


def configure(*, physical: bool, run_dir: str, crop: dict | None = None) -> None:
    global _physical, _executed, _run_dir
    global _crop_x1, _crop_y1, _crop_x2, _crop_y2, _crop_active
    _physical = bool(physical)
    _executed = []
    _run_dir = str(run_dir)
    if _physical:
        _init_win32()
    if isinstance(crop, dict) and all(k in crop for k in ("x1", "y1", "x2", "y2")):
        _crop_x1 = int(crop["x1"])
        _crop_y1 = int(crop["y1"])
        _crop_x2 = int(crop["x2"])
        _crop_y2 = int(crop["y2"])
        _crop_active = _crop_x2 > _crop_x1 and _crop_y2 > _crop_y1
    else:
        _crop_active = False


def get_results() -> list[str]:
    return list(_executed)


def _valid(name: str, v: object) -> int:
    if not isinstance(v, (int, float)):
        raise TypeError(f"{name} must be a number, got {type(v).__name__}")
    iv = int(v)
    if not 0 <= iv <= 1000:
        raise ValueError(f"{name}={iv} outside 0-1000")
    return iv


def _record(canon: str) -> bool:
    _executed.append(canon)
    return _physical


def click(x: int, y: int) -> None:
    ix, iy = _valid("x", x), _valid("y", y)
    if _record(f"click({ix}, {iy})"):
        _phys_click("click", ix, iy)


def right_click(x: int, y: int) -> None:
    ix, iy = _valid("x", x), _valid("y", y)
    if _record(f"right_click({ix}, {iy})"):
        _phys_click("right_click", ix, iy)


def double_click(x: int, y: int) -> None:
    ix, iy = _valid("x", x), _valid("y", y)
    if _record(f"double_click({ix}, {iy})"):
        _phys_click("double_click", ix, iy)


def drag(x1: int, y1: int, x2: int, y2: int) -> None:
    c = [_valid(n, v) for n, v in zip(("x1", "y1", "x2", "y2"), (x1, y1, x2, y2))]
    if _record(f"drag({c[0]}, {c[1]}, {c[2]}, {c[3]})"):
        _phys_drag(c[0], c[1], c[2], c[3])


def write(text: str) -> None:
    if not isinstance(text, str):
        raise TypeError(f"write() requires str, got {type(text).__name__}")
    if _record(f"write({json.dumps(text, ensure_ascii=True)})"):
        _send_unicode(text)


def _memory_path() -> Path:
    return (Path(_run_dir) / "memory.json") if _run_dir else Path("memory.json")


def remember(text: str) -> None:
    if not isinstance(text, str):
        raise TypeError(f"remember() requires str, got {type(text).__name__}")
    p = _memory_path()
    items: list[str] = []
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(obj, list):
            items = [str(x) for x in obj]
    except Exception:
        items = []
    items.append(text)
    p.write_text(json.dumps(items, indent=2, ensure_ascii=True), encoding="utf-8")
    _record(f"remember({json.dumps(text, ensure_ascii=True)})")


def recall() -> str:
    try:
        obj = json.loads(_memory_path().read_text(encoding="utf-8"))
        if isinstance(obj, list) and obj:
            return "\n".join(f"- {str(s)}" for s in obj)
    except Exception:
        pass
    return "(no memories yet)"


TOOL_NAMES: Final[tuple[str, ...]] = (
    "click",
    "right_click",
    "double_click",
    "drag",
    "write",
    "remember",
    "recall",
)
