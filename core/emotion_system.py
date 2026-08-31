"""动态情绪与羁绊系统（事前语气引导）。

- 情绪状态：PAD 三维模型（愉悦度 P / 激活度 A / 支配度 D），范围 [-1, 1]，
  初始 [0.5, 0.3, 0.0]。基于规则分析更新（负面词 -> P 降低、感叹号 -> A 升高…），
  指数移动平均（EMA）平滑变化。
- 羁绊系统：四维向量 [温暖, 信任, 正式度, 幽默]，初始 0，
  根据对话内容调整（分享秘密 -> 信任 +0.1），长时间无对话每日衰减 0.05。
- 每次对话前根据情绪与羁绊生成自然语言语气指令，注入 System Prompt 末尾。

情绪与羁绊数据随角色 JSON 持久化。
"""
import datetime

# ---------- 情绪规则词表 ----------
POSITIVE_WORDS = [
    "开心", "高兴", "喜欢", "爱", "棒", "太好了", "哈哈", "嘻嘻", "笑", "幸福",
    "温暖", "感谢", "谢谢", "赞", "厉害", "可爱", "好耶", "期待", "兴奋", "惊喜",
]
NEGATIVE_WORDS = [
    "难过", "伤心", "哭", "泪", "生气", "愤怒", "讨厌", "恨", "烦", "痛苦",
    "崩溃", "失望", "害怕", "担心", "焦虑", "累", "疲惫", "压力", "悲伤",
    "孤独", "委屈", "疼", "痛", "死", "绝望", "郁闷",
]
AGGRESSIVE_WORDS = ["滚", "闭嘴", "烦死", "别烦", "命令", "必须", "立刻"]
SOFT_WORDS = ["对不起", "抱歉", "可以吗", "好吗", "求求", "帮帮", "拜托"]

# ---------- 羁绊规则词表 ----------
SECRET_WORDS = ["秘密", "悄悄", "只告诉你", "倾诉", "心事", "私密", "悄悄话"]
SHARE_WORDS = ["难过", "伤心", "哭", "害怕", "压力", "累", "烦", "孤独", "委屈"]
PRAISE_WORDS = ["喜欢", "可爱", "棒", "厉害", "好看", "好棒", "爱了", "赞", "优秀"]
HUMOR_WORDS = ["哈哈", "hhh", "笑死", "233", "hh", "搞笑", "整活"]
FORMAL_WORDS = ["您好", "您", "请", "谢谢您", "麻烦您", "贵"]
CASUAL_WORDS = ["草", "卧槽", "淦", "yyds", "emo", "摆烂", "整活"]


def _now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


