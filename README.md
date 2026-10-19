# 个人 Agent（agent_my_test）

跑在本机的个人 AI 助手平台：**Flask Web UI + 自研文本工具协议 + 多模型路由 +
本地向量知识库 + Skill 体系 + ComfyUI 生图 + 漫画工作台 + QQ 机器人**。

一套 Agent 内核，两个入口（网页、QQ），三个独立进程。设计前提是「单人、本机、自用」：
没有账号体系，也没有面向多租户的隔离，公网暴露前必须先加鉴权。

---

## 系统架构

```
入口层    网页  Flask :5174（8 个页面）
          QQ    SnowLuma / NapCat  --OneBot 3000/3001-->  qq_bot（独立进程）
          看门狗 watchdog.py（独立进程，盯协议端与 ComfyUI）
                            ↓
Agent 层   多 agent：main / qq / draw / writing …
          Agent 循环 + 文本工具协议 + 按 agent 拼装的 system prompt
          QQ 前置闸：消息合并、生图确认、直达管道、扮演态、主动接话
                            ↓
工具层     37 个工具：通用 / 生图 / 知识库 / 文档 / 预设 / 长期记忆
                            ↓
能力层     LLM 多 provider 降级   生图队列   漫画引擎   RAG   识图与审核
                            ↓
存储层     会话 JSONL   ChromaDB   state/（账本·锁·队列）   comics/ 与图片
```

**三个进程为什么要分开**：QQ 掉线重连、协议端重启、扫码失效这些事不该影响网页端；
反过来改网页端代码要重启，也不该把 QQ 连接一起断掉。看门狗如果再放进 qq_bot 内部，
适配层自己卡死时就没人能救它了。三者共享同一套 `app/*` 代码，只是进程分开。

---

## 功能特性

### 对话内核

- **多模型热切换**：火山方舟（ARK）/ 豆包 / DeepSeek / Ollama，网页右侧面板切换，免重启。
- **自研文本工具协议**：`[[TOOL:name]]{json}`，**刻意不走 OpenAI function calling**。
  协议层带宽容解析（大小写、空白、省略前缀、缺关闭标签都能认），为本地小模型做了妥协。
- **Agent 循环**：单轮可执行多个工具，带轮次上限与异常兜底。
- **真流式输出**：`/api/chat` 以 NDJSON 逐事件推送，工具调用与结果实时可见。
- **上下文压缩**：按「占用率 + 缓存命中率」双判据触发整段摘要，目标是保住 prefix cache 热前缀。
- **多 agent**：一个人设一个目录，工具与 Skill 走白名单，看不见也调不动。

### 生图

- **统一队列**：全进程串行，一次只让 ComfyUI 跑一张；跨进程锁保证网页面与漫画台不互相打断。
- **服务重启不拒单**：ComfyUI 离线时任务照常入队，worker 每 5 秒探活，等它上线自动开画；
  回执会如实说「正在重启，一上线就开始」，不会假称「已经在画」。
- **渠道**：约 50 个工作流渠道，覆盖动漫 / 写实 / 高清 / 放大 / 图生图 / 多图批量等家族，
  支持按尺寸横竖屏、多图批量、真 i2i 垫图。
- **交付**：按来源分目录落盘（群聊、私聊、生图页、漫画任务各自独立），
  发图可配置为压缩 JPG 或带工作流元数据的原图 PNG。
- **安全与配额**：发图前识图审核，拦下的进人工二审队列；私聊按人计日额度，失败自动退还。
- **可恢复**：任务账本记录已提交未投递的图，重启后补发而不是重画。

### 漫画工作台

一句话设定 → 剧本 → 逐格出图 → 加气泡台词，也可只跑「写真集」。

- **编剧**：模型查标签库写分镜，每个标签必须真实存在；不存在的会被告知候选。
- **画面模式**：第一人称 POV，镜头即观众眼睛，同格禁止出现男性与多人。
- **补正**：代码只做结构检测并出清单，改字与拍板交给 AI，不拿硬编码词表做准入闸门。
- **渲染参数**：任务级 > 全局默认 > 模板，三级覆盖，可在页面上改完立即重跑。
- **任务产物**：`comics/C<日期>-<序号>_<剧本标题>/`，剧本、原图、成品、状态各一份。

