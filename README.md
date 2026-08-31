# HakoniwaRPD —— 智能角色扮演桌面宠物

一个悬浮在你桌面上的 AI 伙伴：只需输入 **角色名** + **作品名**，程序自动调用大模型 API，
生成详尽且结构化的角色设定（人设提示词、L2 核心记忆、Worldbook 世界观等），
宠物随即逼真地扮演该角色。风格参考 [Cyrene-Agent](https://github.com/Playa-0v0/Cyrene-Agent)，
聚焦**沉浸式角色扮演**与**长期陪伴**。

Hakoniwa(“箱庭”) —— 桌面上一个小小的、只属于你和角色的微缩世界。

## ✨ 特性

| 模块 | 说明 |
|------|------|
| 双后端 LLM | OpenAI 兼容云端 API（DeepSeek 等）+ 本地 Ollama，全局 / 角色可独立切换 |
| 联网搜索 | 自动生成角色时搜索参考资料注入生成提示（Bing / DuckDuckGo，可开关） |
| 生成进度 | 多阶段进度条 + LLM 流式思考过程实时显示，全程不阻塞 UI |
| 对话式修改 | 生成后可向 LLM 提出自然语言修改要求，人设细节随时微调 |
| 分层记忆 | L0 工作记忆 + L1 长期记忆（ChromaDB 向量检索）+ L2 核心记忆（永不裁剪） |
| Worldbook | 用户消息关键词命中时注入世界观背景，未命中不注入以省 token |
| 情绪系统 | PAD 三维模型（愉悦 / 激活 / 支配），规则更新 + EMA 平滑，事前语气引导 |
| 羁绊系统 | [温暖, 信任, 正式度, 幽默] 四维，随对话成长、随时间衰减 |
| 漂移检测 | 人设锚点 + 余弦相似度，偏离角色时自动重写（防 OOC） |
| 主动搭话 | 空闲时角色主动找你说话，台词库 / LLM 生成，聊天气泡 + 可选语音 |
| 回复风格 | 用户可选“简洁日常”或“丰富长文”，控制对话篇幅 |
| TTS 语音 | GPT-SoVITS 合成语音（可选，需自行部署服务） |
| 视觉自定义 | 宠物形象、双方头像、聊天背景均可自定义图片 |

> 说明：本版本**不包含**语音识别（ASR）、txt 台词导入、LoRA 训练 / 本地 GGUF 模型加载。
> L1 记忆与漂移检测为可选依赖，首次启动缺失时会询问并通过国内镜像源自动安装。

## 🚀 快速开始

**方式 A —— 源码运行**

```bash
# 1. 安装依赖（必需项即可运行；可选依赖按需安装）
pip install -r requirements.txt

# 2. 配置 LLM（二选一）
#    方式 1（推荐）：复制模板并填入你的 API Key
copy config.example.json config.json        # Windows
#   cp config.example.json config.json      # Linux/macOS
#   然后编辑 config.json，把 openai.api_key 填上
#    方式 2：用环境变量，不把 Key 写进文件
set DEEPSEEK_API_KEY=sk-xxxxxx              # Windows
#   export DEEPSEEK_API_KEY=sk-xxxxxx       # Linux/macOS
#   可选：DEEPSEEK_BASE_URL / DEEPSEEK_MODEL

# 3. 启动
python main.py
```

> 首次无 `config.json` 时会自动套用 `config.example.json` 模板。
> 默认使用 DeepSeek（`deepseek-v4-flash`），可在 设置 → 全局设置 改为本地 Ollama 或 根据其他模型的接口档案自行接入。

**方式 B —— 直接运行**

下载 Releases 中的 `Hakoniwa-win64.zip`，解压后双击 `Hakoniwa.exe` 即可。
exe 首次启动若检测到 L1 记忆 / 漂移检测依赖缺失，会**询问并通过国内镜像源自动安装**
到程序目录 `_deps` 文件夹，重启后即可使用完整功能。

## 🎮 使用流程

1. 启动后在对话框点 **设置 → 角色管理**（或托盘“全局设置”）。
2. 输入 **角色名**（必填）与 **作品名**（选填），点 **自动生成**：
   - 先联网搜索作品 / 角色的参考资料注入生成提示；
   - 面板实时显示进度条与 **LLM 流式思考过程**；
   - 输出结构化 JSON（人设 / traits / 口头禅 / L2 核心记忆 / Worldbook）；
   - 模型不认识该角色时返回 `{"error":"unknown"}` 并提示。
3. **对话式修改**：若口头禅、细节需微调，点 **对话修改（LLM）** 用自然语言提要求，
   LLM 在保留其他字段的前提下修改并保存。
4. 也可在编辑器手动微调所有字段（人设、L2、Worldbook、主动搭话台词、
   独立后端、TTS 路径、角色形象 / 头像），点 **保存角色**。
5. 回到对话框即可开聊。**`@角色名` 前缀可让指定角色回复**（多角色群聊）。

> 对话修改后角色旧锚点会自动清除，下次对话按人设自动重建；
> 也可在编辑器手动“生成 / 更新锚点”。

## 🖼️ 视觉自定义

在 设置 → 全局设置 / 角色管理 中：

- **宠物形象**：在角色编辑器设置，宠物窗显示当前角色的形象（`character.pet_image`）；
  未设置则显示占位圆点。
- **双方头像**：全局“我的头像”（`window.user_avatar`）+ 角色编辑器“角色头像”
  （`character.avatar`），聊天气泡圆形裁剪显示，未设则首字占位。
- **聊天背景**：全局“聊天背景”（`window.chat_bg`），支持 png/jpg/webp/bmp，
  背景铺底、消息浮于其上；留空则无背景。
- **窗口大小**：拖动缩放后自动记住（`window.chat_w/h`），下次启动恢复。

## ⚙️ 常用配置（config.json）

```jsonc
{
  "llm": {
    "backend": "openai",                       // openai | ollama
    "context_window": 1048576,                 // 上下文窗口（上限 2M，ds-v4 可 1M）
    "thinking_chat": false,                    // 聊天是否开启思考模式（关闭更口语化）
    "thinking_generate": true,                 // 生成/修改人设是否开启思考模式
    "chat_style": "concise",                   // 回复风格：concise 简洁 | detailed 长文
    "openai": { "api_key": "", "base_url": "https://api.deepseek.com/v1", "model": "deepseek-v4-flash", "timeout": 90 },
    "ollama": { "base_url": "http://127.0.0.1:11434", "model": "qwen2.5:7b", "timeout": 180 }
  },
  "web_search": { "enabled": true, "engine": "auto", "top_k": 5, "timeout": 15 },
  "hf": { "endpoint": "https://hf-mirror.com", "cache_dir": "models/hub" }
}
```

- **回复风格**：`chat_style` 选 `concise`（1~3 句日常）或 `detailed`（像倾诉/写信的长文）。
- **思考模式**：DeepSeek V4 支持；聊天默认关闭（更快、更省），生成人设默认开启（更稳）。
- **联网搜索**：`web_search.engine` 可选 `auto`（bing → duckduckgo）/ `bing` / `duckduckgo`。

## 🧠 可选依赖（L1 记忆 + 漂移检测）

```bash
pip install chromadb sentence-transformers
```

- **源码运行**：首次启动自动检测缺失，询问后**通过国内镜像源**自动安装（torch 自动用
  CPU 版，约 400 MB，安装前会提示体积）。
- **exe 运行**：同样会询问并自动安装到程序目录 `_deps` 文件夹（不影响系统 Python），
  重启后生效。
- 嵌入模型默认 `BAAI/bge-small-zh-v1.5`（可改 `embedding.model`），向量数据存 `data/chroma/`。
- 不安装时，L1 记忆、锚点、OOC 检测静默降级，不影响核心对话。

## 🔊 TTS 语音（GPT-SoVITS，可选）

1. 自行部署 GPT-SoVITS 推理服务（运行其 `api.py`，默认端口 9880）。
2. 训练并导出音色模型。
3. 在角色编辑器把 **TTS 语音包文件夹** 指向包含参考音频（wav/mp3/flac）的目录。
4. 服务不可达 / 未配置时自动降级为纯文本，无任何报错。

## 🗂️ 项目结构

```
Hakoniwa/
├── main.py                  # 程序入口（信号装配 + 可选依赖检测安装）
├── requirements.txt         # 必需 / 可选依赖
├── config.example.json      # 配置模板（无 API Key）
├── build.bat                # 打包脚本（PyInstaller）
├── characters/              # 角色 JSON 存放目录
├── core/
│   ├── character_manager.py # 角色自动生成 / 保存 / 加载 / 编辑 / 修改
│   ├── chat_engine.py       # 统一 LLM 调度、消息拼装、上下文裁剪、OOC 重试
│   ├── llm_backends.py      # OllamaBackend / OpenAIBackend（含思考模式）
│   ├── emotion_system.py    # PAD 情绪、羁绊、语气指令
│   ├── memory_manager.py    # L0 工作记忆 + L1 ChromaDB（可选）
│   ├── drift_detector.py    # 锚点生成、余弦相似度、OOC 判定
│   ├── history_store.py     # 对话历史持久化（重启恢复）
│   ├── usage_tracker.py     # token 用量统计 + 账户余额查询
│   ├── dependency_check.py  # 可选依赖检测 + 国内镜像自动安装
│   └── web_search.py        # 联网搜索（角色生成参考资料）
├── tts/                     # GPT-SoVITS 语音封装
├── ui/
│   ├── pet_window.py        # 悬浮窗、拖拽、贴边隐藏、托盘、主动搭话
│   ├── chat_bubble.py       # QQ/微信风格聊天窗、头像、聊天背景、缩放
│   ├── settings_dialog.py   # 全局设置 + 角色编辑器
│   └── resources.qrc        # 可选资源注册
└── utils/
    ├── config.py            # 配置加载 / 保存（支持环境变量覆盖）
    ├── theme.py             # 亮/暗主题适配
    └── logger.py            # 日志
```

## ❓ 常见问题

- **提示“未配置 API Key”**：在 设置 → 全局设置 填写，或编辑 `config.json` 的 `llm.openai.api_key`。
- **想用本地模型**：全局设置切换为 Ollama，并确保已 `ollama pull <模型名>`。
- **自动生成失败 / 报错 JSON**：换更擅长中文的模型，或手动创建角色后编辑。
- **漂移检测误判**：调低 / 调高 `chat.drift_threshold`（默认 0.52），或重建角色锚点。
- **聊天背景 / 窗口大小未生效**：重启应用（部分配置在启动时加载）。
- **Ollama 长回复超时**：调大 `llm.ollama.timeout`。

## 📄 许可与致谢

**许可证**：[MIT License](LICENSE) © 2026 StrawberryAO

**致谢**：本项目的**角色扮演机制、分层记忆（L2 核心记忆）与 Worldbook 世界书**在
**设计思路上**参考了优秀的开源项目
[Cyrene-Agent](https://github.com/Playa-0v0/Cyrene-Agent)。
本项目代码为**独立编写**，未复制其源码；若其中含有参考其思路的实现，
请以 Cyrene-Agent 的 LICENSE 为准。
