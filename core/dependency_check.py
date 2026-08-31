"""可选依赖检测与自动安装（L1 长期记忆 + 角色漂移检测）。

首次启动时检测 chromadb 与 sentence-transformers 是否可用；
缺失时引导用户通过国内镜像源自动安装（torch 用 CPU 版镜像，避免拉取数 GB 的 CUDA 版）。
打包为 exe（frozen）时无法把依赖 pip 进冻结环境，仅提示、不提供安装。
"""
import importlib.util
import os
import subprocess
import sys

# 国内镜像源（chromadb / sentence-transformers 用清华 PyPI 镜像）
PYPI_MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"
# torch CPU 版（官方 CPU 索引，避免默认 CUDA 版数 GB 体积）
TORCH_CPU_INDEX = "https://download.pytorch.org/whl/cpu"

# 模块名 -> pip 包名
OPTIONAL_PACKAGES = {
    "chromadb": "chromadb>=0.4.22",
    "sentence_transformers": "sentence-transformers>=2.2.0",
}

# 各依赖预计下载体积（粗略估算，用于安装前提示）
PACKAGE_SIZES = {
    "torch": "约 200 MB",
    "chromadb": "约 80 MB",
    "sentence_transformers": "约 120 MB（含 transformers 等依赖）",
}


def size_estimate_text(missing: list) -> str:
    """根据缺失依赖生成安装体积提示文本（如"torch CPU 版约 200MB + chromadb 约 80MB…"）。"""
    if not missing:
        return ""
    parts = []
    total_mb = 0
    if "sentence_transformers" in missing:
        parts.append(f"torch CPU 版{PACKAGE_SIZES['torch']}")
        total_mb += 200
        parts.append(f"sentence-transformers{PACKAGE_SIZES['sentence_transformers']}")
        total_mb += 120
    if "chromadb" in missing:
        parts.append(f"chromadb{PACKAGE_SIZES['chromadb']}")
        total_mb += 80
    return "预计下载约 " + " + ".join(parts) + f"（合计约 {total_mb} MB，需几分钟）"


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打包环境中（exe）。"""
    return bool(getattr(sys, "frozen", False))


def check_available() -> dict:
    """返回 {"chromadb": bool, "sentence_transformers": bool}。"""
    return {name: importlib.util.find_spec(name) is not None for name in OPTIONAL_PACKAGES}


def missing_packages() -> list:
    """返回缺失的模块名列表。"""
    return [name for name, ok in check_available().items() if not ok]


def deps_dir() -> str:
    """exe 运行时返回"exe 旁 _deps 依赖目录"；源码运行时返回 None（用当前环境）。"""
    if is_frozen():
        return os.path.join(os.path.dirname(sys.executable), "_deps")
    return None


def ensure_deps_on_path() -> None:
    """exe 启动时把 exe 旁的 _deps 目录加入 sys.path，使安装到该目录的库可被导入。"""
    d = deps_dir()
    if d and os.path.isdir(d) and d not in sys.path:
        sys.path.insert(0, d)


def install_missing(on_output=None, target_dir=None) -> int:
    """自动安装缺失的可选依赖，返回退出码（0=成功）。

    Args:
        on_output: 日志回调。
        target_dir: 指定 pip --target 安装目录（exe 场景装到 exe 旁 _deps）；
                    为 None 时装入当前 Python 环境。
    """
    missing = missing_packages()
    if not missing:
        return 0
    python = sys.executable

    def pip_cmd():
        cmd = [python, "-m", "pip", "install", "--no-input", "--disable-pip-version-check"]
        if target_dir:
            cmd += ["--target", target_dir]
        return cmd

    commands = []
    # 1) 需要 sentence-transformers 时，先装 CPU 版 torch（避免默认 CUDA 版体积巨大）
    if "sentence_transformers" in missing:
        commands.append(pip_cmd() + ["torch", "--index-url", TORCH_CPU_INDEX])
    # 2) 通过清华镜像安装 chromadb / sentence-transformers
    pkgs = [OPTIONAL_PACKAGES[name] for name in missing]
    commands.append(pip_cmd() + ["-i", PYPI_MIRROR] + pkgs)

    for cmd in commands:
        if on_output:
            on_output("> " + " ".join(cmd))
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8", errors="replace"
            )
        except Exception as exc:
            if on_output:
                on_output(f"执行失败：{exc}")
            return 1
        output = (proc.stdout or "") + (proc.stderr or "")
        if on_output:
            for line in output.strip().splitlines()[-8:]:
                on_output(line)
        if proc.returncode != 0:
            return proc.returncode
    return 0
