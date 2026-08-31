"""设置对话框：全局设置 + 角色管理/编辑器。

- 全局设置：LLM 后端（OpenAI 兼容 / Ollama）、API Key、Base URL、模型、
  上下文窗口、漂移阈值、主动搭话、TTS、ASR、窗口参数、后端连通性测试。
- 角色管理：新建 / 删除 / 自动生成（LLM 结构化输出）/ 手动编辑全部字段
  （system_prompt、traits、catchphrases、background、L2 核心记忆、
  worldbook、initiative_dialogues、独立后端、TTS 语音包文件夹、锚点重建）。
"""
import copy
import os

from PySide6.QtCore import QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QMovie, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSpinBox, QSplitter,
    QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from core.character_manager import Character, GenerationError
from core.drift_detector import DriftDetector
from core.llm_backends import LLMError, create_backend
from utils.logger import get_logger
from utils.theme import get_theme

BACKEND_NAMES = {"openai": "OpenAI 兼容 API（DeepSeek 等）", "ollama": "本地 Ollama"}
BACKEND_KEYS = {v: k for k, v in BACKEND_NAMES.items()}


class _TaskThread(QThread):
    """设置界面通用后台任务线程（支持阶段进度回调）。"""

    ok = Signal(object)
    failed = Signal(str)
    progress = Signal(str, int, str)   # (stage, percent, message)

    def __init__(self, fn, *args, wants_progress: bool = False):
        super().__init__()
        self._fn = fn
        self._args = args
        self._wants_progress = wants_progress

    def run(self):
        try:
            if self._wants_progress:
                result = self._fn(self._emit_progress, *self._args)
            else:
                result = self._fn(*self._args)
            self.ok.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))

    def _emit_progress(self, stage: str, percent: int, message: str) -> None:
        # Qt 信号跨线程发射是线程安全的，槽在接收者（UI）线程执行
        self.progress.emit(stage, percent, message)


