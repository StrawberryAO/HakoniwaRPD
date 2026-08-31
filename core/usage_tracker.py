"""用量统计与账户余额查询。

- 已用 token：DeepSeek 官方无公开的用量查询接口，采用**本地统计**方案——
  在每次 LLM 响应的 usage 字段（流式/非流式均支持）累加到 data/usage.json，
  按日期记录（输入/输出 token），可查"今日已用"。
- 余额：DeepSeek 官方 GET {root}/user/balance（OpenAI 兼容扩展接口），
  root 为去掉尾部 /v1 的 Base URL；非 DeepSeek 后端或请求失败时返回 None，
  界面显示"暂无数据"，不弹错误。
"""
import json
import os
import threading
import time

import requests

_USAGE_PATH = os.path.join("data", "usage.json")
_LOCK = threading.Lock()


def _normalize_usage(usage: dict) -> tuple:
    """从后端 usage 字段提取 (prompt_tokens, completion_tokens)。"""
    if not usage:
        return 0, 0
    try:
        prompt = int(usage.get("prompt_tokens", 0) or 0)
        completion = int(usage.get("completion_tokens", 0) or 0)
    except (TypeError, ValueError):
        return 0, 0
    return prompt, completion


def add_usage(usage: dict) -> None:
    """累加一次请求的 token 用量到今日统计（线程安全，失败静默）。"""
    prompt, completion = _normalize_usage(usage)
    if prompt <= 0 and completion <= 0:
        return
    with _LOCK:
        try:
            data = {}
            if os.path.exists(_USAGE_PATH):
                with open(_USAGE_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f) or {}
            today = time.strftime("%Y-%m-%d")
            day = data.setdefault(today, {"prompt": 0, "completion": 0})
            day["prompt"] = int(day.get("prompt", 0)) + prompt
            day["completion"] = int(day.get("completion", 0)) + completion
            os.makedirs(os.path.dirname(_USAGE_PATH), exist_ok=True)
            with open(_USAGE_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass


def today_usage() -> tuple:
    """返回今日 (prompt_tokens, completion_tokens)。"""
    with _LOCK:
        try:
            if os.path.exists(_USAGE_PATH):
                with open(_USAGE_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f) or {}
                day = data.get(time.strftime("%Y-%m-%d"), {}) or {}
                return int(day.get("prompt", 0)), int(day.get("completion", 0))
        except Exception:
            pass
    return 0, 0


def fetch_balance(base_url: str, api_key: str, timeout: float = 10.0):
    """查询 DeepSeek 账户余额（实时）。失败返回 None。

    返回 dict: {"is_available": bool, "balance_infos": [{"currency","total_balance",...}]}
    """
    if not api_key or not base_url:
        return None
    root = (base_url or "").rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3]
    url = f"{root}/user/balance"
    try:
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return None


def format_balance_text(balance: dict) -> str:
    """把余额接口返回格式化为可读文本；异常返回 None。"""
    if not balance:
        return None
    infos = balance.get("balance_infos") or []
    if not infos:
        return None
    parts = []
    for info in infos:
        currency = info.get("currency", "")
        total = info.get("total_balance", "?")
        granted = info.get("granted_balance", "")
        topped = info.get("topped_up_balance", "")
        line = f"{currency} {total}"
        if granted and str(granted) != "0.00" and str(granted) != "0":
            line += f"（赠送 {granted}）"
        if topped and str(topped) != "0.00" and str(topped) != "0":
            line += f"（充值 {topped}）"
        parts.append(line)
    return " / ".join(parts)
