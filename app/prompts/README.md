# app/prompts

提示词主文件夹。按 feature + 用例分文件，不做一个 `prompts.py` 大字符串。

目标结构：

```text
app/prompts/
├── chat/       # 对话、工具、插话
├── image/      # generate.py / audit.py / outfit.py
├── comic/      # story.py / panel.py / character.py
├── persona/    # roleplay.py / memory.py / opening.py
├── rag/        # 文档问答
├── novel/      # 世界书、章节、裁定
├── card/       # 角色卡、封面
└── admin/      # 管理页文案
```

规则：

- 一个文件只负责一个场景；改漫画编剧不碰生图审核。
- 提示词只放文本/模板，不放业务分支和 HTTP。
- 需要变量时用明确的 `format` 占位，不靠字符串拼接。
- 迁移时从现有 God 文件按场景搬，先搬一个场景，跑该功能测试。
