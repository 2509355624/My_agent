# app/core

负责：配置、路径、日志、安全、统一错误。

不负责：业务规则、HTTP 路由、外部系统调用、任何 feature 的内部实现。

约束：

- `core` 不 import `features`、`gateways`、`workers`。
- feature 可以 import `core`；共享代码只有出现第二个调用方后才抽到这里。
- 不建万能 `utils/`；路径进 `paths.py`，日志进 `logging.py`，鉴权/限流进 `security.py`。

状态：`config/` 包已建立，`paths.py`、`env.py`、`llm.py`、`persona.py`、
`context.py`、`search.py`、`agent.py`、`image.py`、`vision.py`、`rag.py`、
`web.py`、`card.py`、`qq.py`、`watchdog.py`、`runtime.py` 已从旧
`app/config.py` 拆出；`cancel.py`（跨域取消信号）、`logsetup.py`（各入口日志初始化）
也归 core；`usage.py`（token 用量流水/汇总）、`provider_store.py`（自定义中转站
持久化）、`model_catalog.py`（模型清单缓存）、`skills.py`（`skills/` 目录加载/
清单/校验）同样归 core。`runtime.py` 提供管理页可热更新的非密钥设置，
`llm.py` 提供统一 LLM 降级链/超时/重试/预算的默认值、校验与自定义中转站合并，
`persona.py` 管角色扮演默认模型和玩家白名单，`context.py` 管轮次、token 预算
和物理窗口封顶，`search.py` 管联网搜索，`agent.py` 管默认 agent ID，
`image.py` 管生图渠道、ComfyUI 水位和生图审核阈值，`vision.py` 管通用识图
参数，`rag.py` 管知识库开关、角色记忆向量库和世界书预算，`web.py` 管端口/
管理访问/画廊/工坊入口，`card.py` 管内置卡保护名单，`qq.py` 管 QQ 接入、
回复策略、主动接话、长期记忆与掉线通知，`watchdog.py` 管静默判据和 ComfyUI
看门狗分支；调用逻辑仍在 `app/vision.py`、`app/image_audit.py`、
`app/persona_rag.py`、`app/rag/`、`app/memory.py`、`app/qq_bot.py`、
`app/notify.py`、`app/watchdog.py` 与 `app/image_jobs.py`。配置包已显式导出，
旧 `app/config.py` 只剩兼容门面和自定义渠道启动合并。
`admin_auth.py`（管理页登录/会话/改密）也已归 `core`；`friend_auth.py` 属 5175
群友网关，待随 `gateways` 一并搬入。
迁移顺序见 `docs/project-skeleton.md` 第 9 节。
