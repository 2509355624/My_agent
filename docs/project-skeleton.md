# 项目骨架 v1.0

> 配套规范：`docs/development-standard.md`、`docs/architecture.md`、`docs/adr/0001-flask-modular-monolith.md`。
> 目标：根目录少文件、`app/` 按业务分、每个 feature 内部职责固定、老代码可兼容迁移。

---

## 1. 根目录

最终只留这些“每天会碰”的文件：

```text
my_agent_restructure/
├── agent.py              # 5174 主站入口
├── admin_app.py          # 5176 管理网关入口
├── friend_app.py         # 5175 群友网关入口
├── AGENTS.md             # AI 开工契约（短）
├── README.md             # 给人看的项目说明
├── pyproject.toml        # 项目元数据、测试/静态检查命令
├── requirements.txt
├── app/                  # 业务代码
├── web/                  # 静态 HTML（保持现状）
├── tests/                # 测试
├── tools/                # 脚本、运维、legacy
├── docs/                 # 规范、架构、ADR、plan、基线
├── scripts/windows/      # .bat 启动脚本（后续从根目录搬）
├── artifacts/            # 截图、日志、对比图（后续从根目录搬）
└── data/ state/          # 运行数据（gitignore）
```

根目录不再放：`_probe_*.py`、`_tmp_*.py`、`*.log`、`*_before_*.html`、一次性对比图。
这些分别进 `tools/legacy/`、`artifacts/`、`web/_backup/`（确认无引用后再搬）。

`clean_zombies.py`、`export_presets.py`、`import_presets.py`、`napcat_qr.py`、
`restart_servers.py` 这些运维脚本，后续进 `tools/ops/`；根目录保留 3 个进程入口即可。

---

## 2. `app/` 骨架

```text
app/
├── __init__.py
├── bootstrap.py              # create_app(profile)，只做装配
├── main.py                   # 兼容 shim：app = create_app("main")
├── core/
│   ├── __init__.py
│   ├── config/               # 配置按职能拆，不做一个万能 config.py
│   │   ├── __init__.py       # 公开入口；最终只做显式 re-export
│   │   ├── paths.py          # BASE_DIR / WEB_DIR / DATA_DIR / state_path
│   │   ├── env.py            # .env 加载、_env_bool / _env_list
│   │   ├── llm.py            # provider / 模型 / 降级链 / 超时 / 预算
│   │   ├── image.py          # ComfyUI / 渠道 / 额度 / NAI / LoRA
│   │   ├── rag.py            # RAG 开关 / 向量库 / 重排
│   │   ├── web.py            # 端口 / 页面 / 画廊 / 管理远端
│   │   ├── qq.py             # QQ_* / 通知
│   │   ├── persona.py        # 角色扮演模型白名单
│   │   └── runtime.py        # 运行期可热更新设置（JSON，非密钥）
│   ├── logging.py            # 原 logsetup.py
│   ├── security.py           # 令牌、HMAC、来源闸、限流
│   ├── errors.py             # 统一错误类型与响应
│   └── README.md
├── features/
│   ├── chat/
│   ├── image/
│   ├── comic/
│   ├── persona/
│   ├── rag/
│   ├── novel/
│   ├── card/
│   ├── qq/
│   └── admin/
├── prompts/                  # 提示词主文件夹，按 feature + 用例分文件
│   ├── README.md
│   ├── chat/
│   ├── image/
│   ├── comic/
│   ├── persona/
│   ├── rag/
│   ├── novel/
│   ├── card/
│   └── admin/
├── gateways/
│   ├── __init__.py
│   ├── admin_gateway.py      # 原 admin_app.py 主体
│   ├── friend_gateway.py     # 原 friend_app.py 主体
│   └── README.md
├── integrations/
│   ├── __init__.py
│   ├── comfy/                # 提交、轮询、输出、状态
│   ├── llm/                  # provider、降级链、流式
│   ├── qq/                   # OneBot 协议
│   ├── volc/                 # 火山/豆包
│   └── README.md
├── persistence/
│   ├── __init__.py
│   ├── json_store.py         # 包住现有 state/*.json
│   ├── job_ledger.py
│   ├── db.py                 # 以后 SQLAlchemy
│   ├── migrations/           # 以后 Alembic
│   └── README.md
├── workers/
│   ├── __init__.py
│   ├── image_worker.py
│   ├── comic_worker.py
│   ├── watchdog.py
│   └── README.md
└── web/
    ├── __init__.py
    ├── pages.py              # 静态页面路由、首页、作品页
    └── README.md
```

`bootstrap.py` 只做三件事：建 Flask app、按 profile 注册 blueprint、挂公共钩子。
不写业务、不读业务文件、不写 89 条路由。

---

## 3. 一个 feature 里放什么

