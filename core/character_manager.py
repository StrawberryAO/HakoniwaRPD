"""角色管理：自动生成（调用 LLM 结构化输出）、保存、加载、编辑、删除。

角色 JSON 结构见 characters/README.md。所有字段在加载时都会做默认值补齐，
保证旧文件或手写文件也能正常加载。
"""
import datetime
import json
import os
import re
from dataclasses import dataclass, field
from typing import Optional

from core.llm_backends import LLMError, create_backend
from utils.logger import get_logger


class GenerationError(Exception):
    """角色自动生成失败（模型不识别、返回非法 JSON 等）。"""


# 自动生成时要求模型输出的 JSON 模板（与需求一致）
# 说明：口头禅有明确定义，防止把"单次著名台词"误录为口头禅。
GENERATION_SCHEMA = {
    "system_prompt": "第一人称的完整扮演提示，包含性格、说话风格、口头禅、背景、禁忌",
    "traits": ["性格标签"],
    "catchphrases": [
        "角色的标志性口头禅——指角色在台词中高频反复出现的固定用语，如\"真是拿你没办法\"\"嗯哼\"。"
        "注意：只收录真正反复出现的口头禅，不要收录角色单次说出的著名台词或标志性名场面台词（那不算口头禅）。"
        "若角色没有明显口头禅，返回空数组。"
    ],
    "background": "角色简要背景",
    "l2_core_memories": [
        "属于该角色最底层、不可遗忘的设定，比如身患绝症、特殊身份、与用户的关系等。这些将永远保留在上下文中。"
    ],
    "worldbook": [
        {
            "keywords": ["触发词1", "触发词2"],
            "content": "当用户消息包含上述触发词时，注入的世界观背景知识",
        }
    ],
}


def _now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


@dataclass
class Character:
    """角色数据模型（与 characters/{name}.json 一一对应）。"""

    name: str = ""
    work: str = ""
    created_at: str = ""
    system_prompt: str = ""
    traits: list = field(default_factory=list)
    catchphrases: list = field(default_factory=list)
    background: str = ""
    # L2 核心记忆：永远无条件置于 System Prompt 顶部，不可裁剪
    l2_core_memories: list = field(default_factory=list)
    # Worldbook：关键词触发注入
    worldbook: list = field(default_factory=list)
    # 主动搭话台词库（为空时用 LLM 临时生成）
    initiative_dialogues: list = field(default_factory=list)
    # 角色锚点向量（漂移检测用）
    anchor_vector: Optional[list] = None
    # PAD 情绪
    emotion: dict = field(default_factory=lambda: {"pad": [0.5, 0.3, 0.0], "updated_at": ""})
    # 四维羁绊
    bond: dict = field(
        default_factory=lambda: {
            "warmth": 0.0, "trust": 0.0, "formality": 0.0, "humor": 0.0, "last_active": "",
        }
    )
    # 独立后端设置（可选）：{"backend": "openai"|"ollama", "openai": {...}, "ollama": {...}}
    backend: Optional[dict] = None
    # GPT-SoVITS 语音包文件夹（可选）
    tts_folder: str = ""
    # 角色头像：图片路径（可选，留空显示圆形占位）
    avatar: str = ""
    # 角色形象：宠物窗显示的图片/GIF 路径（可选，留空回退到全局 window.pet_image）
    pet_image: str = ""

    # ---------- 序列化 ----------
    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "work": self.work,
            "created_at": self.created_at,
            "system_prompt": self.system_prompt,
            "traits": self.traits,
            "catchphrases": self.catchphrases,
            "background": self.background,
            "l2_core_memories": self.l2_core_memories,
            "worldbook": self.worldbook,
            "initiative_dialogues": self.initiative_dialogues,
            "anchor_vector": self.anchor_vector,
            "emotion": self.emotion,
            "bond": self.bond,
            "backend": self.backend,
            "tts_folder": self.tts_folder,
            "avatar": self.avatar,
            "pet_image": self.pet_image,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Character":
        char = cls()
        for key, value in (data or {}).items():
            if hasattr(char, key) and value is not None:
                setattr(char, key, value)
        char.ensure_defaults()
        return char

    def ensure_defaults(self) -> None:
        """补齐缺失字段，保证旧版本/手写 JSON 可加载。"""
        if not self.emotion or not isinstance(self.emotion, dict):
            self.emotion = {"pad": [0.5, 0.3, 0.0], "updated_at": ""}
        if not isinstance(self.emotion.get("pad"), list) or len(self.emotion["pad"]) != 3:
            self.emotion["pad"] = [0.5, 0.3, 0.0]
        if not self.bond or not isinstance(self.bond, dict):
            self.bond = {"warmth": 0.0, "trust": 0.0, "formality": 0.0, "humor": 0.0, "last_active": ""}
        for key in ("warmth", "trust", "formality", "humor"):
            self.bond.setdefault(key, 0.0)
        self.bond.setdefault("last_active", "")
        for attr in ("traits", "catchphrases", "l2_core_memories", "initiative_dialogues"):
            if not isinstance(getattr(self, attr), list):
                setattr(self, attr, [])
        if not isinstance(self.worldbook, list):
            self.worldbook = []
        self.worldbook = [w for w in self.worldbook if isinstance(w, dict)]
        if not isinstance(self.backend, dict):
            self.backend = None
        if not isinstance(self.anchor_vector, list):
            self.anchor_vector = None
        if not isinstance(self.avatar, str):
            self.avatar = ""
        if not isinstance(self.pet_image, str):
            self.pet_image = ""
        if not self.created_at:
            self.created_at = _now_iso()


