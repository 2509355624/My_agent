# 如何完成项目部署（给编程 AI 看的）

> **读者是负责部署的编程 AI。** 人类用户只是想在 Windows 电脑上跑起这个项目的网页端：
> 一个「通用助手」聊天界面 + 一个生图台。你照第 2 节五步做完，再按第 8 节逐条验收即可。
>
> 项目已经预配置成「**一个 DeepSeek API Key + 一个本地 llama.cpp 模型**」就能跑通全部网页端功能，
> 生图只依赖 **anima-line** 一个 ComfyUI 工作流。**不要擅自增加渠道、不要改代码逻辑。**

---

## 0. 它是什么

| 模块 | 说明 |
|---|---|
| 网页端 | Flask 单进程，默认监听 `0.0.0.0:5174`，浏览器打开 `http://localhost:5174` |
| 主 agent | `agents/main/`：一个「通用助手」人设，工具全开（生图、查标签、读写文件、搜网页…） |
| 写作 agent | `agents/writing/`：一个「写作助手」，只挂 `writing` 一个 skill、上下文 100 万，专写长文 |
| 模型 | 主用 DeepSeek API，失败自动降级到本机 llama.cpp（都走 OpenAI 兼容接口） |
| 生图 | 通过 HTTP 调本机 ComfyUI（`127.0.0.1:8188`），跑 `skills/anima-line/workflow.json` |
| 数据 | 会话在 `agents/main/session.jsonl`，出图在 ComfyUI 的 `output/`，日志在 `logs/agent.log` |
| 不包含 | QQ 机器人（已关闭）、多渠道路由、RAG 知识库（可选装） |

**技术栈**：Python 3.10+ / Flask / requests / Pillow。没有数据库，没有前端构建步骤。

---

## 1. 前置条件（缺一不可）

- [ ] **Windows 10/11**，项目放在**纯英文路径**下（例如 `D:\agent_my_test`，不要放桌面/中文目录）
- [ ] **Python 3.10 ~ 3.12** 已安装，且 `python -V` 在任意 cmd 窗口能跑通
- [ ] **ComfyUI 已装好并能自己出图**（浏览器能打开 `http://127.0.0.1:8188`）
- [ ] **anima-line 工作流需要的模型文件都已放进 ComfyUI**（清单见第 4 节，**这一步最容易漏**）
- [ ] **一个 DeepSeek API Key**（https://platform.deepseek.com/api_keys）
- [ ] 可选：本机 **llama.cpp**（`llama-server`），不装也能只靠 DeepSeek 跑

> ⚠️ 不要把项目放进 `C:\Program Files` 或带空格/中文的目录：bat 脚本、venv 路径、ComfyUI 回传路径都会踩坑。

---

## 2. 五步跑通

### 第 1 步：解压
把 zip 解压到 `D:\agent_my_test`（或任意纯英文路径）。

### 第 2 步：装依赖
双击 **`安装依赖.bat`**。它会：
1. 用系统 `python` 建 `.venv`
2. 升级 pip
3. `pip install -r requirements.txt`（约 1~3 分钟）

装完窗口里会提示「装好了」。若因网络失败，按窗口里给的清华源命令重跑一次。

命令行等价操作（如需手动执行）：
```bat
cd /d D:\agent_my_test
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### 第 3 步：填 `.env`
项目根目录已有 `.env`（**里面只有占位符，没有任何真实密钥**）。用编辑器打开，**至少改这一行**：

```ini
DEEPSEEK_API_KEY=sk-在这里填你自己的
```

其余保持默认即可。逐项含义见第 3 节。

### 第 4 步：启动 ComfyUI
用 ComfyUI 自己的启动方式起服务，确认浏览器能打开 `http://127.0.0.1:8188`。

### 第 5 步：启动本服务
双击 **`一键启动.bat`**。它会：
1. 优先用 `.venv\Scripts\python.exe`，其次 `venv`，最后系统 `python`
2. 检查 flask 是否已装（没装会提示先去装依赖，不会「闪一下就没了」）
3. 杀掉占用 5174 的旧进程
4. 打开 `http://localhost:5174` 并前台运行服务

