# 贡献指南（Contributing）

感谢你对 **Hakoniwa（箱庭）** 感兴趣！无论是提交 bug、建议功能还是代码贡献，都欢迎。

## 环境准备

```bash
# 克隆后创建虚拟环境并安装依赖
python -m venv .venv
.venv\Scripts\activate           # Windows
# source .venv/bin/activate     # Linux/macOS

pip install -r requirements.txt
# 可选（L1 记忆 / 漂移检测）
pip install chromadb sentence-transformers
```

启动：`python main.py`（首次会复制 `config.example.json` → `config.json` 并填入 API Key）。

## 开发规范

- **Python 3.10+**，GUI 用 PySide6。
- 可选依赖必须 `try/except` 导入，缺失时静默降级（如 `core/memory_manager.py`）。
- UI 文案避免 emoji（统一用汉字/文本字形）。
- 所有颜色/主题色通过 `utils/theme.py` 的主题变量，不要硬编码（保证亮/暗主题正确）。

## 提交信息

建议使用 [Conventional Commits](https://www.conventionalcommits.org/) 格式：

```
feat(ui): 新增聊天背景自定义
fix(chat): 修复主动搭话不触发的 AttributeError
docs(readme): 更新使用说明
refactor(core): 重构依赖检测
```

## 分支与 PR

1. 从 `main` 创建分支：`git checkout -b feat/xxx`
2. 开发、验证后提交
3. 推送分支并创建 Pull Request，描述改动与验证结果
4. PR 合并前请保持 `main` 可运行（`python main.py` 能启动）

## 测试

项目没有完整的自动化测试套件，但**改动后**请至少保证：

```bash
.venv\Scripts\python.exe -m compileall -q core ui utils tts main.py   # 语法检查
```

涉及 UI 的改动，可用 offscreen 验证（`QT_QPA_PLATFORM=offscreen`）确认不崩溃。
涉及 LLM / 对话的改动，请用假后端或最小化真实调用验证，避免消耗过多 token。

## 风格提醒

- 保持模块职责单一（`core/` 逻辑、`ui/` 界面、`utils/` 工具）。
- 新增配置项请同步 `utils/config.py` 的默认值与 `config.example.json`。
- 新增依赖请在 `requirements.txt` 标注必需 / 可选。

有任何不确定的地方，先开 Issue 讨论再动手。
