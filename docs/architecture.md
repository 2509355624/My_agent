# 目标架构（2026-11-17）

## 结论先说

**继续用 Flask，做“模块化单体”，现在不换 Django。**

原因不是 Django 不好，而是这个项目的主体不是 CRUD 网站，而是：

- 一个常驻的 Agent 运行时（对话循环、工具协议、多模型路由）
- 一条生图/漫画后台任务链（ComfyUI 轮询、队列、审核、落盘）
- 一个 QQ 机器人进程（OneBot 事件、合并消息、确认闸）
- 两个独立网关（5175 群友页、5176 管理页）
- 大量静态 HTML 页面，没有构建步骤

Django 能解决的是 ORM、迁移、后台管理、URL 分层；它解决不了上面这些“长驻进程 + 后台任务 + 多入口”的问题。现在换 Django，等于把 116 个模块、89 条主站路由、165 个测试文件全部换一层皮，但 `persona.py`、`direct_gen.py`、`image_jobs.py` 这些 God 模块还是会以另一种形式继续存在。**先换框架，不先拆边界，只会把同一堆东西搬到新框架里。**

Django 仍然保留为后手：如果以后真的要做多用户 SaaS、复杂后台、强 ORM 管理，可以把 Django 作为一个**独立的管理/控制面服务**接同一个数据库，而不是替换掉现在的运行时。那是一条低风险路径，不是现在。

---

## 从 `dj-tianqi` 借什么

`dj-tianqi` 值得学的不是 Django 本身，而是它的分层习惯：

| 它的做法 | 我们采用的方式 |
|---|---|
| `tianqi_project/settings/{base,development,production}.py` 按环境拆配置 | `app/core/config.py` + `APP_PROFILE`，测试/开发/生产分开 |
| `views/` 只做请求解析和返回 | 每个 feature 的 `routes.py` 只做 HTTP |
| `services/` 放业务编排 | 每个 feature 的 `service.py` 放用例 |
| `repositories/` 管数据访问 | 每个 feature 的 `repository.py` 管 JSON/SQL |
| `application/chat/` 放复杂流程 | 复杂链路用 `service.py` + 小函数拆，不搞空壳层 |
| `urls.py` 只做路由汇总 | `app/bootstrap.py` 只做 blueprint 注册 |

不照抄的地方：它的根目录仍有 `aop`（71 个 py）、`utils`（57 个 py）这种大口袋。我们不放“万能 utils”，也不做 layer-first 大目录；按**功能**分文件夹，功能内部再分 routes/service/repository。

---

## 目标目录