### 检索与记忆

- **RAG 知识库**：ChromaDB + 本地 BGE-M3 + MMR，跑在**独立子进程 daemon** 里
  （重依赖隔离、懒加载，不装 torch 也不拖垮主服务）。
- **文档**：Word / PDF / Excel 分段读取与检索。
- **QQ 长期记忆**：群聊历史摘要成「回忆」按群存、按需注入。
- **表情包库**：群里出现的好图趁链接活着存下来、打标签，回头能甩出去。

### QQ 接入

- **多群多私聊**：一条会话线一个 `session_key`，历史各自隔离；同线串行、跨线并行。
- **消息合并**：连发几句攒成一条一起送，避免刷屏式回三条。
- **生图二次确认**：拦住后等一句「好」，确认了才真正入队。
- **直达管道**：纯文本与生图指令走轻量判定，省掉整轮 Agent 循环的 token。
- **角色扮演**：人设卡 + 会话级扮演态，每轮配一张快图。
- **主动接话**：按冷却与最小间隔判断「此刻值不值得插一句」。
- **会话借用**：某个群可以整套切换成另一个 agent（人设、工具、历史一起换）。

---

## 技术栈

| 层 | 选型 |
|----|------|
| 语言 | Python 3.10+ |
| Web 后端 | Flask（`app/main.py`，静态目录直接托管 `web/`） |
| 前端 | 原生 HTML/CSS/JS 单文件，**零构建、无框架** |
| LLM 接入 | 自研 `app/llm.py`，云侧统一 OpenAI 兼容接口，Ollama 走原生 `/api/chat` |
| 向量库 | ChromaDB + sentence-transformers(BGE-M3) + MMR |
| 生图 | ComfyUI HTTP API + 自定义节点 |
| QQ | OneBot 11，协议端可用 SnowLuma 或 NapCat |
| 持久化 | 会话 JSONL，**原子写**（临时文件 + fsync + `os.replace`） |

---

## 快速开始

### 1. 环境

