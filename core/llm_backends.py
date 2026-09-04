"""LLM 后端抽象层：本地 Ollama 后端 + OpenAI 兼容云端后端。

统一暴露 `chat(messages) -> str` 与 `chat_stream(messages, on_chunk) -> str` 接口，
根据配置自动选择后端。所有网络错误统一包装为 LLMError 抛出，由上层捕获处理。
"""
import abc
import json

import requests

from utils.logger import get_logger


class LLMError(Exception):
    """LLM 调用失败（网络、鉴权、解析等）。"""


class AgentTurn:
    """一次 Agent 回合的结果：文本回复 + 工具调用（OpenAI 兼容 function calling）。

    Attributes:
        content: 模型文本内容（工具轮通常为空）。
        tool_calls: [{"id", "name", "arguments": dict, "raw_arguments": str}]。
        usage: 本次请求 token 用量（后端未返回时为 None）。
        assistant_message: 完整 assistant 消息（含 tool_calls，用于回填 messages）。
    """

    def __init__(self, content: str = "", tool_calls: list = None, usage: dict = None,
                 assistant_message: dict = None):
        self.content = content or ""
        self.tool_calls = tool_calls or []
        self.usage = usage
        self.assistant_message = assistant_message

    def wants_tools(self) -> bool:
        """本轮是否请求调用工具。"""
        return bool(self.tool_calls)


class LLMBackend(abc.ABC):
    """LLM 后端基类。"""

    name = "base"

    def __init__(self, cfg: dict, logger=None):
        self.cfg = cfg or {}
        self.log = logger or get_logger()
        # 最近一次请求的 token 用量（归一化为 {"prompt_tokens","completion_tokens"}，
        # 供用量统计使用；后端未返回时为 None）
        self.last_usage = None

    @abc.abstractmethod
    def chat(self, messages, temperature: float = 0.8, max_tokens: int = None,
             json_mode: bool = False, thinking: bool = False) -> str:
        """发送消息列表并返回助手回复文本。

        Args:
            messages: [{"role": "system|user|assistant", "content": "..."}, ...]
            temperature: 采样温度。
            max_tokens: 回复最大 token 数。
            json_mode: 是否要求模型输出 JSON。
            thinking: 是否开启思考模式（OpenAI 兼容后端的 DeepSeek V4 支持；
                      关闭时回复更快更口语化，适合角色扮演聊天）。
        """

    def chat_stream(self, messages, temperature: float = 0.8, max_tokens: int = None,
                    json_mode: bool = False, on_chunk=None, thinking: bool = False) -> str:
        """流式发送消息列表，逐块回调 on_chunk(piece)，返回完整回复文本。

        子类未实现流式时，此默认实现退化为一次性调用（on_chunk 收到完整文本）。
        """
        text = self.chat(messages, temperature=temperature, max_tokens=max_tokens,
                         json_mode=json_mode, thinking=thinking)
        if on_chunk and text:
            on_chunk(text)
        return text

    def chat_agent_stream(self, messages, tools=None, temperature: float = 0.8,
                          max_tokens: int = None, thinking: bool = False,
                          on_chunk=None) -> AgentTurn:
        """Agent 回合的流式调用（tools 存在时返回可回填的工具调用）。

        子类未实现时退化为普通对话（tools 被忽略，无害降级）；
        引擎层只对 OpenAI 兼容后端传 tools，Ollama 走此兜底，行为与现状一致。
        """
        text = self.chat(messages, temperature=temperature, max_tokens=max_tokens,
                         thinking=thinking)
        if on_chunk and text:
            on_chunk(text)
        return AgentTurn(
            content=text,
            usage=self.last_usage,   # chat() 内部已填充；保证用量统计不丢
            assistant_message={"role": "assistant", "content": text or None},
        )


