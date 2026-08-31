# Desktop Character Pet —— 智能角色扮演桌面宠物

一个悬浮在桌面上的智能宠物：只需输入 **角色名** + **作品名**，程序自动调用大模型
API 生成详尽且结构化的角色设定（人设提示词、L2 核心记忆、Worldbook 世界观等），
随后宠物即可逼真地扮演该角色。

内置：**动态情绪/羁绊系统**（事前语气引导）、**分层记忆引擎**（L0 工作记忆 +
L1 长期记忆 + L2 核心记忆）、**Worldbook 关键词触发**、**角色漂移检测**（防 OOC）、
**主动搭话引擎**，并可选加载用户自行训练的 **GPT-SoVITS** 语音包输出语音。
所有高级模块均为可选，缺失依赖时自动降级，不影响核心对话。

## 快速开始

```bash
# 1. 安装依赖（必需项即可运行；可选依赖按需安装）
pip install -r requirements.txt

# 2. 配置 LLM（二选一）：
#    方式 A（推荐）：复制模板并填入你的 API Key
#      copy config.example.json config.json
#      然后编辑 config.json，把 openai.api_key 填上
#    方式 B：用环境变量，不把 Key 写进文件
#      set DEEPSEEK_API_KEY=sk-xxxxxx
#      （可选 DEEPSEEK_BASE_URL / DEEPSEEK_MODEL）
#    默认使用 DeepSeek（OpenAI 兼容接口，deepseek-v4-flash）；
#    也可在 设置 -> 全局设置 中改为本地 Ollama。

# 3. 启动
python main.py
```

> 注意：`config.json` 已被 .gitignore 排除（内含 API Key），请勿提交。
> 首次无 config.json 时会自动套用 config.example.json 模板。

## 使用流程

1. 启动后在对话气泡窗点击 **设置 -> 角色管理**（或托盘菜单"全局设置"）。
2. 输入 **角色名**（必填）与 **作品名**（选填），点击 **自动生成**。
   - 程序先**联网搜索**作品/角色的参考资料（Bing RSS，可配置引擎）注入生成提示；
   - 面板实时显示**多阶段进度条**与 **LLM 流式思考过程**（逐字显示）；
   - 程序调用 LLM 输出结构化 JSON（人设 / traits / 口头禅 / L2 核心记忆 / Worldbook）；
   - 模型不识别该角色时返回 `{"error": "unknown"}`，界面会提示。
3. **对话式修改**：生成后发现口头禅、细节等需要微调？点击 **对话修改（LLM）**，
   用自然语言直接提出要求（例如"口头禅太生硬了，改成更傲娇的风格"），
   LLM 会在保留其他字段的前提下修改并保存，进度与思考过程同样实时可见。
4. 也可以在编辑器中手动微调所有字段（system_prompt、L2、Worldbook、
   主动搭话台词、独立后端、TTS 语音包路径），点击 **保存角色**。
5. 回到对话窗输入消息即可开聊。**`@角色名` 前缀可让指定角色回复**（多角色群聊）。

> 说明：对话修改后角色的旧锚点会被自动清除（内容已变），下次对话时按新人设
> 自动重建，也可在设置中手动"生成/更新锚点"。

## 功能说明

| 功能 | 说明 | 依赖 |
|------|------|------|
| 双后端 LLM | OpenAI 兼容云端 API（DeepSeek 等）+ 本地 Ollama，全局/角色可独立切换 | requests |
| 联网搜索 | 自动生成角色时搜索参考资料注入提示（bing / duckduckgo，可开关） | requests |
| 生成进度 | 多阶段进度条 + LLM 流式思考过程实时显示，全程不阻塞 UI | 内置 |
| 对话式修改 | 生成/编辑后可用自然语言让 LLM 修改人设细节（保留其他字段、自动清锚点） | 内置 |
| L0 工作记忆 | 当前对话窗口历史轮次，随上下文裁剪 | 内置 |
| L1 长期记忆 | ChromaDB 向量检索，加权打分 `0.6*相似度+0.2*重要性+0.2*时效性`，命中提升 importance | chromadb + sentence-transformers |
| L2 核心记忆 | 每条永远无条件置于 System Prompt 顶部，**不可裁剪** | 内置 |
| Worldbook | 用户消息关键词匹配命中后注入，未命中不注入以省 token | 内置 |
| 情绪系统 | PAD 三维模型（愉悦/激活/支配），规则更新 + EMA 平滑，生成事前语气指令 | 内置 |
| 羁绊系统 | [温暖, 信任, 正式度, 幽默] 四维，秘密分享加信任、每日衰减 | 内置 |
| 漂移检测 | 人设+口头禅+台词+近期回复嵌入均值作锚点，回复余弦相似度 < 阈值则重写（最多重试 1 次） | sentence-transformers |
| 主动搭话 | 定时检查空闲状态，随机台词库 / LLM 临时生成，气泡 + TTS 输出 | 内置 |
| TTS | GPT-SoVITS 推理服务合成语音，QMediaPlayer 播放 | GPT-SoVITS（独立部署） |
| 悬浮窗 | 无边框半透明置顶、拖拽、贴边自动隐藏、系统托盘、状态动画 | PySide6 |

> 说明：本版本已**移除语音识别（ASR）功能**；L1 记忆与漂移检测为可选依赖，
> 首次启动缺失时会询问并通过国内镜像源自动安装。

### 联网搜索配置（config.json）

```json
"web_search": {
  "enabled": true,        // 角色自动生成时是否联网搜索
  "engine": "auto",       // auto（bing -> duckduckgo）| bing | duckduckgo
  "top_k": 5,             // 注入的参考条数
  "timeout": 15
}
```

