# 2026-11-17 config 兼容层

> 状态：**已被 `2026-11-17-config-split.md` 取代**。本文记录第一步 facade 的决策与回滚点；
> `paths` / `env` 已从旧 `app/config.py` 拆出，后续按域继续拆。

## 目标

建立 `app/core/config.py` 作为新的公开入口；旧 `app/config.py` 继续作为实现，
保证 106 处旧 import 和运行时路径不变。

## 非目标

- 不把 1,078 行的 `app/config.py` 整块复制进 `app/core/config.py`。
- 不重写配置项、不改 `.env` 读取逻辑、不改 `state_path()` 的测试隔离。
- 不一次改 106 个调用方。

## 现状与影响面

- `app/config.py` 1,078 行，超过 1,000 行硬限，属于老 God 文件。
- `BASE_DIR = dirname(dirname(__file__))`。复制到 `app/core/config.py` 后，
  `__file__` 深一层，`BASE_DIR` 会变成 `app/`，`WEB_DIR`、`DATA_DIR`、
  `SKILLS_DIR`、`.env` 路径全错。
- `state_path()` 依赖 `BASE_DIR` 和测试进程判定；不能随手改。
- 旧 import 约 106 处，直接改调用方风险大。

## 方案

**Facade 兼容层**：

1. 新增 `app/core/config.py`，内部 `from app.config import *`。
2. 旧 `app/config.py` 不动实现，只作为临时实现层。
3. 新代码统一从 `app.core.config` import；旧代码继续 `app.config`。
4. 加 `tests/test_core_config.py`，对比两边的 `BASE_DIR`、`ROOT_DIR`、
   `WEB_DIR`、`DATA_DIR`、`SKILLS_DIR`、`state_path()`。
5. 等 facade 稳定后，再按“路径/LLM/图像/RAG/前端”拆 `app/config.py`，
   最后把 `app/config.py` 反转成 shim。

## 数据与兼容性

- 不新增配置项，不删除配置项。
- 不改任何 URL、返回结构、环境变量名。
- `app.core.config` 与 `app.config` 暴露的名字必须一致；`import *` 是临时兼容，
  后续拆分时改成显式导出。

## 测试计划

- 新增 `tests/test_core_config.py`：4 条路径一致性断言。
- `test_repo_layout.py`：3 条。
- `test_project_skeleton.py`：3 条。
- 手机端冒烟：84 条。
- `compileall app`。

## 回滚

删除 `app/core/config.py` 和 `tests/test_core_config.py` 即可；旧 `app/config.py`
完全未改，回滚无副作用。

## 风险

- `import *` 会把 `os`、`sys`、`tempfile`、`load_dotenv` 等也带进来；这是临时
  facade 的已知副作用，等拆分时改显式导出。
- `app.core.config` 目前是“新入口指向旧实现”，不是最终形态；但它是安全的
  第一步，不会同时引爆路径和 106 个 import。