以 `app/features/image/` 为例：

```text
app/features/image/
├── __init__.py       # 只导出公开 API
├── routes.py         # Flask Blueprint：/api/gen、/api/image/*
├── service.py        # 业务用例：提交、审核、排队、取消
├── repository.py     # 读写 state/、任务账本
├── jobs.py           # 后台任务：轮询 ComfyUI、落盘、通知
├── models.py         # dataclass / DTO（需要时）
└── README.md         # 边界、状态文件、外部依赖、怎么测
```

提示词不放在 feature 内部，统一放 `app/prompts/` 主文件夹（见第 2 节目录树）。

不是每个 feature 都要建全。规则：

- `routes.py` 可以 import Flask。
- `service.py` 不 import Flask，不碰 `request` / `jsonify`。
- `repository.py` 不做业务判断，不做 HTTP。
- `jobs.py` 不处理 HTTP，只处理后台任务。
- `models.py` 只放数据结构，不放业务逻辑。
- `__init__.py` 只做公开导出，不写代码。

### 文件职责示例

```python
# routes.py
bp = Blueprint("image", __name__, url_prefix="/api/image")

@bp.post("/generate")
def generate():
    payload = request.get_json(force=True)
    result = service.submit(payload)
    return jsonify(result)
```

```python
# service.py
def submit(payload: dict) -> dict:
    req = validate_request(payload)
    job = repository.create_job(req)
    jobs.enqueue(job.id)
    return {"ok": True, "job_id": job.id}
```

```python
# repository.py
def create_job(req: GenerateRequest) -> Job:
    ...
```

```python
# jobs.py
def run(job_id: str) -> None:
    ...
```

代码不在这一步写；这里只定职责。实际迁移时一个函数一个函数搬。

---

## 4. 当前模块的起点映射

这是**迁移起点**，不是最终判决；搬的时候按实际 import 再调整。

| feature | 当前主要模块（起点） | 目标职责 |
|---|---|---|
| `core` | `config.py`、`logsetup.py`、`admin_auth.py`、`friend_auth.py` | 配置、路径、日志、安全、错误 |
| `chat` | `agent.py`、`agent_prompt.py`、`memory.py`、`longterm.py`、`llm.py`、`vision.py`、`search_agent.py`、`interject.py`、`chat_bg.py`、`confirm_gate.py`、`cancel.py` | 对话循环、提示词、记忆、模型路由 |
| `chat` 迁移后 | `features/chat/background.py`、`longterm.py`、`recent.py`、`interject.py`、`confirm_gate.py`、`memory.py`、`agent_prompt.py` 已接线；`agent.py`、`llm.py` 等仍待迁 | 背景图、长期记忆、近期窗口、主动插话、确认门、会话读写、系统提示词先归位；对话主循环/模型层待拆 |
| `image` | `image_jobs.py`、`image_audit.py`、`image_log.py`、`image_migrate.py`、`image_out.py`、`image_quota.py`、`image_review.py`、`direct_gen.py`、`gen_api.py`、`gen_params.py`、`gen_steps.py`、`comfy_*.py`、`lora_*.py`、`anima_random.py`、`nai.py`、`outfit_api.py` | 生图、渠道、队列、审核、LoRA |
| `image` 迁移后 | `features/image/` 的 `log/out/quota/audit/review/migrate/delivery/gen_steps/default_loras/comfy_status/comfy_launch/comfy_progress/comfy_out/job_ledger/random_tags/gen_params/char_guard/routes/outfit_api/comfy_gate/comfy_src/lora_lib/lora_presets/lora_slots/lora_bot/char_loras/anima_random/nai` 已接线；`image_jobs.py` 暂留根目录 | 编号账本、发图副本、额度、审核、复核、旧图迁移、caption 投递、步数/默认 LoRA、ComfyUI 状态/启动/进度/门禁/源目录、output 分流、任务台账、标签随机、参数规整、角色内容守卫、手动跑图接口、双图换装接口、LoRA 索引/预设/槽位/指令、角色 LoRA、ANIMA 随心配、NAI 渠道；队列仍待拆 |
| `comic` | `comic*.py`、`moss_*.py`、`silver_comic.py` | 漫画、编剧、渲染、额度、作品 |
| `comic` 迁移后 | `features/comic/` 的 `quota/thumb/prompt_suffix/delivery/portrait/story/render_opts/writer/moss_msgs/moss_prompt/moss_series/moss_api/silver_comic/soda_letter/routes` 已接线；漫画主流程暂留根目录 | 漫画额度、缩略图、提示词后缀、交付、头像、故事、渲染参数、编剧、Moss 消息/提示词/连载/接口、Silver 编剧、Soda 排字、工作台接口；分镜/渲染待拆 |
| `persona` | `persona*.py`、`affection*.py`、`galgame.py`、`pregnancy.py`、`achievements.py`、`items.py` | 角色扮演、计费、记忆、查看台 |
| `persona` 迁移后 | `features/persona/` 已接线 `billing`、`adult`、`rag`、`pregnancy`、`items`、`achievements`、`writing_style`、`galgame`；`persona.py` 仍暂留根目录 | 计费、成人状态、长期记忆、怀孕、道具、成就、写法档位、Galgame；人设/查看台待拆 |
| `rag` | `rag/`、`rag_lab.py`、`rerank.py` | 文档问答、切片、检索、重排 |
| `rag` 迁移后 | `features/rag/lab.py`、`rag_client.py`、`rag_daemon.py`、`vector_store.py`、`rerank.py` | 已接线，URL 不变 |
| `novel` | `novel.py`、`novel_api.py`、`novel_writing.py` | 小说模式、世界书、裁定链 |
| `novel` 迁移后 | `features/novel/routes.py` 已接线；`novel.py` 暂留根目录 | 小说接口；开书/续写/裁定待拆 |
| `card` | `card_api.py`、`card_bot.py`、`card_maker.py` | 角色卡生成、封面、五框 |
| `card` 迁移后 | `features/card/routes.py`、`bot.py`、`service.py` | 已接线，URL 不变 |
| `qq` | `qq_api.py`、`qq_bot.py`、`qq_bill.py`、`qq_names.py`、`qq_status.py`、`friend_api.py`、`friend_site.py`、`guest*.py` | QQ 事件、账单、群友页内核 |
| `qq` 迁移后 | `features/qq/bill.py`、`names.py`、`status.py`、`notify.py`、`stickers.py` 已接线；`qq_api.py`、`qq_bot.py` 暂留根目录 | QQ 账单、名字缓存、状态快照、掉线/扫码通知、表情收藏；消息发送/事件循环待拆 |
| `admin` | `admin_site.py`、`usage.py`、`job_ledger.py`、`provider_store.py`、`model_catalog.py`、`agents.py`、`skills.py` | 用量、账本、预设、配置、后台 |
| `admin` 迁移后 | `features/admin/site.py`、`routes.py` 已接线；`admin_auth.py` 仍在 core | 后台入口与 `/api/characters` |
| `gateways` | `admin_app.py`、`friend_app.py` | 反代、登录、限流、CF 头 |
| `integrations` | `comfy_*`、`llm.py`、`vision.py`、`nai.py`、`qq_api.py` | 外部系统适配 |
| `persistence` | `agents.py`、`memory.py`、`longterm.py`、`job_ledger.py`、`recent.py`、`image_log.py` | JSON/SQL 存储 |
| `workers` | `image_jobs.py`、`comic_delivery.py`、`watchdog.py` | 后台任务、看门狗 |