```text
my_agent_restructure/
├── agent.py                  # 5174 主站入口，只调用 app.bootstrap
├── admin_app.py              # 5176 管理网关入口，只调用 app.bootstrap
├── friend_app.py             # 5175 群友网关入口，只调用 app.bootstrap
├── pyproject.toml            # 项目元数据、测试/静态检查命令
├── README.md
├── app/
│   ├── bootstrap.py          # create_app(profile)：main / admin / friend
│   ├── core/                 # 跨功能基础设施
│   │   ├── config.py         # 原 app/config.py 的新家
│   │   ├── paths.py          # BASE_DIR / WEB_DIR / STATE_DIR
│   │   ├── logging.py        # 原 logsetup.py
│   │   ├── security.py       # 令牌、HMAC、来源闸
│   │   └── errors.py         # 统一错误响应
│   ├── features/             # 按业务功能分，不再按技术层分
│   │   ├── chat/             # 对话、工具协议、记忆
│   │   ├── image/            # 生图、渠道、队列、审核
│   │   ├── comic/            # 漫画、编剧、渲染、额度
│   │   ├── persona/          # 角色扮演、计费、查看台
│   │   ├── rag/              # 文档问答、切片、检索
│   │   ├── novel/            # 小说模式
│   │   ├── card/             # 角色卡
│   │   ├── qq/               # QQ 消息、账单、状态
│   │   └── admin/            # 用量、账本、预设、配置
│   ├── gateways/             # 两个反代网关
│   │   ├── admin_gateway.py  # 原 admin_app.py 的主体
│   │   └── friend_gateway.py # 原 friend_app.py 的主体
│   ├── integrations/         # 外部系统，不写业务
│   │   ├── comfy/            # ComfyUI 提交、轮询、输出
│   │   ├── llm/              # provider、降级链、流式
│   │   ├── qq/               # OneBot 协议
│   │   └── volc/             # 火山/豆包
│   ├── persistence/          # 存储抽象
│   │   ├── json_store.py     # 先包住现有 state/*.json
│   │   ├── job_ledger.py     # 任务账本
│   │   ├── db.py             # 以后 SQLAlchemy 引擎/session
│   │   └── migrations/       # 以后 Alembic
│   ├── workers/              # 后台任务
│   │   ├── image_worker.py
│   │   ├── comic_worker.py
│   │   └── watchdog.py
│   └── web/                  # 页面路由、静态文件挂载
├── web/                      # 现有 HTML 原样保留，不共享全局 CSS
├── tests/
│   ├── unit/                 # 纯函数、service、repository
│   ├── integration/          # Flask test_client + 临时目录
│   ├── contract/             # 页面/接口契约、公网闸
│   └── e2e/                  # 少量关键链路
├── tools/
│   ├── generators/
│   ├── legacy/
│   ├── ops/                  # 启动、重启、清理、二维码
│   └── modern-web-guidance/  # 前端规范参考（git clone，不参与运行）
├── docs/
│   ├── architecture.md       # 本文
│   └── handoff/
├── data/                     # 运行数据（gitignore）
├── state/                    # 运行状态（gitignore）
└── requirements.txt
```

### 一个 feature 内部长什么样

```text
app/features/image/
├── __init__.py
├── routes.py       # Flask Blueprint，只解析参数、调 service、返回 JSON
├── service.py      # 业务用例；不 import flask / request
├── repository.py   # 读写 state/、数据库；不写业务判断
├── jobs.py         # 后台任务；不处理 HTTP
├── models.py       # dataclass / DTO（需要时才有）
└── README.md       # 这个功能的边界、状态、外部依赖（需要时才有）
```

不是每个 feature 都要把所有文件建齐。**先有 `routes.py` 和 `service.py`，需要落盘再抽 `repository.py`，需要后台跑再抽 `jobs.py`。** 不搞为了分层而分层的空文件。

---

## 依赖方向（硬规则）

```text
routes.py  →  service.py  →  repository.py / integrations/
                 ↓
              models.py
```

- `routes.py` 可以 import Flask；`service.py` 不可以。
- `service.py` 不直接读 `request`、不直接 `jsonify`、不直接读文件路径。
- `repository.py` 不判断业务规则，只负责“取/存”。
- `integrations/` 只包外部 API，不写“什么时候该出图”这种业务。
- `core/` 可以被任何层 import；`features/` 之间尽量不互相 import，跨功能走 service 接口。
- 旧的 `app/xxx.py` 先保留为兼容 shim，新代码只从新路径 import。这样 83 个 `import config` 不会一次性炸。

---

## 部署形态：模块化单体，不是微服务

保持现在“进程分开、代码一套”的拓扑，不拆成一堆微服务：

| 进程 | 入口 | 注册的模块 |
|---|---|---|
| 主站 5174 | `agent.py` | chat、image、comic、persona、rag、novel、card、admin、web |
| 群友网关 5175 | `friend_app.py` | friend_gateway + friend_auth + 最小静态页 |
| 管理网关 5176 | `admin_app.py` | admin_gateway + admin_auth |
| QQ 机器人 | `python -m app.processes.qq_bot` | qq feature，不注册 Web |
| 看门狗 | `python -m app.processes.watchdog` | 只做进程/ComfyUI 检查 |

`create_app(profile)` 只注册该 profile 需要的 blueprint。这样“分模块部署”是真的按入口裁剪，不是把全部模块塞进每个进程。

---

## 数据库：用 SQLAlchemy + Alembic，不用 Django ORM