搜索失败（无网络/引擎被墙）时自动降级为纯模型知识生成，不影响主流程。
搜索引擎结果为外部数据，仅作参考资料注入，可能不准确。

## 上下文裁剪策略（防 token 溢出）

当消息列表估算 token 超过 `context_window * context_ratio`（在 设置 -> 全局设置 中配置，
上限 2M tokens，ds-v4 系列可设 1M）时：
保留优先级 **L2 核心记忆 > 最近 3 轮完整对话 > 高重要性 L1（importance>0.5）
> 普通 L1 > 更早对话轮次**，`trim_context()` 在 `core/chat_engine.py` 中实现，
角色核心设定永不丢失。

## 可选功能配置

### 1. L1 记忆与漂移检测（推荐安装）

```bash
pip install chromadb sentence-transformers
```

首次使用会下载嵌入模型（默认 `BAAI/bge-small-zh-v1.5`，可改 config.json 的
`embedding.model` 为 `all-MiniLM-L6-v2` 等）。向量数据存放在 `data/chroma/`。

### 2. TTS 语音（GPT-SoVITS）

1. 自行部署 GPT-SoVITS 推理服务（运行其 `api.py`，默认端口 9880）。
2. 训练并导出你的音色模型。
3. 在角色编辑器中把 **TTS 语音包文件夹** 指向包含参考音频（wav/mp3/flac）的目录。
4. 服务不可达 / 未配置时自动降级为纯文本，无任何报错。

## 自定义宠物形象

在 设置 -> 全局设置 ->「宠物形象（图片/GIF）」中
导入你自己的静态图片（png/jpg/jpeg/webp/bmp）或动图（gif）：

- 选择文件后设置界面会**实时预览**；
- 点击「保存设置」后**立即生效**，无需重启；
- 未导入形象时，悬浮窗显示一个中性圆点占位（悬停有提示）；
- 形象按比例缩放到悬浮窗内（约 110x110），透明背景 PNG 效果最佳。

对应的配置项为 `config.json` 的 `window.pet_image`（绝对路径），也可以直接改文件。

## 打包（PyInstaller）

```bash
pip install pyinstaller
pyinstaller -F -w -n DesktopPet ^
  --hidden-import PySide6.QtMultimedia ^
  main.py
```

打包后请将 `config.json`、`characters/`、`data/` 放在可执行文件同目录。
注意：可选依赖（chromadb / sentence-transformers）体积较大，
如需打包请使用 `--collect-all chromadb` 等参数，或保持它们不打包（功能自动降级）。

## 项目结构

```
DesktopCharacterPet/
├── main.py                  # 程序入口（信号装配 + 可选依赖检测安装）
├── requirements.txt         # 必需 / 可选依赖
├── config.json              # 全局配置
├── characters/              # 角色 JSON 存放目录（见 README.md）
├── core/
│   ├── character_manager.py # 角色自动生成 / 保存 / 加载 / 编辑 / 删除
│   ├── chat_engine.py       # 统一 LLM 调度、消息拼装、上下文裁剪、OOC 重试
│   ├── llm_backends.py      # OllamaBackend / OpenAIBackend
│   ├── emotion_system.py    # PAD 情绪、羁绊、语气指令生成
│   ├── memory_manager.py    # L0 工作记忆 + L1 ChromaDB（可选）、重要性计算
│   ├── drift_detector.py    # 锚点生成、余弦相似度、OOC 判定
│   └── dependency_check.py  # 可选依赖检测 + 国内镜像自动安装
├── tts/
│   ├── tts_adapter.py       # TTS 适配（线程合成 + QMediaPlayer 播放，静默降级）
│   └── sovits_inference.py  # GPT-SoVITS 推理 HTTP 封装
├── ui/
│   ├── pet_window.py        # 悬浮窗、动画、拖拽、贴边隐藏、托盘、主动搭话
│   ├── chat_bubble.py       # 对话气泡、输入框、发送、搭话设置
│   ├── settings_dialog.py   # 全局设置 + 角色编辑器（L2/Worldbook 等）
│   └── resources.qrc        # 可选资源注册（GIF 等）
└── utils/
    ├── config.py            # 配置加载 / 保存（深合并默认值）
    └── logger.py            # 日志（控制台 + 滚动文件）
```

## 常见问题

- **提示"未配置 API Key"**：在设置中填写，或编辑 `config.json` 的 `llm.openai.api_key`。
- **想用本地模型**：全局设置切换为 Ollama，并确保 `ollama pull <模型名>` 已完成。
- **自动生成失败/报错 JSON**：换更擅长中文的模型，或手动创建角色后编辑。
- **生成时窗口卡住/未响应**：新版已修复——生成、联网搜索、锚点生成全部在后台线程
  执行，UI 只接收进度信号；若仍卡顿，请检查是否在"全局设置"里误填了很慢的嵌入模型。
- **漂移检测误判**：调低/调高 `chat.drift_threshold`（默认 0.52，按 bge-small-zh-v1.5
  实测校准），或删除角色 JSON 中 `anchor_vector` 后点击"生成/更新锚点"重建。
- **Ollama 长回复超时**：调大 `llm.ollama.timeout`。
- **联网搜索无结果**：检查 `web_search.enabled` 与网络；引擎被封时自动降级为模型知识生成。

> 注意：本项目**不包含** txt 台词导入、LoRA 微调/训练、本地 GGUF 模型加载功能
> （按需求范围）。