注意：

- 一个模块可能暂时被两个 feature 引用；先搬“主职责”，另一处留兼容 shim，不要一次切干净。
- `comfy_*` 既是 `image` 的业务依赖，也是 `integrations` 的外部适配；先按“谁调用”拆，不按文件名拆。
- `main.py` 不是 feature，是装配层 + 历史路由；最终只剩兼容 shim。

---

## 5. 入口与 profile

| 入口 | profile | 注册什么 |
|---|---|---|
| `agent.py` → 5174 | `main` | 全部 Web feature + `web/pages` + 公共钩子 |
| `admin_app.py` → 5176 | `admin` | 只注册 admin gateway + admin_auth |
| `friend_app.py` → 5175 | `friend` | 只注册 friend gateway + friend_auth + 最小静态页 |
| QQ 机器人 | 无 Flask | `app/features/qq` + `integrations/qq` |
| 看门狗 | 无 Flask | `app/workers/watchdog.py` |

`create_app(profile)` 不读环境变量做分支判断；profile 由入口显式传入。
这样测试可以 `create_app("main")`，不需要真的起 5174。

---

## 6. 兼容层规则

1. 旧路径不删。例：`app/config.py` 先变成 shim：
   ```python
   # standard-exception: PY-04 兼容 shim
   from app.core.config import *  # noqa: F401,F403
   ```
   等 83 个调用方迁完再删。
2. 模块变包：
   - `app/persona.py` → `app/features/persona/__init__.py`
   - 旧 `app/persona.py` 保留 shim，显式 re-export 公开名字。
   - 不要一次改 20 个调用方。
3. `app/main.py` 最终只保留：
   ```python
   from app.bootstrap import create_app
   app = create_app("main")
   ```
4. 每次只搬一个模块；搬完跑旧测试，旧 URL 不改。
5. 兼容 shim 也纳入测试：`tests/contract/test_import_compat.py` 断言旧路径还能 import。

---

## 7. 测试目录

