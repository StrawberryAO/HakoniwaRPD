"""分层记忆引擎。

- L0（工作记忆）：当前对话窗口内的历史轮次（deque，上限 40 条）。
- L1（长期记忆）：使用 ChromaDB 向量数据库存储历史对话切片（可选依赖）。
    存储时记录 importance（情绪波动打分）与 timestamp；
    检索时按加权分数 `0.6*相似度 + 0.2*重要性 + 0.2*时效性` 取 top3 注入；
    每次被检索命中 importance 微幅提升。
- 嵌入模型：sentence-transformers（可选依赖），供 L1 检索与漂移检测共用。

所有可选依赖均 try/except 导入，缺失时静默降级，不影响核心对话。
"""
import hashlib
import math
import os
import re
import threading
import time
import uuid
from collections import deque

from utils.logger import get_logger

# ---------- 可选依赖：sentence-transformers ----------
try:
    from sentence_transformers import SentenceTransformer
    _ST_OK = True
except Exception:  # pragma: no cover - 缺失时降级
    _ST_OK = False
    SentenceTransformer = None

# ---------- 可选依赖：chromadb ----------
try:
    import chromadb
    _CHROMA_OK = True
except Exception:  # pragma: no cover - 缺失时降级
    _CHROMA_OK = False
    chromadb = None

# ---------- 重要性打分词表（情绪波动） ----------
EMOTION_WORDS = {
    "愤怒": 0.30, "恨": 0.35, "哭": 0.30, "泪": 0.30, "开心": 0.30, "高兴": 0.25,
    "难过": 0.30, "伤心": 0.30, "感动": 0.25, "爱": 0.25, "讨厌": 0.30, "害怕": 0.30,
    "担心": 0.25, "惊喜": 0.30, "笑": 0.20, "死": 0.35, "痛": 0.30, "崩溃": 0.35,
    "失望": 0.28, "孤独": 0.28, "秘密": 0.35, "发誓": 0.30, "永远": 0.25, "绝对": 0.22,
    "第一次": 0.25, "最重要": 0.30, "生日": 0.25, "喜欢": 0.22, "讨厌": 0.28,
}


def compute_importance(text: str) -> float:
    """基于情绪词与标点计算对话切片的重要性，范围 [0, 1]。"""
    if not text:
        return 0.0
    score = 0.20
    for word, weight in EMOTION_WORDS.items():
        if word in text:
            score += weight
    score += min(0.15, (text.count("!") + text.count("！")) * 0.02)
    if len(text) > 60:
        score += 0.05
    if len(text) > 150:
        score += 0.05
    return max(0.0, min(1.0, score))


class EmbeddingProvider:
    """sentence-transformers 嵌入模型单例（线程安全懒加载）。"""

    _instance = None
    _lock = threading.Lock()

    def __init__(self, model_name: str, device: str = "cpu", logger=None):
        self.model_name = model_name
        self.device = device
        self.log = logger or get_logger()
        self._model = None
        self._failed = False
        self._lock = threading.Lock()   # 实例锁：串行化懒加载与 encode（ST 非线程安全）

    @classmethod
    def get(cls, config, logger=None) -> "EmbeddingProvider":
        with cls._lock:
            if cls._instance is None:
                cfg = config.get("embedding", default={}) or {}
                cls._instance = cls(
                    cfg.get("model", "BAAI/bge-small-zh-v1.5"),
                    cfg.get("device", "cpu"),
                    logger,
                )
            return cls._instance

    def is_available(self) -> bool:
        if self._model is not None:
            return True
        with self._lock:
            if self._model is not None:
                return True
            if self._failed:
                return False
            if not _ST_OK:
                self._failed = True
                return False
            try:
                self.log.info("正在加载嵌入模型 %s ...", self.model_name)
                self._model = SentenceTransformer(self.model_name, device=self.device)
                return True
            except Exception as exc:
                self.log.warning("嵌入模型加载失败，L1 记忆/漂移检测降级: %s", exc)
                self._failed = True
                return False

    def encode(self, texts) -> list:
        """将文本（或文本列表）编码为归一化向量列表。"""
        if not self.is_available():
            raise RuntimeError("嵌入模型不可用")
        if isinstance(texts, str):
            texts = [texts]
        if not texts:
            return []
        with self._lock:
            vectors = self._model.encode(texts, normalize_embeddings=True)
        return [v.tolist() for v in vectors]


