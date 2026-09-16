# 个人 Agent（agent_my_test）

跑在本机的个人 AI 助理：**Flask Web UI + 自研文本工具协议 + 多模型路由 +
本地向量知识库（RAG）+ Skill 体系 + ComfyUI 生图**。

主要用途是**个人知识管理**——把自己的资料做成 Skill 和向量知识库，用来检索与整理；
生图是次要能力。它不是面向多用户的产品：没有鉴权、没有多会话，设计前提就是
"单人、本机、自用"。

---

## 功能特性

- **多模型热切换**：火山方舟（ARK）/ 豆包 / DeepSeek 官方 / Ollama 本地，
  在网页右侧「模型设置」面板里切换，无需重启服务。
- **自研文本工具协议**：`[[TOOL:name]]{json}[[/TOOL]]`，**刻意不走 OpenAI function
  calling**。协议层带宽容解析（大小写、空白、省略 `TOOL:` 前缀、缺关闭标签都能认），
  专门为本地小模型做了妥协。
- **Agent 循环**：单轮可执行多个工具、`MAX_TURNS` 上限保护、异常兜底不断循环。
- **真流式输出**：`/api/chat` 以 NDJSON 逐事件推送，工具调用与结果实时可见，
  等待期间显示计时器，不再是黑箱。
- **21 个内置工具**：时间 / 搜索 / Skill 管理 / 文件读写删 / 文档分段阅读 /
  生图 / 工作流编辑 / 向量知识库。
- **RAG 向量知识库**：ChromaDB 持久化 + 本地 BGE-M3 embedding + MMR 检索，
  跑在**独立子进程 daemon** 里（重依赖隔离、懒加载，不装 torch 也不会拖垮主服务）。
- **缓存感知的上下文压缩**：按"占用率 + 缓存命中率"双判据触发整段摘要，
  目标是保住 LLM 的 prefix cache 热前缀。
- **Skill 体系**：兼容两种目录风格（本地生图 skill / GitHub 脚手架 skill），
  支持同名嵌套层自动下探。
- **ComfyUI 生图 + 工作流语义化编辑**：让代码改 JSON，而不是让模型手写节点图。
- **手机/平板同网可访问**：服务监听 `0.0.0.0`。

---

## 技术栈

| 层 | 选型 |
|----|------|
| 语言 | Python 3.10+（开发环境用 3.14） |
| Web 后端 | Flask（单文件 `app/main.py`，静态目录直接托管 `web/`） |
| 前端 | 原生 HTML/CSS/JS 单文件，**零构建、无框架** |
| LLM 接入 | 自研 `app/llm.py`，云侧统一 OpenAI 兼容接口，Ollama 走原生 `/api/chat` |
| 向量库 | ChromaDB（持久化，按 `kb_name` 隔离 collection）+ sentence-transformers(BGE-M3) + MMR |
| 生图 | ComfyUI HTTP API + 自定义节点（可选） |
| 持久化 | 会话存 JSONL，**原子写**（临时文件 + fsync + `os.replace`） |

---

## 快速开始

### 1. 环境

