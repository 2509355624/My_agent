# app/core

负责：配置、路径、日志、安全、统一错误。

不负责：业务规则、HTTP 路由、外部系统调用、任何 feature 的内部实现。

约束：

- `core` 不 import `features`、`gateways`、`workers`。
- feature 可以 import `core`；共享代码只有出现第二个调用方后才抽到这里。
- 不建万能 `utils/`；路径进 `paths.py`，日志进 `logging.py`，鉴权/限流进 `security.py`。

状态：`config/` 包已建立，`paths.py`、`env.py`、`llm.py`、`runtime.py` 已从旧
`app/config.py` 拆出；`runtime.py` 提供管理页可热更新的非密钥设置，`llm.py`
提供统一 LLM 降级链/超时/重试/预算的默认值与校验。其余配置域仍暂留旧文件。
迁移顺序见 `docs/project-skeleton.md` 第 9 节。
