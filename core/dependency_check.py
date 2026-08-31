"""可选依赖检测与自动安装（L1 长期记忆 + 角色漂移检测）。

首次启动时检测 chromadb 与 sentence-transformers 是否可用；
缺失时引导用户通过国内镜像源自动安装（torch 用 CPU 版镜像，避免拉取数 GB 的 CUDA 版）。
打包为 exe（frozen）时无法把依赖 pip 进冻结环境，仅提示、不提供安装。
"""
import importlib.util
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


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打包环境中（exe）。"""
    return bool(getattr(sys, "frozen", False))


def check_available() -> dict:
    """返回 {"chromadb": bool, "sentence_transformers": bool}。"""
    return {name: importlib.util.find_spec(name) is not None for name in OPTIONAL_PACKAGES}


def missing_packages() -> list:
    """返回缺失的模块名列表。"""
    return [name for name, ok in check_available().items() if not ok]


def install_missing(on_output=None) -> int:
    """自动安装缺失的可选依赖，返回退出码（0=成功）。"""
    missing = missing_packages()
    if not missing:
        return 0
    python = sys.executable

    commands = []
    # 1) 需要 sentence-transformers 时，先装 CPU 版 torch（避免默认 CUDA 版体积巨大）
    if "sentence_transformers" in missing:
        commands.append([
            python, "-m", "pip", "install", "--no-input", "--disable-pip-version-check",
            "torch", "--index-url", TORCH_CPU_INDEX,
        ])
    # 2) 通过清华镜像安装 chromadb / sentence-transformers
    pkgs = [OPTIONAL_PACKAGES[name] for name in missing]
    commands.append([
        python, "-m", "pip", "install", "--no-input", "--disable-pip-version-check",
        "-i", PYPI_MIRROR,
    ] + pkgs)

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
