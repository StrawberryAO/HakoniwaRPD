"""对话历史持久化。

把聊天消息按角色分键存到 data/history.jsonl（追加式 JSONL，避免每次整文件重写），
应用重启后 ChatBubble 恢复历史消息（含时间戳）。thinking/系统/错误提示不落库，
只存 user / char / initiative 三类消息。

- 追加式写入：每条消息一行 JSON，随聊随 append，文件不随历史增长而重写。
- 旧版 data/history.json（整文件 JSON）在首次使用时自动迁移到 JSONL，
  迁移后原文件改名为 history.json.bak（幂等，只迁移一次）。
- 渲染上限：load() 默认只返回最近 500 条，历史文件本身不删减。
"""
import json
import os
import time

_HISTORY_PATH = os.path.join("data", "history.jsonl")
_LEGACY_PATH = os.path.join("data", "history.json")
_LEGACY_BAK = os.path.join("data", "history.json.bak")
_RENDER_CAP = 500


def _migrate_legacy() -> None:
    """把旧版 history.json（整文件）迁移为 JSONL，幂等（迁移后旧文件改名）。"""
    if not os.path.exists(_LEGACY_PATH):
        return
    records = []
    try:
        with open(_LEGACY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        for character_name, items in (data or {}).items():
            for item in items or []:
                if isinstance(item, dict) and item.get("text"):
                    records.append({
                        "character": character_name,
                        "name": item.get("name", ""),
                        "kind": item.get("kind", ""),
                        "text": item.get("text", ""),
                        "ts": float(item.get("ts") or time.time()),
                    })
    except Exception:
        return
    try:
        _append_records(records)
    except Exception:
        return
    # 迁移后重命名原文件，避免重复迁移
    try:
        os.replace(_LEGACY_PATH, _LEGACY_BAK)
    except OSError:
        pass


def _append_records(records: list) -> None:
    if not records:
        return
    os.makedirs(os.path.dirname(_HISTORY_PATH), exist_ok=True)
    with open(_HISTORY_PATH, "a", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def append(character_name: str, name: str, kind: str, text: str, ts: float = None) -> None:
    """追加一条已展示的消息（JSONL 末尾一行，不做整文件重写）。"""
    if kind not in ("user", "char", "initiative"):
        return
    if not text:
        return
    _migrate_legacy()
    try:
        _append_records([{
            "character": character_name,
            "name": name,
            "kind": kind,
            "text": text,
            "ts": float(ts if ts is not None else time.time()),
        }])
    except Exception:
        pass


def load(character_name: str, limit: int = _RENDER_CAP) -> list:
    """返回某角色的历史消息列表（按时间顺序，最多 limit 条）。"""
    _migrate_legacy()
    result = []
    if not os.path.exists(_HISTORY_PATH):
        return result
    try:
        with open(_HISTORY_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("character") == character_name:
                    result.append({
                        "name": rec.get("name", ""),
                        "kind": rec.get("kind", ""),
                        "text": rec.get("text", ""),
                        "ts": rec.get("ts"),
                    })
    except Exception:
        pass
    # 追加顺序天然有序，取末尾 limit 条（仅渲染上限，不删历史）
    return result[-limit:] if limit and limit > 0 else result


def clear(character_name: str) -> None:
    """清空某角色的历史记录（重写 JSONL，仅去掉该角色行）。"""
    _migrate_legacy()
    if not os.path.exists(_HISTORY_PATH):
        return
    try:
        kept = []
        with open(_HISTORY_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    kept.append(line)   # 无法解析的行原样保留，避免丢数据
                    continue
                if rec.get("character") != character_name:
                    kept.append(line)
        tmp = _HISTORY_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            if kept:
                f.write("\n".join(kept) + "\n")
        os.replace(tmp, _HISTORY_PATH)
    except Exception:
        pass