现在没有数据库也能跑，是因为状态都在 `state/`、JSONL、ChromaDB 里。以后要上数据库，建议：

- `SQLAlchemy 2.0` + `Alembic`，Flask 不用换。
- 先建 `app/persistence/db.py`，只在**新表**上用；旧 JSON 继续走 repository，不动存量。
- 优先迁移这四类，收益最大、风险最小：
  1. `job_ledger`：任务账本，天然适合表
  2. `usage_events`：token/费用流水，只追加
  3. `image_assets`：图片元数据、审核状态、归属
  4. `comic_works`：作品、集、章节、发布状态
- `persona` 的会话 JSONL 先不动：它是 append-only、读多写少，贸然进关系库会增加锁和迁移成本。
- 迁移方式：repository 双读写（先写旧存储 + 新表，读旧存储），跑一段时间对账，再切读。不要一次性 `ALTER` 所有状态。

---

## 迁移顺序（从低风险到高风险）

### 阶段 2：核心与配置（下一步）
- 新建 `app/core/{config,paths,logging}.py`。
- `app/config.py` 变成兼容 shim：`from app.core.config import *`。
- `app/logsetup.py` 同理。
- `agent.py` → `app.bootstrap.create_app("main")`。
- 测试锁住：`app.config.BASE_DIR`、`WEB_DIR`、`STATE_DIR` 与旧值一致；旧 import 路径仍可用。

### 阶段 3：入口与网关
- `admin_app.py` 主体移到 `app/gateways/admin_gateway.py`；根文件只留 `run()`。
- `friend_app.py` 同理。
- 两个网关各自加 contract test：登录、转发、CF 头剔除、限流。

### 阶段 4：拆 `main.py`
- `main.py` 现在 89 条路由，先按前缀拆 blueprint：
  - `/api/usage`、`/api/agents`、`/api/session` → `features/chat`
  - `/api/gen/*`、`/api/direct/*` → `features/image`
  - `/api/persona/*` → `features/persona`
  - `/api/comic/*` → `features/comic`
  - 页面路由 → `app/web/pages.py`
- 每拆一组，旧 `main.py` 只保留 `app = create_app(...)` 和兼容注册；测试按 URL 跑，不改断言。

### 阶段 5：God 模块变包
- `app/persona.py`（441 KB）→ `app/features/persona/__init__.py`，先原样 re-export，再按“会话 / 计费 / 记忆 / 提示词”拆。
- `app/direct_gen.py`（228 KB）→ `features/image/direct_gen/`。
- `app/affection.py`、`app/comic_agent.py`、`app/image_jobs.py`、`app/qq_bot.py` 同理。
- **模块变包是兼容的**：`from app.persona import x` 仍然成立，不需要改 20 多个调用方。

### 阶段 6：测试分组与 CI
- `tests/` 按 `unit/integration/contract/e2e` 分组。
- 保留“改前/改后红项对比”，不追求 0 红。
- 加一条 CI：结构守卫 + 入口编译 + 手机端冒烟 + 新增单测。

### 阶段 7：数据库
- 先接 `job_ledger` / `usage`，再评估图片与漫画。
- 每个 repository 有 contract test，JSON 与 SQL 两套实现跑同一组断言。

---

## 不做什么

- 不推倒重写。
- 不现在换 Django。
- 不把 116 个模块按 `views/services/repositories` 三个大口袋重新堆一遍。
- 不做微服务拆分；当前共享队列、共享状态、单机部署，拆服务只会增加运维成本。
- 不为了“架构好看”建空层、空接口、空基类。

---

## 一句话判断

**Django 可以作为未来的独立管理面，不适合作为现在的替换目标。**  
现在最值钱的动作是：把 `config`、入口、`main.py`、几个 God 模块按 feature 拆开，让代码边界先清楚；数据库用 SQLAlchemy/Alembic 渐进接入。等边界清楚了，换不换 Django 都只是“换一层 HTTP 适配”，而不是“重写整个系统”。

具体到根目录、`app/` 每个包、每个 feature 内部文件的职责，见 [`docs/project-skeleton.md`](project-skeleton.md)。