- Python 3.10 以上。
- （可选）[Ollama](https://ollama.com)：想用本地模型跑 Agent 时安装。
- （可选）ComfyUI：想用生图工具时安装。

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

`requirements.txt` 里 RAG 相关依赖（`chromadb` / `sentence-transformers` / `torch` /
`modelscope`）体量较大。**不装也能启动主服务**——RAG 工具被调用时会返回
"RAG daemon 启动失败"，其余功能不受影响。

### 3. 配置 `.env`

```bash
cp .env_example .env
```

至少填一个 provider 的 API Key（见下方"配置项"）。

### 4. 启动

Windows 下可直接双击 `一键启动.bat`：它会杀掉占用 5174 端口的旧进程、
启动服务并自动打开浏览器。或者手动：

```bash
python agent.py
```

然后访问 <http://localhost:5174>（同网段的手机/iPad 可用 `http://<本机局域网IP>:5174`）。

> 改了后端（`app/*.py`）需要重启服务；只改前端（`web/index.html`）浏览器硬刷新
> （Ctrl+Shift+R）即可，因为 `web/` 是静态目录直接托管。

---

## 目录结构

```
agent_my_test/
├── agent.py                     # 启动入口（调用 app.main.run）
├── 一键启动.bat                  # Windows 一键启动（杀旧进程 + 开浏览器）
├── requirements.txt
├── .env_example                # 配置模板
├── app/
│   ├── config.py               # 配置加载 + PROVIDERS 表
│   ├── main.py                 # Flask 路由（页面 / chat 流式 / 文档 / 模型列表 / agent 列表）
│   ├── agents.py               # 多 agent：目录扫描、白名单过滤、路径安全、热加载
│   ├── agent.py                # Agent 循环 + 工具协议解析
│   ├── agent_prompt.py         # System Prompt 构造（按 agent 组装：人设 + 白名单 + 状态栏）
│   ├── llm.py                  # 多 provider 路由 + token 命中率统计
│   ├── memory.py               # 会话原子持久化 + 缓存感知上下文压缩（按 agent 隔离）
│   ├── skills.py               # Skill 目录解析与加载
│   ├── tools/
│   │   ├── registry.py         # 工具注册表（execute_tool 统一入口）
│   │   ├── sandbox.py          # skills 沙箱路径解析（read/write/list/delete 共用）
│   │   ├── normal/             # 普通工具（时间/搜索/文件/文档/生图/工作流）
│   │   └── rag/kb_tools.py     # 向量知识库工具（检索/入库/切块）
│   └── rag/
│       ├── rag_client.py       # 主进程侧客户端（stdio JSON-RPC）
│       ├── rag_daemon.py       # 常驻子进程（持有模型与向量库）
│       ├── local_embeddings.py # 本地 BGE-M3
│       └── vector_store.py     # ChromaDB 封装 + MMR 检索
├── web/
│   └── index.html              # 前端单文件（无构建）
├── tests/                      # 单元测试（标准库 unittest，见下）
├── skills/                     # ← 私有数据，不进版本库
├── documents/                  # ← 私有数据，不进版本库
├── agents/                     # ← agent 人设与会话，不进版本库
│   └── <agent-id>/             #   一个 agent = 一个自包含目录
│       ├── agent.json          #     显示名 + 工具/skills 白名单（null = 不限制）
│       ├── prompt.md           #     角色人设（缺失则用默认角色）
│       └── session.jsonl       #     该 agent 的会话历史
├── data/                       # ← 日志等（旧单会话文件已迁至 agents/）
└── vector_store/               # ← 向量库，不进版本库
```

---

## 配置项（`.env`）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `LLM_PROVIDER` | `volc` | 默认 provider：`volc` / `doubao` / `deepseek` / `ollama` |
| `VOLC_API_KEY` | — | 火山方舟 API Key（volc 与 doubao 共用） |
| `VOLC_BASE_URL` | `https://ark.cn-beijing.volces.com/api/v3` | 方舟兼容 OpenAI 接口地址 |
| `VOLC_CHAT_MODEL` | `deepseek-v4-flash-ga-260731` | 火山引擎默认模型；也可填控制台的 `ep-xxxx` 接入点 ID |
| `DEEPSEEK_API_KEY` | — | DeepSeek 官方 Key |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com/v1` | |
| `DEEPSEEK_MODEL` | `deepseek-chat` | |
| `DOUBAO_MODEL` | `doubao-1-5-thinking-pro-250615` | 豆包自家模型 |
| `OLLAMA_BASE_URL` | `http://127.0.0.1:11434` | 本地 Ollama |
| `OLLAMA_MODEL` | `qwen2.5:7b` | |
| `SEARCH_API_KEY` | — | 豆包搜索专用 Key（与方舟 Key 不同）；未配置时 `web_search` 回退 DuckDuckGo |
| `COMFYUI_URL` | `http://127.0.0.1:8188` | |
| `ENABLE_IMAGE_GEN` | `true` | 设为 `false` 时不注册生图相关工具（没装 ComfyUI 的机器应关掉） |
| `AGENT_PORT` | `5174` | |
| `MAX_TURNS` | `10` | Agent 单次请求的最大循环轮次 |
| `LOCAL_EMBEDDING_MODEL` | `BAAI/bge-m3` | RAG 向量模型 |
| `RAG_EMBED_DEVICE` | `cuda` | 无 GPU 时改 `cpu` |
| `RAG_AUTO_DOWNLOAD` | `0` | 是否允许自动下载模型 |
| `RAG_RETRIEVE_K` | `4` | 检索返回条数 |
| `RAG_DATA_DIR` | `vector_store/chroma` | 向量库数据目录 |

前端每个 provider 的可选模型列表在 `web/index.html` 的 `MODEL_PRESETS` 里维护
（选 provider 后以常驻按钮呈现，也支持手动输入任意模型 ID）。

---

## 多 agent

一个 agent = `agents/` 下的一个自包含目录：人设、可见范围、会话历史都封在自己目录里。

```
agents/
├── main/                # 默认 agent（请求未指定或指定了不存在的 id 时兜底到它）
│   ├── agent.json       # 显示名 + 工具/skills 白名单
│   ├── prompt.md        # 角色人设
│   └── session.jsonl    # 会话历史（与其他 agent 完全隔离）
└── writing/
    ├── agent.json
    ├── prompt.md
    └── session.jsonl
```

`agent.json` 字段：

| 字段 | 说明 |
|------|------|
| `name` | 显示名（前端下拉用），缺省用目录名 |
| `description` | 一句话说明，作下拉的 title |
| `prompt` | 内联人设（`prompt_file` 不存在时的兜底） |
| `prompt_file` | 人设文件名，默认 `prompt.md`（不允许带路径分隔符） |
| `tools` | `null` = 全部工具；数组 = 白名单 |
| `skills` | `null` = 全部 Skill；数组 = 白名单 |

**加一个 agent**：在 `agents/` 下建目录、写 `agent.json` 与 `prompt.md` 即可——不用改代码、不用重启服务（配置按文件 mtime 热加载，`GET /api/agents` 每次实时扫目录）。刷新页面就能在顶栏下拉里看到。

**隔离是两层的**：白名单外的工具既不出现在 system prompt 的工具列表里（看不见），执行层也会拒绝调用（调不动）——所以在「写作 agent」下，即使模型硬写出 `[[TOOL:generate_image]]`，也只会拿到「该工具在当前 agent 不可用」。同理，工具使用提示与「环境信息」里跟生图相关的条目也会一并裁掉，不会留下"提了个不存在的工具"的噪音。

**路径安全**：`agent_id` 直接参与文件路径（`agents/<id>/session.jsonl`），因此统一过 `safe_agent_id()` 校验：只允许字母数字 / 下划线 / 连字符，且 realpath 解析后必须落在 `agents/` 的直接子目录内。非法或指向不存在目录的 id 一律兜底到 `DEFAULT_AGENT_ID`（默认 `main`）。

**接口**：`/api/history`、`/api/chat`、`/api/vision`、`/api/clear` 都接受 `agent` 参数（query 或 body 字段）；`GET /api/agents` 返回 agent 列表。

**并发**：服务端不持有常驻会话对象，每个请求自带 agent id 并各自读盘，所以多标签页各连一个 agent 是天然支持的。

---

## Web 界面

- **模型设置面板**：选 provider → 选/填模型 → 顶部徽标同步。切换后立即生效。
- **会话区**：流式输出；工具调用折叠成可展开分组；等待时显示"正在思考… 已等待 N 秒"。
- **文档**：上传到 `documents/`，可在页面上列出/读取。
- **图片**：ComfyUI 输出图片通过 `/api/image/<filename>` 代理显示。

---

## 工具清单

共 21 个（`ENABLE_IMAGE_GEN=false` 时为 18 个，仅少生图/工作流三项）。
参数名后带 `*` 为必填。

### 通用

| 工具 | 参数 | 说明 |
|------|------|------|
| `get_time` | — | 获取当前日期时间 |
| `web_search` | `query*`, `max_results` | 网页搜索（豆包搜索优先，回退 DuckDuckGo） |
| `list_skills` | — | 列出所有可用 Skill（只到顶层目录名） |
| `list_files` | `path`, `depth` | 列出 `skills/` 的文件结构（默认展开 3 层，跳过 `.git`/`__pycache__`） |
| `load_skill` | `skill_name*` | 读取某个 Skill 主规范（**会带上全部 `references/`**，上下文开销大） |
| `read_file` | `path*`, `offset`, `limit` | 读 `skills/` 内任意文件，**支持子目录**，默认单次最多 1000 行 |
| `write_file` | `path*`, `content*` | 写 `skills/` 内文件（父目录自动创建，可新建 Skill） |
| `delete_file` | `rel_path*`, `recursive` | 删除 `skills/` 内文件或子目录（强沙箱，只认相对路径） |
| `list_documents` | — | 列出 `documents/` 下可读文件 |
| `file_info` | `filename*` | 文件大小 / 行数 |
| `read_document` | `filename*`, `offset`, `limit` | 分段读取（带行号，单次最多 500 行） |
| `search_document` | `filename*`, `query*` | 文档内关键词搜索（带上下文，最多 10 处） |

> `read_file` / `write_file` / `list_files` 的 `path` 是**相对 `skills/` 的路径**，
> 可带任意层级子目录（如 `writing/01-structure/write-structure.md`），也接受落在
> `skills/` 内的绝对路径。路径规则统一实现在 `app/tools/sandbox.py`。

### 生图（需 ComfyUI，`ENABLE_IMAGE_GEN=true`）

| 工具 | 参数 | 说明 |
|------|------|------|
| `generate_image` | `prompt*` 等 | 调 ComfyUI 生成图片；`---` 分隔可一次批量出图 |
| `get_workflow` | `skill_name*` 等 | 查看当前工作流（模型 / LoRA 链 / 采样参数 / 放大）+ ComfyUI 可用资源 |
| `update_workflow` | `skill_name*`, 语义化 ops | 用语义化操作调整工作流并写回 `workflow.json` |

### 向量知识库（RAG）

| 工具 | 参数 | 说明 |
|------|------|------|
| `search_kb` | `kb_name*`, `query*`, `k` | 在指定知识库检索相关片段 |
| `ingest_kb` | `kb_name*`, `entries*` | 将片段批量入库 |
| `list_kb` | — | 列出知识库及条目数 |
| `delete_kb` | `kb_name*` | 删除整个知识库 |
| `delete_entries` | `kb_name*`, `entry_ids*` | 按 id 批量删除片段 |
| `chunk_document` | `content*`, `source*`, `chunk_size` | 按标题/段落把长文档切成 500-800 字段落 |

---

## Skill 体系

`skills/<name>/` 是 Skill 的根，兼容两种风格：

**1）本地生图 Skill**

```
skills/image_gen_v1/
├── skill.md        # 规范说明（小写）
├── workflow.json   # ComfyUI 工作流模板（可选）
└── character.txt   # 角色底模（可选）
```

**2）标准 / 脚手架 Skill**（GitHub 下载风格）

```
skills/<name>/
├── SKILL.md          # 主规范（大写优先于 skill.md）
├── VERSION           # 版本（可选）
├── references/*.md   # 参考手册，加载时拼接附带
└── assets/
```

> 历史上兼容「同名多一层嵌套」（`skills/foo/foo/SKILL.md`）——`_resolve_skill_dir`
> 会下探一层。2026-09-15 迁移后 `skills/` 下已无此形态，逻辑保留以防再遇到。
> 该逻辑**只认「子目录名 == skill 名」**，`foo-main/foo/` 这种不会下探（会读到空）。

相关工具：`list_skills` 列顶层、`list_files` 看内部结构、`read_file` 按
`path` 读取任意层级文件（如 `writing/04-liveness/human-writing.md`）、`load_skill`
一次加载主规范 + 全部 references、`write_file` / `delete_file` 增删。
Agent 也能用 `write_file` 自己创建新 Skill。

---

## 向量知识库（RAG）

**架构**：主进程不直接持有向量库。`RagClient` 通过 stdin/stdout 的 JSON Lines
与常驻子进程 `rag_daemon.py` 通信，daemon 内持有 BGE-M3 与 ChromaDB。好处是
embedding / torch 这些重依赖被隔离在子进程里，首次调用才拉起（懒加载）。

**检索**：ChromaDB 用 cosine 距离；先取候选池 `max(k*5, 20)`，再用 MMR
（`λ=0.6`）选出 k 条兼顾相关性与多样性；`k` 默认 4。

**常用知识库**：`documents`（上传的通用文档）、`skills`（技能规范）、
`interview`（面试知识）。

> 首次使用会下载 BGE-M3（约 2GB）。不想用知识库就完全不碰这些工具。

---

## 上下文与缓存策略

这是本项目里"最工程"的一块，围绕一个目标：**尽量不打断 LLM 的 prefix cache 热前缀**。

- **稳定前缀**：`system` 内容固定；动态状态栏（时间/轮数/上次工具）**追加在消息数组末尾**，
  每轮只让它自己那几十 token 未命中，前面全部历史仍能命中缓存。
- **压缩阈值**（见 `app/memory.py`）：
  - 占用 ≥ 50%：仅记录，不压缩；
  - 占用 ≥ 80% **且** 命中率 < 30%：把旧轮一次性交给 LLM 摘要，替换为单条摘要消息；
  - 占用 ≥ 90%：无论命中率，强制压缩（保命，防爆上下文）；
  - 压缩冷却：距上次压缩 token 增长不足 10 万则不重复压缩；
  - 最近 3 轮始终完整保留，作为新热前缀的锚点。
- **会话持久化**：`data/session.jsonl` 采用原子写（写临时文件 → `flush`+`fsync`
  → `os.replace`），并有进程内写锁，避免读到半截文件。

---

## 测试

测试用**标准库 `unittest`** 编写，**零额外依赖**即可运行；装了 pytest 也能跑。
所有用例都不发真实网络请求（LLM / 工具 / Flask 客户端均被 mock）。

```bash
# 在项目根目录执行
python -m unittest discover -s tests -v     # 方式一：标准库
python -m pytest tests -v                   # 方式二：若已安装 pytest
```

覆盖范围：

| 文件 | 覆盖内容 |
|------|----------|
| `test_tool_protocol.py` | 工具协议解析：标准/简写/大小写/空白/嵌套 JSON/正文剥离 |
| `test_agent_loop.py` | Agent 主循环事件流、多工具、`pre_tool_results` 注入、异常兜底、轮次上限 |
| `test_memory.py` | 会话原子写（并发/损坏行/无残留临时文件）、压缩阈值与冷却 |
| `test_skills.py` | Skill 目录解析：大小写规范、同名嵌套、`__SEED__` 修复、references |
| `test_file_tools.py` | `skills/` 文件沙箱：子目录读写、`..` 与越界绝对路径拦截、分段读、`list_files` 深度与噪音过滤 |
| `test_documents.py` | 文档读取/搜索/上限与路径沙箱 |
| `test_tools_registry.py` | 注册表完整性、执行兜底、切块与知识库工具文案 |
| `test_web_api.py` | Flask 路由、`/api/chat` NDJSON 流 + 用户工具块预执行、文件名净化 |
| `test_rag_utils.py` | ChromaDB collection 名净化（未装 chromadb 时自动跳过） |

---

## 项目边界：代码 vs 私有数据

`.gitignore` 有意排除了以下路径——**代码是引擎，可跨机复用；这些是个人数据，
换机器时需要自己重新准备**：

- `.env`（密钥）
- `data/`（会话记录）
- `skills/`（个人技能）
- `documents/`（个人文档）
- `vector_store/`（向量库）

换一台机器后：`pip install -r requirements.txt` → 填 `.env` → 把资料放进
`skills/` / `documents/` → 用 `chunk_document` + `ingest_kb` 重建向量库即可。

---

## 已知限制 / Roadmap

**限制**

- 无鉴权、无多用户、**全局单会话**（一个 `session.jsonl`）；仅适合本机/可信局域网。
- 会话保存是整文件重写（已原子化，但未加跨进程文件锁；多进程同时写时以最后一次为准）。
- 依赖未锁版本（`requirements.txt` 全是 `>=`），换机器复现性一般。
- 没有 CI；`web_search` 的 DuckDuckGo 回退上游已停止维护，可能失败。
- `safe_collection_name` 对净化后长度 < 3 的知识库名（如 `x`）不会补足下限，
  而 ChromaDB 要求 3-63 字符，此类极短名字会在建 collection 时报错。

**可做**

- [ ] 多会话管理 + 会话列表
- [ ] 会话写的跨进程文件锁（为将来多 Agent 并发做铺垫）
- [ ] 工具结果定长上界（避免长文档整篇灌进上下文）
- [ ] 用 GitHub Actions 跑 `tests/`
- [ ] 锁定依赖版本
