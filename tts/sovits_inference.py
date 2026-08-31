"""GPT-SoVITS 推理封装。

GPT-SoVITS 需要用户自行部署推理服务（api.py，默认端口 9880）。
本模块仅通过 HTTP 调用其接口合成语音，不加载任何本地模型。
所有异常在内部捕获并返回 False，保证上层静默降级。
"""
import requests


class GPTSoVITSInference:
    """GPT-SoVITS 推理客户端。"""

    def __init__(self, base_url: str = "http://127.0.0.1:9880",
                 ref_audio_path: str = "",
                 prompt_text: str = "",
                 prompt_language: str = "中文",
                 logger=None):
        self.base_url = (base_url or "http://127.0.0.1:9880").rstrip("/")
        self.ref_audio_path = ref_audio_path
        self.prompt_text = prompt_text
        self.prompt_language = prompt_language
        self.log = logger

    def ping(self, timeout: float = 2.0) -> bool:
        """探测服务是否可达（任何 HTTP 响应都算可达）。"""
        try:
            requests.get(f"{self.base_url}/", timeout=timeout)
            return True
        except Exception:
            try:
                requests.post(f"{self.base_url}/", timeout=timeout)
                return True
            except Exception:
                return False

    def synthesize(self, text: str, out_path: str, timeout: float = 120.0) -> bool:
        """合成语音并保存到 out_path（wav）。成功返回 True，任何失败返回 False。"""
        if not text or not text.strip():
            return False
        params = {
            "text": text,
            "text_lang": "中文",
            "ref_audio_path": self.ref_audio_path or "",
            "prompt_text": self.prompt_text,
            "prompt_lang": self.prompt_language,
        }
        try:
            resp = requests.get(f"{self.base_url}/", params=params, timeout=timeout)
            if resp.status_code == 200 and resp.content:
                with open(out_path, "wb") as f:
                    f.write(resp.content)
                return True
        except Exception:
            pass
        # 部分版本服务只支持 POST，兜底再试一次
        try:
            resp = requests.post(f"{self.base_url}/", params=params, timeout=timeout)
            if resp.status_code == 200 and resp.content:
                with open(out_path, "wb") as f:
                    f.write(resp.content)
                return True
        except Exception:
            pass
        if self.log:
            self.log.warning("GPT-SoVITS 合成失败（服务地址 %s）", self.base_url)
        return False
