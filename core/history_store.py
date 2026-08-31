"""对话历史持久化。

把聊天消息按角色分键存到 data/history.json，应用重启后 ChatBubble
从该文件恢复历史消息（含时间戳）。thinking/系统/错误提示不落库，
只存 user / char / initiative 三类消息。
"""
import json
import os
import time

_HISTORY_PATH = os.path.join("data", "history.json")


def _load() -> dict:
    if os.path.exists(_HISTORY_PATH):
        try:
            with open(_HISTORY_PATH, "r", encoding="utf-8") as f:
                return json.load(f) or {}
        except Exception:
            return {}
    return {}


def _save(data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_HISTORY_PATH), exist_ok=True)
        with open(_HISTORY_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def append(character_name: str, name: str, kind: str, text: str, ts: float = None) -> None:
    """记录一条已展示的消息。

    Args:
        character_name: 消息所属角色（文件分键）。
        name: 显示名（如 "你" / 角色名）。
        kind: user / char / initiative（其余类型不落库）。
        text: 消息文本。
        ts: 时间戳（默认当前时间）。
    """
    if kind not in ("user", "char", "initiative"):
        return
    if not text:
        return
    try:
        data = _load()
        data.setdefault(character_name, []).append({
            "name": name,
            "kind": kind,
            "text": text,
            "ts": float(ts if ts is not None else time.time()),
        })
        _save(data)
    except Exception:
        pass


def load(character_name: str) -> list:
    """返回某角色的历史消息列表（按时间顺序）。"""
    return _load().get(character_name, []) or []


def clear(character_name: str) -> None:
    """清空某角色的历史记录。"""
    try:
        data = _load()
        if character_name in data:
            data.pop(character_name, None)
            _save(data)
    except Exception:
        pass
