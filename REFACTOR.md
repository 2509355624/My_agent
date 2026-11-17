# my_agent 重构基线

> 2026-11-17 建立。分支 `my_agent_restructure`，工作树 `D:\AI\my_agent_restructure`。
> 目标是先把结构变清楚，不改变现有行为；当前 `main` 分支继续可用。

## 一、现状数据

| 项目 | 数量 |
|---|---|
| 仓库内已跟踪文件 | 631 |
| `app/` Python 模块 | 116 个 / 70,159 行 |
| 测试文件 | 165 个 / 65,761 行 |
| 仓库根目录已跟踪 Python | 70 个，绝大多数是 `_probe_*`、`_verify_*`、`_tmp_*` 诊断脚本 |
| 当前工作树体积 | 旧目录约 18.7 GB（含模型、状态、日志）；新工作树约 107 MB（只含 git 跟踪文件） |
| 测试基线 | 全量 `unittest discover` 约 155 条既有红项，不能把“0 红”当目标 |

## 二、耦合热点

按 `app/` 内部 import 统计：

| 模块 | 被多少个模块 import |
|---|---|
| `config` | 83 |
| `qq_api` | 24 |
| `llm` | 22 |
| `image_jobs` | 21 |
| `tools` | 21 |
| `agents` | 15 |
| `skills` | 15 |

单个模块的依赖数量：

| 模块 | 内部 import 数 |
|---|---|
| `main` | 43 |
| `qq_bot` | 32 |
| `direct_gen` | 22 |
| `image_jobs` | 19 |
| `persona` | 14 |
| `comic_api` | 13 |
| `persona_api` | 13 |
| `comic` | 12 |
| `gen_api` | 12 |

结论：不是“文件放得乱”这么简单，`config`、`qq_api`、`llm`、`image_jobs` 是事实上的全局单例，直接搬文件会把半个系统一起拽动。

## 三、难度判断

| 做法 | 难度 | 预计工作量 | 风险 |
|---|---|---|---|
| 只整理目录、临时脚本、测试命名 | 低 | 2–4 天 | 低 |
| 建 `app/core` / `app/infra` 骨架，旧路径做兼容层 | 中 | 1–2 周 | 中 |
| 拆 `config` / `qq_api` / `llm` / `image_jobs` 的全局耦合 | 高 | 3–5 周 | 高，最容易出隐性回归 |
| 拆 `main` / `qq_bot` / `direct_gen` / `persona` | 高 | 4–8 周 | 高，入口和路由多 |
| 测试按模块重整、补契约测试 | 中高 | 3–4 周 | 中，165 个文件互相依赖共享临时目录 |
| 推倒重写整个系统 | 很高 | 3–6 个月 | 极高，不建议 |

更现实的总量：**分阶段重构约 2–4 个月（按每天能投入的时长浮动）**，不是一两周能“清爽”的工程。先把边界和兼容层做出来，收益最快。

## 四、框架决策（2026-11-17）

**继续用 Flask，做模块化单体；现在不换 Django。**

- 现状主体是常驻 Agent + 后台生图/漫画任务 + QQ 机器人 + 两个网关，不是 CRUD 网站。Django 的 ORM/admin 有 value，但不会自动解决 God 模块和进程拓扑。
- 参考 `dj-tianqi` 只借分层：settings 分环境、views 只做 HTTP、services 放业务、repositories 管存储；不借“换框架”。
- 以后要数据库，用 SQLAlchemy 2.0 + Alembic 渐进接入；要做复杂后台，可以另起一个 Django 管理面服务共享数据库，不替换运行时。
- 完整目标目录、依赖规则、部署形态、迁移顺序见 [`docs/architecture.md`](docs/architecture.md)。

## 五、推荐方案：绞杀者式重构，不推倒重写

原则：

1. 每次只搬一个模块，旧 import 路径保留兼容层。
2. 行为不变，URL 不变，`web/` 不动。
3. 每个模块单独提交，提交前跑同一组测试并对比红项清单。
4. 不在重构分支里顺手改业务逻辑。

目标结构（先骨架，后搬家；完整版见 `docs/architecture.md`）：

