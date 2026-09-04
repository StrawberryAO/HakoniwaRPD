"""角色漂移检测（防 OOC）。

- 准备阶段：将角色 system_prompt 分段计算嵌入向量取均值，作为"角色锚点向量"，
  存入角色 JSON（character.anchor_vector）。
- 每次 LLM 生成回复后，用同一嵌入模型计算回复向量，与锚点计算余弦相似度；
  低于阈值（默认 0.52，见 core.constants.DEFAULT_DRIFT_THRESHOLD）判定 OOC，
  由 chat_engine 追加系统消息让模型重写（最多重试 1 次）。

嵌入模型与 L1 记忆共用 memory_manager.EmbeddingProvider（sentence-transformers）。
模型缺失时自动降级：不检测、不拦截，不影响对话。
"""
import math
import re

from core import constants
from utils.logger import get_logger


class DriftDetector:
    """角色锚点生成与 OOC 余弦相似度检测。"""

    def __init__(self, config, logger=None):
        self.config = config
        self.log = logger or get_logger()
        self._provider = None

    def _get_provider(self):
        if self._provider is None:
            from core.memory_manager import EmbeddingProvider
            self._provider = EmbeddingProvider.get(self.config, self.log)
        return self._provider

    # ---------- 锚点 ----------
    @staticmethod
    def _segment(text: str) -> list:
        """把 system_prompt 切成有意义的短片段（段落 + 句子），限制最多 16 段。"""
        parts = [p.strip() for p in re.split(r"\n+|(?<=[。！？!?；;])", text) if p.strip()]
        segments, current = [], ""
        for part in parts:
            current += part
            if len(current) >= 12:
                segments.append(current)
                current = ""
        if current:
            segments.append(current)
        return segments[:16]

    def build_anchor(self, system_prompt: str):
        """生成角色锚点向量（各片段嵌入的均值）。模型不可用时返回 None。"""
        try:
            provider = self._get_provider()
            if not provider.is_available():
                return None
            segments = self._segment(system_prompt or "")
            if not segments:
                return None
            vectors = provider.encode(segments)
            dim = len(vectors[0])
            mean = [sum(v[i] for v in vectors) / len(vectors) for i in range(dim)]
            return mean
        except Exception as exc:
            self.log.debug("锚点生成失败: %s", exc)
            return None

    # ---------- 相似度 ----------
    @staticmethod
    def cosine_similarity(vec_a, vec_b):
        """纯 Python 余弦相似度；维度不符或零向量返回 None。"""
        if not vec_a or not vec_b or len(vec_a) != len(vec_b):
            return None
        dot = sum(x * y for x, y in zip(vec_a, vec_b))
        norm_a = math.sqrt(sum(x * x for x in vec_a))
        norm_b = math.sqrt(sum(x * x for x in vec_b))
        if norm_a == 0 or norm_b == 0:
            return None
        return dot / (norm_a * norm_b)

    def check(self, reply: str, anchor, threshold: float = constants.DEFAULT_DRIFT_THRESHOLD):
        """检测回复是否偏离角色设定。

        Returns:
            (similarity, is_ooc)：模型不可用、锚点缺失或回复过短时返回 (None, False)。
        """
        if not anchor:
            return None, False
        # 极短回复（语气词/附和）嵌入噪声大，跳过检测避免误判
        if len(reply.strip()) < 8:
            return None, False
        try:
            provider = self._get_provider()
            if not provider.is_available():
                return None, False
            vector = provider.encode([reply])[0]
            similarity = self.cosine_similarity(anchor, vector)
            if similarity is None:
                return None, False
            return similarity, similarity < threshold
        except Exception as exc:
            self.log.debug("漂移检测失败: %s", exc)
            return None, False
