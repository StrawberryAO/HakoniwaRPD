# characters 目录：角色 JSON 存放位置

每个角色一个 JSON 文件，文件名即角色名。由程序自动生成 / 保存，也可手动编辑。

字段说明：

| 字段                   | 类型            | 说明 |
|------------------------|-----------------|------|
| name                   | string          | 角色名（必填，唯一） |
| work                   | string          | 作品名 |
| created_at             | string          | 创建时间（ISO） |
| system_prompt          | string          | 第一人称完整扮演提示（性格/说话风格/口头禅/背景/禁忌） |
| traits                 | string[]        | 性格标签 |
| catchphrases           | string[]        | 口头禅 |
| background             | string          | 角色简要背景 |
| l2_core_memories       | string[]        | L2 核心记忆：每条永远无条件置于 System Prompt 顶部，不可裁剪 |
| worldbook              | object[]        | 世界书：`[{"keywords": ["触发词"], "content": "注入内容"}]` |
| initiative_dialogues   | string[]        | 主动搭话台词库（为空时由 LLM 临时生成） |
| anchor_vector          | number[]\|null  | 角色锚点向量（漂移检测用，嵌入模型生成） |
| emotion                | object          | PAD 情绪：`{"pad": [P, A, D], "updated_at": "..."}`，范围 [-1,1] |
| bond                   | object          | 四维羁绊：`{"warmth","trust","formality","humor","last_active"}` |
| backend                | object\|null    | 角色独立后端设置（null 表示跟随全局） |
| tts_folder             | string          | GPT-SoVITS 语音包文件夹路径（可选） |

示例（节选）：

```json
{
  "name": "林晚晴",
  "work": "原创",
  "system_prompt": "你是一个温柔细腻的少女……",
  "l2_core_memories": [
    "你是人类与精灵的混血，体内封印着上古魔王的灵魂。",
    "你与用户是自幼一起长大的青梅竹马。"
  ],
  "worldbook": [
    {"keywords": ["精灵", "森林"], "content": "精灵族居住在雾之森林，寿命漫长，厌恶战争。"}
  ]
}
```