**关掉那个命令行窗口 = 停止服务。**

命令行等价：
```bat
cd /d D:\agent_my_test
.venv\Scripts\python.exe agent.py
```

---

## 3. `.env` 逐项说明

### 必填

| 键 | 值 | 说明 |
|---|---|---|
| `DEEPSEEK_API_KEY` | `sk-...` | 唯一必填项 |
| `DEEPSEEK_MODEL` | `deepseek-chat` | 通用对话；`deepseek-reasoner` 更慢，不建议 |
| `IMAGE_DEFAULT_SKILL` | `anima-line` | **默认生图渠道**，必须和 `skills/` 下真实存在的目录名一致 |

### 常用可调

| 键 | 默认 | 说明 |
|---|---|---|
| `LLM_PROVIDER` | `deepseek` | 主模型。想「本地为主」改成 `llama` |
| `LLM_FALLBACK_CHAIN` | `deepseek:deepseek-chat,llama:local` | 降级顺序，逗号分隔，`provider:模型名` |
| `LLAMA_BASE_URL` | `http://127.0.0.1:8081/v1` | llama.cpp 的 llama-server，**必须带 `/v1`** |
| `LLAMA_MODEL` | `local` | 仅用于日志展示，llama-server 不校验 |
| `LLAMA_NUM_CTX` | `32768` | **必须等于 llama-server 启动参数 `-c` 的值**，代码用它给上下文预算封顶 |
| `CONTEXT_BUDGET` | `100000` | 上下文预算（token）。写长文的关键，见第 6 节 |
| `PROMPT_BRIEF` | `1` | 工具描述只留首句，系统提示从约 1.6 万字符压到 5 千 |
| `COMFYUI_URL` | `http://127.0.0.1:8188` | ComfyUI 地址 |
| `COMFYUI_OUTPUT_DIR` | 空 | 填 ComfyUI 的 `output` 目录可多一层「HTTP 失败从磁盘兜底」 |
| `IMAGE_GEN_TIMEOUT` | `3600` | 单张图上限（秒）。冷启动第一次要留余量 |
| `AGENT_PORT` | `5174` | 网页端口 |
| `MAX_TURNS` | `10` | 单次对话最多几轮工具调用 |
| `ENABLE_RAG` | `0` | 0 = 不启用知识库工具（默认） |
| `QQ_ENABLE` | `false` | **保持 false**，本项目不含 QQ 部分 |

### 识图相关（可选，见第 6 节）

`VISION_PROVIDER` / `VISION_MODEL`：留空 = 关闭识图（引用图反推提示词会失败，其它功能不受影响）。

---

## 4. 生图渠道 anima-line 的依赖（**重点**）

`skills/anima-line/workflow.json` 是一个**已导出的 ComfyUI API 工作流**（20 个节点），
里面按**文件名**引用模型。文件必须存在且同名，否则出图时报
`ComfyUI 报错 / 找不到模型`。

| 节点 | ComfyUI 目录 | 文件名 |
|---|---|---|
| `UNETLoader`（主底模） | `models/diffusion_models/`（旧版 `models/unet/`） | `silvermoonmixAnimaEvolved_v2329BTurbo.safetensors` |
| `UNETLoader`（第二段底模） | 同上 | `miaomiaoRealskin_anima13.safetensors` |
| `CLIPLoader`（文本编码） | `models/text_encoders/`（旧版 `models/clip/`） | `qwen_3_06b_base.safetensors` |
| `VAELoader` | `models/vae/` | `qwen_image_vae.safetensors` |
| `UpscaleModelLoader` | `models/upscale_models/` | `2x_Ani4Kv2_G6i2_Compact_107500.pth` |
| `LoraLoaderModelOnly` ×6 | `models/loras/` | `ANIMA_MODARE_V2.safetensors`<br>`_cowani_epoch_10.safetensors`<br>`anima_context_detailer_base10.safetensors`<br>`miaomiaoRealskin_anima13.safetensors`<br>`reina_v1_epoch14.safetensors`<br>`star cat-000022.safetensors` |

