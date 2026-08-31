"""联网搜索模块（角色自动生成时获取参考资料）。

- 引擎顺序：cn.bing.com RSS（国内可达、稳定）-> DuckDuckGo HTML（备选）。
- 所有失败静默返回空列表，不影响角色生成主流程。
- 搜索引擎返回的是外部数据，仅作为参考资料注入 prompt，可能不准确。
"""
import html
import re
import xml.etree.ElementTree as ET
from urllib.parse import parse_qs, quote, unquote, urlparse

import requests

from utils.logger import get_logger

_UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}


class WebSearcher:
    """轻量联网搜索。"""

    def __init__(self, config, logger=None):
        self.config = config
        self.log = logger or get_logger()
        cfg = config.get("web_search", default={}) or {}
        self.enabled = bool(cfg.get("enabled", True))
        self.engine = (cfg.get("engine", "auto") or "auto").strip().lower()
        self.top_k = int(cfg.get("top_k", 5))
        self.timeout = float(cfg.get("timeout", 15))

    def search(self, query: str, top_k: int = None) -> list:
        """搜索并返回 [{title, snippet, url}, ...]；失败返回 []。"""
        if not self.enabled or not query.strip():
            return []
        top_k = top_k or self.top_k
        engines = []
        if self.engine in ("auto", "bing"):
            engines.append(self._search_bing)
        if self.engine in ("auto", "duckduckgo"):
            engines.append(self._search_duckduckgo)
        for engine_fn in engines:
            try:
                results = engine_fn(query, top_k)
                if results:
                    return results
            except Exception as exc:
                self.log.debug("搜索引擎 %s 失败: %s", engine_fn.__name__, exc)
        return []

    # ---------- 引擎：Bing RSS ----------
    def _search_bing(self, query: str, top_k: int) -> list:
        url = f"https://cn.bing.com/search?q={quote(query)}&format=rss&count={top_k}"
        resp = requests.get(url, headers=_UA, timeout=self.timeout)
        if resp.status_code != 200:
            return []
        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError:
            return []
        results = []
        for item in root.findall(".//item")[:top_k]:
            title = self._clean(item.findtext("title") or "")
            link = (item.findtext("link") or "").strip()
            desc = self._clean(item.findtext("description") or "")
            if title or desc:
                results.append({"title": title, "snippet": desc, "url": link})
        return results

    # ---------- 引擎：DuckDuckGo HTML ----------
    def _search_duckduckgo(self, query: str, top_k: int) -> list:
        url = f"https://html.duckduckgo.com/html/?q={quote(query)}"
        resp = requests.get(url, headers=_UA, timeout=self.timeout)
        if resp.status_code != 200:
            return []
        text = resp.text
        titles = re.findall(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', text, re.S)
        snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', text, re.S)
        results = []
        for i, (href, title) in enumerate(titles[:top_k]):
            results.append({
                "title": self._clean(title),
                "snippet": self._clean(snippets[i]) if i < len(snippets) else "",
                "url": self._normalize_ddg_url(href),
            })
        return results

    @staticmethod
    def _normalize_ddg_url(href: str) -> str:
        if "uddg=" in href:
            parsed = parse_qs(urlparse(href).query)
            if parsed.get("uddg"):
                return unquote(parsed["uddg"][0])
        return unquote(href)

    @staticmethod
    def _clean(raw: str) -> str:
        text = html.unescape(raw or "")
        text = re.sub(r"<[^>]+>", "", text)
        return re.sub(r"\s+", " ", text).strip()


def format_references(results: list, max_items: int = 5) -> str:
    """把搜索结果格式化为注入 prompt 的参考文本块（无结果返回空串）。"""
    if not results:
        return ""
    lines = ["以下是网络搜索到的参考资料（可能不准确或不完整，仅作参考，以你对该角色的已有知识为准）："]
    for i, item in enumerate(results[:max_items], start=1):
        title = item.get("title") or ""
        snippet = item.get("snippet") or ""
        url = item.get("url") or ""
        lines.append(f"{i}. 标题：{title}")
        if snippet:
            lines.append(f"   摘要：{snippet[:300]}")
        if url:
            lines.append(f"   来源：{url[:200]}")
    return "\n".join(lines)
