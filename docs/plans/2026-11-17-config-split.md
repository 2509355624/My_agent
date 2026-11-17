# 2026-11-17 config 按职能拆分

## 决策

`config` 不再是一个 `config.py`，而是一个包：

```text
app/core/config/
├── __init__.py     # 公开入口；最终只做显式 re-export
├── paths.py        # 绝对路径、state_path、测试隔离
├── env.py          # .env 加载、_env_bool / _env_list
├── llm.py          # provider / 模型 / 降级链 / 超时 / 预算 / 视觉
├── image.py        # ComfyUI / 渠道 / 额度 / NAI / LoRA
├── rag.py          # RAG 开关 / 向量库 / 重排 / 角色记忆
├── web.py          # 端口 / 页面 / 画廊 / 管理远端
├── qq.py           # QQ / 通知
├── persona.py      # 角色扮演模型白名单
└── runtime.py      # 上下文 / 记忆 / 看门狗
```

行数口径：600 行开始规划，1,000 行硬警戒，1,200 行内可容忍，1,500 行硬 blocker。
不为了凑行数拆 `config2.py`；拆的是职责。

## 当前事实

- `app/config.py` 顶层定义到第 1,208 行（AST），约 106 处 import 语句。
- 它不是“一个配置”，是 9 个域混在一起：路径、环境变量、LLM、视觉、图像、
  RAG、Web、QQ、角色扮演、看门狗。
- `BASE_DIR` 用 `__file__` 算，直接复制文件会把所有路径算错。

## 归位表

| 目标模块 | 从 `app/config.py` 搬什么 |
|---|---|
| `paths.py` | `BASE_DIR`、`ROOT_DIR`、`_TEST_STATE_DIR`、`_is_test_process`、`state_path`、`SKILLS_DIR`、`WEB_DIR`、`DATA_DIR`、`DOCUMENTS_DIR`、`VECTOR_STORE_DIR`、`AGENTS_DIR` |
| `env.py` | `.env` 加载、`_env_path`、`_env_bool`、`_env_list` |
| `llm.py` | `LLM_PROVIDER`、`PERSONA_*`、`VOLC_*`、`DEEPSEEK_*`、`SCNET*`、`MIMO_*`、`LLM_FALLBACK_CHAIN`、超时/重试、`OLLAMA_*`、`LLAMA_*`、`PROVIDERS`、`provider_vision`、`SEARCH_*`、`physical_budget`、`CUSTOM_PROVIDERS_FILE`、`load_custom_providers` |
| `image.py` | `ENABLE_IMAGE_GEN`、`COMFYUI_*`、`IMAGE_*`（含 `IMAGE_AUDIT_*`）、`COMFY_*`、`QWEN_COOLDOWN`、`DISABLED_IMAGE_SKILLS`、`NAI_*` |
| `vision.py` | `VISION_PROVIDER`、`VISION_MODEL`、`OLLAMA_VISION_*`、`VISION_TIMEOUT`、`VISION_MAX_EDGE`、`VISION_PROMPT_MAX` |
| `rag.py` | `rag_enabled`、`PERSONA_RAG*`、`PERSONA_LORE_*` |
| `web.py` | `AGENT_PORT`、`ADMIN_ALLOW_REMOTE*`、`_parse_gallery_roots`、`GALLERY_*`、`WORKSHOP_HTML` |
| `card.py` | `BUILTIN_CARD_IDS` |
| `qq.py` | `QQ_*`、`NOTIFY_*`、`WEBUI_PORT` |
| `watchdog.py` | `WATCHDOG_SILENCE_*`、`WATCHDOG_COMFY_ENABLED` |
| `persona.py` | 角色扮演模型白名单与 `persona_model_ids` / `persona_model_of` |
| `context.py` | `MAX_TURNS`、`CONTEXT_BUDGET`、`_PROVIDER_CONTEXT_WINDOW`、`physical_budget`、`CONTEXT_MAX_TURNS`、`MEMORY_TURN_DIGEST_CHARS` |
| `runtime.py` | 非密钥运行期 JSON 设置（管理页读写）；不再承担轮次/预算常量 |

`DEFAULT_AGENT_ID` 归 `runtime.py` 或 `paths.py`，按调用方复核。

## 迁移顺序

1. `paths.py` + `env.py`：所有模块都依赖，先搬最底层的。
2. `llm.py`：最大一块，单独搬，单独测。
3. `image.py`。
4. `vision.py`（通用识图；生图审核仍归 image）。
5. `rag.py`。
6. `web.py` + `card.py`。
7. `qq.py`（`WEBUI_PORT`、`NOTIFY_*` 一并归入；`runtime.py` 已提前完成）。
8. `watchdog.py`。
9. `persona.py`（角色模型默认值与白名单）。
10. `context.py`（轮次/预算/物理窗口）。
11. `search.py`、`agent.py`、自定义中转站归入 `llm.py`。
12. `__init__.py` 改成显式导出；`app/config.py` 最后变成 shim。

每一步：旧 `app/config.py` 只减不增；新模块成为该域的唯一实现；跑
`test_core_config.py` + 相关 feature 测试；不允许两边各留一份同名常量。

## 兼容策略

- 迁移期 `app/core/config/__init__.py` 用惰性 `__getattr__` 指向旧实现，
  避免 `app.config` 与包初始化循环 import。
- 旧代码继续 `from app import config`；新代码用
  `from app.core.config.llm import ...` 这种域内路径。
- 每次只搬一个域；搬完把旧文件里的定义删掉，不能留两份。

## 提示词拆分

提示词进 `app/prompts/` 主文件夹，按 feature/用例分文件：

```text
prompts/comic/story.py       # 编剧
prompts/comic/panel.py       # 分镜
prompts/comic/character.py   # 角色一致性
prompts/image/generate.py    # 生图主提示词
prompts/image/audit.py       # 审核
prompts/persona/roleplay.py  # 扮演
prompts/persona/memory.py    # 记忆压缩
```

一个文件一个场景；不做一个 `prompts.py`，更不把 20 个场景塞进一个字符串。

## 验收

- `app/core/config/` 下每个模块只做一个域。
- `app/config.py` 最终 ≤ 100 行，只做 re-export。
- 旧 import 全部可用，`test_core_config.py` 通过。
- `compileall` + 定向测试 + 手机端 84 条通过。
- 没有同名常量两份实现。