class EmotionSystem:
    """PAD 情绪系统。"""

    # EMA 平滑系数
    ALPHA = 0.35

    def __init__(self, character):
        pad = list(character.emotion.get("pad") or [0.5, 0.3, 0.0])
        while len(pad) < 3:
            pad.append(0.0)
        self.pad = [float(x) for x in pad[:3]]

    # ---------- 更新 ----------
    def update(self, text: str, speaker: str = "user") -> None:
        """根据发言内容用规则更新情绪，再 EMA 平滑。"""
        delta = [0.0, 0.0, 0.0]  # [P, A, D]

        if any(w in text for w in POSITIVE_WORDS):
            delta[0] += 0.15
        if any(w in text for w in NEGATIVE_WORDS):
            delta[0] -= 0.15
        if any(w in text for w in AGGRESSIVE_WORDS):
            delta[0] -= 0.06
            delta[2] += 0.08
        if any(w in text for w in SOFT_WORDS):
            delta[2] -= 0.08

        if ("！" in text) or ("!" in text):
            delta[1] += 0.15
        if ("？" in text) or ("?" in text):
            delta[1] += 0.08
        if ("……" in text) or ("..." in text):
            delta[1] -= 0.08

        if len(text) >= 120:
            delta[2] += 0.06
        if len(text) >= 300:
            delta[2] += 0.04

        # EMA：目标 = clamp(pad + delta)，向目标平滑移动
        for i in range(3):
            target = max(-1.0, min(1.0, self.pad[i] + delta[i]))
            self.pad[i] += self.ALPHA * (target - self.pad[i])

    # ---------- 语气指令 ----------
    def tone_instruction(self, bond: dict) -> str:
        """根据当前情绪与羁绊生成自然语言语气指令。"""
        p, a, d = self.pad
        notes = []

        if p <= -0.3 and a <= -0.2:
            notes.append("注意：你现在的情绪是疲惫且感伤，说话速度放慢，多使用省略号。")
        elif p <= -0.3:
            notes.append("注意：你现在的情绪有些低落，语气应低沉柔和，少说玩笑话。")
        elif p >= 0.5 and a >= 0.4:
            notes.append("注意：你现在心情很好，语气轻快活泼，可以多使用感叹号和轻松的语气词。")
        elif a >= 0.5:
            notes.append("注意：你现在情绪比较激动，语速稍快，用词更有力度。")

        if d >= 0.5:
            notes.append("注意：你现在处于主导位置，态度可以主动、果断一些。")
        elif d <= -0.3:
            notes.append("注意：你现在比较顺从和依赖，语气柔软，多征求对方意见。")

        trust = float(bond.get("trust", 0.0))
        warmth = float(bond.get("warmth", 0.0))
        humor = float(bond.get("humor", 0.0))
        formality = float(bond.get("formality", 0.0))

        if trust > 0.8:
            notes.append("注意：当前与用户关系极度亲密，可以展现脆弱和依赖的一面，使用亲昵的称呼。")
        elif trust > 0.5:
            notes.append("注意：当前与用户已经比较信任，可以适当表达真实想法和情绪。")
        elif warmth > 0.6:
            notes.append("注意：你对用户已经产生好感，语气可以更温柔亲近。")

        if humor > 0.6:
            notes.append("注意：你们之间氛围轻松，可以适当开玩笑、吐槽。")
        if formality > 0.7:
            notes.append("注意：保持礼貌和正式的语气，避免过于随意的用语。")

        if not notes:
            notes.append("注意：保持自然平和的语气，完全按照角色设定说话。")
        return "\n".join(notes)

    # ---------- 持久化 ----------
    def apply(self, character) -> None:
        character.emotion["pad"] = [round(x, 4) for x in self.pad]
        character.emotion["updated_at"] = _now_iso()


class BondSystem:
    """四维羁绊系统：[温暖, 信任, 正式度, 幽默]。"""

    KEYS = ["warmth", "trust", "formality", "humor"]

    def __init__(self, character):
        self.vals = {k: float(character.bond.get(k, 0.0)) for k in self.KEYS}

    def values(self) -> dict:
        return dict(self.vals)

    def update(self, text: str, speaker: str = "user") -> None:
        """根据发言内容调整羁绊（主要依据用户发言）。"""
        if speaker != "user":
            return
        t = text
        if any(w in t for w in SECRET_WORDS):
            self.vals["trust"] += 0.10   # 分享秘密 -> 信任 +0.1
            self.vals["warmth"] += 0.05
        if any(w in t for w in SHARE_WORDS):
            self.vals["trust"] += 0.05   # 分享情绪 -> 信任微升
        if any(w in t for w in PRAISE_WORDS):
            self.vals["warmth"] += 0.06
        if any(w in t for w in HUMOR_WORDS):
            self.vals["humor"] += 0.08
        if any(w in t for w in FORMAL_WORDS):
            self.vals["formality"] += 0.05
        if any(w in t for w in CASUAL_WORDS):
            self.vals["formality"] -= 0.04
        for k in self.KEYS:
            self.vals[k] = max(0.0, min(1.0, self.vals[k]))

    @staticmethod
    def decay_if_idle(character) -> bool:
        """长时间无对话：每天四个维度各衰减 0.05。返回是否发生了衰减。"""
        last = character.bond.get("last_active") or ""
        changed = False
        if last:
            try:
                last_dt = datetime.datetime.fromisoformat(last)
                days = (datetime.datetime.now() - last_dt).days
                if days >= 1:
                    for k in BondSystem.KEYS:
                        character.bond[k] = max(0.0, float(character.bond.get(k, 0.0)) - 0.05 * days)
                    changed = True
            except ValueError:
                pass
        character.bond["last_active"] = _now_iso()
        return changed

    def apply(self, character) -> None:
        for k in self.KEYS:
            character.bond[k] = round(self.vals[k], 4)
        character.bond["last_active"] = _now_iso()