- Python 3.10 以上
- （可选）[Ollama](https://ollama.com)：想用本地模型时安装
- （可选）ComfyUI：想用生图时安装
- （可选）SnowLuma 或 NapCat：想接 QQ 时装

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

RAG 相关依赖（`chromadb` / `sentence-transformers` / `torch`）体量较大。
**不装也能启动主服务**——RAG 工具被调用时返回「daemon 启动失败」，其余功能不受影响。

### 3. 配置

```bash
cp .env_example .env
```

至少填一个 provider 的 API Key（见下方「配置项」）。

### 4. 启动网页端

```bash
python agent.py
```

访问 <http://localhost:5174>（同网段手机可用 `http://<局域网IP>:5174`）。

> 管理页（`/admin`）的**写接口**默认只对本机开放。手机/平板要能改，
> 在 `.env` 里设 `ADMIN_ALLOW_REMOTE=lan`（只放行内网）。

> 改后端（`app/*.py`）需重启；改前端（`web/*.html`）浏览器硬刷新即可。

### 5. 接 ComfyUI（可选）

```bash
# 单独起 ComfyUI，监听 8188
COMFYUI_URL=http://127.0.0.1:8188
ENABLE_IMAGE_GEN=true
```

没装 ComfyUI 就把 `ENABLE_IMAGE_GEN` 设为 `false`，生图类工具不会注册。

### 6. 接 QQ（可选）

默认按 **SnowLuma** 配置，OneBot 端口与 NapCat 完全一致：

```bash
QQ_ENABLE=true
QQ_WS_URL=ws://127.0.0.1:3001      # 收事件
QQ_HTTP_URL=http://127.0.0.1:3000  # 发消息
QQ_TOKEN=<协议端里设置的 accessToken>
QQ_WEBUI_PORT=5099                 # SnowLuma 的 WebUI 端口
```

然后：

```bash
python -m app.qq_bot    # QQ 适配层，独立进程
python -m app.watchdog  # 看门狗，独立进程（可选但建议）
```

浏览器打开 `http://127.0.0.1:5099` 扫码登录。协议端只做「发现 `QQ.exe` → 注入」，
**不会自己拉起 QQ**，所以要先手动开 QQ。

> **换回 NapCat**：`QQ_WEBUI_PORT=6099`，重启协议端即可（OneBot 端口不用改）。
> 两个协议端的差异见下方「协议端说明」。

---

## 目录结构

```
agent_my_test/
├── agent.py                     # 网页端启动入口
├── app/
│   ├── main.py                  # Flask 路由（页面 / chat 流式 / 图片 / 管理接口）
│   ├── config.py                # 配置加载 + PROVIDERS 表
│   ├── agent.py                 # Agent 循环 + 工具协议解析
│   ├── agent_prompt.py          # system prompt 按 agent 拼装
│   ├── agents.py                # 多 agent：扫描、白名单、路径安全、热加载
│   ├── llm.py                   # 多 provider 路由 + 降级链
│   ├── memory.py                # 会话持久化 + 上下文压缩
│   ├── skills.py                # Skill 解析与加载
│   ├── tools/
│   │   ├── registry.py          # 工具注册表（execute_tool 统一入口）
│   │   ├── sandbox.py           # skills 路径沙箱
│   │   ├── normal/              # 通用 / 生图 / 文件 / 文档 / 表情包
│   │   └── rag/                 # 知识库、预设库、长期记忆
│   ├── rag/                     # 向量引擎（客户端 + daemon + 存储）
│   ├── image_jobs.py            # 生图队列与 worker
│   ├── direct_gen.py            # QQ 直达生图管道
│   ├── image_audit.py           # 发图前 AI 审核
│   ├── image_review.py          # 人工二审队列
│   ├── image_out.py             # 发图格式与临时副本
│   ├── image_quota.py           # 私聊日额度
│   ├── job_ledger.py            # 未投递任务账本
│   ├── comfy_gate.py            # 跨进程 ComfyUI 锁
│   ├── comfy_out.py             # 输出分目录
│   ├── comic_agent.py           # 漫画编剧
│   ├── comic.py                 # 漫画渲染调度
│   ├── comic_api.py             # 漫画台接口
│   ├── comic_portrait.py        # 写真模式
│   ├── comic_render_opts.py     # 渲染参数覆盖
│   ├── comic_story.py           # QQ 版连续小漫画
│   ├── qq_bot.py                # QQ 适配层（独立进程）
│   ├── qq_api.py                # OneBot HTTP 封装
│   ├── persona.py               # 角色扮演
│   ├── interject.py             # 主动接话
│   ├── confirm_gate.py          # 生图二次确认
│   ├── longterm.py              # 群聊长期记忆
│   ├── stickers.py              # 表情包库
│   ├── watchdog.py              # 看门狗（独立进程）
│   └── ...
├── web/                         # 8 个页面，纯静态
├── tests/                       # 标准库 unittest
├── skills/                      # ← 私有数据，不进版本库
├── documents/                   # ← 私有数据，不进版本库
├── agents/                      # ← agent 人设与会话，不进版本库
│   └── <agent-id>/
│       ├── agent.json           #   显示名 + 工具/skills 白名单（null = 不限制）
│       ├── prompt.md            #   角色人设
│       ├── settings.json        #   会话级配置（可选）
│       └── sessions/*.jsonl     #   该 agent 的会话历史
├── comics/                      # ← 漫画任务产物，不进版本库
├── state/                       # ← 运行态：锁、账本、状态快照
├── vector_store/                # ← 向量库，不进版本库
└── logs/                        # ← 运行日志
```

---

## 配置项（`.env`）

### LLM

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `LLM_PROVIDER` | `volc` | 默认 provider：`volc` / `doubao` / `deepseek` / `ollama` |
| `VOLC_API_KEY` | — | 火山方舟 Key（volc 与 doubao 共用） |
| `VOLC_BASE_URL` | `https://ark.cn-beijing.volces.com/api/v3` | 方舟兼容 OpenAI 接口 |
| `VOLC_CHAT_MODEL` | — | 默认模型，也可填 `ep-xxxx` 接入点 ID |
| `DEEPSEEK_API_KEY` | — | DeepSeek 官方 Key |
| `DEEPSEEK_MODEL` | `deepseek-chat` | |
| `DOUBAO_MODEL` | — | 豆包多模态模型，可作识图 |
| `OLLAMA_BASE_URL` | `http://127.0.0.1:11434` | 本地 Ollama |
| `OLLAMA_MODEL` | `qwen2.5:7b` | |
| `SEARCH_API_KEY` | — | 搜索 Key；未配置时 `web_search` 回退 DuckDuckGo |

> 降级链与拉黑表也在 `.env`：某个 provider 失败后被标 `_DEAD` 一段时间，
> 期间流量自动走下一家，避免每轮都白等一次超时。

### 服务与上下文

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `AGENT_PORT` | `5174` | 网页端端口 |
| `ADMIN_ALLOW_REMOTE` | `false` | 管理页写接口放行：`false` 只本机 / `lan` 只内网 / `true` 任何来源 |
| `MAX_TURNS` | `10` | Agent 单次请求最大循环轮次 |
| `CONTEXT_BUDGET` | `32000` | 上下文预算（token） |
| `CONTEXT_MAX_TURNS` | `20` | 送进模型的最大历史轮数 |
| `IMAGE_GEN_TIMEOUT` | `180` | 单张图等待上限（秒） |

### 生图

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `ENABLE_IMAGE_GEN` | `true` | `false` 时不注册生图类工具 |
| `COMFYUI_URL` | `http://127.0.0.1:8188` | |
| `COMFYUI_OUTPUT_DIR` | — | ComfyUI 的 output 绝对路径，用于直接读盘取图 |
| `COMFY_WAIT_UP` | `120` | ComfyUI 离线时 worker 最多等多久 |
| `COMFY_MIN_FREE_RAM_GB` | — | 低于阈值时在任务间隙重启 ComfyUI 释放内存 |
| `COMFY_RELEASE_AFTER_SAME` | `2` | 同一渠道连画几张后重启 |

> 发图格式（压缩 JPG / 带工作流原图 PNG）不在 `.env` 里，是管理页的设置，
> 存在 `agents/<id>/settings.json`：全局一个总开关，还可按单个会话覆盖。

### QQ / 协议端

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `QQ_ENABLE` | `false` | 打开 QQ 适配层与相关工具 |
| `QQ_WS_URL` | `ws://127.0.0.1:3001` | 收事件 |
| `QQ_HTTP_URL` | `http://127.0.0.1:3000` | 发消息 |
| `QQ_TOKEN` | — | OneBot accessToken，需与协议端一致 |
| `QQ_WEBUI_PORT` | `6099` | 协议端 WebUI：NapCat `6099` / SnowLuma `5099`（本项目当前用 `5099`） |
| `QQ_AGENT_ID` | `qq` | QQ 侧默认 agent |
| `QQ_DEBOUNCE_SECONDS` | `1.5` | 消息合并窗口 |
| `QQ_MAX_CONCURRENCY` | `2` | 跨会话并行上限 |
| `QQ_GROUP_AT_ONLY` | `true` | 群里只响应 @ |
| `QQ_WHITELIST_GROUPS` / `QQ_BLACKLIST_USERS` | — | 群与用户名单 |
| `QQ_INTERJECT_MODE` | `off` | 主动接话：`off` / `on` |
| `QQ_INTERJECT_GROUPS` | — | 只在哪些群接话 |
| `QQ_INTERJECT_COOLDOWN` | — | 冷却时间 |

---

## 协议端说明（SnowLuma / NapCat）

默认使用 **SnowLuma**，NapCat 仍可直接换回。两者的 OneBot 端口一致（HTTP 3000 / WS 3001），
差异只有 WebUI 端口和启动方式：

| | SnowLuma | NapCat |
|---|---|---|
| OneBot HTTP / WS | 3000 / 3001 | 3000 / 3001 |
| WebUI | **5099** | **6099** |
| 注入方式 | hook `QQ.exe` | hook `QQ.exe` |
| 谁拉起 QQ | **不拉，需先手动开 QQ** | launcher 自己拉（`-q` 快速登录） |
| 登录方式 | 仅扫码 | 扫码 / 快速登录 |
| 掉线推二维码到手机 | **不支持**（二维码只在 WebUI 网页里画，不落盘） | 支持 |

切换方式：改 `QQ_WEBUI_PORT` 后重启协议端。看门狗会按这个端口反推该拉起哪个协议端，
不会拉错。

---

## 多 agent

一个 agent = `agents/` 下的一个自包含目录：人设、可见范围、会话历史都封在自己目录里。

`agent.json` 字段：

| 字段 | 说明 |
|------|------|
| `name` | 显示名（前端下拉用） |
| `description` | 一句话说明，作下拉的 title |
| `prompt` / `prompt_file` | 内联人设 / 人设文件名（默认 `prompt.md`） |
| `tools` | `null` = 全部工具；数组 = 白名单 |
| `skills` | `null` = 全部 Skill；数组 = 白名单 |
| `provider` / `model` | 该 agent 固定使用的模型（留空则继承 `.env` 的降级链） |

**隔离是两层的**：白名单外的工具既不出现在 system prompt 里（看不见），
执行层也会拒绝（调不动）。所以在「写作 agent」下，即使模型硬写出
`[[TOOL:generate_image]]`，也只会拿到「该工具在当前 agent 不可用」。

**加一个 agent**：建目录、写 `agent.json` 与 `prompt.md` 即可，不用改代码、不用重启
（按文件 mtime 热加载，`GET /api/agents` 每次实时扫目录）。

**会话借用**：`agents/<id>/settings.json` 里的 `session_prompt_agents` 可以让某条
QQ 会话线整套借用另一个 agent（人设、工具、历史目录一起换）。
借用线会跳过直达管道、生图确认与扮演态，整轮交给被借的 agent。

**路径安全**：`agent_id` 直接参与文件路径，统一过 `safe_agent_id()` 校验——
只允许字母数字 / 下划线 / 连字符，realpath 解析后必须落在 `agents/` 直接子目录内。

---

## Web 界面

| 页面 | 路径 | 用途 |
|------|------|------|
| 个人 Agent | `/` | 聊天主界面：流式输出、工具调用折叠、模型切换、文档上传 |
| 管理 | `/admin` | 总控制台：模型、agent、Skill、QQ 会话、生图设置、发图格式 |
| 漫画工作台 | `/comic` | 剧本模式 + 写真模式；剧本、补正、对话修订、渲染参数、逐格预览 |
| 跑图台 | `/gen` | 不经过 AI 的手动文生图界面 |
| 状态后台 | `/status` | 队列、轮次、ComfyUI、协议端的实时看板 |
| 人工二审 | `/review` | 审核 AI 拦下的图，点「过审」才发回 |
| 图片迁移 | `/migrate` | 翻看历史图，挑中的复制到指定目录 |
| 人设卡池 | `/characters` | 角色扮演的人设卡管理 |

---

## 工具清单

共 **37 个**，按 `ENABLE_IMAGE_GEN` / `QQ_ENABLE` 条件注册。参数名后带 `*` 为必填。

### 通用

| 工具 | 说明 |
|------|------|
| `get_time` | 当前日期时间 |
| `web_search` | 网页搜索（搜索 API 优先，回退 DuckDuckGo） |
| `list_skills` | 列出顶层 Skill 目录 |
| `load_skill` | 读取某个 Skill 主规范（**会带上全部 `references/`**，注意上下文开销） |
| `list_files` / `read_file` / `grep_file` | `skills/` 内列目录、读文件、按行检索 |
| `write_file` / `delete_file` | 在 `skills/` 内增删（强沙箱，只认相对路径） |
| `search_tags` | 标签库结构化检索（内存索引 + 中文滑窗），返回候选而非原始行 |

### 文档

| 工具 | 说明 |
|------|------|
| `list_documents` / `file_info` | 列出可读文件、大小与行数 |
| `read_document` | 分段读取（带行号） |
| `search_document` | 文档内关键词搜索 |

### 生图（`ENABLE_IMAGE_GEN=true`）

| 工具 | 说明 |
|------|------|
| `generate_image` | 生成图片，支持指定渠道、横竖屏、垫图、批量 |
| `generate_comic` | 一句话剧情 → 连续小漫画 |
| `recall_image` | 按图片编号反查当初用的提示词与参数 |
| `get_workflow` / `update_workflow` | 查看 / 语义化调整某渠道的工作流并写回 |

### 知识库

| 工具 | 说明 |
|------|------|
| `search_kb` | 在指定知识库检索 |
| `ingest_kb` | 批量入库 |
| `list_kb` / `delete_kb` / `delete_entries` | 列出 / 删除知识库 / 按 id 删片段 |
| `chunk_document` | 按标题与段落把长文档切块 |

### QQ（`QQ_ENABLE=true`）

| 工具 | 说明 |
|------|------|
| `send_qq_message` | 主动发文本（不带参数则发回当前会话） |
| `send_sticker` / `delete_sticker` / `collect_sticker` | 发表情包 / 删除 / 收藏 |
| `preset_save` / `preset_search` / `preset_list` / `preset_delete` | 私聊文生图预设库 |
| `memory_save` / `memory_search` / `memory_list` / `memory_delete` | 按 QQ 号隔离的长期记忆 |

---

## Skill 体系

`skills/<name>/` 是 Skill 的根，兼容两种风格：

**1）本地生图 Skill**

```
skills/<channel>/
├── skill.md          # 规范说明
├── workflow.json     # ComfyUI 工作流模板（文生图）
└── workflow_i2i.json # 图生图模板（可选）
```

**2）标准 / 脚手架 Skill**

```
skills/<name>/
├── SKILL.md          # 主规范（大写优先于 skill.md）
├── references/*.md   # 参考手册，load_skill 时拼接附带
└── assets/
```

**两类 Skill**：

- **渠道**：带 `workflow.json`，负责真正跑图。当前约 50 个，按家族分
  （动漫 / 写实 / 高清 / 快速 / 放大 / 多图批量 / 图生图），可自己新增。
- **规范**：不带工作流，纯文本知识——标签库、分镜规范、写作方法、姿势库等，
  供 agent 用 `read_file` / `search_tags` 按需查阅。

> `load_skill` 会把 `references/` 顶层**所有** `.md` 一次性拼进返回值。
> 规范类 Skill 文档多时优先 `read_file` 按需读单篇，别整包加载。

---

## 生图队列与发图

```
入口（工具 / 跑图台 / 漫画台）
        ↓  入队（从不碰 ComfyUI）
    统一队列（全局串行，每会话在途上限）
        ↓
   worker：先探活 → 离线则每 5 秒重试（上限 COMFY_WAIT_UP）
        ↓
   跨进程锁 comfy_gate（QQ 与漫画台不能同时动 ComfyUI）
        ↓
   ComfyUI /prompt → 轮询 /history → 下载
```

几个关键取舍：

- **探活只决定回执口径，不决定收不收单**。离线时回执说「正在重启，一上线就自动开始」，
  而不是「已经在画了」——后者是假话，几十秒后就会打脸。
- **等待放在 worker，不放入口**：放入口会堵住整个 QQ 会话线程；
  持锁等会锁死漫画台；放 `process()` 开头会先跑一遍重启判断白等一轮。
- **每张都探活**（在线时约 7 ms），顺带盖住「探活通过 → 内存守卫重启 → 提交失败」的窗口。
- **输出分目录由保存节点的 `filename_prefix` 决定**，ComfyUI 自己建目录，不搬文件。
- **批量节点的 `subfolder` 在 history 里恒为空**，取图时用本次算出的目录补，不能只信 history。

---

## 上下文与缓存策略

这是本项目里「最工程」的一块，目标只有一个：**尽量不打断 LLM 的 prefix cache 热前缀**。

- **稳定前缀**：`system` 内容固定；动态状态栏追加在消息数组末尾，
  每轮只让它自己那几十 token 未命中，前面全部历史仍能命中缓存。
- **压缩阈值**（见 `app/memory.py`）：
  - 占用 ≥ 50%：仅记录，不压缩；
  - 占用 ≥ 80% **且** 命中率 < 30%：把旧轮一次性摘要成单条消息；
  - 占用 ≥ 90%：无论命中率都强制压缩（防爆上下文）；
  - 冷却：距上次压缩 token 增长不足 10 万则不重复压缩；
  - 最近 3 轮始终完整保留，作为新热前缀的锚点。
- **会话持久化**：JSONL 原子写（临时文件 → `flush` + `fsync` → `os.replace`），
  进程内写锁，避免读到半截文件。

---

## 测试

标准库 `unittest`，零额外依赖；装了 pytest 也能跑。所有用例不发真实网络请求
（LLM / 工具 / Flask 客户端均被 mock）。

```bash
python -m unittest discover -s tests -v
```

覆盖范围（部分）：

| 文件 | 覆盖内容 |
|------|----------|
| `test_tool_protocol.py` | 工具协议解析：标准/简写/大小写/空白/嵌套 JSON/正文剥离 |
| `test_agent_loop.py` | 主循环事件流、多工具、异常兜底、轮次上限 |
| `test_memory.py` | 会话原子写、压缩阈值与冷却 |
| `test_skills.py` | Skill 目录解析、references 拼装 |
| `test_file_tools.py` | 文件沙箱：越界路径拦截、分段读 |
| `test_image_jobs.py` | 队列、等待与失败口径、渠道能力校验 |
| `test_image_receipt.py` | 回执文案：排队 / 正在画 / 等待重启 |
| `test_confirm_gate.py` | 生图二次确认闸 |
| `test_comic.py` | 剧本解析、画面检测、POV 规则、任务目录命名 |
| `test_comic_api.py` | 漫画台接口、补正稿流转 |
| `test_web_api.py` | Flask 路由、`/api/chat` NDJSON 流、取图 |
| `test_qq.py` | QQ 适配层：合并、借用、前置闸顺序 |

全量约 **3200+ 例 / 1.5 分钟内**。有 5 条是长期已知失败（历史遗留断言），
其余应保持全绿。

---

## 项目边界：代码 vs 私有数据

`.gitignore` 有意排除以下路径——**代码是引擎，可跨机复用；这些是个人数据**：

- `.env`（密钥）
- `agents/`（人设、白名单、会话历史）
- `skills/`（技能与渠道）
- `documents/`（个人文档）
- `vector_store/`（向量库）
- `comics/`（漫画产物）
- `state/` / `logs/` / `usage/`（运行态）

换一台机器后：`pip install -r requirements.txt` → 填 `.env` → 把资料放进
`skills/` / `documents/` / `agents/` → 用 `chunk_document` + `ingest_kb` 重建向量库。

---

## 已知限制

- **无鉴权**：只有管理页写接口做了来源限制，接口本身不校验身份。公网暴露前必须加。
- **单人设计**：会话按 agent + 会话线隔离，但没有租户概念，不适合多用户。
- **单机假设**：跨进程锁与队列都基于本地文件，不跨机器。
- **协议端掉线通知不全**：SnowLuma 不落盘二维码，掉线时手机收不到码，得自己开 WebUI 扫。
- **依赖未锁版本**：`requirements.txt` 基本是 `>=`，换机复现性一般。
- **无 CI**。

## 可做

- [ ] 接口鉴权与来源白名单
- [ ] 会话列表与多会话管理
- [ ] 用 GitHub Actions 跑 `tests/`
- [ ] 锁定依赖版本
- [ ] 工具结果定长上界（避免长文档整篇灌进上下文）