> 文件名区分大小写与空格（`star cat-000022` 中间有空格）。

**怎么快速核对**：让助手画一张最简单的图，成功就说明依赖齐了；失败时看
`logs/agent.log` 里 ComfyUI 回传的错误，通常是「`Value not in list` / 找不到 xxx.safetensors」。

**如果你手上这份工作流的模型路径不一样**：不要改代码，直接编辑
`skills/anima-line/workflow.json` 里对应的文件名字段即可（那是纯 JSON 模板，
`__SEED__`、`__WIDTH__` 这类占位符由代码替换，不要动它们）。

---

## 5. 本地 llama.cpp 接入（可选但推荐）

```bat
llama-server -m D:\models\你的模型.gguf --port 8081 -c 32768 -ngl 99
```

- 端口要和 `.env` 的 `LLAMA_BASE_URL` 对上（默认 8081）
- **`-c` 必须和 `.env` 的 `LLAMA_NUM_CTX` 填同一个数**（这里都是 32768）。
  填小了，`CONTEXT_BUDGET` 设多高都会被按这个窗口打折：
  16384 的窗口只能用到约 1.1 万 token，写长文会一直触发压缩
- 想让它能读图，加 `--mmproj D:\models\mmproj.gguf`（见第 7 节）
- `-c` 越大越吃显存/内存，12 GB 显卡先试 32768；不够就降回 16384 并接受写短篇

验证：
```bat
curl http://127.0.0.1:8081/v1/models
```
返回 JSON 里能看到模型即可。

**切换主备**：
```ini
# 本地为主、DeepSeek 兜底
LLM_PROVIDER=llama
LLM_FALLBACK_CHAIN=llama:local,deepseek:deepseek-chat
```

---

## 6. 上下文与写作助手（写长文必看）

### 为什么要把预算调大

系统提示 + 工具说明**每一轮都要重新发一遍**，先占掉一部分预算，剩下的才轮到聊天史和
读进来的文件。默认 `CONTEXT_BUDGET=32000` 时，固定开销能占掉一半以上；而
`skills/writing/01-structure/write-structure.md` 一份就 22 KB（约 1.3 万 token），
读两份规范预算就满了，于是触发压缩：旧内容被摘成一小段，细节丢失，长文越写越飘。

包里已经预设成：

```ini
CONTEXT_BUDGET=100000   # DeepSeek 窗口 100 万，装得下
PROMPT_BRIEF=1          # 工具描述只留首句，固定开销减半
```

**不要去调小**。这一项直接决定长文质量。

### 两个 agent 怎么选

| agent | 目录 | 上下文 | 看得见的 skill | 用途 |
|---|---|---|---|---|
| 通用助手 | `agents/main/` | 继承 `.env`（10 万） | 全部 | 聊天、生图、查标签、日常问答 |
| 写作助手 | `agents/writing/` | 自己带 **100 万** | 只有 `writing` | 写小说 / 短文 / 改稿，不碰生图 |

写作助手单独的预算写在 `agents/writing/agent.json` 的 `context_budget`（0 表示继承全局）。
它的工具里没有 `generate_image`——那一段描述本身就有 9 千多字符，去掉之后留给正文的空间更多。

**在网页上切换**：界面顶部的 agent 下拉框（`GET /api/agents` 的列表）里选「写作助手」即可。
切换后是**另一条会话线**，历史不互通，这是设计如此。

### 没装 RAG 也能写

写作助手的说明书里原本有一步「`search_kb` 取 3~5 条范文」。没装知识库时这个工具
**不会注册**，模型看不见它，那一步会被自动跳过——已经在该 agent 的人设里写明了
「看不到就跳过，别报错、别编造范文」。所以**不装 RAG 不影响写作流程**，只是少了范文参考。