class SettingsDialog(QDialog):
    """全局设置 + 角色管理对话框。"""

    config_saved = Signal()
    characters_changed = Signal()

    def __init__(self, config, manager, engine=None, logger=None, parent=None):
        super().__init__(parent)
        self.config = config
        self.manager = manager
        self.engine = engine
        self.log = logger or get_logger()
        self._theme = get_theme()
        self._current = None          # 当前编辑的角色（Character）
        self._tasks = []
        self._drift = engine._drift if engine is not None else DriftDetector(config, self.log)

        self.setWindowTitle("设置")
        self.resize(780, 640)

        layout = QVBoxLayout(self)
        self._tabs = QTabWidget(self)
        self._global_tab = QWidget()
        self._char_tab = QWidget()
        self._tabs.addTab(self._global_tab, "全局设置")
        self._tabs.addTab(self._char_tab, "角色管理")
        layout.addWidget(self._tabs)

        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.accept)
        bottom = QHBoxLayout()
        bottom.addStretch(1)
        bottom.addWidget(btn_close)
        layout.addLayout(bottom)

        self._build_global_tab()
        self._build_char_tab()
        self._load_global()
        self._refresh_char_list()

    # ==================================================================
    # 全局设置
    # ==================================================================
    def _build_global_tab(self) -> None:
        form = QFormLayout(self._global_tab)
        form.setContentsMargins(20, 16, 20, 8)

        self._backend_combo = QComboBox()
        self._backend_combo.addItem(BACKEND_NAMES["openai"], "openai")
        self._backend_combo.addItem(BACKEND_NAMES["ollama"], "ollama")

        self._api_key = QLineEdit()
        self._api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._base_url = QLineEdit()
        self._model = QLineEdit()
        self._ctx_window = QSpinBox()
        self._ctx_window.setRange(1024, 2097152)   # 上限 2M，覆盖 ds-v4 系列 1M 上下文
        self._ctx_window.setSingleStep(1024)
        self._ctx_window.setSuffix(" tokens")
        self._thinking_chat_check = QCheckBox("聊天使用思考模式（关闭：更快、更口语化、省 token）")
        self._thinking_generate_check = QCheckBox("生成/修改人设使用思考模式（提升结构化输出）")
        self._chat_style_combo = QComboBox()
        self._chat_style_combo.addItem("简洁日常（1~3 句）", "concise")
        self._chat_style_combo.addItem("丰富长文（像倾诉/写信）", "detailed")

        self._ollama_url = QLineEdit()
        self._ollama_model = QLineEdit()

        self._embedding_model = QLineEdit()
        self._drift_threshold = QDoubleSpinBox()
        self._drift_threshold.setRange(0.0, 1.0)
        self._drift_threshold.setSingleStep(0.05)
        self._drift_threshold.setDecimals(2)
        self._ooc_retries = QSpinBox()
        self._ooc_retries.setRange(0, 3)

        self._init_enabled = QCheckBox("启用主动搭话")
        self._init_interval = QSpinBox()
        self._init_interval.setRange(1, 240)
        self._init_interval.setSuffix(" 分钟")
        self._init_idle = QSpinBox()
        self._init_idle.setRange(1, 240)
        self._init_idle.setSuffix(" 分钟")

        self._tts_enabled = QCheckBox("启用 TTS（GPT-SoVITS）")
        self._sovits_url = QLineEdit()

        self._opacity = QDoubleSpinBox()
        self._opacity.setRange(0.2, 1.0)
        self._opacity.setSingleStep(0.05)
        self._opacity.setDecimals(2)
        self._auto_hide = QCheckBox("贴边自动隐藏")
        self._topmost_check = QCheckBox("窗口置顶（宠物与对话窗保持最前）")

        # 我的头像（聊天窗口用户头像）
        self._user_avatar_edit = QLineEdit()
        self._user_avatar_edit.setPlaceholderText("留空则显示圆形占位（名字首字符）")
        btn_user_browse = QPushButton("浏览…")
        btn_user_browse.clicked.connect(self._browse_user_avatar)
        btn_user_clear = QPushButton("清除")
        btn_user_clear.clicked.connect(self._clear_user_avatar)
        user_row = QHBoxLayout()
        user_row.addWidget(self._user_avatar_edit, 1)
        user_row.addWidget(btn_user_browse)
        user_row.addWidget(btn_user_clear)
        self._user_avatar_preview = QLabel()
        self._user_avatar_preview.setFixedSize(72, 72)
        self._user_avatar_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._user_avatar_preview.setStyleSheet(
            "border:1px dashed %s; border-radius:8px; background:%s;" % (self._theme.border, self._theme.card)
        )

        # 聊天背景（自定义聊天窗口背景图）
        self._chat_bg_edit = QLineEdit()
        self._chat_bg_edit.setPlaceholderText("留空则不显示背景")
        btn_bg_browse = QPushButton("浏览…")
        btn_bg_browse.clicked.connect(self._browse_chat_bg)
        btn_bg_clear = QPushButton("清除")
        btn_bg_clear.clicked.connect(self._clear_chat_bg)
        bg_row = QHBoxLayout()
        bg_row.addWidget(self._chat_bg_edit, 1)
        bg_row.addWidget(btn_bg_browse)
        bg_row.addWidget(btn_bg_clear)
        self._chat_bg_preview = QLabel()
        self._chat_bg_preview.setFixedSize(140, 76)
        self._chat_bg_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._chat_bg_preview.setStyleSheet(
            "border:1px dashed %s; border-radius:8px; background:%s;" % (self._theme.border, self._theme.card)
        )

        form.addRow("LLM 后端", self._backend_combo)
        form.addRow("API Key", self._api_key)
        form.addRow("Base URL", self._base_url)
        form.addRow("模型名", self._model)
        form.addRow("上下文窗口", self._ctx_window)
        form.addRow("回复风格", self._chat_style_combo)
        form.addRow("", self._thinking_chat_check)
        form.addRow("", self._thinking_generate_check)
        form.addRow("Ollama 地址", self._ollama_url)
        form.addRow("Ollama 模型", self._ollama_model)
        form.addRow("嵌入模型", self._embedding_model)
        form.addRow("漂移阈值", self._drift_threshold)
        form.addRow("OOC 最大重试", self._ooc_retries)
        form.addRow("", self._init_enabled)
        form.addRow("两次搭话最小间隔", self._init_interval)
        form.addRow("空闲多少分钟才搭话", self._init_idle)
        form.addRow("", self._tts_enabled)
        form.addRow("GPT-SoVITS 地址", self._sovits_url)
        form.addRow("窗口不透明度", self._opacity)
        form.addRow("", self._auto_hide)
        form.addRow("", self._topmost_check)
        form.addRow("我的头像（聊天窗口）", user_row)
        form.addRow("头像预览", self._user_avatar_preview)
        form.addRow("聊天背景（图片）", bg_row)
        form.addRow("背景预览", self._chat_bg_preview)

        btn_row = QHBoxLayout()
        btn_save = QPushButton("保存设置")
        btn_save.clicked.connect(self._save_global)
        btn_test = QPushButton("测试后端连接")
        btn_test.clicked.connect(self._test_backend)
        btn_row.addWidget(btn_save)
        btn_row.addWidget(btn_test)
        btn_row.addStretch(1)
        form.addRow(btn_row)

        note = QLabel("提示：Ollama 与 OpenAI 参数会分别保存，后端切换后自动使用对应配置。")
        note.setStyleSheet("color:%s; font-size:11px;" % self._theme.muted)
        form.addRow(note)

    def _load_global(self) -> None:
        llm = self.config.get("llm", default={}) or {}
        openai = llm.get("openai", {}) or {}
        ollama = llm.get("ollama", {}) or {}
        chat = self.config.get("chat", default={}) or {}
        init = self.config.get("initiative", default={}) or {}
        tts = self.config.get("tts", default={}) or {}
        win = self.config.get("window", default={}) or {}
        emb = self.config.get("embedding", default={}) or {}

        idx = self._backend_combo.findData(llm.get("backend", "openai"))
        self._backend_combo.setCurrentIndex(max(0, idx))
        self._api_key.setText(openai.get("api_key", ""))
        self._base_url.setText(openai.get("base_url", ""))
        self._model.setText(openai.get("model", ""))
        self._ctx_window.setValue(int(llm.get("context_window", 8192)))
        self._thinking_chat_check.setChecked(bool(llm.get("thinking_chat", False)))
        self._thinking_generate_check.setChecked(bool(llm.get("thinking_generate", True)))
        idx = self._chat_style_combo.findData(llm.get("chat_style", "concise"))
        self._chat_style_combo.setCurrentIndex(max(0, idx))
        self._ollama_url.setText(ollama.get("base_url", ""))
        self._ollama_model.setText(ollama.get("model", ""))
        self._embedding_model.setText(emb.get("model", ""))
        self._drift_threshold.setValue(float(chat.get("drift_threshold", 0.75)))
        self._ooc_retries.setValue(int(chat.get("max_ooc_retries", 1)))
        self._init_enabled.setChecked(bool(init.get("enabled", True)))
        self._init_interval.setValue(int(init.get("interval_minutes", 15)))
        self._init_idle.setValue(int(init.get("idle_minutes", 10)))
        self._tts_enabled.setChecked(bool(tts.get("enabled", True)))
        self._sovits_url.setText(tts.get("sovits_url", ""))
        self._opacity.setValue(float(win.get("opacity", 0.92)))
        self._auto_hide.setChecked(bool(win.get("auto_hide", True)))
        self._topmost_check.setChecked(bool(win.get("always_on_top", False)))
        user_avatar = win.get("user_avatar", "") or ""
        self._user_avatar_edit.setText(user_avatar)
        self._update_user_avatar_preview(user_avatar)
        chat_bg = win.get("chat_bg", "") or ""
        self._chat_bg_edit.setText(chat_bg)
        self._update_chat_bg_preview(chat_bg)

    def _save_global(self) -> None:
        self.config.set(self._backend_combo.currentData(), "llm", "backend")
        self.config.set(self._api_key.text().strip(), "llm", "openai", "api_key")
        self.config.set(self._base_url.text().strip(), "llm", "openai", "base_url")
        self.config.set(self._model.text().strip(), "llm", "openai", "model")
        self.config.set(self._ctx_window.value(), "llm", "context_window")
        self.config.set(self._thinking_chat_check.isChecked(), "llm", "thinking_chat")
        self.config.set(self._chat_style_combo.currentData(), "llm", "chat_style")
        self.config.set(self._thinking_generate_check.isChecked(), "llm", "thinking_generate")
        self.config.set(self._ollama_url.text().strip(), "llm", "ollama", "base_url")
        self.config.set(self._ollama_model.text().strip(), "llm", "ollama", "model")
        self.config.set(self._embedding_model.text().strip(), "embedding", "model")
        self.config.set(self._drift_threshold.value(), "chat", "drift_threshold")
        self.config.set(self._ooc_retries.value(), "chat", "max_ooc_retries")
        self.config.set(self._init_enabled.isChecked(), "initiative", "enabled")
        self.config.set(self._init_interval.value(), "initiative", "interval_minutes")
        self.config.set(self._init_idle.value(), "initiative", "idle_minutes")
        self.config.set(self._tts_enabled.isChecked(), "tts", "enabled")
        self.config.set(self._sovits_url.text().strip(), "tts", "sovits_url")
        self.config.set(self._opacity.value(), "window", "opacity")
        self.config.set(self._auto_hide.isChecked(), "window", "auto_hide")
        self.config.set(self._topmost_check.isChecked(), "window", "always_on_top")
        self.config.set(self._user_avatar_edit.text().strip(), "window", "user_avatar")
        self.config.set(self._chat_bg_edit.text().strip(), "window", "chat_bg")
        self.config_saved.emit()
        QMessageBox.information(self, "设置", "全局设置已保存。")

    def _test_backend(self) -> None:
        self._run_task(
            self._test_backend_job,
            on_ok=lambda text: QMessageBox.information(self, "连接测试", f"连接成功：{text[:80]}"),
            on_err=lambda msg: QMessageBox.warning(self, "连接测试", f"连接失败：{msg}"),
        )

    # ---------- 宠物形象 ----------
    # ---------- 我的头像 ----------
    def _browse_user_avatar(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择我的头像", self._user_avatar_edit.text() or "",
            "图片文件 (*.png *.jpg *.jpeg *.webp *.bmp)",
        )
        if path:
            self._user_avatar_edit.setText(os.path.normpath(path))
            self._update_user_avatar_preview(path)

    def _clear_user_avatar(self) -> None:
        self._user_avatar_edit.clear()
        self._user_avatar_preview.clear()

    # ---------- 聊天背景 ----------
    def _browse_chat_bg(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择聊天背景", self._chat_bg_edit.text() or "",
            "图片文件 (*.png *.jpg *.jpeg *.webp *.bmp)",
        )
        if path:
            self._chat_bg_edit.setText(os.path.normpath(path))
            self._update_chat_bg_preview(path)

    def _clear_chat_bg(self) -> None:
        self._chat_bg_edit.clear()
        self._chat_bg_preview.clear()

    def _update_chat_bg_preview(self, path: str) -> None:
        self._chat_bg_preview.clear()
        if not path or not os.path.isfile(path):
            return
        pixmap = QPixmap(path)
        if pixmap.isNull():
            self._chat_bg_preview.setText("无法加载")
            return
        scaled = pixmap.scaled(
            136, 72,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        x = (scaled.width() - 136) // 2
        y = (scaled.height() - 72) // 2
        self._chat_bg_preview.setPixmap(scaled.copy(x, y, 136, 72))

    def _update_user_avatar_preview(self, path: str) -> None:
        self._user_avatar_preview.clear()
        if not path or not os.path.isfile(path):
            return
        pixmap = QPixmap(path)
        if pixmap.isNull():
            self._user_avatar_preview.setText("无法加载")
            return
        scaled = pixmap.scaled(
            68, 68,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._user_avatar_preview.setPixmap(scaled)

    def _test_backend_job(self) -> str:
        if self.engine is not None:
            return self.engine.test_backend()
        return create_backend(self.config.backend_config(), self.log).chat(
            [{"role": "user", "content": "你好，请只回复四个字：连接成功"}],
            temperature=0.1,
            max_tokens=16,
        )

    # ==================================================================
    # 角色管理
    # ==================================================================
    def _build_char_tab(self) -> None:
        splitter = QSplitter(self._char_tab)
        splitter.setContentsMargins(8, 8, 8, 8)

        # ---- 左侧：角色列表 ----
        left = QWidget()
        left_layout = QVBoxLayout(left)
        self._char_list = QListWidget()
        self._char_list.currentItemChanged.connect(self._on_char_selected)
        btn_new = QPushButton("新建角色")
        btn_new.clicked.connect(self._new_char)
        btn_delete = QPushButton("删除角色")
        btn_delete.clicked.connect(self._delete_char)
        left_layout.addWidget(self._char_list, 1)
        left_layout.addWidget(btn_new)
        left_layout.addWidget(btn_delete)
        left.setMinimumWidth(170)
        splitter.addWidget(left)

        # ---- 右侧：编辑器 ----
        right = QScrollArea()
        right.setWidgetResizable(True)
        editor = QWidget()
        form = QFormLayout(editor)
        form.setContentsMargins(16, 12, 16, 12)

        self._name_edit = QLineEdit()
        self._work_edit = QLineEdit()
        self._btn_gen = QPushButton("自动生成（调用 LLM）")
        self._btn_gen.setToolTip("输入角色名与作品名后点击，自动生成完整角色设定（联网搜索 + 实时进度）")
        self._btn_gen.clicked.connect(self._auto_generate)
        self._btn_refine = QPushButton("对话修改（LLM）")
        self._btn_refine.setToolTip(
            "生成/编辑后的角色设定若需微调，用自然语言向 LLM 提出修改要求\n"
            "（例如：口头禅太生硬了，改成更傲娇的风格）"
        )
        self._btn_refine.clicked.connect(self._refine_character)
        gen_row = QHBoxLayout()
        gen_row.addWidget(self._btn_gen)
        gen_row.addWidget(self._btn_refine)
        gen_row.addStretch(1)
        form.addRow("角色名 *", self._name_edit)
        form.addRow("作品名", self._work_edit)
        form.addRow("", gen_row)

        # ---- 生成/修改进度面板（自动生成或对话修改时实时显示） ----
        gen_box = QGroupBox("生成 / 修改进度（联网搜索 + LLM 实时思考过程）")
        gen_layout = QVBoxLayout(gen_box)
        self._gen_bar = QProgressBar()
        self._gen_bar.setRange(0, 100)
        self._gen_bar.setValue(0)
        self._gen_bar.setFormat("%p%")
        self._gen_log = QPlainTextEdit()
        self._gen_log.setReadOnly(True)
        self._gen_log.setMaximumHeight(130)
        self._gen_log.setStyleSheet(
            "QPlainTextEdit { font-family: Consolas, 'Microsoft YaHei'; font-size: 11px;"
            " color:%s; background:%s; border:1px solid %s; border-radius:4px; }"
            % (self._theme.text, self._theme.card, self._theme.border)
        )
        gen_layout.addWidget(self._gen_bar)
        gen_layout.addWidget(self._gen_log)
        form.addRow(gen_box)
        # 思考流节流刷新：流式回调高频，用定时器合并刷新避免卡 UI
        self._gen_pending = None
        self._gen_timer = QTimer(self)
        self._gen_timer.setInterval(250)
        self._gen_timer.timeout.connect(self._flush_gen_thinking)

        self._traits_edit = QLineEdit()
        self._traits_edit.setPlaceholderText("逗号分隔，如：温柔, 傲娇, 毒舌")
        self._catchphrases_edit = QLineEdit()
        self._catchphrases_edit.setPlaceholderText("逗号分隔，如：真是拿你没办法, 哼！")
        self._background_edit = QPlainTextEdit()
        self._background_edit.setMaximumHeight(60)
        self._system_prompt_edit = QPlainTextEdit()
        self._system_prompt_edit.setMaximumHeight(140)
        form.addRow("性格标签", self._traits_edit)
        form.addRow("口头禅", self._catchphrases_edit)
        form.addRow("背景简介", self._background_edit)
        form.addRow("System Prompt（人设）", self._system_prompt_edit)

        # L2 核心记忆
        l2_box = QGroupBox("L2 核心记忆（永远置于 System Prompt 顶部，不可裁剪）")
        l2_layout = QVBoxLayout(l2_box)
        self._l2_list = QListWidget()
        l2_layout.addWidget(self._l2_list)
        l2_btns = QHBoxLayout()
        btn_l2_add = QPushButton("添加")
        btn_l2_add.clicked.connect(lambda: self._edit_list_item(self._l2_list, add=True))
        btn_l2_edit = QPushButton("编辑")
        btn_l2_edit.clicked.connect(lambda: self._edit_list_item(self._l2_list, add=False))
        btn_l2_del = QPushButton("删除")
        btn_l2_del.clicked.connect(lambda: self._remove_list_item(self._l2_list))
        l2_btns.addWidget(btn_l2_add)
        l2_btns.addWidget(btn_l2_edit)
        l2_btns.addWidget(btn_l2_del)
        l2_btns.addStretch(1)
        l2_layout.addLayout(l2_btns)
        form.addRow(l2_box)

        # Worldbook
        wb_box = QGroupBox("Worldbook 世界书（关键词触发注入世界观）")
        wb_layout = QVBoxLayout(wb_box)
        self._wb_table = QTableWidget(0, 2)
        self._wb_table.setHorizontalHeaderLabels(["关键词（逗号分隔）", "注入内容"])
        self._wb_table.horizontalHeader().setStretchLastSection(True)
        self._wb_table.setColumnWidth(0, 130)
        wb_layout.addWidget(self._wb_table)
        wb_btns = QHBoxLayout()
        btn_wb_add = QPushButton("添加条目")
        btn_wb_add.clicked.connect(self._add_wb_row)
        btn_wb_del = QPushButton("删除选中行")
        btn_wb_del.clicked.connect(self._remove_wb_row)
        wb_btns.addWidget(btn_wb_add)
        wb_btns.addWidget(btn_wb_del)
        wb_btns.addStretch(1)
        wb_layout.addLayout(wb_btns)
        form.addRow(wb_box)

        # 主动搭话台词
        init_box = QGroupBox("主动搭话台词库（为空时由 LLM 临时生成）")
        init_layout = QVBoxLayout(init_box)
        self._init_list = QListWidget()
        init_layout.addWidget(self._init_list)
        init_btns = QHBoxLayout()
        btn_init_add = QPushButton("添加")
        btn_init_add.clicked.connect(lambda: self._edit_list_item(self._init_list, add=True))
        btn_init_edit = QPushButton("编辑")
        btn_init_edit.clicked.connect(lambda: self._edit_list_item(self._init_list, add=False))
        btn_init_del = QPushButton("删除")
        btn_init_del.clicked.connect(lambda: self._remove_list_item(self._init_list))
        init_btns.addWidget(btn_init_add)
        init_btns.addWidget(btn_init_edit)
        init_btns.addWidget(btn_init_del)
        init_btns.addStretch(1)
        init_layout.addLayout(init_btns)
        form.addRow(init_box)

        # TTS 语音包
        tts_row = QHBoxLayout()
        self._tts_folder_edit = QLineEdit()
        btn_tts_browse = QPushButton("浏览…")
        btn_tts_browse.clicked.connect(self._browse_tts_folder)
        tts_row.addWidget(self._tts_folder_edit, 1)
        tts_row.addWidget(btn_tts_browse)
        form.addRow("TTS 语音包文件夹", tts_row)

        # 角色头像
        avatar_row = QHBoxLayout()
        self._char_avatar_edit = QLineEdit()
        self._char_avatar_edit.setPlaceholderText("留空则显示圆形占位（角色名首字符）")
        btn_avatar_browse = QPushButton("浏览…")
        btn_avatar_browse.clicked.connect(self._browse_char_avatar)
        btn_avatar_clear = QPushButton("清除")
        btn_avatar_clear.clicked.connect(self._clear_char_avatar)
        avatar_row.addWidget(self._char_avatar_edit, 1)
        avatar_row.addWidget(btn_avatar_browse)
        avatar_row.addWidget(btn_avatar_clear)
        self._char_avatar_preview = QLabel()
        self._char_avatar_preview.setFixedSize(72, 72)
        self._char_avatar_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._char_avatar_preview.setStyleSheet(
            "border:1px dashed %s; border-radius:8px; background:%s;" % (self._theme.border, self._theme.card)
        )
        form.addRow("角色头像", avatar_row)
        form.addRow("头像预览", self._char_avatar_preview)

        # 角色形象（宠物窗显示）
        petimg_row = QHBoxLayout()
        self._char_pet_image_edit = QLineEdit()
        self._char_pet_image_edit.setPlaceholderText("留空则显示占位圆点")
        btn_petimg_browse = QPushButton("浏览…")
        btn_petimg_browse.clicked.connect(self._browse_char_pet_image)
        btn_petimg_clear = QPushButton("清除")
        btn_petimg_clear.clicked.connect(self._clear_char_pet_image)
        petimg_row.addWidget(self._char_pet_image_edit, 1)
        petimg_row.addWidget(btn_petimg_browse)
        petimg_row.addWidget(btn_petimg_clear)
        self._char_pet_image_preview = QLabel()
        self._char_pet_image_preview.setFixedSize(72, 72)
        self._char_pet_image_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._char_pet_image_preview.setStyleSheet(
            "border:1px dashed %s; border-radius:8px; background:%s;" % (self._theme.border, self._theme.card)
        )
        form.addRow("角色形象（宠物显示）", petimg_row)
        form.addRow("形象预览", self._char_pet_image_preview)

        # 独立后端
        backend_box = QGroupBox("角色独立后端（可选，留空则跟随全局设置）")
        backend_layout = QFormLayout(backend_box)
        self._char_backend_combo = QComboBox()
        self._char_backend_combo.addItem("跟随全局设置", "")
        self._char_backend_combo.addItem(BACKEND_NAMES["openai"], "openai")
        self._char_backend_combo.addItem(BACKEND_NAMES["ollama"], "ollama")
        self._char_api_key = QLineEdit()
        self._char_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._char_base_url = QLineEdit()
        self._char_model = QLineEdit()
        self._char_ollama_url = QLineEdit()
        self._char_ollama_model = QLineEdit()
        backend_layout.addRow("后端", self._char_backend_combo)
        backend_layout.addRow("API Key", self._char_api_key)
        backend_layout.addRow("Base URL", self._char_base_url)
        backend_layout.addRow("模型名", self._char_model)
        backend_layout.addRow("Ollama 地址", self._char_ollama_url)
        backend_layout.addRow("Ollama 模型", self._char_ollama_model)
        form.addRow(backend_box)

        # 锚点
        self._anchor_label = QLabel("锚点：未生成")
        self._anchor_label.setStyleSheet("color:%s;" % self._theme.muted)
        btn_anchor = QPushButton("生成 / 更新锚点")
        btn_anchor.clicked.connect(self._regenerate_anchor)
        anchor_row = QHBoxLayout()
        anchor_row.addWidget(self._anchor_label, 1)
        anchor_row.addWidget(btn_anchor)
        form.addRow("角色锚点（漂移检测）", anchor_row)

        # ---- 开发者模式：情绪 / 好感数值手动调节 ----
        dev_box = QGroupBox("开发者模式：情绪 / 好感数值调节（保存角色后生效）")
        dev_layout = QVBoxLayout(dev_box)

        def make_spin(lo, hi):
            spin = QDoubleSpinBox()
            spin.setRange(lo, hi)
            spin.setSingleStep(0.05)
            spin.setDecimals(2)
            return spin

        # 情绪 PAD
        pad_row = QHBoxLayout()
        self._dev_pad_p = make_spin(-1.0, 1.0)
        self._dev_pad_a = make_spin(-1.0, 1.0)
        self._dev_pad_d = make_spin(-1.0, 1.0)
        for label, spin in (("愉悦 P", self._dev_pad_p), ("活跃 A", self._dev_pad_a), ("支配 D", self._dev_pad_d)):
            cell = QVBoxLayout()
            cell.setSpacing(0)
            lbl = QLabel(label)
            lbl.setStyleSheet("color:%s; font-size:10px;" % self._theme.muted)
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cell.addWidget(lbl)
            cell.addWidget(spin)
            pad_row.addLayout(cell)
        dev_layout.addLayout(pad_row)

        # 羁绊四维
        bond_row = QHBoxLayout()
        self._dev_bond_warmth = make_spin(0.0, 1.0)
        self._dev_bond_trust = make_spin(0.0, 1.0)
        self._dev_bond_formal = make_spin(0.0, 1.0)
        self._dev_bond_humor = make_spin(0.0, 1.0)
        for label, spin in (("温暖", self._dev_bond_warmth), ("信任", self._dev_bond_trust),
                            ("正式", self._dev_bond_formal), ("幽默", self._dev_bond_humor)):
            cell = QVBoxLayout()
            cell.setSpacing(0)
            lbl = QLabel(label)
            lbl.setStyleSheet("color:%s; font-size:10px;" % self._theme.muted)
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cell.addWidget(lbl)
            cell.addWidget(spin)
            bond_row.addLayout(cell)
        dev_layout.addLayout(bond_row)

        btn_reset_dev = QPushButton("重置为默认（P=0.5 A=0.3 D=0，羁绊全 0）")
        btn_reset_dev.clicked.connect(self._reset_dev_values)
        dev_layout.addWidget(btn_reset_dev)
        form.addRow(dev_box)

        self._btn_save_char = QPushButton("保存角色")
        self._apply_save_char_style(False)
        self._btn_save_char.clicked.connect(self._save_char)
        save_row = QHBoxLayout()
        save_row.addWidget(self._btn_save_char)
        save_row.addStretch(1)
        form.addRow(save_row)
        # 保存反馈状态提示（非模态，可明确感知保存成功）
        self._save_status = QLabel("")
        self._save_status.setStyleSheet("color:%s; font-size:11px;" % self._theme.success)
        form.addRow(self._save_status)

        right.setWidget(editor)
        splitter.addWidget(right)
        splitter.setSizes([180, 600])
        outer = QVBoxLayout(self._char_tab)
        outer.addWidget(splitter)

    # ---------- 角色列表 ----------
    def _refresh_char_list(self) -> None:
        names = self.manager.list_characters()
        current = self._name_edit.text().strip()
        self._char_list.blockSignals(True)
        self._char_list.clear()
        self._char_list.addItems(names)
        self._char_list.blockSignals(False)
        if current and current in names:
            items = self._char_list.findItems(current, Qt.MatchFlag.MatchExactly)
            if items:
                self._char_list.setCurrentItem(items[0])

    def _on_char_selected(self, current: QListWidgetItem, _previous) -> None:
        if current is None:
            return
        try:
            char = self.manager.load(current.text())
        except Exception as exc:
            QMessageBox.warning(self, "角色", f"加载失败：{exc}")
            return
        self._current = char
        self._load_char_to_form(char)

    def _load_char_to_form(self, char: Character) -> None:
        self._name_edit.setText(char.name)
        self._work_edit.setText(char.work)
        self._traits_edit.setText(", ".join(char.traits))
        self._catchphrases_edit.setText(", ".join(char.catchphrases))
        self._background_edit.setPlainText(char.background)
        self._system_prompt_edit.setPlainText(char.system_prompt)

        self._l2_list.clear()
        self._l2_list.addItems(char.l2_core_memories)
        self._init_list.clear()
        self._init_list.addItems(char.initiative_dialogues)

        self._wb_table.setRowCount(0)
        for entry in char.worldbook:
            self._add_wb_row(",".join(entry.get("keywords", [])), entry.get("content", ""))

        self._tts_folder_edit.setText(char.tts_folder or "")
        char_avatar = char.avatar or ""
        self._char_avatar_edit.setText(char_avatar)
        self._update_char_avatar_preview(char_avatar)
        char_pet_image = char.pet_image or ""
        self._char_pet_image_edit.setText(char_pet_image)
        self._update_char_pet_image_preview(char_pet_image)

        backend = char.backend or {}
        idx = self._char_backend_combo.findData(backend.get("backend", ""))
        self._char_backend_combo.setCurrentIndex(max(0, idx))
        oai = (backend.get("openai") or {}) if isinstance(backend, dict) else {}
        oll = (backend.get("ollama") or {}) if isinstance(backend, dict) else {}
        self._char_api_key.setText(oai.get("api_key", ""))
        self._char_base_url.setText(oai.get("base_url", ""))
        self._char_model.setText(oai.get("model", ""))
        self._char_ollama_url.setText(oll.get("base_url", ""))
        self._char_ollama_model.setText(oll.get("model", ""))

        dim = len(char.anchor_vector) if char.anchor_vector else 0
        self._anchor_label.setText(f"锚点：{'已生成（dim=%d）' % dim if dim else '未生成'}")

        # 开发者模式：情绪/羁绊数值
        pad = list(char.emotion.get("pad") or [0.5, 0.3, 0.0])
        while len(pad) < 3:
            pad.append(0.0)
        self._dev_pad_p.setValue(float(pad[0]))
        self._dev_pad_a.setValue(float(pad[1]))
        self._dev_pad_d.setValue(float(pad[2]))
        self._dev_bond_warmth.setValue(float(char.bond.get("warmth", 0.0)))
        self._dev_bond_trust.setValue(float(char.bond.get("trust", 0.0)))
        self._dev_bond_formal.setValue(float(char.bond.get("formality", 0.0)))
        self._dev_bond_humor.setValue(float(char.bond.get("humor", 0.0)))

    def _reset_dev_values(self) -> None:
        """开发者模式：重置情绪/羁绊为默认值。"""
        self._dev_pad_p.setValue(0.5)
        self._dev_pad_a.setValue(0.3)
        self._dev_pad_d.setValue(0.0)
        self._dev_bond_warmth.setValue(0.0)
        self._dev_bond_trust.setValue(0.0)
        self._dev_bond_formal.setValue(0.0)
        self._dev_bond_humor.setValue(0.0)

    # ---------- 表单 -> 角色 ----------
    def _collect_char_from_form(self) -> Character:
        name = self._name_edit.text().strip()
        if not name:
            raise ValueError("角色名不能为空。")
        base = self._current if (self._current and self._current.name == name) else Character()
        base.name = name
        base.work = self._work_edit.text().strip()
        base.traits = [t.strip() for t in self._traits_edit.text().split(",") if t.strip()]
        base.catchphrases = [c.strip() for c in self._catchphrases_edit.text().split(",") if c.strip()]
        base.background = self._background_edit.toPlainText().strip()
        base.system_prompt = self._system_prompt_edit.toPlainText().strip()
        base.l2_core_memories = [self._l2_list.item(i).text() for i in range(self._l2_list.count())]
        base.initiative_dialogues = [self._init_list.item(i).text() for i in range(self._init_list.count())]

        worldbook = []
        for row in range(self._wb_table.rowCount()):
            kw_item = self._wb_table.item(row, 0)
            ct_item = self._wb_table.item(row, 1)
            keywords = [k.strip() for k in (kw_item.text() if kw_item else "").split(",") if k.strip()]
            content = (ct_item.text() if ct_item else "").strip()
            if content:
                worldbook.append({"keywords": keywords, "content": content})
        base.worldbook = worldbook

        base.tts_folder = self._tts_folder_edit.text().strip()
        base.avatar = self._char_avatar_edit.text().strip()
        base.pet_image = self._char_pet_image_edit.text().strip()
        base.backend = self._collect_char_backend()
        # 开发者模式：情绪/羁绊数值写回
        base.emotion["pad"] = [
            round(self._dev_pad_p.value(), 4),
            round(self._dev_pad_a.value(), 4),
            round(self._dev_pad_d.value(), 4),
        ]
        base.bond["warmth"] = round(self._dev_bond_warmth.value(), 4)
        base.bond["trust"] = round(self._dev_bond_trust.value(), 4)
        base.bond["formality"] = round(self._dev_bond_formal.value(), 4)
        base.bond["humor"] = round(self._dev_bond_humor.value(), 4)
        return base

    def _collect_char_backend(self):
        kind = self._char_backend_combo.currentData()
        if not kind:
            return None
        backend = {"backend": kind}
        if kind == "openai":
            backend["openai"] = {
                "api_key": self._char_api_key.text().strip(),
                "base_url": self._char_base_url.text().strip(),
                "model": self._char_model.text().strip(),
            }
        else:
            backend["ollama"] = {
                "base_url": self._char_ollama_url.text().strip(),
                "model": self._char_ollama_model.text().strip(),
            }
        return backend

    # ---------- 角色操作 ----------
    def _apply_save_char_style(self, saved: bool) -> None:
        """保存按钮样式：默认主题蓝；保存成功时短暂变绿显示"已保存"。"""
        if saved:
            self._btn_save_char.setStyleSheet(
                "QPushButton { background:%s; color:white; border:none;"
                " border-radius:8px; padding:8px 16px; }" % self._theme.success
            )
        else:
            self._btn_save_char.setStyleSheet(
                "QPushButton { background:%s; color:white; border:none;"
                " border-radius:8px; padding:8px 16px; }"
                "QPushButton:hover { background:%s; }" % (self._theme.accent, self._theme.accent_hover)
            )

    def _show_save_feedback(self, name: str) -> None:
        """保存成功反馈：按钮短暂变绿显示"已保存"2 秒后恢复 + 底部状态提示。"""
        self._btn_save_char.setText("已保存")
        self._apply_save_char_style(True)
        self._save_status.setText(f"角色「{name}」已保存。")
        QTimer.singleShot(2000, self._restore_save_feedback)

    def _restore_save_feedback(self) -> None:
        self._btn_save_char.setText("保存角色")
        self._apply_save_char_style(False)
        self._save_status.setText("")

    def _save_char(self) -> None:
        try:
            char = self._collect_char_from_form()
        except ValueError as exc:
            QMessageBox.warning(self, "保存角色", str(exc))
            return
        self.manager.save(char)
        self._current = char
        self._refresh_char_list()
        self.characters_changed.emit()
        self._show_save_feedback(char.name)

    def _new_char(self) -> None:
        self._current = None
        self._name_edit.clear()
        self._work_edit.clear()
        self._traits_edit.clear()
        self._catchphrases_edit.clear()
        self._background_edit.clear()
        self._system_prompt_edit.clear()
        self._l2_list.clear()
        self._init_list.clear()
        self._wb_table.setRowCount(0)
        self._tts_folder_edit.clear()
        self._char_avatar_edit.clear()
        self._char_avatar_preview.clear()
        self._char_pet_image_edit.clear()
        self._char_pet_image_preview.clear()
        self._char_backend_combo.setCurrentIndex(0)
        self._anchor_label.setText("锚点：未生成")
        self._reset_dev_values()
        self._char_list.clearSelection()

    def _delete_char(self) -> None:
        item = self._char_list.currentItem()
        if item is None:
            return
        name = item.text()
        if QMessageBox.question(self, "删除角色", f"确定删除角色「{name}」吗？此操作不可恢复。") != QMessageBox.StandardButton.Yes:
            return
        self.manager.delete(name)
        self._new_char()
        self._refresh_char_list()
        self.characters_changed.emit()

    def _auto_generate(self) -> None:
        name = self._name_edit.text().strip()
        work = self._work_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "自动生成", "请先填写角色名。")
            return
        # 清空进度面板并禁用按钮
        self._gen_bar.setValue(0)
        self._gen_log.clear()
        self._btn_gen.setEnabled(False)
        self._btn_refine.setEnabled(False)
        self._btn_gen.setText("正在生成…")
        thread = _TaskThread(self._auto_generate_job, name, work, wants_progress=True)
        thread.progress.connect(self._on_gen_progress)
        thread.ok.connect(self._on_generated)
        thread.failed.connect(self._on_gen_failed)
        thread.finished.connect(lambda t=thread: self._cleanup_task(t))
        self._tasks.append(thread)
        self._gen_thread = thread
        thread.start()

    def _auto_generate_job(self, progress_cb, name: str, work: str) -> Character:
        return self.manager.auto_generate(name, work, progress_cb=progress_cb)

    # ---------- 对话式修改 ----------
    def _refine_character(self) -> None:
        name = self._name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "对话修改", "请先填写角色名。")
            return
        instruction, ok = QInputDialog.getMultiLineText(
            self, "对话修改角色设定",
            "描述你想修改的地方（例如：口头禅太生硬了，改成更傲娇的风格；"
            "背景里补充一个设定；L2 记忆里加一条……）：",
            "",
        )
        if not ok or not instruction.strip():
            return
        try:
            char = self._collect_char_from_form()
        except ValueError as exc:
            QMessageBox.warning(self, "对话修改", str(exc))
            return
        # 清空进度面板并禁用按钮
        self._gen_bar.setValue(0)
        self._gen_log.clear()
        self._btn_gen.setEnabled(False)
        self._btn_refine.setEnabled(False)
        self._btn_refine.setText("正在修改…")
        thread = _TaskThread(self._refine_job, char, instruction, wants_progress=True)
        thread.progress.connect(self._on_gen_progress)
        thread.ok.connect(self._on_refined)
        thread.failed.connect(self._on_gen_failed)
        thread.finished.connect(lambda t=thread: self._cleanup_task(t))
        self._tasks.append(thread)
        self._gen_thread = thread
        thread.start()

    def _refine_job(self, progress_cb, char: Character, instruction: str) -> Character:
        return self.manager.refine_character(char, instruction, progress_cb=progress_cb)

    def _on_refined(self, char: Character) -> None:
        self._gen_timer.stop()
        self._flush_gen_thinking()
        self._btn_gen.setEnabled(True)
        self._btn_refine.setEnabled(True)
        self._btn_refine.setText("对话修改（LLM）")
        self._gen_bar.setValue(100)
        self._current = char
        self._load_char_to_form(char)
        self._refresh_char_list()
        self.characters_changed.emit()
        # 内容已修改，锚点已被清除（manager 中处理）；提示用户可重新生成
        self._anchor_label.setText("锚点：已清除（内容已修改，可在需要时点击右侧按钮重新生成）")
        QMessageBox.information(
            self, "对话修改",
            f"角色「{char.name}」已按你的要求修改并保存。\n"
            "旧的角色锚点已清除（漂移检测会按新人设重建）。",
        )

    # ---------- 生成进度 ----------
    def _on_gen_progress(self, stage: str, percent: int, message: str) -> None:
        if stage == "thinking":
            # 流式思考文本：节流缓存，定时器统一刷新
            if percent:
                self._gen_bar.setValue(percent)
            self._gen_pending = message
            self._gen_timer.start()
        elif stage == "error":
            self._gen_timer.stop()
            self._gen_log.appendPlainText(f"[失败] {message}")
        else:
            self._gen_timer.stop()
            self._flush_gen_thinking()
            if percent is not None:
                self._gen_bar.setValue(percent)
            if message:
                self._gen_log.appendPlainText(f"[{percent}%] {message}")
            self._gen_log.verticalScrollBar().setValue(self._gen_log.verticalScrollBar().maximum())

    def _flush_gen_thinking(self) -> None:
        if self._gen_pending is None:
            return
        self._gen_log.setPlainText(f"—— LLM 实时思考过程 ——\n{self._gen_pending}")
        self._gen_log.verticalScrollBar().setValue(self._gen_log.verticalScrollBar().maximum())
        self._gen_pending = None

    def _on_gen_failed(self, message: str) -> None:
        self._gen_timer.stop()
        self._flush_gen_thinking()
        self._btn_gen.setEnabled(True)
        self._btn_gen.setText("自动生成（调用 LLM）")
        self._btn_refine.setEnabled(True)
        self._btn_refine.setText("对话修改（LLM）")
        self._gen_log.appendPlainText(f"[失败] {message}")
        QMessageBox.warning(self, "生成/修改", f"失败：{message}")

    def _on_generated(self, char: Character) -> None:
        self._gen_timer.stop()
        self._flush_gen_thinking()
        self._btn_gen.setEnabled(True)
        self._btn_gen.setText("自动生成（调用 LLM）")
        self._btn_refine.setEnabled(True)
        self._gen_bar.setValue(100)
        self._current = char
        self._load_char_to_form(char)
        self._refresh_char_list()
        self.characters_changed.emit()
        # 锚点生成放进后台线程（嵌入模型可能首次加载/下载，避免卡住 UI 线程）
        self._run_task(
            lambda c=char: self._drift.build_anchor(self._build_anchor_text(c)),
            on_ok=lambda anchor: self._on_anchor_done(char, anchor, silent=True),
            on_err=lambda _msg: None,
        )
        QMessageBox.information(
            self, "自动生成",
            f"角色「{char.name}」生成成功！\n已在设置中打开，可继续手动微调后保存。",
        )

    def _build_anchor_text(self, char: Character) -> str:
        # 与 ChatEngine.anchor_text_for 保持一致（人设 + 口头禅 + 主动台词，判别力更佳）
        from core.chat_engine import ChatEngine
        return ChatEngine.anchor_text_for(char)

    def _regenerate_anchor(self) -> None:
        try:
            char = self._collect_char_from_form()
        except ValueError as exc:
            QMessageBox.warning(self, "锚点", str(exc))
            return
        self._run_task(
            lambda: self._drift.build_anchor(self._build_anchor_text(char)),
            on_ok=lambda anchor: self._on_anchor_done(char, anchor),
            on_err=lambda msg: QMessageBox.warning(self, "锚点", f"生成失败：{msg}"),
        )

    def _on_anchor_done(self, char: Character, anchor, silent: bool = False) -> None:
        if anchor:
            char.anchor_vector = anchor
            self.manager.save(char)
            self._current = char
            self._anchor_label.setText(f"锚点：已生成（dim={len(anchor)}）")
            if not silent:
                QMessageBox.information(self, "锚点", "角色锚点已生成并保存。")
        elif not silent:
            QMessageBox.warning(
                self, "锚点",
                "锚点生成失败：嵌入模型不可用（需安装 sentence-transformers）。\n"
                "漂移检测将自动降级为不检测，不影响对话。",
            )

    # ---------- 列表/表格编辑 ----------
    def _edit_list_item(self, widget: QListWidget, add: bool) -> None:
        current_text = widget.currentItem().text() if widget.currentItem() else ""
        text, ok = QInputDialog.getMultiLineText(self, "编辑内容", "内容：", current_text)
        if not ok or not text.strip():
            return
        if add:
            widget.addItem(text.strip())
        else:
            if widget.currentItem():
                widget.currentItem().setText(text.strip())
            else:
                widget.addItem(text.strip())

    def _remove_list_item(self, widget: QListWidget) -> None:
        row = widget.currentRow()
        if row >= 0:
            widget.takeItem(row)

    def _add_wb_row(self, keywords: str = "", content: str = "") -> None:
        row = self._wb_table.rowCount()
        self._wb_table.insertRow(row)
        self._wb_table.setItem(row, 0, QTableWidgetItem(keywords))
        self._wb_table.setItem(row, 1, QTableWidgetItem(content))

    def _remove_wb_row(self) -> None:
        row = self._wb_table.currentRow()
        if row >= 0:
            self._wb_table.removeRow(row)

    def _browse_tts_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择 GPT-SoVITS 语音包文件夹", self._tts_folder_edit.text() or "")
        if folder:
            self._tts_folder_edit.setText(os.path.normpath(folder))

    # ---------- 角色头像 ----------
    def _browse_char_avatar(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择角色头像", self._char_avatar_edit.text() or "",
            "图片文件 (*.png *.jpg *.jpeg *.webp *.bmp)",
        )
        if path:
            self._char_avatar_edit.setText(os.path.normpath(path))
            self._update_char_avatar_preview(path)

    def _clear_char_avatar(self) -> None:
        self._char_avatar_edit.clear()
        self._char_avatar_preview.clear()

    def _update_char_avatar_preview(self, path: str) -> None:
        self._char_avatar_preview.clear()
        if not path or not os.path.isfile(path):
            return
        pixmap = QPixmap(path)
        if pixmap.isNull():
            self._char_avatar_preview.setText("无法加载")
            return
        scaled = pixmap.scaled(
            68, 68,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._char_avatar_preview.setPixmap(scaled)

    # ---------- 角色形象（宠物显示） ----------
    def _browse_char_pet_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择角色形象", self._char_pet_image_edit.text() or "",
            "图片文件 (*.png *.jpg *.jpeg *.webp *.bmp *.gif)",
        )
        if path:
            self._char_pet_image_edit.setText(os.path.normpath(path))
            self._update_char_pet_image_preview(path)

    def _clear_char_pet_image(self) -> None:
        self._char_pet_image_edit.clear()
        self._char_pet_image_preview.clear()

    def _update_char_pet_image_preview(self, path: str) -> None:
        self._char_pet_image_preview.clear()
        if not path or not os.path.isfile(path):
            return
        pixmap = QPixmap(path)
        if pixmap.isNull():
            self._char_pet_image_preview.setText("无法加载")
            return
        scaled = pixmap.scaled(
            68, 68,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._char_pet_image_preview.setPixmap(scaled)

    # ---------- 后台任务 ----------
    def _run_task(self, fn, on_ok=None, on_err=None) -> None:
        thread = _TaskThread(fn)
        if on_ok:
            thread.ok.connect(on_ok)
        if on_err:
            thread.failed.connect(on_err)
        thread.finished.connect(lambda t=thread: self._cleanup_task(t))
        self._tasks.append(thread)
        thread.start()

    def _cleanup_task(self, thread: _TaskThread) -> None:
        if thread in self._tasks:
            self._tasks.remove(thread)

    # ---------- 对外 ----------
    def open_character_tab(self) -> None:
        self._tabs.setCurrentIndex(1)
        self.show()
        self.raise_()
        self.activateWindow()