class OllamaBackend(LLMBackend):
    """本地 Ollama 后端，调用 /api/chat（不支持思考模式，忽略 thinking 参数）。"""

    name = "ollama"

    def chat(self, messages, temperature: float = 0.8, max_tokens: int = None,
             json_mode: bool = False, thinking: bool = False) -> str:
        base = (self.cfg.get("base_url") or "http://127.0.0.1:11434").rstrip("/")
        url = f"{base}/api/chat"
        model = self.cfg.get("model") or "qwen2.5:7b"
        timeout = float(self.cfg.get("timeout", 180))

        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": temperature},
        }
        if max_tokens:
            payload["options"]["num_predict"] = int(max_tokens)
        if json_mode:
            payload["format"] = "json"

        try:
            resp = requests.post(url, json=payload, timeout=timeout)
        except requests.RequestException as exc:
            raise LLMError(f"Ollama 请求失败（{base}）: {exc}") from exc
        if resp.status_code != 200:
            raise LLMError(f"Ollama 返回 HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            data = resp.json()
            if data.get("prompt_eval_count") is not None or data.get("eval_count") is not None:
                self.last_usage = {
                    "prompt_tokens": int(data.get("prompt_eval_count") or 0),
                    "completion_tokens": int(data.get("eval_count") or 0),
                }
            return str(data["message"]["content"]).strip()
        except (KeyError, ValueError) as exc:
            raise LLMError(f"Ollama 响应解析失败: {exc} | 原文: {resp.text[:300]}") from exc

    def chat_stream(self, messages, temperature: float = 0.8, max_tokens: int = None,
                    json_mode: bool = False, on_chunk=None, thinking: bool = False) -> str:
        """流式调用 /api/chat（stream=true），逐块回调 on_chunk。"""
        base = (self.cfg.get("base_url") or "http://127.0.0.1:11434").rstrip("/")
        url = f"{base}/api/chat"
        model = self.cfg.get("model") or "qwen2.5:7b"
        timeout = float(self.cfg.get("timeout", 180))

        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
            "options": {"temperature": temperature},
        }
        if max_tokens:
            payload["options"]["num_predict"] = int(max_tokens)
        if json_mode:
            payload["format"] = "json"

        try:
            resp = requests.post(url, json=payload, timeout=timeout, stream=True)
        except requests.RequestException as exc:
            raise LLMError(f"Ollama 请求失败（{base}）: {exc}") from exc
        if resp.status_code != 200:
            raise LLMError(f"Ollama 返回 HTTP {resp.status_code}: {resp.text[:300]}")

        resp.encoding = "utf-8"
        collected = []
        try:
            for line in resp.iter_lines(decode_unicode=True):
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                piece = (obj.get("message") or {}).get("content") or ""
                if piece:
                    collected.append(piece)
                    if on_chunk:
                        on_chunk(piece)
                if obj.get("done"):
                    # Ollama 最后一块带用量统计
                    if obj.get("prompt_eval_count") is not None or obj.get("eval_count") is not None:
                        self.last_usage = {
                            "prompt_tokens": int(obj.get("prompt_eval_count") or 0),
                            "completion_tokens": int(obj.get("eval_count") or 0),
                        }
                    break
            return "".join(collected).strip()
        except requests.RequestException as exc:
            raise LLMError(f"Ollama 流式响应中断: {exc}") from exc


