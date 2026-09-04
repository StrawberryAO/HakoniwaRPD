"""Desktop Character Pet —— 智能角色扮演桌面宠物（程序入口）。

启动流程：
    1. 初始化日志与全局配置
    2. 初始化角色管理器、对话引擎
    3. 创建悬浮宠物窗、对话气泡窗、设置对话框
    4. 连接全部信号并进入事件循环

运行：python main.py
"""
import os
import sys

from PySide6.QtCore import QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication, QDialog, QLabel, QMessageBox, QPlainTextEdit, QPushButton, QVBoxLayout,
)

from core.chat_engine import ChatEngine
from core.character_manager import CharacterManager
from core import dependency_check
from tts.tts_adapter import TTSAdapter
from ui.chat_bubble import ChatBubble
from ui.pet_window import PetWindow
from ui.settings_dialog import SettingsDialog
from utils.config import Config
from utils.logger import setup_logger
from utils.theme import apply_theme


class _InstallWorker(QThread):
    """后台安装可选依赖线程。"""

    progress = Signal(str)
    done = Signal(bool)   # True=成功

    def __init__(self, target_dir=None):
        super().__init__()
        self._target = target_dir

    def run(self):
        ok = dependency_check.install_missing(on_output=self.progress.emit, target_dir=self._target) == 0
        self.done.emit(ok)


class _InstallDialog(QDialog):
    """依赖安装进度对话框（实时滚动日志）。"""

    def __init__(self, parent=None, target_dir=None):
        super().__init__(parent)
        self._target = target_dir
        self.setWindowTitle("正在安装可选依赖")
        self.resize(520, 360)
        layout = QVBoxLayout(self)
        note = "到程序目录 _deps 文件夹" if target_dir else "到当前 Python 环境"
        self._status = QLabel(f"正在通过国内镜像源安装 {note}（torch CPU 版 + chromadb + sentence-transformers）…")
        self._status.setWordWrap(True)
        layout.addWidget(self._status)
        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        layout.addWidget(self._log, 1)
        self._btn = QPushButton("请稍候，安装中…")
        self._btn.setEnabled(False)
        layout.addWidget(self._btn)
        self._worker = None

    def start(self):
        self._worker = _InstallWorker(target_dir=self._target)
        self._worker.progress.connect(self._append)
        self._worker.done.connect(self._on_done)
        self._worker.start()

    def _append(self, line: str) -> None:
        self._log.appendPlainText(line)

    def _on_done(self, ok: bool) -> None:
        self._btn.setEnabled(True)
        if ok:
            self._status.setText("安装完成！重启应用后 L1 长期记忆与角色漂移检测即可生效。")
            self._btn.setText("关闭")
            self._btn.clicked.connect(self.accept)
        else:
            self._status.setText("安装失败，请手动执行：pip install chromadb sentence-transformers")
            self._btn.setText("关闭")
            self._btn.clicked.connect(self.accept)


