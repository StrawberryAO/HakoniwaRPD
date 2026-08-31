"""全局配置管理。

负责加载 / 保存 config.json，并与内置默认配置做深合并，
保证新增配置项缺省时程序依然可运行。
"""
import copy
import json
import os

DEFAULT_CONFIG = {
    "llm": {
        # 默认后端：openai（OpenAI 兼容，含 DeepSeek）或 ollama
        "backend": "openai",
        # 模型上下文窗口大小（token），用于上下文裁剪预算。
        # 默认保守值；ds-v4 系列等大窗口模型可在设置中调到 1M（上限 2M）。
        "context_window": 8192,
        # 回复风格：concise 简洁日常（默认）| detailed 丰富长文
        "chat_style": "concise",
        # DeepSeek V4 思考模式开关：聊天/搭话默认关闭（更快更口语化），
        # 生成/修改人设默认开启（结构化 JSON 输出更稳）。
        "thinking_chat": False,
        "thinking_generate": True,
        "openai": {
            "api_key": "",
            "base_url": "https://api.deepseek.com/v1",
            "model": "deepseek-chat",
            "timeout": 90,
        },
        "ollama": {
            "base_url": "http://127.0.0.1:11434",
            "model": "qwen2.5:7b",
            "timeout": 180,
        },
    },
    "embedding": {
        # 嵌入模型：用于 L1 记忆检索与角色漂移检测
        "model": "BAAI/bge-small-zh-v1.5",
        "device": "cpu",
    },
    "hf": {
        # HuggingFace 镜像端点（留空 = 官方 huggingface.co）。
        # 国内网络建议 https://hf-mirror.com，用于下载嵌入模型。
        "endpoint": "",
        # 模型缓存目录（相对项目根，保证便携可写；默认官方用户目录时可留空）
        "cache_dir": "models/hub",
    },
    "chat": {
        # 上下文窗口使用比例（超过则裁剪）
        "context_ratio": 0.8,
        # 角色漂移（OOC）判定阈值（余弦相似度）。
        # 按 bge-small-zh-v1.5 实测校准：锚点=人设+口头禅+台词+近期回复时，
        # 人设内回复约 0.53+，明显偏离约 0.43-0.53，默认 0.52 宁可漏检不误伤。
        "drift_threshold": 0.52,
        # OOC 时最大重试次数
        "max_ooc_retries": 1,
    },
    "web_search": {
        # 角色自动生成时的联网搜索（参考资料注入生成提示）
        "enabled": True,
        # 引擎：auto（依次尝试 bing -> duckduckgo）| bing | duckduckgo
        "engine": "auto",
        "top_k": 5,
        "timeout": 15,
    },
    "initiative": {
        # 主动搭话引擎
        "enabled": True,
        "interval_minutes": 15,   # 每隔多少分钟检查一次
        "idle_minutes": 10,       # 用户多久未说话视为"空闲"
    },
    "tts": {
        "enabled": True,
        # GPT-SoVITS 推理服务地址（api.py 默认 9880 端口）
        "sovits_url": "http://127.0.0.1:9880",
    },
    "window": {
        "opacity": 0.92,
        "auto_hide": True,        # 贴边自动隐藏
        "always_on_top": False,   # 窗口置顶（宠物与对话窗），默认不置顶
        # 宠物形象：用户自行导入的图片/GIF 路径（留空显示占位圆点）
        "pet_image": "",
        # 用户头像：聊天窗口中"我"的头像路径（留空显示圆形占位）
        "user_avatar": "",
        # 聊天窗口背景图片（留空 = 无背景）
        "chat_bg": "",
        # 聊天窗口尺寸（拖动缩放后记住，下次启动恢复）
        "chat_w": 430,
        "chat_h": 580,
    },
    "storage": {
        "chroma_dir": "data/chroma",
    },
}


def deep_merge(base: dict, override: dict) -> dict:
    """递归合并字典：override 覆盖 base，返回新字典。"""
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


class Config:
    """全局配置对象。"""

    def __init__(self, path: str = "config.json"):
        self.path = path
        self.data = copy.deepcopy(DEFAULT_CONFIG)
        self.load()

    # ---------- 读写 ----------
    def load(self) -> None:
        if not os.path.exists(self.path):
            # 无 config.json 时：若存在 config.example.json 模板，按模板初始化
            example = os.path.join(os.path.dirname(self.path) or ".", "config.example.json")
            if os.path.exists(example) and not os.path.abspath(self.path).endswith("config.example.json"):
                try:
                    with open(example, "r", encoding="utf-8") as f:
                        self.data = deep_merge(copy.deepcopy(DEFAULT_CONFIG), json.load(f))
                except Exception as exc:
                    print(f"[config] 示例配置解析失败，使用默认配置: {exc}")
        else:
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                self.data = deep_merge(copy.deepcopy(DEFAULT_CONFIG), loaded)
            except Exception as exc:  # 配置损坏也不影响启动
                print(f"[config] 配置文件解析失败，使用默认配置: {exc}")
        self._apply_env_overrides()

    def _apply_env_overrides(self) -> None:
        """环境变量覆盖（开源场景：不把 API Key 写在文件里）。

        支持：DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL / DEEPSEEK_MODEL，
        覆盖 llm.openai 下对应字段。
        """
        openai = self.data.setdefault("llm", {}).setdefault("openai", {})
        if os.environ.get("DEEPSEEK_API_KEY"):
            openai["api_key"] = os.environ["DEEPSEEK_API_KEY"]
        if os.environ.get("DEEPSEEK_BASE_URL"):
            openai["base_url"] = os.environ["DEEPSEEK_BASE_URL"]
        if os.environ.get("DEEPSEEK_MODEL"):
            openai["model"] = os.environ["DEEPSEEK_MODEL"]

    def save(self) -> None:
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            print(f"[config] 保存配置失败: {exc}")

    # ---------- 访问 ----------
    def get(self, *keys, default=None):
        """按路径读取配置，如 config.get("llm", "backend")。"""
        node = self.data
        for key in keys:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    def set(self, value, *keys) -> None:
        """按路径写入配置并立即落盘。"""
        node = self.data
        for key in keys[:-1]:
            node = node.setdefault(key, {})
        node[keys[-1]] = value
        self.save()

    def backend_config(self) -> dict:
        """返回当前激活 LLM 后端的配置块（含 backend 标识与上下文窗口）。"""
        llm = self.get("llm", default={}) or {}
        backend = llm.get("backend", "openai")
        block = copy.deepcopy(llm.get(backend, {}))
        block["backend"] = backend
        block["context_window"] = llm.get("context_window", 8192)
        return block