class OpenAIBackend(LLMBackend):
    """OpenAI 兼容云端后端（DeepSeek / OpenAI / 各类中转），调用 /chat/completions。"""

    name = "openai"

    def chat(self, messages, temperature: float = 0.8, max_tokens: int = None,
             json_mode: bool = False, thinking: bool = False) -> str:
        base = (self.cfg.get("base_url") or "https://api.deepseek.com/v1").rstrip("/")
        url = f"{base}/chat/completions"
        api_key = self.cfg.get("api_key") or ""
        model = self.cfg.get("model") or "deepseek-chat"
        timeout = float(self.cfg.get("timeout", 90))

        if not api_key:
            raise LLMError("未配置 API Key，请在 设置 -> 全局设置 中填写。")

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            # DeepSeek V4 思考模式开关（默认关闭 -> 更快更口语化）
            "thinking": {"type": "enabled" if thinking else "disabled"},
        }
        if max_tokens:
            payload["max_tokens"] = int(max_tokens)
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
        except requests.RequestException as exc:
            raise LLMError(f"云端 API 请求失败（{base}）: {exc}") from exc
        if resp.status_code != 200:
            raise LLMError(f"云端 API 返回 HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            data = resp.json()
            usage = data.get("usage")
            if usage:
                self.last_usage = {
                    "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
                    "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
                }
            # 注意：推理模型（deepseek-v4-flash）会把思考过程放在 reasoning_content，
            # 最终答案在 content；这里只取 content（思考过程不应作为回复）。
            message = data["choices"][0].get("message") or {}
            return str(message.get("content") or "").strip()
        except (KeyError, IndexError, ValueError) as exc:
            raise LLMError(f"云端 API 响应解析失败: {exc} | 原文: {resp.text[:300]}") from exc

    def chat_stream(self, messages, temperature: float = 0.8, max_tokens: int = None,
                    json_mode: bool = False, on_chunk=None, thinking: bool = False) -> str:
        """流式调用 /chat/completions（SSE），逐块回调 on_chunk。"""
        base = (self.cfg.get("base_url") or "https://api.deepseek.com/v1").rstrip("/")
        url = f"{base}/chat/completions"
        api_key = self.cfg.get("api_key") or ""
        model = self.cfg.get("model") or "deepseek-chat"
        timeout = float(self.cfg.get("timeout", 90))

        if not api_key:
            raise LLMError("未配置 API Key，请在 设置 -> 全局设置 中填写。")

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
            "thinking": {"type": "enabled" if thinking else "disabled"},
        }
        if max_tokens:
            payload["max_tokens"] = int(max_tokens)
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=timeout, stream=True)
        except requests.RequestException as exc:
            raise LLMError(f"云端 API 请求失败（{base}）: {exc}") from exc
        if resp.status_code != 200:
            raise LLMError(f"云端 API 返回 HTTP {resp.status_code}: {resp.text[:300]}")

        resp.encoding = "utf-8"
        collected = []
        try:
            for line in resp.iter_lines(decode_unicode=True):
                if not line:
                    continue
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                choices = obj.get("choices") or []
                if not choices:
                    # 流式最后一帧可能只带 usage（choices 为空）
                    if obj.get("usage"):
                        self.last_usage = {
                            "prompt_tokens": int(obj["usage"].get("prompt_tokens", 0) or 0),
                            "completion_tokens": int(obj["usage"].get("completion_tokens", 0) or 0),
                        }
                    continue
                delta = choices[0].get("delta") or {}
                piece = delta.get("content")
                if piece:
                    collected.append(piece)
                    if on_chunk:
                        on_chunk(piece)
                if choices[0].get("finish_reason"):
                    if obj.get("usage"):
                        self.last_usage = {
                            "prompt_tokens": int(obj["usage"].get("prompt_tokens", 0) or 0),
                            "completion_tokens": int(obj["usage"].get("completion_tokens", 0) or 0),
                        }
            return "".join(collected).strip()
        except requests.RequestException as exc:
            raise LLMError(f"云端 API 流式响应中断: {exc}") from exc

    def chat_agent_stream(self, messages, tools=None, temperature: float = 0.8,
                          max_tokens: int = None, thinking: bool = False,
                          on_chunk=None) -> AgentTurn:
        """OpenAI 兼容的流式 Agent 回合：tools 传入时解析流式 tool_calls 增量。

        - content 分片照常逐块回调 on_chunk（打字机效果）；
        - tool_calls 的 id/name/arguments 分片按 index 合并拼接；
        - 返回 AgentTurn（含可回填 messages 的 assistant_message）。
        """
        base = (self.cfg.get("base_url") or "https://api.deepseek.com/v1").rstrip("/")
        url = f"{base}/chat/completions"
        api_key = self.cfg.get("api_key") or ""
        model = self.cfg.get("model") or "deepseek-chat"
        timeout = float(self.cfg.get("timeout", 90))

        if not api_key:
            raise LLMError("未配置 API Key，请在 设置 -> 全局设置 中填写。")

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
            "thinking": {"type": "enabled" if thinking else "disabled"},
        }
        if tools:
            payload["tools"] = tools
        if max_tokens:
            payload["max_tokens"] = int(max_tokens)

        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=timeout, stream=True)
        except requests.RequestException as exc:
            raise LLMError(f"云端 API 请求失败（{base}）: {exc}") from exc
        if resp.status_code != 200:
            raise LLMError(f"云端 API 返回 HTTP {resp.status_code}: {resp.text[:300]}")

        resp.encoding = "utf-8"
        try:
            content, tool_calls, usage = self._consume_agent_sse(
                resp.iter_lines(decode_unicode=True), on_chunk
            )
        except requests.RequestException as exc:
            raise LLMError(f"云端 API 流式响应中断: {exc}") from exc

        self.last_usage = usage
        assistant_message = {"role": "assistant", "content": content or None}
        if tool_calls:
            assistant_message["tool_calls"] = [
                {
                    "id": c["id"] or f"call_{idx}",
                    "type": "function",
                    "function": {"name": c["name"], "arguments": c["raw_arguments"] or "{}"},
                }
                for idx, c in enumerate(tool_calls)
            ]
        return AgentTurn(
            content=content,
            tool_calls=tool_calls,
            usage=usage,
            assistant_message=assistant_message,
        )

    @staticmethod
    def _consume_agent_sse(lines, on_chunk=None):
        """解析 OpenAI 兼容的 SSE 行流（含流式 tool_calls 增量）。

        Args:
            lines: 已 decode 的文本行迭代器（含 "data: " 前缀）。
            on_chunk: content 分片回调。

        Returns:
            (content, tool_calls, usage)：
                tool_calls 形如 [{"index","id","name","arguments":dict,"raw_arguments":str}]。
        纯函数、无网络，便于离线单测。
        """
        collected = []
        tc_acc = {}          # index -> {"id": str, "name": str, "arguments": str}
        usage = None
        for line in lines:
            if not line:
                continue
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            choices = obj.get("choices") or []
            if not choices:
                if obj.get("usage"):
                    usage = {
                        "prompt_tokens": int(obj["usage"].get("prompt_tokens", 0) or 0),
                        "completion_tokens": int(obj["usage"].get("completion_tokens", 0) or 0),
                    }
                continue
            delta = choices[0].get("delta") or {}
            piece = delta.get("content")
            if piece:
                collected.append(piece)
                if on_chunk:
                    on_chunk(piece)
            for t in delta.get("tool_calls") or []:
                try:
                    idx = int(t.get("index", 0))
                except (TypeError, ValueError):
                    idx = 0
                slot = tc_acc.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                if t.get("id"):
                    slot["id"] = t["id"]
                fn = t.get("function") or {}
                if fn.get("name"):
                    slot["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["arguments"] += fn["arguments"]
            if choices[0].get("finish_reason"):
                if obj.get("usage"):
                    usage = {
                        "prompt_tokens": int(obj["usage"].get("prompt_tokens", 0) or 0),
                        "completion_tokens": int(obj["usage"].get("completion_tokens", 0) or 0),
                    }

        content = "".join(collected).strip()
        tool_calls = []
        for idx in sorted(tc_acc):
            slot = tc_acc[idx]
            raw = slot["arguments"]
            try:
                arguments = json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError:
                arguments = {}
            if not isinstance(arguments, dict):
                arguments = {}
            tool_calls.append({
                "index": idx,
                "id": slot["id"],
                "name": slot["name"],
                "arguments": arguments,
                "raw_arguments": raw,
            })
        return content, tool_calls, usage


# 受支持的 LLM 后端标识。新增后端时须同步登记，否则 create_backend 会拒绝创建。
SUPPORTED_BACKENDS = ("openai", "ollama")


def create_backend(cfg: dict, logger=None) -> LLMBackend:
    """根据配置块创建对应后端实例。

    Args:
        cfg: 含 "backend" 键的配置块（见 Config.backend_config()）。

    Raises:
        LLMError: backend 取值不在 SUPPORTED_BACKENDS 内。

    注意：此前未知 backend 会静默回落 OpenAIBackend，把请求发往 DeepSeek 地址——
    用户把 "ollama" 拼错时既不报错又会消耗 token，故改为显式报错。
    """
    kind = (cfg or {}).get("backend", "openai")
    if kind not in SUPPORTED_BACKENDS:
        raise LLMError(
            f"未知的 LLM 后端：{kind!r}。受支持的取值为 {'、'.join(SUPPORTED_BACKENDS)}，"
            f"请检查 config.json 中 llm.backend 的配置（注意大小写）。"
        )
    if kind == "ollama":
        return OllamaBackend(cfg, logger)
    return OpenAIBackend(cfg, logger)