def _check_optional_dependencies(app) -> None:
    """首次启动：检测 L1 记忆 / 漂移检测依赖，缺失时询问并自动安装。

    - 源码运行：安装到当前 Python 环境；
    - exe 运行：先把 exe 旁 _deps 目录加入 sys.path（若已存在则直接可用），
      仍缺失时询问并安装到 exe 旁 _deps 目录（不依赖用户是否装有 Python）。
    """
    dependency_check.ensure_deps_on_path()
    missing = dependency_check.missing_packages()
    if not missing:
        return
    names = "、".join(missing)
    target = dependency_check.deps_dir()
    where = "程序目录下的 _deps 文件夹（不影响你的系统 Python）" if target else "当前 Python 环境"
    size_text = dependency_check.size_estimate_text(missing)
    ret = QMessageBox.question(
        None, "检测到可选依赖缺失",
        f"检测到 L1 长期记忆与角色漂移检测所需的依赖未安装：\n{names}\n\n"
        f"{size_text}\n"
        f"是否现在通过国内镜像源自动安装到：{where}？\n"
        "选择「否」将跳过，相关功能自动降级，之后也可手动安装。",
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.Yes,
    )
    if ret != QMessageBox.StandardButton.Yes:
        return
    dialog = _InstallDialog(target_dir=target)
    dialog.show()
    dialog.start()


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Desktop Character Pet")
    app.setQuitOnLastWindowClosed(False)
    # 主题适配：检测系统亮/暗主题，Fusion 风格 + QPalette（须在创建窗口前）
    apply_theme(app)

    logger = setup_logger()
    config = Config("config.json")

    # 可选依赖检测（L1 记忆 / 漂移检测）：缺失时询问并自动安装
    _check_optional_dependencies(app)

    # HuggingFace 配置（config.json 的 hf 块）：
    # - endpoint：镜像端点，留空用官方源（国内网络建议 hf-mirror.com）
    # - cache_dir：模型缓存目录（项目内，避免写用户目录被权限限制）
    hf_endpoint = (config.get("hf", "endpoint", default="") or "").strip()
    if hf_endpoint:
        os.environ["HF_ENDPOINT"] = hf_endpoint
    hf_cache = (config.get("hf", "cache_dir", default="") or "").strip()
    if hf_cache:
        os.environ["HF_HUB_CACHE"] = os.path.abspath(hf_cache)

    manager = CharacterManager("characters", config, logger)
    engine = ChatEngine(config, manager, logger)

    pet = PetWindow(config, logger)
    bubble = ChatBubble(config, manager, logger)
    settings = SettingsDialog(config, manager, engine, logger)
    tts = TTSAdapter(config, logger)
    tts.bind()

    # ================= 回调（先定义，后连接信号） =================
    def on_reply(char_name: str, reply: str) -> None:
        """角色回复：显示、状态动画、TTS。"""
        bubble.append_message(char_name, reply, kind="char")
        pet.notify_activity()
        pet.set_state("speaking")
        QTimer.singleShot(2200, lambda: pet.set_state("idle"))
        tts.stop_all()   # 打断上一段未播完的语音，避免串音
        try:
            char = manager.load(char_name)
        except Exception as exc:   # 角色加载失败不再静默吞掉
            logger.warning("加载角色失败（%s）: %s", char_name, exc)
            return
        tts.speak(reply, char)

    def on_initiative_text(char_name: str, text: str) -> None:
        """主动搭话：气泡展示 + 记录 + 语音。"""
        pet.wake_for_initiative()
        pet.show_speech(text)
        bubble.append_message(char_name, text, kind="initiative")
        tts.stop_all()
        try:
            char = manager.load(char_name)
        except Exception as exc:
            logger.warning("加载角色失败（%s）: %s", char_name, exc)
            return
        tts.speak(text, char)

    def apply_character_image(name: str) -> None:
        """按角色加载宠物形象（空则回退全局默认）。"""
        try:
            char = manager.load(name)
            pet.set_character_image(char.pet_image)
        except Exception:
            pet.set_character_image("")

    def on_switch_character(name: str) -> None:
        engine.set_active_character(name)
        bubble.set_active(name)
        pet.set_characters_menu(manager.list_characters(), name)
        apply_character_image(name)
        tts.stop_all()   # 切换角色时停止旧角色语音

    def on_characters_changed() -> None:
        names = manager.list_characters()
        active = engine.active_character()
        bubble.refresh_characters(names, active)
        pet.set_characters_menu(names, active)
        apply_character_image(active)
        tts.stop_all()

    def on_config_saved() -> None:
        pet.apply_config()
        bubble.apply_config()
        logger.info("全局配置已保存")

    def show_bubble() -> None:
        bubble.show()
        bubble.raise_()
        bubble.activateWindow()

    # ================= 信号连接 =================
    # 对话
    bubble.send_requested.connect(engine.chat)
    engine.reply_ready.connect(on_reply)
    engine.reply_error.connect(bubble.show_error)
    engine.state_changed.connect(lambda state: bubble.set_thinking(state == "thinking"))
    engine.thinking_chunk.connect(bubble.set_thinking_text)
    engine.thinking_reset.connect(bubble.reset_thinking)
    engine.status_updated.connect(bubble.set_status)
    engine.initiative_text.connect(on_initiative_text)
    tts.failed.connect(lambda msg: bubble.show_error("语音播报不可用：" + msg))

    # 宠物窗
    pet.clicked.connect(show_bubble)
    pet.initiative_triggered.connect(engine.generate_initiative)
    pet.settings_requested.connect(lambda: settings.show())
    pet.quit_requested.connect(app.quit)
    pet.character_switch_requested.connect(on_switch_character)

    # 气泡窗
    bubble.open_settings.connect(lambda: settings.show())
    bubble.character_switched.connect(on_switch_character)

    # 设置窗
    settings.config_saved.connect(on_config_saved)
    settings.characters_changed.connect(on_characters_changed)

    # ================= 初始化 =================
    names = manager.list_characters()
    if names:
        engine.set_active_character(names[0])
    bubble.refresh_characters(names, engine.active_character())
    pet.set_characters_menu(names, engine.active_character())
    apply_character_image(engine.active_character())

    pet.show()
    show_bubble()
    logger.info("Desktop Character Pet 已启动")

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
