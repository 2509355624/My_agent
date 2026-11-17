# 旧项目运行数据迁移（2026-11-17）

新项目 `D:\AI\my_agent_restructure` 是代码重构版，`skills/`、`agents/`、`state/`、`.env` 都在 `.gitignore` 里，不会被提交。本次从旧项目 `D:\AI\agent_my_test` 迁移了实际运行数据，旧项目未改动。

## 已迁移

| 内容 | 来源 | 新项目位置 | 结果 |
|---|---|---|---|
| 生图渠道 | `agent_my_test/skills/` | `skills/` | 1376 个文件，71 份 `workflow.json`，52 个可对外渠道 |
| 自定义模型渠道 | `agent_my_test/data/custom_providers.json` | `data/custom_providers.json` | 2 个中转站 provider，`LLM_PROVIDER=cx_a1b2c3d4` 才能注册 |
| 角色卡库 | `agent_my_test/agents/qq/` | `agents/qq/` | 31 张官方角色卡 |
| 全部 agent 运行目录 | `agent_my_test/agents/{qq,draw,main,moss,self_233,writing}` | `agents/` | QQ 会话、记忆、近期上下文、贴纸等 |
| 角色扮演会话 | `agent_my_test/state/persona_sessions.json` | `state/persona_sessions.json` | 530 条会话键 |
| 玩家自建角色卡 | `agent_my_test/state/persona_user_cards/` | `state/persona_user_cards/` | 3 张 |
| 角色扮演图片映射/回放/剧情 | `state/persona_pic_map.json`、`state/persona_replay/`、`state/persona_mothers/`、`state/backgrounds/` | 同路径 | 已迁移 |
| 计费与生图运行数据 | `state/persona_billing.json`、`state/lora_*.json`、`state/image_log.jsonl`、`state/image_migrate.json` | 同路径 | 已迁移 |
| 环境变量 | `agent_my_test/.env` | `.env` | 已复制；移除 `NAPCAT_QRCODE_PATH`，`QQ_WEBUI_PORT` 改为 `5099` |

## 单测与真实运行数据的边界

迁移完成后，`tools/preflight_runtime.py` 是真正按启动场景跑的门禁：它用真实 `agents/qq`、`skills/`、`state/persona_sessions.json` 和真实 HTTP 请求验证，当前全 PASS。

全量单测仍会直接读真实 `.env`、`agents/qq`、`skills/`，因此迁移真实数据后结果会和“空数据基线”不同，不能把这类红项当成迁移失败：

- 迁移前基线：`Ran 6457 tests`，failures 457 / errors 163 / skipped 11。
- 迁移后：`Ran 6457 tests`，failures 133 / errors 38 / skipped 3。
- 与基线相比：新增 15 条、减少 324 条红项。

新增红项集中在 `test_prompt_sync`、`test_custom_model`、`test_llm_chain`、`test_persona_billing`、`test_skills`，原因是单测没有隔离真实 `.env`（例如 `LLM_PROVIDER=cx_a1b2c3d4`）、真实 `agents/qq` prompt 和 `skills/ai-prompts` 的引用格式。做纯单测基线时，应在不带 `.env` 的隔离环境跑；真实验收用上面的 preflight。

## 未迁移

这些是旧项目的大体积缓存/备份/日志，不影响新项目启动和上述功能：

- `state/review/`（约 1.8 GB）
- `state/gallery_cache/`（约 692 MB）
- `state/runbench_covers/`（约 417 MB）
- `state/output_trash/`、`friend_gallery/`、`outfit/`、`output_thumbs/`、`tmp/` 等
- `persona_sessions.json` 的小时备份与历史 `.bak`
- 旧项目 `logs/`

## 验证方式

迁移后运行真实接口冒烟，而不是只跑单元测试：

```powershell
D:\AI\confyui_env\Scripts\python.exe tools\preflight_runtime.py
```

该脚本会检查：

- `skills/` 是否真的存在、`/api/gen/channels` 是否返回渠道
- 默认渠道 `silver` 的 `workflow.json` 是否能加载
- `agents/qq/characters.json` 与 `/api/persona/cards` 是否有角色
- `state/persona_sessions.json` 是否有记录，并用真实 QQ 会话键请求 `/api/persona/session`
- `.env` 的 `LLM_PROVIDER` 是否已注册进 `PROVIDERS`（缺 `data/custom_providers.json` 会在这里失败）
- `.env` 是否存在且 `QQ_WEBUI_PORT=5099`
- ComfyUI 是否在线（只警告，不强制失败）

## 注意

本次迁移是**当前快照**。如果迁移之后旧项目还在继续聊天/生图，新项目不会自动收到那部分增量。停掉旧服务后，如果要最终对齐一次，再重新复制上述 `state/` 与 `agents/qq/` 文件即可。
