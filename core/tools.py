"""Agent 工具注册表（Phase 1：OpenAI 兼容 function calling）。

- Tool：单个工具（OpenAI function 描述 + 处理器）。
- ToolRegistry：按名登记，产出 OpenAI `tools` 数组（schema），并执行工具调用。
- 处理器异常不外抛：统一格式化为「工具错误」文本回给模型，模型可自行修正或放弃。

内置工具（处理器由 ChatEngine 注入，因依赖 config / memory）：
- memory_search  检索该角色的 L1 长期记忆（Agentic RAG 的第一步：检索工具化）
- web_search     联网搜索（跟随全局 web_search.enabled 配置）
- get_time       当前日期时间（角色常需要"现在几点"）
"""
import ast
import json
import operator


class ToolError(Exception):
    """工具调用错误。"""


class Tool:
    """一个工具：OpenAI function schema + 执行处理器。"""

    def __init__(self, name: str, description: str, parameters: dict, handler):
        self.name = name
        self.description = description
        self.parameters = parameters or {"type": "object", "properties": {}}
        self._handler = handler

    # ---------- OpenAI function calling ----------
    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    # ---------- 执行 ----------
    def run(self, arguments: dict) -> str:
        """执行工具，把结果格式化为文本（给模型当 tool 消息内容）。"""
        args = arguments if isinstance(arguments, dict) else {}
        return str(self._handler(args))


class ToolRegistry:
    """工具登记与分发。"""

    def __init__(self):
        self._tools = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str):
        return self._tools.get(name)

    def names(self) -> list:
        return sorted(self._tools)

    def schemas(self, whitelist: list = None) -> list:
        """产出 OpenAI tools 数组；whitelist 非空时只输出白名单内的工具。"""
        if whitelist:
            return [t.schema() for name, t in sorted(self._tools.items()) if name in whitelist]
        return [t.schema() for _, t in sorted(self._tools.items())]

    def run(self, name: str, arguments: dict) -> str:
        """执行工具调用并返回文本结果；错误也以文本形式返回（不抛给上层）。"""
        tool = self._tools.get(name)
        if tool is None:
            return f"[工具错误] 不存在名为「{name}」的工具。"
        try:
            return tool.run(arguments)
        except Exception as exc:  # noqa: BLE001 - 工具错误文本化回给模型
            return f"[工具错误] {name} 执行失败: {type(exc).__name__}: {exc}"


# ---------- 参数 JSON Schema ----------
SCHEMA_MEMORY_SEARCH = {
    "type": "object",
    "properties": {
        "queries": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "要回忆的检索关键词列表（数组）。请把用户含糊的指代（如“她”“上次那件事”）"
                "改写为可检索的人名/事件/约定等具体描述，并可给多个角度；工具会分别检索并合并去重。"
            ),
        },
        "top_k": {
            "type": "integer",
            "default": 3,
            "minimum": 1,
            "maximum": 8,
            "description": "每个关键词返回的记忆条数（默认 3）。",
        },
    },
    "required": ["queries"],
    "additionalProperties": False,
}

SCHEMA_WEB_SEARCH = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "要联网搜索的关键词或问题。"}
    },
    "required": ["query"],
    "additionalProperties": False,
}

SCHEMA_GET_TIME = {"type": "object", "properties": {}, "additionalProperties": False}

SCHEMA_WEATHER = {
    "type": "object",
    "properties": {
        "city": {
            "type": "string",
            "description": "要查询天气的城市名（中文或英文/拼音均可，如「北京」「Shanghai」）。",
        }
    },
    "required": ["city"],
    "additionalProperties": False,
}

SCHEMA_CALCULATOR = {
    "type": "object",
    "properties": {
        "expression": {
            "type": "string",
            "description": "要计算的算术表达式，只能包含数字、小数点和 + - * / ( ) % 运算符。",
        }
    },
    "required": ["expression"],
    "additionalProperties": False,
}


def _parse_arguments(raw: str) -> dict:
    """把模型输出的 arguments（JSON 字符串）解析为 dict；失败返回 {}。"""
    if not raw or not str(raw).strip():
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError):
        return {}


# ---------- 计算器安全求值 ----------
_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UN_OPS = {ast.USub: operator.neg, ast.UAdd: operator.pos}


def safe_eval(expression: str) -> float:
    """用 AST 白名单安全求值算术表达式，仅允许数字与 + - * / % ( ) **；非法抛 ValueError。"""
    try:
        node = ast.parse(str(expression), mode="eval")
    except (SyntaxError, ValueError) as exc:
        raise ValueError("表达式语法无效") from exc

    def _ev(n):
        if isinstance(n, ast.Expression):
            return _ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool):
            return n.value
        if isinstance(n, ast.BinOp) and type(n.op) in _BIN_OPS:
            return _BIN_OPS[type(n.op)](_ev(n.left), _ev(n.right))
        if isinstance(n, ast.UnaryOp) and type(n.op) in _UN_OPS:
            return _UN_OPS[type(n.op)](_ev(n.operand))
        raise ValueError("表达式包含不支持的成分")

    return _ev(node)


def format_tool_call_summary(name: str, arguments: dict, max_len: int = 60) -> str:
    """工具调用摘要（给 UI/日志显示），如 memory_search("上次的秘密")。"""
    args = arguments if isinstance(arguments, dict) else {}
    parts = []
    for key in ("query", "text"):
        if key in args:
            parts.append(str(args[key]))
    brief = "，".join(parts)[:max_len]
    if brief:
        return f"{name}({brief})"
    return name
