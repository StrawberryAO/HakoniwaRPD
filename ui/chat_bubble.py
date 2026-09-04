"""对话气泡窗口（QQ/微信风格单聊窗口）。

- 无边框普通窗口（非 Qt.Tool）：Alt+Tab / 任务栏可见，标题栏可拖拽。
- 标题栏：左侧当前角色名 + 设置（齿轮图标，QPainter 绘制），
  右侧角色下拉 + 最小化（—）/ 最大化（□）/ 关闭（✕，隐藏窗口）。
- 消息区：QScrollArea + 自绘气泡（_MessageItem）：
  用户消息右对齐绿气泡 + 右侧头像；角色消息左对齐白/深气泡 + 左侧头像；
  主动搭话/系统/错误/思考消息为居中样式。
- 头像：按配置与角色 JSON 的 avatar 字段加载，圆形裁剪（36x36），
  未配置或加载失败时显示灰色圆形占位（名字首字符）。
- 对外接口保持：send_requested(str) / open_settings / character_switched(str) /
  append_message(name, text, kind) / set_thinking(bool) / set_status(str) /
  clear_history() / refresh_characters(names, active) / set_active(name) /
  apply_config()。
"""
import math
import os
import re
import time
from datetime import datetime

from PySide6.QtCore import QEvent, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (
    QComboBox, QFrame, QHBoxLayout, QLabel, QProgressBar, QPushButton, QScrollArea,
    QStackedLayout, QTextEdit, QVBoxLayout, QWidget,
)

from core import history_store
from utils.logger import get_logger
from utils.theme import get_theme

# 边缘拖拽缩放热区宽度（像素，落在窗口 margin 附近；调大便于命中右边/下边）
_RESIZE_EDGE = 14

# 头像显示尺寸（圆形裁剪，任何图片不变形）
AVATAR_SIZE = 36


def _format_time(ts) -> str:
    """把时间戳格式化为友好时间：今天 HH:MM，昨天 HH:MM，更早 M月D日 HH:MM。"""
    try:
        dt = datetime.fromtimestamp(float(ts))
    except (TypeError, ValueError, OSError):
        return ""
    now = datetime.now()
    hm = dt.strftime("%H:%M")
    if dt.date() == now.date():
        return hm
    if (now.date() - dt.date()).days == 1:
        return f"昨天 {hm}"
    return f"{dt.month}月{dt.day}日 {hm}"


# ---------- 头像 -----------------
def _circular_avatar(pixmap: QPixmap, size: int = AVATAR_SIZE) -> QPixmap:
    """方形图 -> 居中裁剪并圆形裁剪。"""
    if pixmap.isNull():
        return QPixmap()
    scaled = pixmap.scaled(
        size, size,
        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation,
    )
    x = (scaled.width() - size) // 2
    y = (scaled.height() - size) // 2
    crop = scaled.copy(x, y, size, size)
    out = QPixmap(size, size)
    out.fill(Qt.GlobalColor.transparent)
    painter = QPainter(out)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    path.addEllipse(0, 0, size, size)
    painter.setClipPath(path)
    painter.drawPixmap(0, 0, crop)
    painter.end()
    return out


def _avatar_placeholder(text: str, size: int = AVATAR_SIZE) -> QPixmap:
    """灰色圆形占位 + 名字首字符（禁 emoji，取首字汉字/字母）。"""
    first = (text or "?").strip()[:1] or "?"
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#9aa0a6"))
    painter.drawEllipse(0, 0, size, size)
    painter.setPen(QColor("#ffffff"))
    font = painter.font()
    font.setPixelSize(16)
    font.setBold(True)
    painter.setFont(font)
    painter.drawText(QRectF(0, 0, size, size), Qt.AlignmentFlag.AlignCenter, first)
    painter.end()
    return pm


def _load_avatar_file(path: str) -> QPixmap:
    """按路径加载头像并圆形裁剪；失败返回空 QPixmap。"""
    if not path or not os.path.isfile(path):
        return QPixmap()
    try:
        pm = QPixmap(path)
        if pm.isNull():
            return QPixmap()
        return _circular_avatar(pm)
    except Exception:
        return QPixmap()