## 7. 识图（可选）

「引用一张图 → 反推提示词」需要**多模态**模型。DeepSeek 官方接口没有视觉能力，所以：

- llama.cpp 起的模型**带 `--mmproj`**：
  ```ini
  VISION_PROVIDER=llama
  VISION_MODEL=local
  ```
- 或本地装了 ollama 且拉过视觉模型：
  ```ini
  VISION_PROVIDER=ollama
  OLLAMA_VISION_MODEL=qwen2.5vl:7b
  ```
- 两者都没有：**留空**。此时纯文字聊天、文生图、图生图（垫图）都正常，
  只有「反推提示词」这一类会报错。

---

## 8. RAG 向量知识库（可选，默认不装）

**默认不装，也不影响任何功能。** 没装时 `search_kb` / `ingest_kb` 等工具根本不会注册，
模型也看不见它们 —— 所以不用去「关掉」什么。

想装：

```bat
cd /d D:\agent_my_test
.venv\Scripts\python.exe -m pip install -r requirements-rag.txt
```

装完后改 `.env`：

```ini
ENABLE_RAG=1
LOCAL_EMBEDDING_MODEL=BAAI/bge-m3
RAG_EMBED_DEVICE=cpu        # 有独立显卡且装了 CUDA 版 torch 才写 cuda
RAG_AUTO_DOWNLOAD=1         # 首次自动下载 BGE-M3（约 2 GB）
RAG_RETRIEVE_K=4
```

- 数据落在 `vector_store/chroma/`，可以直接备份/删除
- 嵌入模型跑在**独立子进程**（`app/rag/rag_daemon.py`），首次调用会加载模型，第一下会慢
- 没网的环境：先手动把 BGE-M3 下到本地，把 `LOCAL_EMBEDDING_MODEL` 指到那个**目录**，
  并保持 `RAG_AUTO_DOWNLOAD=0`

验证：在聊天里发「把 documents/ 下的 xx 存进知识库，然后搜一下 xx」，能返回片段即成功。

---

## 9. 验收清单（逐条打勾）

| # | 检查 | 操作 | 期望 |
|---|---|---|---|
| 1 | 服务起来了 | 双击 `一键启动.bat` | 窗口打印 `本机: http://localhost:5174`，无 Traceback |
| 2 | 首页可访问 | 浏览器开 `http://localhost:5174` | 出聊天界面 |
| 3 | 技能列表正确 | `http://localhost:5174/api/skills` | 只出现 `anima-line` / `anima-tags` / `storyboard-prompt` / `writing` 等白名单渠道，**没有** krea2 / miao / moss 之类 |
| 4 | 能聊天 | 界面上发「你好」 | 有中文回复（说明 DeepSeek key 与网络正常） |
| 5 | 能降级 | 临时把 key 改错重启 | 回复变慢但**仍有回复**（走本地 llama），日志出现降级记录 |
| 6 | 能出图 | 发「画一只猫」 | 返回图片；`logs/agent.log` 里有 `[直达生图] anima-line` |
| 7 | 生图台 | 打开 `http://localhost:5174/gen` | 页面可选到 `anima-line`，提交后出图 |
| 8 | 标签库可用 | 聊天里问「初音未来 有哪些 tag」 | 返回一串标签候选（说明 `anima-tags` 索引读到了） |
| 9 | 写作助手可用 | 界面 agent 下拉框里能看到「写作助手」，切过去发「写一段 300 字的小说开头」 | 有正文返回；回复里**不应**出现「search_kb 失败」之类报错 |
| 10 | 日志 | 看 `logs/agent.log` | 有启动、模型调用、生图记录，无反复重试 |

任何一条不过，看第 11 节。

---

## 10. 项目结构（哪些能动、哪些别动）

