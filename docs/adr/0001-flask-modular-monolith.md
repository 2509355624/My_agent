# ADR 0001：继续 Flask，做模块化单体

- 状态：已接受
- 日期：2026-11-17
- 决策人：项目 owner + AI 协作

## 背景

现有系统是 Flask + 原生 HTML/CSS/JS，116 个 `app/` 模块、约 70,159 行；
165 个测试文件、约 65,761 行。它的主体不是 CRUD 网站，而是：

- 常驻 Agent 运行时（对话循环、工具协议、多模型路由）
- 生图/漫画后台任务（ComfyUI 轮询、队列、审核、落盘）
- QQ 机器人进程
- 5174 主站、5175 群友网关、5176 管理网关
- 大量静态 HTML 页面，无构建步骤

维护困难的主因是边界不清：`config` 被 83 个模块 import，`main.py` 89 条路由，
`persona.py` 441 KB，`direct_gen.py` 228 KB。

## 决策

继续使用 Flask，按 feature 拆成模块化单体；不整体迁移 Django。

- 入口拆为 `agent.py` / `admin_app.py` / `friend_app.py`，都调用
  `app.bootstrap.create_app(profile)`。
- 代码按 `app/features/<domain>/` 分，内部 `routes/service/repository/jobs`。
- 旧 import 路径保留兼容 shim，逐模块搬家。
- 数据库后续用 SQLAlchemy 2.0 + Alembic，渐进接入。
- Django 只保留为未来“独立管理面服务”的选项，不替换运行时。

## 备选方案

### 方案 A：整体迁移 Django（否决）

优点：ORM、migrations、admin、settings、URL 分层开箱即用。

否决原因：

1. 116 个模块、89 条路由、165 个测试文件都要换一层；风险与工作量接近重写。
2. Django 的 request/response 生命周期不解决长驻任务、QQ 事件、ComfyUI 轮询；
   仍需自建 worker/management command。
3. 如果 God 模块不先拆，迁到 Django 后仍会是 `views2.py` / `models.py` 大文件，
   只是换了框架名字。
4. 参考 `dj-xiyou` 已经证明：Django 不自动带来模块化，`goku` 仍有 183 个 py、
   `views2.py` 68 KB、`models.py` 64 KB。

### 方案 B：推倒重写（否决）

3–6 个月，且现有业务规则、测试、页面细节会大量丢失；风险极高。

### 方案 C：Flask 模块化单体（接受）

保留运行时、页面、URL、测试；按 feature 补边界和兼容层，逐模块绞杀。
数据库用 SQLAlchemy/Alembic，不绑死框架。

## 后果

正向：

- 业务不中断，旧 URL/旧 import 可逐步兼容。
- 先解决 God 文件、文字墙页面、跨层 import，再谈框架。
- 未来若真需要 Django，可在边界清楚后作为独立管理面接入。

代价：

- 需要维护一段时间的兼容 shim。
- 需要严格执行目录/依赖/测试规范，否则会退回旧形态。
- 数据库迁移是另一条并行工作，不是换框架就能自动完成。

## 验证方式

- `tests/test_repo_layout.py` 守住根目录结构。
- 每个阶段跑定向测试 + 全量对比“新增红项”。
- 旧 import、旧 URL、旧返回结构保持兼容，直到调用方迁移完。
