"""全局日志工具。

提供统一的 logger：同时输出到控制台与 logs/app.log（滚动文件）。
所有模块通过 get_logger() 获取同一个实例，避免重复初始化。
"""
import logging
import os
from logging.handlers import RotatingFileHandler

_DEFAULT_LOG_DIR = "logs"
_LOGGER = None


def setup_logger(name: str = "DesktopPet", log_dir: str = None, level: int = logging.INFO) -> logging.Logger:
    """初始化全局 logger（幂等，重复调用直接返回已有实例）。

    Args:
        name: logger 名称。
        log_dir: 日志目录，默认 logs/。
        level: 日志级别。
    """
    global _LOGGER
    if _LOGGER is not None:
        return _LOGGER
    log_dir = log_dir or _DEFAULT_LOG_DIR
    os.makedirs(log_dir, exist_ok=True)

    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    file_handler = RotatingFileHandler(
        os.path.join(log_dir, "app.log"),
        maxBytes=2 * 1024 * 1024,   # 2MB 滚动
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)
    logger.addHandler(stream_handler)

    _LOGGER = logger
    return logger


def get_logger() -> logging.Logger:
    """获取全局 logger；未初始化时自动初始化。"""
    return _LOGGER or setup_logger()