class LongTermMemory:
    """ChromaDB 长期记忆（L1）：按角色分 collection 存储对话切片。"""

    def __init__(self, character_name: str, config, logger=None):
        self.character_name = character_name
        self.config = config
        self.log = logger or get_logger()
        self._collection = None

    def _ensure(self):
        """懒初始化 chroma 客户端与 collection。"""
        if self._collection is not None:
            return True
        if not _CHROMA_OK:
            return False
        try:
            chroma_dir = self.config.get("storage", "chroma_dir", default="data/chroma")
            os.makedirs(chroma_dir, exist_ok=True)
            client = chromadb.PersistentClient(path=chroma_dir)
            # chromadb 1.x 要求 collection 名仅含 [a-zA-Z0-9._-] 且长度 3-512：
            # 中文角色名先转 ASCII，再附加短哈希保证唯一性
            safe = re.sub(r"[^a-zA-Z0-9._-]", "_", self.character_name).strip("._")[:40] or "char"
            if safe != self.character_name:
                safe = f"{safe}_{hashlib.md5(self.character_name.encode('utf-8')).hexdigest()[:8]}"
            self._collection = client.get_or_create_collection(
                name=f"char_{safe}", metadata={"hnsw:space": "cosine"}
            )
            return True
        except Exception as exc:
            self.log.warning("ChromaDB 初始化失败，L1 记忆降级: %s", exc)
            return False

    def available(self) -> bool:
        return self._ensure()

    def store_slice(self, text: str, importance: float, ts: float = None) -> None:
        """存储一段对话切片（含重要性元数据）。"""
        if not self._ensure():
            return
        try:
            emb = EmbeddingProvider.get(self.config, self.log).encode([text])[0]
        except Exception:
            return
        try:
            self._collection.add(
                ids=[str(uuid.uuid4())],
                embeddings=[emb],
                documents=[text],
                metadatas=[{"importance": float(importance), "timestamp": ts or time.time()}],
            )
        except Exception as exc:
            self.log.debug("L1 写入失败: %s", exc)

    def recall(self, query: str, top_k: int = 3) -> list:
        """加权检索：0.6*相似度 + 0.2*重要性 + 0.2*时效性，返回 top_k 记忆。"""
        if not self._ensure():
            return []
        try:
            count = self._collection.count()
            if count == 0:
                return []
            emb = EmbeddingProvider.get(self.config, self.log).encode([query])[0]
            n = min(top_k, count)
            res = self._collection.query(query_embeddings=[emb], n_results=n)
        except Exception as exc:
            self.log.debug("L1 检索失败: %s", exc)
            return []

        ids = (res.get("ids") or [[]])[0]
        distances = (res.get("distances") or [[]])[0]
        documents = (res.get("documents") or [[]])[0]
        metadatas = (res.get("metadatas") or [[]])[0]
        now = time.time()

        meta_by_id = {}
        for i, doc_id in enumerate(ids):
            meta_by_id[doc_id] = metadatas[i] if i < len(metadatas) and metadatas[i] else {}

        scored = []
        for i, doc_id in enumerate(ids):
            similarity = 1.0 - float(distances[i])          # 余弦相似度
            meta = meta_by_id.get(doc_id, {})
            importance = float(meta.get("importance", 0.3))
            ts = float(meta.get("timestamp", now))
            age_days = max(0.0, (now - ts) / 86400.0)
            recency = math.exp(-age_days / 30.0)             # 30 天半衰期
            score = 0.6 * similarity + 0.2 * importance + 0.2 * recency
            scored.append({"id": doc_id, "content": str(documents[i]), "score": score})

        scored.sort(key=lambda x: x["score"], reverse=True)
        top = scored[:top_k]

        # 命中提升重要性（微幅 +0.05）
        try:
            for item in top:
                meta = meta_by_id.get(item["id"], {})
                new_imp = min(1.0, float(meta.get("importance", 0.3)) + 0.05)
                self._collection.update(
                    ids=[item["id"]],
                    metadatas=[{"importance": new_imp, "timestamp": meta.get("timestamp", now)}],
                )
        except Exception:
            pass
        return [{"content": x["content"], "score": round(x["score"], 4)} for x in top]


class MemoryManager:
    """按角色管理的分层记忆入口：L0 工作记忆 + L1 长期记忆。"""

    def __init__(self, character_name: str, config, logger=None):
        self.character_name = character_name
        self.config = config
        self.log = logger or get_logger()
        # L0：最近 40 条消息的工作记忆
        self._short = deque(maxlen=40)
        self._l1 = None

    # ---------- L0 工作记忆 ----------
    def add_turn(self, role: str, content: str) -> None:
        self._short.append({"role": role, "content": content})

    def recent_turns(self, limit: int = None) -> list:
        turns = [{"role": t["role"], "content": t["content"]} for t in self._short]
        if limit is not None:
            turns = turns[-limit:]
        return turns

    def clear_short(self) -> None:
        self._short.clear()

    # ---------- L1 长期记忆 ----------
    def l1(self) -> LongTermMemory:
        if self._l1 is None:
            self._l1 = LongTermMemory(self.character_name, self.config, self.log)
        return self._l1

    def store_conversation(self, user_text: str, reply_text: str, ts: float = None) -> None:
        """把一轮完整对话切片存入 L1（带情绪重要性打分）。"""
        l1 = self.l1()
        if not l1.available():
            return
        text = f"用户：{user_text}\n角色：{reply_text}"
        importance = max(compute_importance(user_text), compute_importance(reply_text))
        l1.store_slice(text, importance, ts or time.time())

    def recall(self, query: str, top_k: int = 3) -> list:
        """检索 L1 记忆（加权打分，top_k 默认 3）。"""
        l1 = self.l1()
        if not l1.available():
            return []
        return l1.recall(query, top_k)