```text
app/
  bootstrap.py   # create_app(profile)
  core/          # config、路径、日志、安全、错误
  features/      # chat / image / comic / persona / rag / novel / card / qq / admin
  gateways/      # 5175 群友网关、5176 管理网关
  integrations/  # comfy / llm / qq / volc
  persistence/   # json_store / job_ledger / db / migrations
  workers/       # 生图、漫画、看门狗
  web/           # 页面路由与静态挂载
tests/
  unit/
  integration/
  contract/
  e2e/
tools/
  legacy/
  generators/
  ops/
```

### 阶段

**阶段 0：基线（已完成）**
- 新分支 + 独立工作树，旧分支不受影响。
- 新工作树冒烟：84 条手机端测试通过。
- 记录全量测试红项，后续只对比差异。

**阶段 1：卫生清理（已完成第一刀）**
- 根目录 61 个 `_*.py` 已归档：10 个进 `tools/generators/`，51 个进 `tools/legacy/`。
- 根目录只剩 9 个入口/运维脚本；`tests/test_repo_layout.py` 3 条守卫通过。
- 新增 `tools/README.md`、`tests/README.md`、`.editorconfig`，接手的人先看这三份。
- `tests/` 尚未物理搬家，下一步再按模块分组。

**阶段 2：核心骨架 + 兼容层（中风险）**
- 先建 `app/core/config.py`，原 `app/config.py` 改成一行 re-export。
- 同样处理 `logsetup`、`cancel`、`usage`。
- 83 个 import `config` 的模块一个都不改，先让兼容层生效。

**阶段 3：入口与网关（中风险）**
- `admin_app.py` 主体移到 `app/gateways/admin_gateway.py`，根文件只留 `run()`。
- `friend_app.py` 同理。
- 两个网关补 contract test：登录、转发、CF 头剔除、限流。

**阶段 4：拆 `main.py`（高风险）**
- `main.py` 现在 89 条路由，按前缀拆 blueprint：chat、image、persona、comic、页面。
- 每拆一组，旧 URL 和返回结构不变；`main.py` 最终只做装配。
- 这一步才动业务文件，前面阶段不要提前做。

**阶段 5：God 模块变包**
- `persona.py`、`direct_gen.py`、`affection.py`、`comic_agent.py`、`image_jobs.py`、`qq_bot.py` 先变成同名包，`__init__.py` 原样 re-export，调用方不用改。
- 再按会话/计费/记忆/提示词/出图拆内部文件。

**阶段 6：测试分组与 CI**
- 旧测试保持可运行，新增测试按 `unit/integration/contract/e2e` 放。
- 加 CI：结构守卫 + 入口编译 + 手机端冒烟 + 新增单测。
- 兼容层没有调用者后删除。

**阶段 7：数据库**
- SQLAlchemy 2.0 + Alembic，先接 `job_ledger` / `usage`，再评估图片与漫画。
- repository 双读写，对账后再切读，不一次性搬完所有 JSON 状态。

## 六、运行注意事项

新工作树不含被 `.gitignore` 忽略的运行数据。需要联调时，从旧目录复制这些**本机私有**内容，但不要提交：

- `.env`
- `state/`
- `agents/`
- `skills/`
- `data/`
- `config/` 里被忽略的私有配置
- `models/`（体积大，按需软链接或复制）

## 七、对应 Skill

- 代码重构流程用 `coding-playbook`：走“重构 → TDD → verification”这条，不靠即兴改。
- 架构分析用 `coding-playbook/references/repo-qa-evidence.md`，本次数据就是按它的证据口径统计的。
- 目前没有专门的“Python 单体架构重构”Skill；`impeccable` / `unslop` / `modern-web-guidance` 都只管前端视觉，不管后端架构。若以后要固化本项目重构规范，可用 `skill-creator-for-work` 写一个仓库专用 Skill。

## 八、下一步（建议按顺序）

1. 在新分支跑一次全量测试，保存红项清单作为基线。
2. 建 `app/core` 和 `config` 兼容层，先验证 83 个调用点不受影响。
3. 再拆两个网关和 `main.py` 路由。
4. `persona` / `direct_gen` / `image_jobs` 变包后再拆内部。
5. 最后接 SQLAlchemy + Alembic。