# ---------- 图标 ----------
def _make_gear_icon(color: str) -> QIcon:
    """用 QPainter 绘制齿轮图标（圆环 + 8 齿），不依赖任何 emoji/图片资源。"""
    size = 64
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(color))
    cx = cy = size / 2.0
    for i in range(8):
        ang = math.radians(i * 45.0)
        painter.save()
        painter.translate(cx + 24.0 * math.cos(ang), cy + 24.0 * math.sin(ang))
        painter.rotate(math.degrees(ang) + 90.0)
        painter.drawRoundedRect(-5.0, -5.5, 10.0, 11.0, 2.0, 2.0)
        painter.restore()
    painter.drawEllipse(QRectF(10.0, 10.0, 44.0, 44.0))
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
    painter.drawEllipse(QRectF(20.0, 20.0, 24.0, 24.0))
    painter.end()
    return QIcon(pm)


class _HeaderBar(QWidget):
    """可拖拽的标题栏。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._drag_offset = None

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.window().frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.window().move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()

    def mouseReleaseEvent(self, event):
        self._drag_offset = None
        event.accept()


class _MessageItem(QWidget):
    """一条消息：左/右对齐气泡（带头像）或居中样式，可选时间戳。"""

    def __init__(self, name: str, text: str, kind: str, theme, avatar: QPixmap,
                 ts=None, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 2, 0, 2)
        outer.setSpacing(1)
        self._center_label = None   # 居中样式（系统/错误/思考）的文本标签，供流式更新

        # 时间戳（居中灰色小字）
        if ts:
            time_label = QLabel(_format_time(ts), self)
            time_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            time_label.setStyleSheet(
                "color:%s; font-size:10px; background:transparent;" % theme.muted
            )
            outer.addWidget(time_label)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        if kind in ("user", "char", "initiative"):
            avatar_label = QLabel(self)
            avatar_label.setFixedSize(AVATAR_SIZE, AVATAR_SIZE)
            if avatar is not None and not avatar.isNull():
                avatar_label.setPixmap(avatar)
            else:
                avatar_label.setPixmap(_avatar_placeholder(name))
            bubble = QLabel(text, self)
            bubble.setWordWrap(True)
            bubble.setMaximumWidth(300)
            bubble.setToolTip(name)
            bubble.setTextFormat(Qt.TextFormat.PlainText)
            bubble.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            if kind == "user":
                bubble.setStyleSheet(
                    "QLabel { background:%s; color:%s; border-radius:10px;"
                    " padding:8px 12px; font-size:13px; }"
                    % (theme.user_bubble, theme.user_bubble_text)
                )
                row.addStretch(1)
                row.addWidget(bubble)
                row.addWidget(avatar_label)
            else:
                # char / initiative 均为角色气泡（左对齐 + 头像）
                bubble.setStyleSheet(
                    "QLabel { background:%s; color:%s; border-radius:10px;"
                    " padding:8px 12px; font-size:13px; }"
                    % (theme.char_bubble, theme.char_bubble_text)
                )
                row.addWidget(avatar_label)
                row.addWidget(bubble)
                row.addStretch(1)
        else:
            # 系统 / 错误 / 思考：居中单色样式
            color = theme.muted
            if kind == "error":
                color = theme.danger
            label = QLabel(text, self)
            label.setWordWrap(True)
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setMaximumWidth(380)
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setStyleSheet("color:%s; font-size:12px; background:transparent;" % color)
            self._center_label = label
            row.addStretch(1)
            row.addWidget(label)
            row.addStretch(1)
        outer.addLayout(row)

    def update_text(self, text: str) -> None:
        """更新居中样式的文本（用于思考流式输出）；非居中消息为 no-op。"""
        if self._center_label is not None:
            self._center_label.setText(text)


class ChatBubble(QWidget):
    """对话气泡窗口。"""

    send_requested = Signal(str)
    open_settings = Signal()
    character_switched = Signal(str)

    def __init__(self, config, manager, logger=None):
        super().__init__()
        self.config = config
        self.manager = manager
        self.log = logger or get_logger()
        self._theme = get_theme()

        # 普通无边框窗口（非 Tool）：Alt+Tab / 任务栏可见
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle("Desktop Pet")
        self.setMinimumSize(340, 420)   # 边缘拖拽缩放的最小尺寸
        # 恢复上次调整的窗口大小（无记录则用默认）
        saved_w = int(self.config.get("window", "chat_w", default=0) or 0)
        saved_h = int(self.config.get("window", "chat_h", default=0) or 0)
        if saved_w >= 340 and saved_h >= 420:
            self.resize(saved_w, saved_h)
        else:
            self.resize(430, 580)

        self._topmost = None
        # 边缘缩放状态
        self._resizing = None
        self._resize_start = None
        self._char_cache = {}        # 角色名 -> Character（头像缓存用）
        self._thinking = False
        self._thinking_item = None
        self._thinking_text = ""
        self._drag_offset = None
        self._loaded_role = None     # 当前已加载历史消息的角色

        self.setMouseTracking(True)  # 让边缘缩放光标在悬停时也能显示

        self._build_ui()
        # 给所有后代控件装事件过滤器：鼠标在子控件区域移动时也能更新边缘缩放光标
        # （无边框窗口的边缘热区可能落在子控件上，仅靠窗口自身 mouseMove 收不到事件）
        for w in self.findChildren(QWidget):
            w.installEventFilter(self)
        self.apply_config()

    # ---------- 界面 ----------
    def _build_ui(self) -> None:
        theme = self._theme
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        card = QFrame()
        card.setObjectName("card")
        card.setStyleSheet("QFrame#card { background:%s; border-radius:12px; }" % theme.card)
        root.addWidget(card)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(10, 6, 10, 10)
        layout.setSpacing(6)

        # ---------- 标题栏 ----------
        header = _HeaderBar(card)
        header.setFixedHeight(38)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(4, 0, 2, 0)
        header_layout.setSpacing(4)
        self._title_label = QLabel("选择角色", header)
        self._title_label.setStyleSheet("font-weight:bold; font-size:14px; color:%s;" % theme.text)
        self._btn_settings = QPushButton(header)
        self._btn_settings.setIcon(_make_gear_icon(theme.text))
        self._btn_settings.setIconSize(QSize(18, 18))
        self._btn_settings.setFixedSize(34, 28)
        self._btn_settings.setToolTip("设置")
        self._btn_settings.setStyleSheet(
            "QPushButton { border:none; background:transparent; border-radius:6px; }"
            "QPushButton:hover { background:%s; }" % theme.hover
        )
        self._btn_settings.clicked.connect(self.open_settings.emit)

        self._combo = QComboBox(header)
        self._combo.setMinimumWidth(110)
        self._combo.setPlaceholderText("选择角色")
        self._combo.setStyleSheet(
            "QComboBox { background:%s; color:%s; border:1px solid %s; border-radius:6px;"
            " padding:2px 8px; font-size:12px; }"
            "QComboBox QAbstractItemView { background:%s; color:%s;"
            " selection-background-color:%s; selection-color:#ffffff; border:1px solid %s; }"
            % (theme.card, theme.text, theme.border,
               theme.card, theme.text, theme.accent, theme.border)
        )
        self._combo.currentTextChanged.connect(self._on_combo_changed)

        win_btn_base = (
            "QPushButton { border:none; background:transparent; color:%s; font-size:14px; }"
            "QPushButton:hover { background:%s; color:%s; }"
        )
        self._btn_min = QPushButton("—", header)
        self._btn_min.setFixedSize(32, 28)
        self._btn_min.setToolTip("最小化")
        self._btn_min.setStyleSheet(win_btn_base % (theme.text, theme.hover, theme.text))
        self._btn_min.clicked.connect(self.showMinimized)
        self._btn_max = QPushButton("□", header)
        self._btn_max.setFixedSize(32, 28)
        self._btn_max.setToolTip("最大化 / 还原")
        self._btn_max.setStyleSheet(win_btn_base % (theme.text, theme.hover, theme.text))
        self._btn_max.clicked.connect(self._toggle_maximize)
        self._btn_close = QPushButton("✕", header)
        self._btn_close.setFixedSize(32, 28)
        self._btn_close.setToolTip("关闭（隐藏窗口）")
        self._btn_close.setStyleSheet(
            "QPushButton { border:none; background:transparent; color:%s; font-size:14px; }"
            "QPushButton:hover { background:#e81123; color:#ffffff; }" % theme.text
        )
        self._btn_close.clicked.connect(self.hide)

        header_layout.addWidget(self._title_label)
        header_layout.addWidget(self._btn_settings)
        header_layout.addStretch(1)
        header_layout.addWidget(self._combo)
        header_layout.addWidget(self._btn_min)
        header_layout.addWidget(self._btn_max)
        header_layout.addWidget(self._btn_close)
        layout.addWidget(header)

        # ---------- 消息区（气泡列表 + 可自定义背景） ----------
        # 用 StackAll 重叠布局：底层背景图，上层透明滚动区，消息浮于背景之上
        msg_wrap = QWidget(card)
        msg_stack = QStackedLayout(msg_wrap)
        msg_stack.setStackingMode(QStackedLayout.StackingMode.StackAll)
        msg_stack.setContentsMargins(0, 0, 0, 0)
        self._bg_label = QLabel(msg_wrap)
        self._bg_label.setScaledContents(True)   # 背景拉伸铺满消息区
        self._bg_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        msg_stack.addWidget(self._bg_label)
        self._scroll = QScrollArea(msg_wrap)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setStyleSheet("QScrollArea { background:transparent; border:none; }")
        self._scroll.viewport().setAutoFillBackground(False)
        container = QWidget()
        container.setStyleSheet("background:transparent;")
        container.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        container.setAutoFillBackground(False)
        self._msg_layout = QVBoxLayout(container)
        self._msg_layout.setContentsMargins(8, 6, 8, 6)
        self._msg_layout.setSpacing(2)
        self._msg_layout.addStretch(1)      # 消息从顶部开始排列
        self._scroll.setWidget(container)
        msg_stack.addWidget(self._scroll)
        # StackAll 模式下当前页绘制在最上层：把滚动区设为当前页（消息在上，背景在下）
        msg_stack.setCurrentWidget(self._scroll)
        self._bg_viewport = self._scroll.viewport()   # 兼容引用
        layout.addWidget(msg_wrap, 1)

        # ---------- 情绪 / 羁绊面板 ----------
        self._status_frame = QFrame(card)
        frame_layout = QVBoxLayout(self._status_frame)
        frame_layout.setContentsMargins(2, 4, 2, 0)
        frame_layout.setSpacing(3)

        self._emotion_label = QLabel(
            "心情  愉悦 +0.50　活跃 +0.30　支配 +0.00", self._status_frame
        )
        self._emotion_label.setTextFormat(Qt.TextFormat.RichText)
        self._emotion_label.setStyleSheet("font-size:11px; background:transparent;")
        frame_layout.addWidget(self._emotion_label)

        bond_row = QHBoxLayout()
        bond_row.setSpacing(6)
        self._bond_bars = {}
        for key, label in (("warmth", "温暖"), ("trust", "信任"),
                           ("formality", "正式"), ("humor", "幽默")):
            cell = QVBoxLayout()
            cell.setSpacing(0)
            name_label = QLabel(label, self._status_frame)
            name_label.setStyleSheet("color:%s; font-size:9px; background:transparent;" % theme.muted)
            name_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            bar = QProgressBar(self._status_frame)
            bar.setRange(0, 100)
            bar.setValue(0)
            bar.setTextVisible(False)
            bar.setFixedHeight(6)
            bar.setStyleSheet(
                "QProgressBar { background:%s; border:none; border-radius:3px; }"
                "QProgressBar::chunk { background:%s; border-radius:3px; }"
                % (theme.hover, theme.accent)
            )
            cell.addWidget(name_label)
            cell.addWidget(bar)
            bond_row.addLayout(cell)
            self._bond_bars[key] = bar
        frame_layout.addLayout(bond_row)
        layout.addWidget(self._status_frame)

        # ---------- 状态栏（临时提示：录音等） ----------
        self._status = QLabel("", card)
        self._status.setStyleSheet("color:%s; font-size:11px;" % theme.muted)
        layout.addWidget(self._status)

        # ---------- 输入区 ----------
        input_row = QHBoxLayout()
        self._input = QTextEdit(card)
        self._input.setPlaceholderText("输入消息…（@角色名 可让指定角色回复，Enter 发送）")
        self._input.setMaximumHeight(84)
        self._input.setStyleSheet(
            "QTextEdit { background:%s; color:%s; border:1px solid %s;"
            " border-radius:10px; padding:6px; font-size:13px; }"
            % (theme.input_bg, theme.text, theme.border)
        )
        self._input.keyPressEvent = self._input_key_press
        self._send_btn = QPushButton("发送", card)
        self._send_btn.setFixedSize(56, 36)
        self._send_btn.setStyleSheet(
            "QPushButton { background:%s; color:white; border:none; border-radius:8px; }"
            "QPushButton:hover { background:%s; }" % (theme.accent, theme.accent_hover)
        )
        self._send_btn.clicked.connect(self._send)
        input_row.addWidget(self._input, 1)
        input_row.addWidget(self._send_btn)
        layout.addLayout(input_row)

    # ---------- 角色 ----------
    def _on_combo_changed(self, name: str) -> None:
        if name:
            self._title_label.setText(name)
            self.setWindowTitle(f"Desktop Pet - {name}")
            self.load_history_for(name)
            self.character_switched.emit(name)

    def set_active(self, name: str) -> None:
        """同步当前角色（不触发信号）。"""
        self._combo.blockSignals(True)
        idx = self._combo.findText(name)
        if idx >= 0:
            self._combo.setCurrentIndex(idx)
            self._title_label.setText(name)
            self.setWindowTitle(f"Desktop Pet - {name}")
        self._combo.blockSignals(False)
        self.load_history_for(name)

    def refresh_characters(self, names: list, active: str = None) -> None:
        """刷新角色下拉框；无角色时显示"选择角色"占位。"""
        names = list(names or [])
        current = self._combo.currentText() or ""
        self._combo.blockSignals(True)
        self._combo.clear()
        self._combo.addItems(names)
        target = None
        if active and active in names:
            target = active
        elif current and current in names:
            target = current
        elif names:
            target = names[0]
        if target:
            self._combo.setCurrentText(target)
            self._title_label.setText(target)
            self.setWindowTitle(f"Desktop Pet - {target}")
        else:
            self._combo.setCurrentIndex(-1)     # 显示占位文本
            self._title_label.setText("选择角色")
            self.setWindowTitle("Desktop Pet")
        self._combo.blockSignals(False)
        self._char_cache.clear()
        self.load_history_for(target)

    # ---------- 发送 ----------
    def _send(self) -> None:
        text = self._input.toPlainText().strip()
        if not text:
            return
        # 界面即时回显用户消息（@路由时显示目标）
        m = re.match(r"^@([^\s@：:]+)[\s：:]*", text)
        label = f"你 → @{m.group(1)}" if m and self.manager.exists(m.group(1)) else "你"
        self.append_message(label, text, kind="user")
        self._scroll_to_bottom(force=True)   # 发送后强制滚到底，确保看到自己刚发的消息
        self._input.clear()
        self.send_requested.emit(text)

    def _input_key_press(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not (event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            self._send()
            event.accept()
        else:
            QTextEdit.keyPressEvent(self._input, event)

    # ---------- 历史显示 ----------
    def _add_message_item(self, name: str, text: str, kind: str, ts=None) -> _MessageItem:
        if kind == "user":
            avatar = self._user_avatar()
        elif kind in ("char", "initiative"):
            avatar = self._char_avatar(name)
        else:
            avatar = None
        item = _MessageItem(name, text, kind, self._theme, avatar, ts=ts, parent=self._scroll.widget())
        # 插到末尾（保留最后的 stretch）
        self._msg_layout.insertWidget(self._msg_layout.count() - 1, item)
        self._scroll_to_bottom()
        return item

    def _current_role(self) -> str:
        """当前对话角色名（用于历史归属）。"""
        return self._combo.currentText() or ""

    def _clear_message_area(self) -> None:
        """清空气泡区（保留末尾 stretch），并重置思考状态。"""
        if self._thinking and self._thinking_item is not None:
            self._thinking = False
            self._thinking_item = None
            self._thinking_text = ""
        while self._msg_layout.count() > 1:   # 最后一项是 stretch
            item = self._msg_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def load_history_for(self, character_name: str) -> None:
        """加载某角色的历史消息（重启/切换角色时调用），重复加载同一角色不重绘。"""
        if not character_name or character_name == self._loaded_role:
            return
        self._loaded_role = character_name
        self._clear_message_area()
        for rec in history_store.load(character_name):
            kind = rec.get("kind", "char")
            name = rec.get("name", "")
            text = rec.get("text", "")
            if not text:
                continue
            if kind == "user":
                self._add_message_item("你", text, "user", ts=rec.get("ts"))
            elif kind == "char":
                self._add_message_item(name or character_name, text, "char", ts=rec.get("ts"))
            elif kind == "initiative":
                self._add_message_item(name or character_name, text, "initiative", ts=rec.get("ts"))

    def append_message(self, name: str, text: str, kind: str = "char") -> None:
        ts = time.time()
        self._add_message_item(name, text, kind, ts=ts)
        # 持久化：user/char/initiative 落到当前角色历史
        if kind in ("user", "char", "initiative"):
            history_store.append(self._current_role(), name, kind, text, ts)

    def show_error(self, message: str) -> None:
        self._add_message_item("系统", message, "error")

    def set_thinking(self, on: bool) -> None:
        if on and not self._thinking:
            self._thinking = True
            self._thinking_text = ""
            self._thinking_item = self._add_message_item("", "正在输入…", "thinking")
        elif not on and self._thinking:
            self._thinking = False
            self._thinking_text = ""
            if self._thinking_item is not None:
                self._msg_layout.removeWidget(self._thinking_item)
                self._thinking_item.deleteLater()
                self._thinking_item = None

    def set_thinking_text(self, char_name: str, piece: str) -> None:
        """思考/回复流式输出：把增量文本实时追加到思考气泡（打字机效果）。"""
        if not self._thinking or self._thinking_item is None or not piece:
            return
        self._thinking_text += piece
        self._thinking_item.update_text(self._thinking_text)
        self._scroll_to_bottom()

    def reset_thinking(self, char_name: str) -> None:
        """OOC 重试前清空已流式显示的文本，重新从空开始累积。"""
        if not self._thinking:
            return
        self._thinking_text = ""
        if self._thinking_item is not None:
            self._thinking_item.update_text("")

    def show_tool_event(self, char_name: str, text: str) -> None:
        """把 Agent 工具活动（如「已调用 memory_search(…)」）追加到思考气泡。

        思考气泡是临时的：工具事件与流式内容一起累积，回复完成时整体移除，
        正式回复仍由 append_message 展示，不会混入工具说明。
        """
        if not self._thinking or self._thinking_item is None or not text:
            return
        self._thinking_text = self._thinking_text.rstrip("\n")
        if self._thinking_text:
            self._thinking_text += "\n"
        self._thinking_text += f"· {text}"
        self._thinking_item.update_text(self._thinking_text)
        self._scroll_to_bottom()

    def set_status(self, data) -> None:
        """接收情绪/羁绊状态并渲染。

        Args:
            data: dict {"pad": [愉悦P, 活跃A, 支配D], "bond": {"warmth","trust","formality","humor"}}
                  旧版兼容：传入 str 时直接显示文本。
        """
        if isinstance(data, str):
            self._emotion_label.setText(data)
            return
        try:
            pad = list(data.get("pad", [0.5, 0.3, 0.0]))
            bond = data.get("bond", {}) or {}
        except Exception:
            return
        while len(pad) < 3:
            pad.append(0.0)
        p, a, d = pad[0], pad[1], pad[2]
        theme = self._theme

        def color(v):
            if v > 0.3:
                return "#16a34a"   # 正向：绿
            if v < -0.3:
                return "#dc2626"   # 负向：红
            return theme.muted

        self._emotion_label.setText(
            '心情 愉悦 <b style="color:%s">%+.2f</b>　活跃 <b style="color:%s">%+.2f</b>　支配 <b style="color:%s">%+.2f</b>'
            % (color(p), p, color(a), a, color(d), d)
        )
        for key in ("warmth", "trust", "formality", "humor"):
            val = float(bond.get(key, 0.0))
            self._bond_bars[key].setValue(int(round(max(0.0, min(1.0, val)) * 100)))

    def clear_history(self) -> None:
        """清空当前显示的聊天区（不删除已持久化的历史）。"""
        self._clear_message_area()
        self._loaded_role = None

    def _scroll_to_bottom(self, force: bool = False) -> None:
        QTimer.singleShot(0, lambda: self._do_scroll_to_bottom(force))

    def _do_scroll_to_bottom(self, force: bool = False) -> None:
        """跟随滚动：默认仅当用户处于底部附近时滚到最底（不打断向上翻阅）；
        force=True 强制滚到底（用于用户主动发送消息后，确保能看到自己刚发的消息）。"""
        bar = self._scroll.verticalScrollBar()
        at_bottom = bar.maximum() - bar.value() <= 24   # 距底部 24px 内视为"在底部"
        if force or at_bottom:
            bar.setValue(bar.maximum())

    # ---------- 头像 ----------
    def _user_avatar(self) -> QPixmap:
        path = (self.config.get("window", "user_avatar", default="") or "").strip()
        pm = _load_avatar_file(path)
        if pm.isNull():
            return _avatar_placeholder("我")
        return pm

    def _char_avatar(self, name: str) -> QPixmap:
        char = self._char_cache.get(name)
        if char is None:
            try:
                char = self._char_cache[name] = self.manager.load(name)
            except Exception:
                char = None
        path = (char.avatar or "").strip() if char else ""
        pm = _load_avatar_file(path)
        if pm.isNull():
            return _avatar_placeholder(name)
        return pm

    # ---------- 窗口配置 ----------
    def apply_config(self) -> None:
        """配置变更后刷新窗口参数（置顶开关、聊天背景）。"""
        on = bool(self.config.get("window", "always_on_top", default=False))
        if on != self._topmost:
            self._topmost = on
            self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, on)
            if self.isVisible():
                self.show()
        self._load_chat_bg()
        self._char_cache.clear()    # 配置可能改动用户头像

    def _load_chat_bg(self) -> None:
        """加载聊天背景图片（config window.chat_bg；失败/未配置则无背景）。"""
        path = (self.config.get("window", "chat_bg", default="") or "").strip()
        pixmap = QPixmap()
        if path and os.path.isfile(path):
            loaded = QPixmap(path)
            if not loaded.isNull():
                pixmap = loaded
        self._bg_label.setPixmap(pixmap)   # scaledContents 已开启 -> 拉伸铺满消息区

    def _toggle_maximize(self) -> None:
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    # ---------- 窗口缩放 / 拖拽 ----------
    def _edge_at(self, pos):
        """返回鼠标位置命中的缩放边缘（l/r/t/b/tl/tr/bl/br），最大化或无命中返回 None。"""
        if self.isMaximized():
            return None
        edge = _RESIZE_EDGE
        x, y = pos.x(), pos.y()
        w, h = self.width(), self.height()
        on_l = x <= edge
        on_r = x >= w - edge
        on_t = y <= edge
        on_b = y >= h - edge
        if on_l and on_t:
            return "tl"
        if on_r and on_t:
            return "tr"
        if on_l and on_b:
            return "bl"
        if on_r and on_b:
            return "br"
        if on_l:
            return "l"
        if on_r:
            return "r"
        if on_t:
            return "t"
        if on_b:
            return "b"
        return None

    _CURSORS = {
        "l": Qt.CursorShape.SizeHorCursor,
        "r": Qt.CursorShape.SizeHorCursor,
        "t": Qt.CursorShape.SizeVerCursor,
        "b": Qt.CursorShape.SizeVerCursor,
        "tl": Qt.CursorShape.SizeFDiagCursor,
        "br": Qt.CursorShape.SizeFDiagCursor,
        "tr": Qt.CursorShape.SizeBDiagCursor,
        "bl": Qt.CursorShape.SizeBDiagCursor,
    }

    @staticmethod
    def _resize_geometry(edge, geo, dx, dy, min_w, min_h):
        """根据拖拽增量计算新几何 (x, y, w, h)，保证不小于最小尺寸。"""
        x, y, w, h = geo.x(), geo.y(), geo.width(), geo.height()
        if "l" in edge:
            nw = max(min_w, w - dx)
            x = geo.x() + (w - nw)
            w = nw
        elif "r" in edge:
            w = max(min_w, w + dx)
        if "t" in edge:
            nh = max(min_h, h - dy)
            y = geo.y() + (h - nh)
            h = nh
        elif "b" in edge:
            h = max(min_h, h + dy)
        return x, y, w, h

    def _do_resize(self, event):
        if not self._resizing or self._resize_start is None:
            return
        (start_global, geo) = self._resize_start
        delta = event.globalPosition().toPoint() - start_global
        x, y, w, h = self._resize_geometry(
            self._resizing, geo, delta.x(), delta.y(),
            self.minimumWidth(), self.minimumHeight(),
        )
        self.setGeometry(x, y, w, h)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            edge = self._edge_at(event.position().toPoint())
            if edge:
                # 边缘热区：进入缩放模式
                self._resizing = edge
                self._resize_start = (event.globalPosition().toPoint(), self.geometry())
                event.accept()
                return
            # 空白区域：拖拽移动窗口
            if self.childAt(event.position().toPoint()) is None:
                self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._resizing:
            self._do_resize(event)
            event.accept()
            return
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()
            return
        # 悬停时按边缘位置切换缩放光标
        edge = self._edge_at(event.position().toPoint())
        cursor = self._CURSORS.get(edge, Qt.CursorShape.ArrowCursor)
        if self.cursor().shape() != cursor:
            self.setCursor(cursor)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_offset = None
        self._resizing = None
        self._resize_start = None
        self.setCursor(Qt.CursorShape.ArrowCursor)
        event.accept()

    def eventFilter(self, obj, event):
        # 子控件区域移动鼠标时，也按窗口边缘位置更新缩放光标（窗口光标被子控件继承）
        if event.type() == QEvent.Type.MouseMove and not self._resizing:
            pos = self.mapFromGlobal(event.globalPosition().toPoint())
            edge = self._edge_at(pos)
            cursor = self._CURSORS.get(edge, Qt.CursorShape.ArrowCursor)
            if self.cursor().shape() != cursor:
                self.setCursor(cursor)
        return super().eventFilter(obj, event)

    def leaveEvent(self, event):
        self.setCursor(Qt.CursorShape.ArrowCursor)
        super().leaveEvent(event)

    def resizeEvent(self, event):
        # 窗口尺寸变化：防抖 500ms 后持久化，下次启动恢复
        super().resizeEvent(event)
        if not hasattr(self, "_size_save_timer"):
            self._size_save_timer = QTimer(self)
            self._size_save_timer.setSingleShot(True)
            self._size_save_timer.setInterval(500)
            self._size_save_timer.timeout.connect(self._save_size)
        if not self._size_save_timer.isActive():
            self._size_save_timer.start()

    def _save_size(self) -> None:
        self.config.set(self.width(), "window", "chat_w")
        self.config.set(self.height(), "window", "chat_h")
