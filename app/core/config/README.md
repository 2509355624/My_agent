# app/core/config

按职能拆分的配置域。旧 `app/config.py` 仍是兼容门面：新代码优先从这里
import，未搬完的配置域暂时由 `__init__.py` 的惰性 `__getattr__` 回落到旧文件。

| 文件 | 负责 |
|---|---|
| `paths.py` | 仓库根、`web/`、`data/`、`documents/`、`state_path()`、测试进程判断 |
| `env.py` | `.env` 加载、`_env_bool`、`_env_list` |
| `image.py` | ComfyUI 地址/启动参数、生图渠道、显存/内存水位、生图审核阈值、NAI 开关 |
| `rag.py` | `rag_enabled()`、角色长期记忆向量库、世界书预算/语义补条开关 |
| `vision.py` | 通用识图 provider/模型、Ollama 视觉参数、识图超时/缩放/提示词上限 |
| `llm.py` | provider/模型表、降级链、请求超时、重试、TTL、`get_llm_settings`、`validate_llm_patch`、`update_llm_settings` |
| `runtime.py` | 非密钥运行期设置 JSON；`AGENT_RUNTIME_SETTINGS` 可覆盖路径；mtime 缓存、原子写 |
| `__init__.py` | 显式导出已拆域；未搬完的名字惰性回落旧 `app.config` |

边界：

- `llm.py` 只放默认值和校验，不直接发请求；请求仍由 `app/llm.py` 负责。
- `runtime.py` 只存非密钥、可热更新的设置；密钥仍留在 `.env`。
- 新增配置先判断属于哪个域，不要继续往旧 `app/config.py` 堆。
- 管理页通过 `GET/PUT /api/llm/settings` 读写 LLM 运行期设置，落盘后下一次
  请求即生效，不需要重启进程。
