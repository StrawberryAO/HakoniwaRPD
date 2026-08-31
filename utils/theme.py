"""主题适配工具：检测 Windows 亮/暗主题并统一管理主题配色。

- 检测注册表 HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize
  的 AppsUseLightTheme 值（1=亮色，0=暗色；读取失败按亮色处理）。
- apply_theme(app)：给 QApplication 设置 Qt Fusion 风格 + 对应 QPalette，
  并返回 Theme 对象（应在创建任何窗口之前调用）。
- 各 UI 模块通过 get_theme() 获取主题变量（卡片背景/文本色/辅助色/按钮色/气泡色等），
  不再硬编码颜色，保证亮/暗系统主题下文字与背景对比正确。
"""
import sys

from PySide6.QtGui import QColor, QPalette

_LIGHT_KEY = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
_LIGHT_VALUE = "AppsUseLightTheme"

_theme_cache = None


def is_dark_theme() -> bool:
    """检测系统是否为暗色主题；读取失败按亮色处理。"""
    dark = False
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _LIGHT_KEY) as key:
                value, _ = winreg.QueryValueEx(key, _LIGHT_VALUE)
                dark = int(value) == 0
        except Exception:
            dark = False
    return dark


class Theme:
    """一组主题变量：核心 UI 颜色（暗色 = 深底白字，亮色 = 浅底黑字）。"""

    def __init__(self, dark: bool):
        self.dark = bool(dark)
        # 背景与卡片
        self.window = "#1e1f22" if dark else "#f0f2f5"          # 窗口背景
        self.card = "#2b2d30" if dark else "#ffffff"            # 卡片 / 容器背景
        self.border = "#45484d" if dark else "#d9dce2"          # 边框
        self.hover = "#3d4045" if dark else "#e6eaf0"           # 悬停背景
        self.pressed = "#484c52" if dark else "#d8dfe8"         # 按下背景
        self.input_bg = "#33363a" if dark else "#ffffff"        # 输入框背景
        # 文本
        self.text = "#e8eaed" if dark else "#1f2328"            # 主文本
        self.secondary = "#aeb4ba" if dark else "#5f6a73"       # 次级文本
        self.muted = "#9aa0a6" if dark else "#8a9199"           # 辅助/占位文本
        # 强调与危险
        self.accent = "#5B8FF9"                                 # 主蓝
        self.accent_hover = "#4a7fe8"
        self.success = "#16a34a"                                # 成功绿
        self.danger = "#e5534b"                                 # 错误红
        # 聊天气泡
        self.user_bubble = "#95ec69"                            # 用户：绿气泡
        self.user_bubble_text = "#1f2328"                       # 绿气泡上深色文字
        self.char_bubble = "#ffffff" if dark else "#ffffff"     # 角色：白气泡
        self.char_bubble_text = "#1f2328"
        if dark:
            self.char_bubble = "#3c3f44"                        # 暗色主题：深色气泡
            self.char_bubble_text = "#e8eaed"                   # 暗色主题：白字


def get_theme() -> Theme:
    """返回当前主题（首次调用时自动检测并缓存）。"""
    global _theme_cache
    if _theme_cache is None:
        _theme_cache = Theme(is_dark_theme())
    return _theme_cache


def set_theme(theme: Theme) -> None:
    """手动设置主题（测试用）。"""
    global _theme_cache
    _theme_cache = theme


def build_palette(theme: Theme) -> QPalette:
    """按主题构建 QPalette（配合 Qt Fusion style）。"""
    palette = QPalette()
    roles = {
        QPalette.ColorRole.Window: theme.window,
        QPalette.ColorRole.WindowText: theme.text,
        QPalette.ColorRole.Base: theme.input_bg,
        QPalette.ColorRole.AlternateBase: theme.card,
        QPalette.ColorRole.Text: theme.text,
        QPalette.ColorRole.Button: theme.card,
        QPalette.ColorRole.ButtonText: theme.text,
        QPalette.ColorRole.BrightText: "#ffffff",
        QPalette.ColorRole.Highlight: theme.accent,
        QPalette.ColorRole.HighlightedText: "#ffffff",
        QPalette.ColorRole.Link: theme.accent,
        QPalette.ColorRole.ToolTipBase: theme.card,
        QPalette.ColorRole.ToolTipText: theme.text,
        QPalette.ColorRole.PlaceholderText: theme.muted,
    }
    for role, color in roles.items():
        palette.setColor(role, QColor(color))
    disabled = (QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText,
                QPalette.ColorRole.WindowText)
    for role in disabled:
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(theme.muted))
    return palette


def apply_theme(app, dark: bool = None) -> Theme:
    """给 QApplication 应用 Fusion 风格 + 主题调色板，返回 Theme。

    Args:
        app: QApplication 实例（必须在创建任何窗口之前调用）。
        dark: 强制暗/亮主题；None 表示按系统检测。
    """
    global _theme_cache
    theme = Theme(is_dark_theme() if dark is None else dark)
    _theme_cache = theme
    app.setStyle("Fusion")
    app.setPalette(build_palette(theme))
    return theme
