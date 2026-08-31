"""宠物悬浮窗。

- 无边框、半透明、置顶（可选）的悬浮窗口，默认 118x118。
- 宠物形象：不再内置默认动画。用户在 设置 -> 全局设置 中自行导入
  静态图片（png/jpg/jpeg/webp/bmp）或动图（gif），保存后立即生效；
  未导入形象时显示一个中性圆点占位，并提示去设置中导入。
- 鼠标拖拽移动；贴边自动隐藏（靠左边缘滑出，靠近边缘唤回）。
- 系统托盘常驻：打开对话 / 立即搭话 / 切换角色 / 全局设置 / 退出。
- 主动搭话引擎：QTimer 周期检查（间隔与空闲阈值读配置），满足条件发射
  initiative_triggered 信号，由主程序生成搭话文本后调用 show_speech() 展示。
"""
import os
import time

from PySide6.QtCore import QEasingCurve, QPoint, QPropertyAnimation, QRectF, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QCursor, QIcon, QMovie, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QLabel, QMenu, QSystemTrayIcon, QVBoxLayout, QWidget

from utils.logger import get_logger
from utils.theme import get_theme

# 支持导入的宠物形象格式
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif")

# 形象显示区域（窗口内）
PET_MAX_W, PET_MAX_H = 110, 110


