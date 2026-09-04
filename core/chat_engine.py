"""统一对话引擎：消息拼装、双后端调度、上下文裁剪、多角色路由、OOC 重试。

消息组装顺序（见 assemble_messages）：
    1. system: L2 核心记忆（每条单独成行，永远在最顶部）
       + 角色 system_prompt
       + 当前情绪/羁绊语气指令
       + L1 相关记忆（若有）
       + Worldbook 命中内容（若有，本轮有效）
    2. user/assistant 历史对话（按优先级裁剪后）
    3. 最新 user 消息

- 多角色群聊：用户消息以 `@角色名` 开头时路由到对应角色的配置与后端。
- 上下文裁剪：token 预算 = context_window * context_ratio（默认 80%）；
  保留优先级 L2 > 最近 3 轮 > 高重要性 L1 > 普通 L1 > 更早轮次，L2 永不丢失。
- 角色漂移检测：回复生成后做余弦相似度校验，OOC 则追加系统消息重试（默认最多 1 次）。
"""
import copy
import datetime
import random
import re
import uuid

from PySide6.QtCore import QObject, QThread, Signal

from core import constants
from core.drift_detector import DriftDetector
from core.emotion_system import BondSystem, EmotionSystem
from core.llm_backends import create_backend
from core.memory_manager import MemoryManager
from core.tools import (
    SCHEMA_CALCULATOR,
    SCHEMA_GET_TIME,
    SCHEMA_MEMORY_SEARCH,
    SCHEMA_WEATHER,
    SCHEMA_WEB_SEARCH,
    Tool,
    ToolRegistry,
    format_tool_call_summary,
    safe_eval,
)
from utils.logger import get_logger

# L2 核心记忆前缀（不可裁剪部分）
L2_PREFIX = "核心设定："
# L1 相关记忆行前缀（裁剪优先级较低）
MEMORY_PREFIX = "【相关记忆】"
# Worldbook 命中行前缀（裁剪优先级最低，本轮有效）
WORLDBOOK_PREFIX = "【世界观参考】"
# OOC 重试系统消息
OOC_RETRY_MESSAGE = "上一句回复偏离了角色设定，请重新生成一句更符合角色性格的回复。"

# 启用工具调用时，追加到 system prompt 尾部的能力说明（通用措辞，不枚举具体工具）
TOOL_HINT = (
    "\n【可用能力】你可以调用工具来更好地回应，例如：回忆与该用户过去的对话、"
    "查询当前日期时间、联网搜索资料。当用户提及过去发生的事、需要具体时间或"
    "外部信息时，自然地调用工具；不需要时就正常聊天，不要为了调用而调用。"
)

# 单次对话中工具调用-回填的最大轮数（防模型空转死循环）
MAX_TOOL_ROUNDS = 4

# 单次对话中 memory_search（记忆检索）的最大调用次数，防检索空转/过度消耗
MAX_MEMORY_SEARCH_PER_CHAT = 2


def _now_text() -> str:
    """当前日期时间文本（供 get_time 工具）。"""
    weekday = "一二三四五六日"[datetime.datetime.now().weekday()]
    return datetime.datetime.now().strftime(f"%Y-%m-%d %H:%M:%S（星期{weekday}）")

# 回复风格指令（用户可在全局设置选择）
CHAT_STYLES = {
    "concise": "【回复风格】对话要简洁自然，通常 1~3 句话，像日常聊天，不要长篇大论。",
    "detailed": "【回复风格】回复可以更丰富、更有层次，像认真倾诉或写信一样，允许较长的段落与细腻的心理、场景描写，但仍要贴合角色本人。",
}