# 角色名白名单：中英文、数字、下划线、连字符与空格，其余一律视为非法字符。
_UNSAFE_NAME_CHARS = re.compile(r"[^\w\-\u4e00-\u9fff ]+")


def _safe_name(name: str) -> str:
    """把角色名归一化为安全的文件名，阻断路径穿越。

    角色名来自 UI 输入框，直接拼路径会让 "../../evil" 之类的输入越出
    characters/ 目录。此处把所有非白名单字符（含路径分隔符与点号）折叠为下划线，
    使任何穿越尝试都退化成一个普通文件名。

    Args:
        name: 用户输入的角色名。

    Returns:
        安全文件名（不含扩展名）。全非法时回退 "_unnamed"。
    """
    safe = _UNSAFE_NAME_CHARS.sub("_", (name or "").strip()).strip()
    return safe or "_unnamed"


class CharacterManager:
    """角色文件管理 + LLM 自动生成。"""

    def __init__(self, characters_dir: str = "characters", config=None, logger=None):
        self.dir = characters_dir
        self.config = config
        self.log = logger or get_logger()
        os.makedirs(self.dir, exist_ok=True)

    # ---------- 文件操作 ----------
    def _path(self, name: str) -> str:
        return os.path.join(self.dir, f"{_safe_name(name)}.json")

    def exists(self, name: str) -> bool:
        return bool(name) and os.path.exists(self._path(name))

    def list_characters(self) -> list:
        names = [f[:-5] for f in os.listdir(self.dir) if f.endswith(".json")]
        return sorted(names)

    def load(self, name: str) -> Character:
        path = self._path(name)
        if not os.path.exists(path):
            raise FileNotFoundError(f"角色不存在: {name}")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return Character.from_dict(data)

    def save(self, character: Character) -> None:
        character.ensure_defaults()
        if not character.created_at:
            character.created_at = _now_iso()
        path = self._path(character.name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(character.to_dict(), f, ensure_ascii=False, indent=2)

    def delete(self, name: str) -> None:
        path = self._path(name)
        if os.path.exists(path):
            os.remove(path)

    # ---------- 自动生成 ----------
    @staticmethod
    def _parse_json(text: str) -> dict:
        """从模型输出中稳健地提取 JSON 对象（容忍 markdown 代码块与前后缀文本）。"""
        cleaned = text.strip()
        if cleaned.startswith("```"):
            # 去掉 ```json ... ``` 围栏
            first = cleaned.find("\n")
            last = cleaned.rfind("```")
            cleaned = cleaned[first + 1:last] if first != -1 and last != -1 else cleaned
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise GenerationError("模型返回的内容中未找到 JSON 对象。")
        try:
            return json.loads(cleaned[start:end + 1])
        except json.JSONDecodeError as exc:
            raise GenerationError(f"模型返回的不是有效 JSON: {exc}") from exc

    @staticmethod
    def _normalize_worldbook(entries) -> list:
        """归一化 worldbook 条目为 [{"keywords": [...], "content": "..."}]。"""
        result = []
        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            keywords = entry.get("keywords") or []
            if isinstance(keywords, str):
                keywords = [k.strip() for k in keywords.split(",")]
            content = str(entry.get("content") or "").strip()
            if not content:
                continue
            result.append({"keywords": [str(k).strip() for k in keywords if str(k).strip()], "content": content})
        return result

    def auto_generate(self, name: str, work: str = "", progress_cb=None, use_search: bool = None) -> Character:
        """调用 LLM 自动生成角色设定并保存。失败抛 GenerationError。

        Args:
            name: 角色名（必填）。
            work: 作品名（选填）。
            progress_cb: 可选进度回调 progress_cb(stage, percent, message)。
                stage 取值：start/search/thinking/parse/save/done/error；
                percent 为 0-100 的整数（thinking 阶段按生成进度递增）；
                message 为日志文本（thinking 阶段为已累积的生成内容，供实时展示）。
            use_search: 是否联网搜索参考资料。None 表示跟随配置 web_search.enabled。
        """
        def report(stage: str, percent: int, message: str) -> None:
            if progress_cb:
                progress_cb(stage, percent, message)

        name = (name or "").strip()
        if not name:
            raise GenerationError("角色名不能为空。")
        if self.config is None:
            raise GenerationError("未加载全局配置，无法调用 LLM。")

        report("start", 5, f"开始生成角色「{name}」…")

        # ---- 1. 联网搜索参考资料 ----
        reference_block = ""
        if use_search is None:
            use_search = bool((self.config.get("web_search", default={}) or {}).get("enabled", True))
        if use_search:
            query = f"{work} {name}".strip() if str(work).strip() else name
            report("search", 12, f"正在联网搜索参考资料：{query} …")
            from core.web_search import WebSearcher, format_references
            searcher = WebSearcher(self.config, self.log)
            results = searcher.search(query)
            reference_block = format_references(results, max_items=5)
            if results:
                report("search", 25, f"搜索到 {len(results)} 条参考资料，已注入生成提示。")
            else:
                report("search", 25, "未获取到搜索结果（搜索引擎不可用），将基于模型自身知识生成。")

        # ---- 2. 组装 prompt ----
        backend = create_backend(self.config.backend_config(), self.log)
        schema_text = json.dumps(GENERATION_SCHEMA, ensure_ascii=False, indent=2)
        prompt = (
            f"你是一个角色设定生成器。请根据作品《{work}》中的角色“{name}”，生成详细的角色扮演配置。\n"
        )
        if reference_block:
            prompt += f"{reference_block}\n"
        prompt += (
            f"请严格输出 JSON 格式，包含以下字段：\n{schema_text}\n"
            f"如果不知道这个角色，请返回 {{\"error\": \"unknown\"}}。"
        )

        # ---- 3. 流式调用 LLM（实时展示思考过程） ----
        report("thinking", 30, "")
        thinking_buf = []
        chunk_count = 0

        def on_chunk(piece: str) -> None:
            nonlocal chunk_count
            thinking_buf.append(piece)
            chunk_count += 1
            # 生成阶段进度：30% -> 85%（每收到一块 +1，封顶 85%）
            report("thinking", min(85, 30 + chunk_count), "".join(thinking_buf))

        messages = [{"role": "user", "content": prompt}]
        reply = None
        # 生成/修改人设默认开启思考模式（提升结构化 JSON 输出），可在全局设置关闭
        thinking_gen = bool(self.config.get("llm", "thinking_generate", default=True))
        try:
            reply = backend.chat_stream(messages, temperature=0.7, max_tokens=4096,
                                        json_mode=True, on_chunk=on_chunk, thinking=thinking_gen)
            # 流式偶发返回空：降级为一次非流式调用，更稳
            if not reply or not reply.strip():
                backend.last_usage = None
                report("thinking", 60, "流式返回为空，改用普通请求重试…")
                reply = backend.chat(messages, temperature=0.7, max_tokens=4096,
                                     json_mode=True, thinking=thinking_gen)
        except LLMError as exc:
            report("error", 0, f"调用 LLM 失败：{exc}")
            raise GenerationError(f"调用 LLM 失败: {exc}") from exc
        if not reply:
            report("error", 0, "模型返回为空。")
            raise GenerationError("模型返回为空，请重试。")

        # ---- 4. 解析校验 ----
        report("parse", 90, "正在解析模型输出…")
        data = self._parse_json(reply)
        if isinstance(data, dict) and data.get("error"):
            report("error", 0, "模型不识别该角色。")
            raise GenerationError("无法自动生成该角色（模型不识别该角色），请尝试手动创建。")

        system_prompt = str(data.get("system_prompt") or "").strip()
        if not system_prompt:
            report("error", 0, "生成结果缺少 system_prompt 字段。")
            raise GenerationError("生成结果缺少 system_prompt 字段，请重试。")

        char = Character(
            name=name,
            work=(work or "").strip(),
            created_at=_now_iso(),
            system_prompt=system_prompt,
            traits=[str(x).strip() for x in (data.get("traits") or []) if str(x).strip()],
            catchphrases=[str(x).strip() for x in (data.get("catchphrases") or []) if str(x).strip()],
            background=str(data.get("background") or "").strip(),
            l2_core_memories=[str(x).strip() for x in (data.get("l2_core_memories") or []) if str(x).strip()],
            worldbook=self._normalize_worldbook(data.get("worldbook")),
        )

        # ---- 5. 保存 ----
        report("save", 95, "设定解析完成，正在保存角色文件…")
        self.save(char)
        report("done", 100, f"角色「{name}」生成完成（联网参考：{'是' if reference_block else '否'}）。")
        return char

    # ---------- 对话式修改 ----------
    def refine_character(self, character: Character, instruction: str, progress_cb=None) -> Character:
        """与 LLM 对话，按用户要求修改角色设定的部分内容（如口头禅、细节）。

        流程：把当前设定的内容字段（system_prompt/traits/catchphrases/background/
        l2_core_memories/worldbook）交给 LLM，模型输出修改后的完整 JSON，然后
        只更新内容字段；name/work/emotion/bond/backend/tts_folder 等元数据保留。

        修改后清除角色锚点（anchor_vector）——内容已变，旧锚点不再代表当前人设，
        下次对话时自动重建，或在设置中手动"生成/更新锚点"。

        Args:
            character: 要修改的角色（内容字段取自其当前值）。
            instruction: 用户的自然语言修改要求。
            progress_cb: 同 auto_generate 的进度回调（start/thinking/parse/save/done）。
        """
        def report(stage: str, percent: int, message: str) -> None:
            if progress_cb:
                progress_cb(stage, percent, message)

        instruction = (instruction or "").strip()
        if not instruction:
            raise GenerationError("修改要求不能为空。")
        if self.config is None:
            raise GenerationError("未加载全局配置，无法调用 LLM。")

        report("start", 5, f"准备修改角色「{character.name}」…")

        current = {
            "system_prompt": character.system_prompt,
            "traits": character.traits,
            "catchphrases": character.catchphrases,
            "background": character.background,
            "l2_core_memories": character.l2_core_memories,
            "worldbook": character.worldbook,
        }
        current_json = json.dumps(current, ensure_ascii=False, indent=2)

        backend = create_backend(self.config.backend_config(), self.log)
        # 增量更新：只让模型输出"被修改的字段"，避免把完整设定重新输出一遍
        # （完整输出在设定较长时易被 max_tokens 截断导致 JSON 解析失败）
        prompt = (
            "你是一个角色设定编辑器。下面是当前的角色扮演配置（JSON 格式），"
            "请根据用户的修改要求进行修改。\n"
            f"当前配置：\n{current_json}\n\n"
            f"用户的修改要求：\n{instruction}\n\n"
            "请只输出你修改了的字段，输出为一个 JSON 对象。"
            "键只能来自：system_prompt, traits, catchphrases, background, "
            "l2_core_memories, worldbook。"
            "注意：catchphrases 指真正反复出现的口头禅（如\"真是拿你没办法\"），"
            "不要把角色单次说出的著名台词当作口头禅。"
            "没有被修改的字段不要输出。只输出 JSON 本身，不要代码块围栏、不要任何解释文字。"
        )

        report("thinking", 30, "")
        thinking_buf = []
        chunk_count = 0

        def on_chunk(piece: str) -> None:
            nonlocal chunk_count
            thinking_buf.append(piece)
            chunk_count += 1
            report("thinking", min(85, 30 + chunk_count), "".join(thinking_buf))

        messages = [{"role": "user", "content": prompt}]
        data = None
        # 生成/修改人设默认开启思考模式，可在全局设置关闭
        thinking_gen = bool(self.config.get("llm", "thinking_generate", default=True))
        # 最多尝试 2 次：首次流式；若解析失败（多半是输出被截断）则非流式重试一次
        # 并加大 max_tokens、提示模型输出完整 JSON。
        for attempt in (1, 2):
            report("thinking", 30 if attempt == 1 else 60, "再次生成…" if attempt == 2 else "")
            reply = None
            try:
                if attempt == 1:
                    reply = backend.chat_stream(messages, temperature=0.5, max_tokens=16384,
                                                json_mode=True, on_chunk=on_chunk, thinking=thinking_gen)
                    if not reply or not reply.strip():
                        backend.last_usage = None
                        report("thinking", 60, "流式返回为空，改用普通请求重试…")
                        reply = backend.chat(messages, temperature=0.5, max_tokens=16384,
                                             json_mode=True, thinking=thinking_gen)
                else:
                    # 重试：明确要求完整输出，避免再被截断
                    retry_prompt = prompt + (
                        "\n\n注意：上一次输出被中断了。请完整输出被修改字段的 JSON 对象，"
                        "不要省略任何内容，确保 JSON 完整闭合。"
                    )
                    reply = backend.chat([{"role": "user", "content": retry_prompt}],
                                         temperature=0.3, max_tokens=32768, json_mode=True,
                                         thinking=thinking_gen)
            except LLMError as exc:
                report("error", 0, f"调用 LLM 失败：{exc}")
                raise GenerationError(f"调用 LLM 失败: {exc}") from exc
            if not reply:
                report("error", 0, "模型返回为空。")
                raise GenerationError("模型返回为空，请重试。")

            report("parse", 90, "正在解析修改结果…")
            try:
                data = self._parse_json(reply)
                break
            except GenerationError as exc:
                # 解析失败：若还有重试机会则继续，否则报错
                if attempt >= 2:
                    report("error", 0, f"模型返回的不是有效 JSON: {exc}")
                    raise GenerationError(f"模型返回的不是有效 JSON: {exc}") from exc
                report("thinking", 55, "解析失败（输出可能被截断），准备重试…")
        if data is None:
            report("error", 0, "模型返回的不是有效 JSON。")
            raise GenerationError("模型返回的不是有效 JSON。")
        if isinstance(data, dict) and data.get("error"):
            report("error", 0, "模型无法完成本次修改。")
            raise GenerationError("模型无法完成修改，请换一种说法再试。")

        # ---- 只更新内容字段（缺失字段保持原值） ----
        if str(data.get("system_prompt") or "").strip():
            character.system_prompt = str(data["system_prompt"]).strip()
        if data.get("traits") is not None:
            character.traits = [str(x).strip() for x in data["traits"] if str(x).strip()]
        if data.get("catchphrases") is not None:
            character.catchphrases = [str(x).strip() for x in data["catchphrases"] if str(x).strip()]
        if data.get("background") is not None:
            character.background = str(data["background"]).strip()
        if data.get("l2_core_memories") is not None:
            character.l2_core_memories = [str(x).strip() for x in data["l2_core_memories"] if str(x).strip()]
        if data.get("worldbook") is not None:
            character.worldbook = self._normalize_worldbook(data["worldbook"])

        # 内容已变，旧锚点失效：清除，由下次对话或手动按钮重建
        if character.anchor_vector:
            character.anchor_vector = None

        report("save", 95, "修改已应用，正在保存角色文件…")
        self.save(character)
        report("done", 100, f"角色「{character.name}」修改完成。")
        return character
