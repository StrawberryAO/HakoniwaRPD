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


def create_backend(cfg: dict, logger=None) -> LLMBackend:
    """根据配置块创建对应后端实例。

    Args:
        cfg: 含 "backend" 键的配置块（见 Config.backend_config()）。
    """
    kind = (cfg or {}).get("backend", "openai")
    if kind == "ollama":
        return OllamaBackend(cfg, logger)
    return OpenAIBackend(cfg, logger)