旧测试先留在 `tests/` 根目录，保证可运行；新增测试按新结构放：

```text
tests/
├── unit/
│   └── features/image/test_service.py
├── integration/
│   └── features/image/test_routes.py
├── contract/
│   ├── test_import_compat.py
│   └── test_public_page_guards.py
├── e2e/
│   └── test_showcase_smoke.py
├── legacy/                 # 后续把旧测试按模块挪进来
└── README.md
```

规则：

- 单测不连真实 ComfyUI/LLM/QQ。
- 集成测试用 `tmp_path` 或 `tempfile`，不写真实 `state/`。
- 页面契约测试保留守卫字符串（`friendNote`、`id="lsScript"`、`<title>`）。
- 全量红项基线进 `docs/tests/baseline-<日期>.md`，只对比新增红项。

---

## 8. 文件拆分规则

- 新文件 600 行开始拆；1,000 行 blocker。
- 拆分按职责/用例，不按 `part1.py`、`part2.py`。
- 一个文件只放一个 feature 的一个职责；跨 feature 的共用逻辑放 `core/`，等第二个调用方出现再抽。
- 老 God 文件冻结增长：`persona.py`、`main.py`、`direct_gen.py`、`image_jobs.py`、`qq_bot.py` 不允许继续加新职责。
- 拆分顺序：先按“读/写/后台/HTTP”分，再按业务用例分；不要为了行数把相关代码切碎。

---

## 9. 建骨架的顺序（不写业务）

### 第一步：只建空包和 README

- 建 `app/core/`、`app/features/{chat,image,comic,persona,rag,novel,card,qq,admin}/`、
  `app/gateways/`、`app/integrations/`、`app/persistence/`、`app/workers/`、`app/web/`。
- 每个包只放 `__init__.py` 和该目录的 `README.md`，不 import 现有代码。
- 跑 `test_repo_layout` + 84 条手机端冒烟，确认零影响。

### 第二步：`app/core/config/` 按域拆

- 先搬 `paths.py`（绝对路径、`state_path`）和 `env.py`（`.env`、`_env_bool`）。
- `app/config.py` 从新模块 import，旧名字继续可用；不一次改 106 个调用方。
- 测试锁 `BASE_DIR`、`ROOT_DIR`、`WEB_DIR`、`DATA_DIR`、`state_path()`。
- 已完成 `paths` / `env` / `llm` / `persona` / `context` / `search` / `agent` /
  `runtime` / `image` / `vision` / `rag` / `web` / `card` / `qq` / `watchdog`：
  `llm.py` 管降级链、超时、重试、TTL、预算与自定义中转站合并；
  `persona.py` 管角色扮演默认模型和玩家白名单；`context.py` 管轮次、token
  预算和物理窗口封顶；`search.py` 管联网搜索；`agent.py` 管默认 agent ID；
  `runtime.py` 存 `data/runtime_settings.json`，管理页通过
  `/api/llm/settings` 读写，下一次请求即生效；`image.py` 管 ComfyUI 地址/启动
  参数、生图渠道、显存/内存水位、生图审核阈值、NAI 开关；`vision.py` 管通用
  识图 provider/模型、超时、缩放、提示词上限；`rag.py` 管知识库开关、角色
  记忆向量库、世界书预算；`web.py` 管主站端口、管理访问范围、画廊根目录、
  工坊入口；`card.py` 管内置卡保护名单；`qq.py` 管 QQ 接入、回复策略、主动
  接话、长期记忆和掉线通知；`watchdog.py` 管静默判据和 ComfyUI 分支总闸。
- `app/core/config/__init__.py` 已显式导出全部域；旧 `app/config.py` 只剩兼容
  门面和自定义渠道启动合并，配置拆分收尾。

### 第三步：`bootstrap.py` + 入口

- `create_app(profile)` 先只注册现有 blueprint，不拆路由。
- `app/main.py` 变 shim；`agent.py` 调 `run()`。
- 跑全部现有测试，确认入口不变。

### 第四步：搬网关

- `admin_app.py` / `friend_app.py` 主体进 `app/gateways/`；根入口只留 `run()`。
- 补登录、转发、CF 头、限流 contract test。

### 第五步：搬 `main.py` 路由

- 按前缀一组一组搬，先搬 blueprint 已独立的（`showcase`、`gallery`、`gen`、`rag`、`persona`）。
- 每搬一组，旧 URL 不改，跑该组测试。

---

## 10. 骨架验收标准

- 根目录 Python 仍只允许 9 个（后续降到 3 个入口 + ops）。
- 空包不 import 旧模块，不影响 84 条手机端冒烟。
- `compileall app` 通过。
- `test_repo_layout` 通过。
- 新目录有 README，接手的人能看懂“这个包负责什么、不负责什么”。
