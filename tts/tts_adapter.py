"""TTS 适配器。

- 在角色设置中指定"语音包文件夹"路径后，TTS 适配器检测该文件夹中的参考音频
  （首个 wav/mp3/flac）作为 GPT-SoVITS 的音色参考。
- 合成在工作线程中进行，完成后通过信号在主线程用 QMediaPlayer 播放。
- 服务不可达时降级为纯文本（首次通过 failed 信号提示一次，之后静默）；
  合成失败会通过 failed 信号上报给 UI，不再完全静默。
"""
import os
import tempfile
import uuid

from PySide6.QtCore import QObject, QThread, QUrl, Signal

from core import constants
from tts.sovits_inference import GPTSoVITSInference
from utils.logger import get_logger


class _SynthWorker(QThread):
    """后台合成线程：合成成功发射 done(path)，失败发射 failed。"""

    done = Signal(str)
    failed = Signal(str)

    def __init__(self, base_url: str, ref_audio: str, text: str, logger=None):
        super().__init__()
        self.base_url = base_url
        self.ref_audio = ref_audio
        self.text = text
        self.log = logger

    def run(self):
        out_path = os.path.join(tempfile.gettempdir(), f"pet_tts_{uuid.uuid4().hex}.wav")
        infer = GPTSoVITSInference(base_url=self.base_url, ref_audio_path=self.ref_audio, logger=self.log)
        if infer.synthesize(self.text, out_path):
            self.done.emit(out_path)
        else:
            _safe_remove(out_path)
            self.failed.emit("合成失败（服务不可达或未部署）")


def _safe_remove(path: str) -> None:
    """删除临时 wav，失败不抛出（避免重复报错）。"""
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


class TTSAdapter(QObject):
    """TTS 适配器：探测服务 -> 线程合成 -> 主线程播放。"""

    play_requested = Signal(str)
    failed = Signal(str)            # 合成失败/服务不可达（首次）时上报，UI 非阻塞提示

    def __init__(self, config, logger=None):
        super().__init__()
        self.config = config
        self.log = logger or get_logger()
        self._player = None
        self._audio_output = None
        self._server_ok = None      # None=未探测, True/False=结果
        self._warned = False        # 服务不可达仅提示一次，避免每次回复都刷屏
        self._current_temp_path = None   # 当前正在播放的临时 wav（用于清理）
        self._workers = []

    # ---------- 可用性 ----------
    def is_enabled(self) -> bool:
        return bool(self.config.get("tts", "enabled", default=constants.DEFAULT_TTS_ENABLED))

    # ---------- 对外接口 ----------
    def speak(self, text: str, character) -> None:
        """合成并播放角色语音。任何环节失败都静默降级。

        注意：不再在主线程同步探测服务（用户曾反馈首次搭话卡 2 秒），
        服务的可达性判断放到合成线程内部完成（失败即降级，零阻塞）。
        """
        if not self.is_enabled():
            return
        if self._server_ok is False:      # 已确认服务不可用，跳过（不反复尝试）
            return
        if not text or not text.strip():
            return

        ref_audio = ""
        folder = (character.tts_folder or "").strip()
        if folder and os.path.isdir(folder):
            candidates = [f for f in os.listdir(folder)
                          if f.lower().endswith((".wav", ".mp3", ".flac", ".ogg"))]
            if candidates:
                ref_audio = os.path.join(folder, candidates[0])

        url = self.config.get("tts", "sovits_url", default="http://127.0.0.1:9880")
        worker = _SynthWorker(url, ref_audio, text, self.log)
        worker.done.connect(self._on_synth_done)
        worker.failed.connect(self._on_synth_failed)
        worker.finished.connect(lambda w=worker: self._cleanup(w))
        self._workers.append(worker)
        worker.start()

    def _on_synth_failed(self, message: str) -> None:
        # 合成失败（多为服务不可达）：记住不可用，避免后续反复无效尝试
        self._server_ok = False
        self.log.info("GPT-SoVITS 合成失败（%s），TTS 降级为纯文本", message)
        if not self._warned:
            self._warned = True
            self.failed.emit(message)

    def _on_synth_done(self, path: str) -> None:
        self._server_ok = True
        self._warned = False   # 服务恢复后，下次再失败仍提示一次
        self.play_requested.emit(path)

    def _cleanup(self, worker) -> None:
        if worker in self._workers:
            self._workers.remove(worker)

    # ---------- 播放（主线程） ----------
    def _ensure_player(self):
        if self._player is None:
            from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
            self._player = QMediaPlayer()
            self._audio_output = QAudioOutput()
            self._player.setAudioOutput(self._audio_output)
            self._audio_output.setVolume(1.0)
            self._player.mediaStatusChanged.connect(self._on_media_status)

    def _on_media_status(self, status) -> None:
        # 播放自然结束后清理临时 wav（防止临时目录无限堆积）
        from PySide6.QtMultimedia import QMediaPlayer
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            path = self._current_temp_path
            self._current_temp_path = None
            _safe_remove(path)

    def play(self, path: str) -> None:
        """播放 wav 文件（connect 到 play_requested，保证在主线程执行）。"""
        # 打断上一段尚未结束的音频，并清理其临时文件
        prev = self._current_temp_path
        self._current_temp_path = None
        if prev and prev != path:
            _safe_remove(prev)
        try:
            self._ensure_player()
            self._current_temp_path = path
            self._player.setSource(QUrl.fromLocalFile(path))
            self._player.play()
        except Exception as exc:
            self.log.debug("TTS 播放失败（降级）: %s", exc)
            self._current_temp_path = None
            _safe_remove(path)

    def stop_all(self) -> None:
        """停止播放并清理临时 wav（切换角色 / 新回复前调用，避免串音）。"""
        try:
            if self._player is not None:
                self._player.stop()
        except Exception:
            pass
        path = self._current_temp_path
        self._current_temp_path = None
        _safe_remove(path)

    # 便捷绑定：把 play_requested 接到 play
    def bind(self) -> None:
        self.play_requested.connect(self.play)
