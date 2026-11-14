# 功能展示页（/showcase）需求清单

> 用途：给界面设计 AI（K3.1 Pro）和后续写代码的 AI 看的**技术需求**，不含 UI 细节。
> 界面设计不在这里做，由另一个 AI 负责。

---

## 一、定位

一个**产品功能展示页**（像官网首页），展示「多智能体助手」现有的功能。

- 不写真实姓名
- 不做营销自夸、不以面试为目的
- 就是客观地把「这个机器人有哪些功能、每个功能长什么样、背后怎么运转」摆出来
- 访客（群友 / 任何人）通过公网链接打开，能**现场试用每个功能，并看到功能背后的调用链路**

页脚只放：邮箱 `2509355624@qq.com` + GitHub 链接。**不写真实姓名。**

---

## 二、五个功能区块（页面主体）

整体用「功能平铺」结构，像官网：五个区块依次排开，访客自己滚 / 点哪个看哪个。

| 功能 | 访客能做什么 | 要展示的「链路」 |
|---|---|---|
| **生图** | 选预置场景，真实出一张图 | 提示词准备（可点看 AI 生成的提示词）→ 审核 → 队列排队位置 / 耗时 → 出图 |
| **角色扮演** | 和一个干净角色卡自由聊天（不限次） | 每轮可点「查看这轮注入了什么」 |
| **记忆（RAG）** | 聊天中查看记忆检索过程 | 检索 query、命中条目数、每条相似度分、最终注入几条 |
| **漫画** | 看一段漫画流水线回放 | 分镜脚本 → 每格 prompt → 成品图，三步走 |
| **降级链** | 只读看模型调度状态 | 当前链头、缓存命中、402 会切到谁、黑名单 |

**核心差异化**：每个功能旁边都要有一个「查看链路 / 看它内部」的入口，让访客能看到后端中间过程。普通官网只贴截图，这个页面能让访客当场试 + 看内部运转。

---

## 三、硬性边界（必须遵守）

1. **不动现有内部版**：现有 31 张角色卡、成人档位、怀孕系统、小说模式、群友私聊——一行代码都不改，公网也摸不到；
2. **干净素材单独新建**：干净角色卡（新造）、干净提示词契约（`showcase_prompts.py`），从源头不含成人 / 涉情 / 政治敏感内容，不是「生成后再过滤」；
3. **独立命名空间**：页面 `/showcase`、接口 `/api/showcase/*`，与现有 `/persona`、`/novel`、`/comic` 完全隔离；
4. **匿名可访问**：访客不需要登录，但生图仍走现有 GPU 队列（排队是正常现象，本身就是「队列治理」的展示点）；
5. **页脚只放邮箱 + GitHub**，不写真实姓名。

---

## 四、技术落地（后续要写的代码）

- 新增 3 个文件：
  - `web/showcase.html` —— 页面
  - `app/showcase_api.py` —— 接口（`/api/showcase/*` blueprint）
  - `app/showcase_prompts.py` —— 干净提示词契约
- 改动 2 个文件各加一行：
  - `app/main.py` 注册 blueprint
  - `friend_app.py` 加 `/showcase` 页面路由 + `/api/showcase/<path>` 反代（`allow_guest=True`）
- RAG 链路可视化：把 `persona_rag.build_block` 内部的检索中间态（query、命中条目、score）暴露成只读接口

---

## 五、小说模式

归入「角色扮演」区块，作为玩法标签提一句（丧尸末日世界观），不单独开块。

---

## 六、后端接口契约（2026-11-14 已实现）

浏览器只请求同源的 `/api/showcase/*`，**前端不得保存或填写内部网关令牌**；
5175 会自动补 `X-Friend-Token`。所有会话都由后端返回 `session_id`，页面可将它
存在 localStorage，后续聊天、检索和生图请求带同一个值。不要复用内部扮演的
`gs_id`、QQ 号或角色线。

| 接口 | 方法 | 用途 |
|---|---|---|
| `/api/showcase/config` | GET | 产品信息、干净角色卡、玩法、预置场景、漫画回放数据 |
| `/api/showcase/chat` | POST | 角色聊天；入参 `message`、`mode`、可选 `session_id` |
| `/api/showcase/memory` | GET | 只读检索；参数 `session_id`、`query` |
| `/api/showcase/chain` | GET | 完整降级链、当前可用链头、冷却黑名单、展示页缓存统计 |
| `/api/showcase/image/submit` | POST | 提交预置场景；入参 `scene_id`、可选 `session_id` |
| `/api/showcase/image/status` | GET | 轮询自己的任务；参数 `tag`、`session_id` |
| `/api/showcase/image` | GET | 读取已过审图片；直接使用状态接口返回的 `image_url` |
| `/api/showcase/comic/replay` | GET | 分镜脚本 → 每格 prompt → 成品图的数据契约 |

聊天成功的核心响应：`session_id`、`reply`、`mode`、`turn`、`usage`、`trace`。
`trace.messages` 是本轮实际发送给模型的完整消息数组，`trace.rag` 包含 query、
召回条数、distance/similarity、最终入选条目及实际注入块。模型不可用时返回 503，
不会把旧历史或伪造回复当成功。

生图只认 `rainy_bookstore`、`rooftop_garden`、`night_tram` 三个场景；即使请求体
另带 `prompt` 也不会进入工作流。状态依次为 `queued / running / reviewing / ready`，
终态另有 `blocked / error`。`trace` 提供最终提示词、seed、排队位置、全局队列深度、
耗时和输出审核状态。图片在独立识图审核通过前不会提供下载。

降级链接口只含 provider、model、顺序、冷却秒数、下一候选和缓存统计，不含
base_url、API key 或内部错误详情。

### 当前待接项

- `web/showcase.html` 留给界面设计，不在本轮后端工作中创建；
- 漫画已有三格干净脚本与 prompt，成品图尚未生成，`status=assets_pending`，
  不得从内部漫画目录取图顶替；
- 生图提示词采用固定干净契约，不做访客自定义或现场 AI 扩写；
- GitHub 地址由 `SHOWCASE_GITHUB_URL` 配置，未配置时前端隐藏该项；
- 代码需由站主手动重启 `agent.py`（5174）和 `friend_app.py`（5175）后生效，
  本任务没有重启任何进程。

### 本轮验证

- `test_showcase.py`：9 项通过；
- `test_persona_rag.py`：46 项通过；
- `test_friend_proxy.py`：45 项通过；
- 修改文件均通过 `py_compile`，主进程可导入并注册 8 个展示接口；
- 全量回归 6125 项，结果为 134 failures + 45 errors；本轮未做改前基线对照，
  不把这些问题归因于本次改动，本次相关测试均独立通过。