class _Worker(QThread):
    """通用后台任务线程（跑一次函数，结果/异常通过信号回传）。"""

    ok = Signal(object)
    failed = Signal(str)

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self._fn = fn
        self._args = args
        self._kwargs = kwargs

    def run(self):
        try:
            self.ok.emit(self._fn(*self._args, **self._kwargs))
        except Exception as exc:  # noqa: BLE001 - 统一上报给 UI
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class ChatEngine(QObject):
    """对话引擎：异步执行 LLM 请求，通过信号回传结果。"""

    # (character_name, reply_text)
    reply_ready = Signal(str, str)
    reply_error = Signal(str)
    state_changed = Signal(str)          # "thinking" | "idle"
    status_updated = Signal(object)      # 情绪/羁绊状态 dict: {"pad": [...], "bond": {...}}
    initiative_text = Signal(str, str)   # (character_name, text)
    thinking_chunk = Signal(str, str)    # (character_name, 增量文本) 对话流式输出
    thinking_reset = Signal(str)         # character_name，OOC 重试前清空已显示文本
    tool_event = Signal(str, str)        # (character_name, 工具调用摘要) Agent 工具活动

    def __init__(self, config, manager, logger=None):
        super().__init__()
        self.config = config
        self.manager = manager
        self.log = logger or get_logger()
        self._memories = {}
        self._drift = DriftDetector(config, self.log)
        self._worker = None
        self._worker_init = None     # 主动搭话/生成任务的线程引用
        self._pending = []
        self._active = None

    # ---------- 角色管理 ----------
    def set_active_character(self, name: str) -> None:
        self._active = name

    def active_character(self) -> str:
        if not self._active or not self.manager.exists(self._active):
            names = self.manager.list_characters()
            self._active = names[0] if names else None
        return self._active

    def get_memory(self, char_name: str) -> MemoryManager:
        if char_name not in self._memories:
            self._memories[char_name] = MemoryManager(char_name, self.config, self.log)
        return self._memories[char_name]

    def clear_memory(self, char_name: str = None) -> None:
        if char_name:
            self._memories.pop(char_name, None)
        else:
            self._memories.clear()

    # ---------- 对话入口 ----------
    def chat(self, user_text: str) -> None:
        """提交一条用户消息（异步）。支持 @角色名 路由。"""
        char_name = self.active_character()
        if not char_name:
            self.reply_error.emit("请先创建或选择一个角色（设置 -> 角色管理）。")
            return
        target, text = self._parse_target(user_text)
        if target:
            char_name = target
        if not text.strip():
            return
        self._pending.append((char_name, text.strip()))
        self._pump()

    def _parse_target(self, text: str):
        """解析 @角色名 前缀，返回 (目标角色, 去除前缀后的文本)；无路由时 (None, 原文)。"""
        m = re.match(r"^@([^\s@：:]+)[\s：:]*", text)
        if m and self.manager.exists(m.group(1)):
            return m.group(1), text[m.end():].strip()
        return None, text

    def _pump(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            return
        if not self._pending:
            self.state_changed.emit("idle")
            return
        char_name, text = self._pending.pop(0)
        self.state_changed.emit("thinking")
        self._worker = _Worker(self._run_chat, char_name, text)
        self._worker.ok.connect(self._on_worker_done)
        self._worker.failed.connect(self._on_worker_failed)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.start()

    def _on_worker_done(self, _result) -> None:
        self._worker = None
        self._pump()

    def _on_worker_failed(self, message: str) -> None:
        self._worker = None
        self.reply_error.emit(message)
        self._pump()

    # ---------- 核心对话流程（工作线程内执行） ----------
    def _run_chat(self, char_name: str, user_text: str) -> None:
        char = self.manager.load(char_name)

        # 羁绊随时间衰减 + 情绪/羁绊读取
        BondSystem.decay_if_idle(char)
        emotion = EmotionSystem(char)
        bond = BondSystem(char)

        emotion.update(user_text, "user")
        bond.update(user_text, "user")

        memory = self.get_memory(char_name)

        backend = self._get_backend(char)
        tools_enabled = bool(getattr(char, "tools_enabled", False)) and backend.name == "openai"
        # Agentic RAG：工具开启时入口不再强制注入 L1（交给 memory_search 工具按需检索，
        # 避免每次重复注入 + 省 token）；工具关闭时保留经典 RAG 兜底（每次都 recall top3 注入）。
        inject_memory = not tools_enabled
        messages = self.assemble_messages(char, user_text, memory, emotion, bond, inject_memory=inject_memory)

        self.log.info("[%s] 请求 LLM (%s) ...", char_name, backend.name)
        # 聊天默认关闭思考模式（更快、更口语化、省 token），可在全局设置开启
        thinking_chat = bool(self.config.get("llm", "thinking_chat", default=False))

        # ---- Agent 工具调用（实验性）：仅 OpenAI 兼容后端 + 角色开启时启用 ----
        registry = None
        tools = None
        search_budget = {"memory_search": MAX_MEMORY_SEARCH_PER_CHAT}
        if tools_enabled:
            registry = self._build_tool_registry(char_name, search_budget)
            tools = registry.schemas(list(getattr(char, "tool_names", None) or []))
            if tools:
                sys0 = messages[0]
                if sys0.get("role") == "system" and "可以调用工具" not in str(sys0.get("content", "")):
                    messages[0] = {"role": "system", "content": str(sys0.get("content", "")) + TOOL_HINT}
                self.log.info("[%s] 已启用工具调用（%d 个工具）", char_name, len(tools))

        reply = self._agent_generate(char_name, messages, backend, thinking_chat, tools, registry)

        # ---- 角色漂移检测（事后校验 + 重试）----
        anchor = char.anchor_vector
        if not anchor:
            # 锚点 = 人设 + 口头禅 + 台词库 + 近期真人设回复（越聊判别越准）
            anchor_text = self.anchor_text_for(char)
            recent_replies = [t["content"] for t in memory.recent_turns() if t["role"] == "assistant"][-5:]
            if recent_replies:
                samples = "、".join("“%s”" % r for r in recent_replies)
                anchor_text = f"{anchor_text}\n你之前还说过：{samples}"
            anchor = self._drift.build_anchor(anchor_text)
            if anchor:
                char.anchor_vector = anchor
        threshold = float(self.config.get("chat", "drift_threshold", default=constants.DEFAULT_DRIFT_THRESHOLD))
        max_retries = int(self.config.get("chat", "max_ooc_retries", default=constants.DEFAULT_MAX_OOC_RETRIES))
        retried = 0
        while retried < max_retries:
            similarity, is_ooc = self._drift.check(reply, anchor, threshold)
            if not is_ooc:
                break
            self.log.info("[%s] 检测到 OOC（相似度 %.3f < %.3f），第 %d 次重试",
                          char_name, similarity or 0.0, threshold, retried + 1)
            retried += 1
            self.thinking_reset.emit(char_name)
            messages.append({"role": "assistant", "content": reply})
            messages.append({"role": "system", "content": OOC_RETRY_MESSAGE})
            reply = self._agent_generate(char_name, messages, backend, thinking_chat, tools, registry)

        # ---- 情绪/羁绊更新并持久化 ----
        emotion.update(reply, "assistant")
        bond.update(reply, "assistant")
        emotion.apply(char)
        bond.apply(char)

        # ---- 记忆落库 ----
        memory.add_turn("user", user_text)
        memory.add_turn("assistant", reply)
        memory.store_conversation(user_text, reply)

        self.manager.save(char)
        self.status_updated.emit({
            "pad": list(char.emotion.get("pad", [0.0, 0.0, 0.0])),
            "bond": dict(char.bond),
        })
        self.reply_ready.emit(char_name, reply)

    # ---------- Agent 工具调用（Phase 1） ----------
    def _build_tool_registry(self, char_name: str, search_budget: dict = None) -> ToolRegistry:
        """按角色构建内置工具注册表（处理器闭包捕获角色名）。

        Args:
            search_budget: {"memory_search": 剩余次数}。当次对话的检索预算（None 不限制）。
        """
        registry = ToolRegistry()

        def _memory_search(args: dict) -> str:
            # 兼容旧版单 query 调用，支持新版 queries 数组
            raw_queries = (args or {}).get("queries") or []
            if isinstance(raw_queries, str):
                raw_queries = [raw_queries]
            single = str((args or {}).get("query") or "").strip()
            if single:
                raw_queries = [single] + list(raw_queries)
            queries = [str(q).strip() for q in raw_queries if str(q).strip()][:4]
            if not queries:
                return "[memory_search] 请提供 queries 数组（要回忆的关键词）。"

            # 检索预算：超出则提示收敛，避免空转
            if search_budget is not None:
                left = int(search_budget.get("memory_search", 0) or 0)
                if left <= 0:
                    return "[memory_search] 本次对话已用尽记忆检索次数，请基于已有信息或用一个最关键的关键词继续。"
                search_budget["memory_search"] = left - 1

            try:
                top_k = int((args or {}).get("top_k", 3) or 3)
            except (TypeError, ValueError):
                top_k = 3
            top_k = max(1, min(top_k, 8))

            # 多查询分别检索并合并去重（按内容），实现一次调用里的"多路召回"
            seen = set()
            merged = []
            for q in queries:
                hits = self.get_memory(char_name).recall(q, top_k=top_k)
                for hit in hits:
                    content = str(hit.get("content") or "").strip()
                    if not content or content in seen:
                        continue
                    seen.add(content)
                    merged.append({
                        "content": content,
                        "score": hit.get("score"),
                        "query": q,
                    })
            if not merged:
                return (
                    "[memory_search] 长期记忆里没有直接相关的记录。"
                    "你可以把用户含糊的指代改写为更具体的人名/事件/约定再查一次，或基于已有信息诚实回应。"
                )
            # 结果按分数降序，并附时间提示给模型自行判断相关性（结果评审交给模型）
            merged.sort(key=lambda x: x["score"] or 0, reverse=True)
            lines = [f"找到 {len(merged)} 条相关记忆（按相关性排序，可据此回答；如细节不足可用更具体的词再查）："]
            for i, m in enumerate(merged, 1):
                content = m["content"][:500]
                score = m["score"]
                line = f"{i}. {content}"
                if score is not None:
                    line += f"（相关度 {float(score):.2f}）"
                lines.append(line)
            return "\n".join(lines)

        registry.register(Tool(
            "memory_search",
            "回忆你与该用户过去的对话内容（长期记忆，Agentic 检索）。当用户提及过去发生的事、你们的约定或共同经历时调用。"
            "可一次给出多个检索关键词（不同说法/实体）以获得更完整回忆；结果不足时可换角度再查。",
            SCHEMA_MEMORY_SEARCH,
            _memory_search,
        ))

        registry.register(Tool(
            "get_time",
            "获取当前的日期与时间（含星期）。当对话涉及时间、日程、节日时调用。",
            SCHEMA_GET_TIME,
            lambda _args: f"当前时间：{_now_text()}",
        ))

        def _weather(args: dict) -> str:
            city = str((args or {}).get("city") or "").strip()
            if not city:
                return "[weather_query] 请提供 city 城市名。"
            import urllib.parse

            import requests
            url = f"https://wttr.in/{urllib.parse.quote(city)}?format=%l:+%c+%t（体感%f）湿度%h+风速%w&lang=zh"
            try:
                resp = requests.get(url, timeout=8, headers={"User-Agent": "curl/8.0"})
                if resp.status_code == 200 and resp.text.strip():
                    return f"{city} 天气：{resp.text.strip()}"
                return "[weather_query] 天气服务暂不可用或未找到该城市。"
            except Exception:
                return "[weather_query] 天气服务请求失败，请稍后再试。"

        def _calculator(args: dict) -> str:
            expr = str((args or {}).get("expression") or "").strip()
            if not expr:
                return "[calculator] 请提供 expression 表达式。"
            try:
                val = safe_eval(expr)
                if isinstance(val, float) and val.is_integer():
                    val = int(val)
                return f"{expr} = {val}"
            except ZeroDivisionError:
                return "[calculator] 除数为零，无法计算。"
            except Exception as exc:
                return f"[calculator] 表达式无效（{type(exc).__name__}）。"

        registry.register(Tool(
            "weather_query",
            "查询指定城市的实时天气（温度、体感、湿度、风力）。当用户询问天气时调用。",
            SCHEMA_WEATHER,
            _weather,
        ))
        registry.register(Tool(
            "calculator",
            "安全计算算术表达式（+ - * / % ( ) **）。当用户要求算数或单位换算时调用。",
            SCHEMA_CALCULATOR,
            _calculator,
        ))

        # 联网搜索工具跟随全局 web_search.enabled 配置（默认开）
        if bool((self.config.get("web_search", default={}) or {}).get("enabled", True)):

            def _web_search(args: dict) -> str:
                query = str((args or {}).get("query") or "").strip()
                if not query:
                    return "[web_search] 缺少 query 参数。"
                from core.web_search import WebSearcher, format_references
                results = WebSearcher(self.config, self.log).search(query)
                block = format_references(results, max_items=5)
                return block or "[web_search] 没有搜到结果（网络不可用或关键词无结果）。"

            registry.register(Tool(
                "web_search",
                "联网搜索实时资料。当用户询问需要最新/外部信息（如新闻、现实事件、人物资料）时调用。",
                SCHEMA_WEB_SEARCH,
                _web_search,
            ))

        return registry

    def _agent_generate(self, char_name, messages, backend, thinking_chat, tools, registry) -> str:
        """Agent 生成循环：LLM <-> 工具执行，直到模型不再请求工具或达轮次上限。

        每轮 LLM 流式输出经 thinking_chunk 实时推送 UI；工具调用经 tool_event 上报。
        返回最终文本回复（供漂移检测 / 情绪 / 记忆落库使用）。
        """
        from core.usage_tracker import add_usage

        def on_chunk(piece: str) -> None:
            self.thinking_chunk.emit(char_name, piece)

        last_content = ""
        for _round in range(MAX_TOOL_ROUNDS):
            turn = backend.chat_agent_stream(
                messages, tools=tools, thinking=thinking_chat, on_chunk=on_chunk,
            )
            if turn.usage:
                add_usage(turn.usage)
            last_content = turn.content

            if not turn.wants_tools():
                if last_content.strip():
                    return last_content
                # 流式偶发返回空且未请求工具：回退一次非流式普通调用
                self.thinking_reset.emit(char_name)
                fallback = backend.chat(messages, thinking=thinking_chat)
                add_usage(backend.last_usage)
                return fallback.strip() or "（…角色一时没有说出话来。）"

            # 模型请求工具：先回填 assistant 消息（含 tool_calls），再逐个执行
            if turn.assistant_message:
                messages.append(turn.assistant_message)
            executed = 0
            for tc in turn.tool_calls:
                name = (tc.get("name") or "").strip()
                if not name:
                    continue
                executed += 1
                call_id = tc.get("id") or f"call_{uuid.uuid4().hex[:8]}"
                arguments = tc.get("arguments") or {}
                output = registry.run(name, arguments) if registry else f"[工具错误] 未启用工具注册表。"
                summary = format_tool_call_summary(name, arguments)
                self.log.info("[%s] 工具调用 %s -> %d 字", char_name, summary, len(output))
                self.tool_event.emit(char_name, f"已调用 {summary}")
                messages.append({"role": "tool", "tool_call_id": call_id, "content": output[:1500]})
            if executed == 0:
                return last_content or "（…角色一时没有说出话来。）"

        self.log.warning("[%s] 工具调用达到 %d 轮上限，结束本轮", char_name, MAX_TOOL_ROUNDS)
        self.tool_event.emit(char_name, f"（工具调用已达 {MAX_TOOL_ROUNDS} 轮上限，结束）")
        return last_content or "（…角色一时没有说出话来。）"

    # ---------- 后端 ----------
    def _get_backend(self, char):
        cfg = self.config.backend_config()
        if char.backend and char.backend.get("backend"):
            override = copy.deepcopy(char.backend)
            for key, value in override.items():
                if key in ("openai", "ollama") and isinstance(value, dict):
                    cfg.setdefault(key, {}).update(value)
                else:
                    cfg[key] = value
        return create_backend(cfg, self.log)

    def test_backend(self) -> str:
        """测试当前全局后端连通性（同步，供设置界面在线程中调用）。"""
        backend = create_backend(self.config.backend_config(), self.log)
        return backend.chat(
            [{"role": "user", "content": "你好，请只回复四个字：连接成功"}],
            temperature=0.1,
            max_tokens=16,
        )

    # ---------- 消息组装 ----------
    def worldbook_match(self, char, user_text: str) -> list:
        """关键词匹配 Worldbook，返回命中的 content 列表（未命中返回空，不浪费 token）。"""
        hits = []
        lowered = user_text.lower()
        for entry in char.worldbook or []:
            content = str(entry.get("content") or "").strip()
            if not content:
                continue
            keywords = [str(k).strip().lower() for k in (entry.get("keywords") or []) if str(k).strip()]
            if any(kw and kw in lowered for kw in keywords):
                hits.append(content)
        return hits

    def _build_system(self, char, tone: str, worldbook_hits: list, l1_texts: list) -> str:
        """按规范顺序组装 system 文本：L2 永远在顶部。"""
        lines = []
        for memory in char.l2_core_memories:
            if str(memory).strip():
                lines.append(f"{L2_PREFIX}{memory}")
        if char.system_prompt:
            lines.append(char.system_prompt)
        if tone:
            lines.append(tone)
        for text in l1_texts:
            if str(text).strip():
                lines.append(f"{MEMORY_PREFIX}{text}")
        for text in worldbook_hits:
            if str(text).strip():
                lines.append(f"{WORLDBOOK_PREFIX}{text}")
        return "\n".join(lines)

    def assemble_messages(self, char, user_text: str, memory: MemoryManager,
                          emotion: EmotionSystem, bond: BondSystem,
                          inject_memory: bool = True) -> list:
        """组装最终消息列表（含 L1 记忆检索、Worldbook 命中、语气指令与上下文裁剪）。

        Args:
            inject_memory: 是否在入口强制注入 L1 记忆（经典 RAG 兜底）。当角色启用工具时
                置 False，检索交给 memory_search 工具按需进行（Agentic RAG），避免重复注入。
        """
        tone = emotion.tone_instruction(bond.values())
        worldbook_hits = self.worldbook_match(char, user_text)
        l1_texts = []
        if inject_memory:
            l1_hits = memory.recall(user_text, top_k=3)
            l1_texts = [m["content"] for m in l1_hits]

        system = self._build_system(char, tone, worldbook_hits, l1_texts)
        # 注入"回复风格"指令（简洁日常 / 丰富长文），由用户在全局面板选择
        style = CHAT_STYLES.get(self.config.get("llm", "chat_style", default="concise"))
        if style:
            system = system + "\n" + style
        history = memory.recent_turns()
        messages = [{"role": "system", "content": system}] + history + [{"role": "user", "content": user_text}]

        window = int(self.config.get("llm", "context_window", default=constants.DEFAULT_CONTEXT_WINDOW))
        ratio = float(self.config.get("chat", "context_ratio", default=0.8))
        budget = int(window * ratio)
        return self.trim_context(messages, budget)

    # ---------- 上下文裁剪 ----------
    @staticmethod
    def estimate_tokens(text: str) -> int:
        """粗略估算 token 数：中文约 1 字/token，英文约 4 字符/token，取折中。"""
        if not text:
            return 0
        return max(1, int(len(text) * 0.6)) + 4

    @staticmethod
    def _total_tokens(messages: list) -> int:
        return sum(ChatEngine.estimate_tokens(m.get("content", "")) for m in messages)

    @staticmethod
    def _strip_lines(system_msg: dict, prefix: str) -> dict:
        lines = system_msg.get("content", "").split("\n")
        kept = [line for line in lines if not line.startswith(prefix)]
        return {"role": "system", "content": "\n".join(kept)}

    def trim_context(self, messages: list, max_tokens: int) -> list:
        """按保留优先级裁剪上下文：L2 > 最近 3 轮 > 高重要性 L1 > 普通 L1 > 更早轮次。

        L2 核心记忆与角色 system_prompt 永远保留（不裁剪）。
        """
        total = self._total_tokens(messages)
        if total <= max_tokens:
            return messages

        system = messages[0]
        history = messages[1:-1]      # 历史轮次（user/assistant 成对）
        last = messages[-1]           # 最新 user 消息（永不裁剪）

        # 1) 从最旧开始成对裁剪历史，至少保留最近 3 轮（6 条）
        while len(history) > 6 and total > max_tokens:
            removed = history.pop(0)
            total -= self.estimate_tokens(removed.get("content", ""))

        # 2) 裁剪 L1 相关记忆行
        if total > max_tokens:
            system = self._strip_lines(system, MEMORY_PREFIX)
            total = self._total_tokens([system] + history + [last])

        # 3) 裁剪 Worldbook 行
        if total > max_tokens:
            system = self._strip_lines(system, WORLDBOOK_PREFIX)
            total = self._total_tokens([system] + history + [last])

        # 4) 仍超出：只保留最近 1 轮
        while len(history) > 2 and total > max_tokens:
            removed = history.pop(0)
            total -= self.estimate_tokens(removed.get("content", ""))

        # 5) 极端情况：仅保留 system + 最新 user（L2 与 system_prompt 永不丢失）
        if total > max_tokens:
            return [system, last]
        return [system] + history + [last]

    # ---------- 主动搭话 ----------
    def generate_initiative(self) -> None:
        """主动搭话：优先从台词库随机选，库为空则用 LLM 临时生成（异步）。"""
        char_name = self.active_character()
        if not char_name:
            return
        # 防重复：已有生成任务在跑则忽略本次点击
        if self._worker_init is not None and self._worker_init.isRunning():
            self.log.info("主动搭话生成中，忽略重复请求")
            return
        worker = _Worker(self._run_initiative, char_name)
        worker.ok.connect(self._on_initiative_ok)
        worker.failed.connect(self._on_initiative_failed)
        worker.finished.connect(worker.deleteLater)
        self._worker_init = worker
        worker.start()

    def _on_initiative_failed(self, message: str) -> None:
        """失败时重置 worker 引用，避免残留已删除对象导致后续点击崩溃。"""
        self._worker_init = None
        self.log.warning("主动搭话生成失败: %s", message)

    def _run_initiative(self, char_name: str):
        char = self.manager.load(char_name)
        candidates = [d for d in (char.initiative_dialogues or []) if str(d).strip()]
        if candidates:
            return char_name, random.choice(candidates)
        # 台词库为空：让 LLM 按角色人设临时生成一句（有指向性、有交互感）
        backend = self._get_backend(char)
        l2 = "\n".join(f"{L2_PREFIX}{m}" for m in char.l2_core_memories if str(m).strip())
        system = f"{l2}\n{char.system_prompt}" if l2 else char.system_prompt

        # 取最近几轮对话，供搭话自然衔接（没有则忽略）
        recent_lines = []
        try:
            memory = self.get_memory(char_name)
            for turn in memory.recent_turns(limit=4):
                speaker = "用户" if turn["role"] == "user" else char.name
                recent_lines.append(f"{speaker}：{turn['content'][:80]}")
        except Exception:
            recent_lines = []
        recent_text = "\n".join(recent_lines)

        recent_block = ""
        if recent_text:
            recent_block = f"以下是你们最近的对话片段（可自然衔接，若无关联则忽略）：\n{recent_text}\n\n"

        prompt = (
            f"（现在是你主动发起对话的时刻）请以{char.name}的身份，向用户发起一句主动搭话。\n"
            f"{recent_block}"
            "要求：\n"
            "1. 有指向性、有交互感——抛出一个具体话题或问句（例如你最近在忙/遇到的事、"
            "你对用户近况的关心、或你们共同经历/关系的回忆），让用户有明确的东西可以回应；\n"
            "2. 口语化，符合你的角色性格与说话风格，不要套话、不要空洞的问候；\n"
            "3. 不超过 60 字，直接输出这句话本身，不要任何前缀、解释或引号。"
        )
        # 主动搭话也走聊天配置的思考模式（默认关闭）
        thinking_chat = bool(self.config.get("llm", "thinking_chat", default=False))
        text = backend.chat(
            [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            temperature=0.9,
            max_tokens=2048,   # 推理模型需要先"思考"再输出答案，max_tokens 要给足
            thinking=thinking_chat,
        )
        # 推理模型偶发 content 为空：重试一次
        if not text or not text.strip():
            self.log.info("主动搭话返回为空，重试一次")
            text = backend.chat(
                [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                temperature=0.9,
                max_tokens=2048,
                thinking=thinking_chat,
            )
        self._track_initiative_usage(backend)
        return char_name, text.strip()

    def _track_initiative_usage(self, backend) -> None:
        from core.usage_tracker import add_usage
        add_usage(backend.last_usage)

    def _on_initiative_ok(self, result) -> None:
        self._worker_init = None
        try:
            char_name, text = result
            if text:
                self.initiative_text.emit(char_name, text)
        except (TypeError, ValueError):
            pass

    # ---------- 工具 ----------
    @staticmethod
    def anchor_text_for(char) -> str:
        """构造用于漂移检测锚点的文本：人设 + 口头禅 + 主动台词示例。

        实测（bge-small-zh-v1.5，2026-08 校准）：
        - 仅人设描述对短句回复区分度不足；
        - 加入角色台词示例后，人设内回复约 0.53-0.59，明显偏离的回复约 0.43-0.53，
          阈值默认 0.52（宁可漏检、避免误伤正常回复）。
        """
        parts = []
        for memory in char.l2_core_memories:
            if str(memory).strip():
                parts.append(f"核心设定：{memory}")
        if char.system_prompt:
            parts.append(char.system_prompt)
        catchphrases = [str(c).strip() for c in (char.catchphrases or []) if str(c).strip()]
        if catchphrases:
            parts.append(f"你说话时可能会说：{'、'.join('“%s”' % c for c in catchphrases[:6])}")
        dialogues = [str(d).strip() for d in (char.initiative_dialogues or []) if str(d).strip()]
        if dialogues:
            parts.append(f"你还会说：{'、'.join('“%s”' % d for d in dialogues[:6])}")
        return "\n".join(parts)

    def build_anchor_for(self, char) -> list:
        """为角色重新生成锚点向量（供设置界面调用）。"""
        anchor = self._drift.build_anchor(self.anchor_text_for(char))
        if anchor:
            char.anchor_vector = anchor
            self.manager.save(char)
        return anchor