class PlaceholderPet(QWidget):
    """未导入形象时的中性占位：圆点 + 问号，颜色随对话状态变化。"""

    # 各状态颜色：idle 待机 / listening 倾听 / speaking 说话 / sleeping 睡眠
    STATES = {
        "idle": "#5B8FF9",
        "listening": "#61C0BF",
        "speaking": "#F6BD60",
        "sleeping": "#8B98A8",
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(PET_MAX_W, PET_MAX_H)
        self.state = "idle"
        self.setToolTip("尚未导入宠物形象：请在 设置 -> 全局设置 中导入图片或 GIF")

    def set_state(self, state: str) -> None:
        if state in self.STATES:
            self.state = state
            self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = QColor(self.STATES.get(self.state, "#5B8FF9"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawEllipse(QRectF(28, 28, 54, 54))
        painter.setPen(QColor("#ffffff"))
        font = painter.font()
        font.setPointSize(20)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "?")
        painter.end()


class PetImageView(QLabel):
    """显示导入的宠物形象（静态图或 GIF 动图）。

    QLabel 不处理鼠标事件，按下/移动/双击会冒泡给 PetWindow，
    因此拖拽与"双击打开对话"逻辑无需改动。
    """


class _UsageWorker(QThread):
    """后台查询：今日 token 用量（本地统计）+ 账户余额（实时接口）。"""

    done = Signal(object)   # list[str] 或 None

    def __init__(self, config, logger=None):
        super().__init__()
        self.config = config
        self.log = logger

    def run(self):
        try:
            from core import usage_tracker
            lines = []
            prompt, completion = usage_tracker.today_usage()
            total = prompt + completion
            lines.append(f"今日已用：输入 {prompt:,} / 输出 {completion:,} tokens（合计 {total:,}）")
            lines.append("（用量为本地统计，余额为实时查询）")
            llm = self.config.get("llm", default={}) or {}
            backend = llm.get("backend", "openai")
            if backend == "openai":
                oai = llm.get("openai", {}) or {}
                balance = usage_tracker.fetch_balance(
                    oai.get("base_url", ""), oai.get("api_key", "")
                )
                text = usage_tracker.format_balance_text(balance)
                lines.append(f"余额：{text}" if text else "余额：暂无数据（查询失败或非 DeepSeek 账户）")
            else:
                lines.append("余额：该后端不支持余额查询（已用 token 仍会统计）")
            self.done.emit(lines)
        except Exception as exc:
            if self.log:
                self.log.debug("用量查询失败: %s", exc)
            self.done.emit(None)


class SpeechBubble(QWidget):
    """主动搭话气泡：显示在宠物上方，点击打开对话窗口，几秒后自动消失。"""

    clicked = Signal()

    def __init__(self, text: str, parent=None, always_on_top: bool = True):
        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
        if always_on_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        super().__init__(parent, flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        theme = get_theme()
        label = QLabel(text, self)
        label.setWordWrap(True)
        label.setMaximumWidth(240)
        label.setStyleSheet(
            "background:%s; color:%s; border-radius:10px; padding:10px; font-size:13px;"
            % (theme.card, theme.text)
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.addWidget(label)
        self._label = label

    def showEvent(self, event):
        super().showEvent(event)
        self.adjustSize()

    def mousePressEvent(self, event):
        self.clicked.emit()
        self.close()


class PetWindow(QWidget):
    """悬浮宠物主窗口。"""

    clicked = Signal()
    initiative_triggered = Signal()
    settings_requested = Signal()
    quit_requested = Signal()
    character_switch_requested = Signal(str)

    def __init__(self, config, logger=None):
        super().__init__()
        self.config = config
        self.log = logger or get_logger()

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle("Desktop Character Pet")
        self.setFixedSize(118, 118)
        self._topmost = None
        self._movie = None
        self._state = "idle"
        self._char_image = ""   # 当前角色形象路径（空 = 回退全局默认）

        # 形象容器：占位 / 图片 二选一显示
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        self._placeholder = PlaceholderPet(self)
        self._image_label = PetImageView(self)
        self._image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._placeholder, 0, Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._image_label, 0, Qt.AlignmentFlag.AlignCenter)
        self._image_label.hide()

        # 拖拽/贴边
        self._drag_offset = None
        self._hidden = False
        self._anim = QPropertyAnimation(self, b"pos", self)
        self._anim.setDuration(220)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        # 主动搭话
        self._last_activity = time.time()   # 用户最后活动时间
        self._last_trigger = 0.0            # 上次搭话触发时刻（0 = 从未触发）
        self._speech = None
        self._init_timer = QTimer(self)
        self._init_timer.timeout.connect(self._check_initiative)
        self._init_timer.start(20000)   # 每 20 秒检查一次（触发条件读配置）

        # 贴边检测
        self._edge_timer = QTimer(self)
        self._edge_timer.timeout.connect(self._check_edge)
        self._edge_timer.start(300)

        self._apply_window_config()
        self._load_pet_image()
        self._build_tray()

    # ---------- 窗口配置 ----------
    def _apply_window_config(self) -> None:
        opacity = float(self.config.get("window", "opacity", default=0.92))
        self.setWindowOpacity(max(0.2, min(1.0, opacity)))
        # 置顶开关（默认不置顶；setWindowFlag 会隐式隐藏窗口，需重新显示）
        on = bool(self.config.get("window", "always_on_top", default=False))
        if on != self._topmost:
            self._topmost = on
            self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, on)
            if self.isVisible():
                self.show()

    def apply_config(self) -> None:
        """配置变更后刷新窗口参数与宠物形象（由主程序调用）。"""
        self._apply_window_config()
        self._load_pet_image()

    def set_character_image(self, path: str) -> None:
        """设置当前角色形象（空则回退全局默认）。主程序在切换角色时调用。"""
        self._char_image = path or ""
        self._load_pet_image()

    # ---------- 宠物形象 ----------
    @staticmethod
    def _fit_size(width: int, height: int):
        """保持纵横比缩放到形象区域内的尺寸。"""
        if width <= 0 or height <= 0:
            return PET_MAX_W, PET_MAX_H
        ratio = min(PET_MAX_W / width, PET_MAX_H / height)
        return max(1, int(width * ratio)), max(1, int(height * ratio))

    def _stop_movie(self) -> None:
        if self._movie is not None:
            self._movie.stop()
            self._movie = None

    def _load_pet_image(self) -> None:
        """加载形象：优先当前角色形象，缺失/空则回退全局默认，最末显示占位。"""
        self._stop_movie()
        self._image_label.clear()
        self._image_label.hide()
        self._placeholder.show()

        path = (self._char_image or "").strip()
        # 形象完全跟角色走：无效（空/文件缺失）则显示占位
        if not (path and os.path.isfile(path)):
            path = ""
        if not path:
            return

        try:
            if path.lower().endswith(".gif"):
                movie = QMovie(path)
                if movie.isValid():
                    # 注意：不要在 start() 之前调用 jumpToFrame（PySide6 6.11 下
                    # 会让 QMovie 卡在 NotRunning）；用 QImageReader 同步取尺寸
                    from PySide6.QtGui import QImageReader
                    reader = QImageReader(path)
                    reader_size = reader.size()
                    w, h = self._fit_size(reader_size.width(), reader_size.height())
                    movie.setScaledSize(QSize(w, h))
                    self._image_label.setMovie(movie)
                    self._image_label.setFixedSize(w, h)
                    self._movie = movie
                    movie.start()
                    self._image_label.show()
                    self._placeholder.hide()
                    self.log.info("宠物形象（动图）已加载：%s", path)
                    return
            else:
                pixmap = QPixmap(path)
                if not pixmap.isNull():
                    w, h = self._fit_size(pixmap.width(), pixmap.height())
                    scaled = pixmap.scaled(
                        w, h,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                    self._image_label.setPixmap(scaled)
                    self._image_label.setFixedSize(w, h)
                    self._image_label.show()
                    self._placeholder.hide()
                    self.log.info("宠物形象（静态图）已加载：%s", path)
                    return
            self.log.warning("宠物形象无法解析：%s", path)
        except Exception as exc:
            self.log.warning("宠物形象加载失败（%s）: %s", path, exc)

    # ---------- 托盘 ----------
    def _make_icon(self) -> QIcon:
        pixmap = QPixmap(64, 64)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#5B8FF9"))
        painter.drawRoundedRect(2, 2, 60, 60, 18, 18)
        painter.setBrush(QColor("#ffffff"))
        painter.drawEllipse(QRectF(16, 22, 10, 12))
        painter.drawEllipse(QRectF(38, 22, 10, 12))
        painter.setBrush(QColor("#2f3542"))
        painter.drawEllipse(QRectF(19, 25, 4, 5))
        painter.drawEllipse(QRectF(41, 25, 4, 5))
        painter.setPen(QPen(QColor("#2f3542"), 3))
        painter.drawArc(QRectF(22, 34, 20, 12), 0, -180 * 16)
        painter.end()
        return QIcon(pixmap)

    def _build_tray(self) -> None:
        self._tray = QSystemTrayIcon(self._make_icon(), self)
        self._tray.setToolTip("Desktop Character Pet")

        menu = QMenu()
        act_open = QAction("打开对话", menu)
        act_open.triggered.connect(self.clicked.emit)
        act_init = QAction("立即搭话", menu)
        act_init.triggered.connect(self.trigger_initiative_now)
        # 用量与余额子菜单（弹出时后台查询）
        self._usage_menu = QMenu("用量与余额", menu)
        self._usage_menu.aboutToShow.connect(self._refresh_usage_menu)
        act_usage_hint = QAction("正在查询…", self._usage_menu)
        act_usage_hint.setEnabled(False)
        self._usage_menu.addAction(act_usage_hint)
        self._role_menu = QMenu("切换角色", menu)
        act_settings = QAction("全局设置", menu)
        act_settings.triggered.connect(self.settings_requested.emit)
        act_quit = QAction("退出", menu)
        act_quit.triggered.connect(self.quit_requested.emit)

        menu.addAction(act_open)
        menu.addAction(act_init)
        menu.addMenu(self._usage_menu)
        menu.addMenu(self._role_menu)
        menu.addSeparator()
        menu.addAction(act_settings)
        menu.addAction(act_quit)
        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._on_tray_activated)
        self._tray.show()

    def _on_tray_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                      QSystemTrayIcon.ActivationReason.DoubleClick):
            self.clicked.emit()

    # ---------- 用量与余额 ----------
    def _refresh_usage_menu(self) -> None:
        """子菜单弹出瞬间刷新（后台线程查询，结果回来后填充）。"""
        if getattr(self, "_usage_worker", None) is not None and self._usage_worker.isRunning():
            return     # 查询中，避免重复
        self._usage_menu.clear()
        loading = QAction("正在查询…", self._usage_menu)
        loading.setEnabled(False)
        self._usage_menu.addAction(loading)
        worker = _UsageWorker(self.config, self.log)
        worker.done.connect(self._on_usage_ready)
        worker.finished.connect(worker.deleteLater)
        self._usage_worker = worker
        worker.start()

    def _on_usage_ready(self, lines) -> None:
        self._usage_worker = None
        self._usage_menu.clear()
        if not lines:
            lines = ["暂无数据（查询失败）"]
        for line in lines:
            action = QAction(line, self._usage_menu)
            action.setEnabled(False)    # 只读展示
            self._usage_menu.addAction(action)

    def set_characters_menu(self, names: list, active: str) -> None:
        """重建托盘"切换角色"子菜单。"""
        self._role_menu.clear()
        for name in names:
            action = QAction(name, self._role_menu)
            action.setCheckable(True)
            action.setChecked(name == active)
            action.triggered.connect(lambda _checked=False, n=name: self.character_switch_requested.emit(n))
            self._role_menu.addAction(action)

    # ---------- 拖拽 ----------
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()

    def mouseReleaseEvent(self, event):
        self._drag_offset = None
        event.accept()

    def mouseDoubleClickEvent(self, event):
        self.clicked.emit()
        event.accept()

    # ---------- 贴边隐藏 ----------
    def _check_edge(self) -> None:
        if not bool(self.config.get("window", "auto_hide", default=True)):
            return
        screen = QApplication.primaryScreen().availableGeometry()
        if self.y() < 0 or self.y() > screen.bottom() - 10:
            return  # 顶部/底部贴边不处理，只处理左边缘
        if not self._hidden and self.x() <= 2:
            self._hidden = True
            self._animate_to(QPoint(8 - self.width(), self.y()))
        elif self._hidden:
            cursor = QCursor.pos()
            if cursor.x() <= 6:
                self._animate_to(QPoint(0, self.y()))
            elif self.x() >= 0 and cursor.x() > 40:
                self._hidden = False

    def _animate_to(self, pos: QPoint) -> None:
        self._anim.stop()
        self._anim.setStartValue(self.pos())
        self._anim.setEndValue(pos)
        self._anim.start()

    # ---------- 主动搭话 ----------
    def notify_activity(self) -> None:
        """用户有活动时调用（发送消息等）。"""
        self._last_activity = time.time()
        if self._state == "sleeping":
            self.set_state("idle")

    def _check_initiative(self) -> None:
        """定时主动搭话检查（每 20 秒）。

        触发条件（用户直觉语义）：
          1. 开启主动搭话；
          2. 距用户最后活动 >= idle_minutes（空闲阈值，即用户设置的等待时长）；
          3. 距上次搭话（含手动触发）>= interval_minutes（两次搭话最小间隔，
             首次触发不受限）；
          4. 当前没有正在展示的搭话气泡。
        """
        cfg = self.config.get("initiative", default={}) or {}
        if not cfg.get("enabled", True):
            return
        idle_limit = float(cfg.get("idle_minutes", 10)) * 60.0
        min_gap = float(cfg.get("interval_minutes", 15)) * 60.0
        now = time.time()
        if now - self._last_activity < idle_limit:
            return                       # 尚未空闲
        if now - self._last_trigger < min_gap:
            return                       # 距上次搭话太近
        if self._speech is not None:
            return                       # 已有气泡在展示
        self._last_trigger = now
        self.set_state("sleeping")
        self.initiative_triggered.emit()

    def trigger_initiative_now(self) -> None:
        """托盘"立即搭话"：手动强制触发一次（记录触发时刻，防止定时器紧跟着重复触发）。"""
        if self._speech is not None:
            self._close_speech()
        self._last_trigger = time.time()
        self.initiative_triggered.emit()

    def wake_for_initiative(self) -> None:
        """主动搭话生成后唤醒宠物。"""
        self.set_state("speaking")
        QTimer.singleShot(2500, lambda: self.set_state("idle"))

    def show_speech(self, text: str, duration_ms: int = 8000) -> None:
        """在宠物上方显示主动搭话气泡。"""
        if self._speech is not None:
            self._speech.close()
        bubble = SpeechBubble(text, always_on_top=bool(self._topmost))
        bubble.clicked.connect(self.clicked.emit)
        self._speech = bubble
        bubble.adjustSize()
        x = self.x() + (self.width() - bubble.width()) // 2
        y = self.y() - bubble.height() - 8
        if y < 0:
            y = self.y() + self.height() + 8
        bubble.move(x, y)
        bubble.show()
        QTimer.singleShot(duration_ms, self._close_speech)

    def _close_speech(self) -> None:
        if self._speech is not None:
            self._speech.close()
            self._speech = None

    # ---------- 状态转发（影响占位颜色；有形象图片时无视觉变化） ----------
    def set_state(self, state: str) -> None:
        self._state = state
        self._placeholder.set_state(state)

    def closeEvent(self, event):
        """窗口关闭时清理气泡与动图（托盘常驻，实际退出走托盘菜单）。"""
        self._close_speech()
        self._stop_movie()
        super().closeEvent(event)