```
agent_my_test\
├─ agent.py                  # 启动入口
├─ app\                      # 后端：Flask 路由、agent 循环、工具、生图管道
├─ web\                      # 前端页面（纯静态 html，改完刷新即可）
├─ agents\main\              # 「通用助手」的人设 prompt.md + agent.json
├─ agents\writing\           # 「写作助手」：只挂 writing skill，上下文 100 万
├─ skills\                   # 技能：每个目录 = 一个生图渠道或一套提示词
│   ├─ anima-line\           #   ← 唯一的生图渠道（workflow.json + skill.md）
│   ├─ anima-tags\           #   ← 标签库（13 MB，search_tags 依赖）
│   ├─ storyboard-prompt\    #   ← 漫画/剧情编剧规范
│   └─ writing\              #   ← 写作
├─ state\ usage\ logs\       # 运行时自动创建：会话状态、用量、日志
├─ .env                      # 配置（不要提交到任何公开仓库）
├─ 一键启动.bat / 安装依赖.bat
└─ requirements.txt / requirements-rag.txt
```

**可以放心改的**：`.env`、`agents/main/prompt.md`（人设，热加载）、`web/*.html`（热加载）。
**别删的**：`skills/anima-tags/data/`（标签库，删了 `search_tags` 全废）。
**别改的**：`app/` 里的代码逻辑——除下面两种情况：

1. 想**换默认生图渠道** → 只改 `.env` 的 `IMAGE_DEFAULT_SKILL`，不要改代码
2. 想加**新渠道** → 在 `skills/` 下建目录（`workflow.json` + `skill.md`，`kind: 生图`），
   程序会自动扫到，不用注册

---

## 11. 常见故障

| 现象 | 原因 | 处理 |
|---|---|---|
| 双击 bat 闪一下就没 | Python 不在 PATH | 装 Python 时勾 `Add python.exe to PATH`；或在 cmd 里手动跑 bat 看报错 |
| `[X] 依赖还没装（缺 flask）` | 没跑安装脚本 | 先双击 `安装依赖.bat` |
| 端口 5174 被占用 | 旧进程没退 | 一键启动会自动杀；仍不行 `netstat -ano \| findstr :5174` 手动 `taskkill /PID xxx /F` |
| 出图报「找不到 workflow / 未知渠道」 | `IMAGE_DEFAULT_SKILL` 与 `skills/` 目录名不一致 | 核对目录名（`anima-line`，不是 `anima线条`） |
| 出图报 `Value not in list` / 找不到 xxx.safetensors | 第 4 节模型缺文件或名字不一致 | 按表核对，注意大小写与空格 |
| 出图一直转圈 | 模型冷启动 / 显存不足 | 第一次等 1~2 分钟；`nvidia-smi` 看显存 |
| 聊天报 401 | DeepSeek key 错或没余额 | 换 key，看 `logs/agent.log` 的 HTTP 状态 |
| 聊天很慢 | 主模型挂了在降级 | 日志里会写拉黑哪家、切到哪家；`LLM_FALLBACK_CHAIN` 调顺序 |
| pip 安装 SSL/超时 | 网络或系统代理 | 加 `-i https://pypi.tuna.tsinghua.edu.cn/simple`；HTTP 请求代码里已 `trust_env=False` 忽略系统代理 |
| 引用图反推失败 | 没配识图模型 | 见第 7 节，或接受这个功能不可用 |
| 中文乱码 | 在非中文 Windows 上跑 bat | bat 是 GBK 编码；用 cmd 手动跑 `python agent.py` 即可绕过 |

---

## 12. 不要做的事

1. **不要把 `.env` 提交到公开仓库**——里面有 API Key。
2. **不要为了「支持更多渠道」去改 `app/`**：本包只带 anima-line 一个工作流，
   其它渠道名写进配置只会让请求落空。
3. **不要开 `QQ_ENABLE=true`**：本包不含 QQ 相关 agent 与协议端依赖，
   需要的话那是另一个功能模块。
4. **不要删 `skills/anima-tags`**：它 13 MB，看着像冗余，其实是标签检索的数据源。
5. **不要用 `pip install -r requirements.txt` 装到全局 Python**：统一用 `.venv`。
