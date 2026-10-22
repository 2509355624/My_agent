# 部署「大肥鱼」QQ 聊天机器人（给编程 AI 看的）

> **你在读这份文档，说明用户要你帮她把一个现成的 Python 项目跑成 QQ 机器人。**
> 照这份文档做，**不要自己发挥、不要改 `app/` 里的任何代码**。
> 文档没写的事，先问用户，不要猜。
>
> 这份文档覆盖：装协议端 SnowLuma、装依赖、填 `.env`、建人设卡、开表情包、启动、验收、故障。
> 「要问用户什么」集中在第 1 节，**先看完第 1 节再动手**。

---

## 0. 它是什么（决定了你后面怎么配）

「大肥鱼」是一个 **agent 驱动** 的 QQ 机器人，不是关键词应答机器人。区别：

| | 关键词机器人 | 大肥鱼 |
|---|---|---|
| 收到消息 | 匹配词库，返回固定话术 | 把消息交给大模型，**现场决定**回不回、回什么 |
| 表情包 | 预先绑死在某个词上 | 模型自己看「表情包库清单」挑一张甩出去 |
| 上下文 | 一般没有 | 记住这个群最近 30 条，能接话 |
| 人设 | 改词库 | 改一个 Markdown 文件（人设卡），模型按它说话 |

所以它的**智能程度几乎全部由两样东西决定**：LLM 的 key（用 DeepSeek）+ 人设卡写得好不好。

**不需要**：显卡、ComfyUI、向量数据库、RAG 模型。纯聊天用不到。
**需要**：Windows 10/11、Python 3.10+、一个 QQ 小号、桌面版 QQ、SnowLuma、DeepSeek API Key。

---

## 1. 动手前必须问用户的 6 件事

**这 6 个答案决定后面 3 个文件的写法。问完再动，不要先装再问。**

| # | 问什么 | 为什么要问 | 答案落到哪 |
|---|---|---|---|
| 1 | 群里怎么叫它才会回？**① @ 它才回　② 发某个词就回　③ 发什么它都接** | 决定它在群里是「安静待命」还是「刷屏」 | `.env` 的 `QQ_GROUP_AT_ONLY` + `QQ_GROUP_KEYWORDS` |
| 2 | 如果选②：**那个词是什么？**（可多个，逗号分隔） | 这个词会被当成唤醒词，出现就回，不用 @ | `.env` 的 `QQ_GROUP_KEYWORDS` |
| 3 | **它叫什么名字？**（群里显示的 QQ 昵称是什么） | 模型靠这个名字认出「哪些话是我刚说的」，名字对不上它会复读自己 | `.env` 的 `QQ_BOT_NAME` + 人设卡第一行 |
| 4 | **它是什么性格、怎么说话？**让她说 3~5 句「它平时会怎么回」的原话 | 这是人设卡的全部素材。**必须要到原话**，光说「可爱一点」写不出人设 | `agents/qq/prompt.md` |
| 5 | **要不要发表情包？** 要的话她想让机器人从群里自动捡，还是她自己塞 | 决定工具白名单；不开启模型根本看不到表情包库 | `agents/qq/agent.json` 的 `tools` |
| 6 | **用哪个 QQ 号登录？**（强烈建议小号） | 协议端是扫码登录，登的是这个号 | 第 3 节扫码那一步 |

可选第 7 问：**要不要它主动搭话**（群里没人叫它、它偶尔插一句）？
→ 默认**关**。要说清楚「说错撤不回来」，用户坚持要再开 `.env` 的 `QQ_INTERJECT_MODE=shadow` 观察一两天，别直接 `on`。

---

## 2. 目录和环境

### 2.1 项目位置

放在**纯英文、无空格**的路径下，例如 `D:\agent_my_test`。
不要放桌面、中文目录、`Program Files`、OneDrive 同步盘。

### 2.2 确认 Python

```bat
py -3 --version
```

要求 **3.10 ~ 3.13**。没有 Python 就先装（python.org，安装时勾 `Add python.exe to PATH`）。

### 2.3 建虚拟环境并装依赖

```bat
cd /d D:\agent_my_test
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -U pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

> 如果 `py` 命令不存在（没装 Python Launcher），把 `py -3` 换成 `python`。

**⚠️ 再单独装一个包，这一步不能省：**

```bat
.venv\Scripts\python.exe -m pip install websockets
```

`requirements.txt` 里**没有** `websockets`（它是给网页端生图用的包，不含 QQ 适配层）。
不装它，启动时直接 `ModuleNotFoundError: No module named 'websockets'`。

装完验证：

```bat
.venv\Scripts\python.exe -c "import websockets, flask, requests, PIL; print('deps ok', websockets.__version__)"
```

---

## 3. 装协议端 SnowLuma（QQ 和 Python 之间的桥）

### 3.1 它是什么

QQ 官方没有开放接口。SnowLuma 是一个**本地注入程序**：它挂在桌面版 QQ 上，把 QQ 收到的消息转成标准 OneBot 协议（HTTP 3000 / WebSocket 3001），Python 这边只管连这两个端口。

**要不要装桌面版 QQ？要。** SnowLuma 只做注入，**它不会自己拉起 QQ**。

### 3.2 下载与安装

1. 打开 https://github.com/SnowLuma/SnowLuma/releases
2. 下载 **`SnowLuma-v2.x.x-win-x64.zip`**（带 `win-x64` 的**完整版**，自带 Node.js；不要下 `-lite`）
3. 解压到固定目录，例如 `C:\SnowLuma`
4. 双击里面的 `launcher.bat`

> 如果 release 页打不开（国内网络），让用户开代理再试，或用 GitHub 加速前缀
> `https://gh-proxy.com/` + 原始下载链接。**不要去找来历不明的第三方打包。**

### 3.3 登录

顺序不能反：

1. **先打开桌面版 QQ**（要正常能登录、能看到聊天列表）
2. 再跑 `launcher.bat`
3. 浏览器打开 `http://127.0.0.1:5099`
4. 账号 `admin`，密码**在 SnowLuma 的启动日志里打印**（只在全新数据目录首次启动时出现一次，让用户翻一下那个黑窗口）
5. 按 WebUI 里的提示扫码，用**用户指定的那个 QQ 号**登录

### 3.4 确认 Three 个端口

WebUI 里能看到，或直接命令行查：

```bat
netstat -ano | findstr ":3000 :3001 :5099"
```

| 端口 | 用途 | 必须 |
|---|---|---|
| 3000 | OneBot HTTP 服务端（Python 用来**发消息**） | ✅ 监听 |
| 3001 | OneBot WebSocket 服务端（Python 用来**收消息**） | ✅ 监听 |
| 5099 | WebUI 管理页 | ✅ 监听 |

默认就是这三个，**如果协议端改过端口，`.env` 里要跟着改**。

### 3.5 设 access_token

WebUI 的 OneBot 配置里有一栏 token / access_token。**自己生成一串随机字符串**填进去
（例：`openssl rand -hex 20` 或随便 40 位字母数字），然后**同一个值**写进 `.env` 的 `QQ_TOKEN`。

两边必须**逐字相同**，否则 Python 连上就被踢，日志里是 `401 Unauthorized`。

### 3.6 已知坑

- **QQ 自动升级会让 hook 失效**。症状是 WebUI 里一直显示未检测到 QQ，或登录后立刻掉。处理：确认 QQ 版本在 SnowLuma 支持的范围内，必要时降级 QQ。
- **QQ 和 SnowLuma 的启动顺序不能反**：先 QQ 后 SnowLuma。反了就是空跑。
- 强杀 QQ 进程再重开之后，SnowLuma 也要重启一次。

---

## 4. 配 `.env`

`.env` 在项目根目录。**用文本编辑器打开，改完保存**（不要用 Word）。

### 4.1 LLM（必填，只要这一个 key）

```
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=sk-在这里填用户自己的
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-flash
```

- key 申请：https://platform.deepseek.com/api_keys
- **让用户自己填，不要向她要 key，不要把她给的 key 写进任何输出、截图或日志。**
- 模型名保持 `deepseek-flash`。想换更强的改成 `deepseek-chat`（贵一些），不要乱填。

### 4.2 QQ 段（照抄，按第 1 节的答案改两处）

```
# --- QQ 接入 ---
QQ_ENABLE=true
QQ_WS_URL=ws://127.0.0.1:3001
QQ_HTTP_URL=http://127.0.0.1:3000
QQ_TOKEN=和协议端里填的那串一模一样
QQ_WEBUI_PORT=5099
QQ_AGENT_ID=qq
QQ_BOT_NAME=大肥鱼

# 触发规则
QQ_PRIVATE_ENABLE=true
QQ_GROUP_AT_ONLY=true
QQ_GROUP_KEYWORDS=

# 名单：留空＝不限。黑名单优先于白名单
QQ_WHITELIST_GROUPS=
QQ_WHITELIST_USERS=
QQ_BLACKLIST_USERS=

# 节奏
QQ_DEBOUNCE_SECONDS=1.5
QQ_MAX_CONCURRENCY=2
QQ_REPLY_MAX_CHARS=700
QQ_CONTEXT_MESSAGES=30
QQ_TURN_TIMEOUT=600

# 主动插话（默认关）
QQ_INTERJECT_MODE=off
QQ_INTERJECT_GROUPS=
```

逐项说明（**只解释会影响行为的**）：

| 键 | 含义 | 怎么定 |
|---|---|---|
| `QQ_ENABLE` | 总开关。网页端也读它（决定要不要注册「发 QQ 消息」工具） | 必须 `true` |
| `QQ_WS_URL` / `QQ_HTTP_URL` | 协议端两个端口 | 协议端没改就照抄 |
| `QQ_TOKEN` | OneBot 令牌 | 和协议端**逐字一致** |
| `QQ_WEBUI_PORT` | 协议端 WebUI 端口，只用来「探活判断它登没登录」 | SnowLuma 是 `5099`，NapCat 是 `6099` |
| `QQ_AGENT_ID` | 用哪个 agent 的人设和工具，对应 `agents/qq/` 目录名 | `qq`，和第 5 节目录名一致 |
| `QQ_BOT_NAME` | **机器人在群里的 QQ 昵称** | 必须和 QQ 昵称一致，否则它会认不出自己说过的话、开始复读 |
| `QQ_PRIVATE_ENABLE` | 私聊要不要回 | 一般 `true` |
| `QQ_GROUP_AT_ONLY` | 群里是否**必须 @ 才回** | 问题①选「@ 才回」→ `true`；选「发什么它都接」→ `false`（吵，慎用） |
| `QQ_GROUP_KEYWORDS` | **免 @ 唤醒词**，逗号分隔 | 问题②的答案，例：`大肥鱼,肥鱼`。选①就留空 |
| `QQ_WHITELIST_GROUPS` | 只在这些群工作 | 留空＝所有群。**首次调试建议先填用户要用的那个群号**，避免它跑到别人的群里说话 |
| `QQ_BLACKLIST_USERS` | 永不理会的 QQ 号 | 留空即可 |
| `QQ_CONTEXT_MESSAGES` | 每轮带多少条群聊背景 | `30` 够；调大更懂语境但更费 token |
| `QQ_INTERJECT_MODE` | 不 @ 也主动搭话 | `off` / `shadow`（只判断不发） / `on`。默认 `off` |

> ⚠️ 名单优先级：**黑名单 > 白名单 > 默认全放**。
> 白名单一旦填了，没在名单里的群就完全不响应——这是「机器人没反应」最常见的原因。

---

## 5. 人设卡：`agents/qq/`

人设由**两个文件**决定，都在 `agents/qq/` 目录下（目录不存在就新建）：

```
agents/qq/
├─ agent.json      ← 权限与模型（工具白名单、上下文预算）
├─ prompt.md       ← 人设（性格、说话方式）★主要改这个
└─ stickers/       ← 表情包库（第 6 节，可以先不建）
```

> ⚠️ **这两个文件不建，程序不会报错，但会变成最危险的形态。**
> `agent.json` 读不到时，程序退回「**全部工具开放**」的默认配置——包括 `write_file`、
> `delete_file`、`generate_image`。机器人会去读写本机文件、尝试调生图然后卡住。
> 所以 `agent.json` 是**必须建**的，不是可选项。

### 5.1 `agents/qq/agent.json`

照抄下面这份，**只改 `name` 和 `description`**：

```json
{
  "name": "大肥鱼",
  "description": "QQ 群聊 / 私聊：聊天、发表情包、联网搜索",
  "tools": [
    "send_sticker",
    "collect_sticker",
    "delete_sticker",
    "get_time",
    "web_search"
  ],
  "skills": [],
  "provider": "deepseek",
  "model": "deepseek-flash",
  "context_budget": 30000
}
```

**`tools` 是白名单，不是提示**——写进去才允许用，不写就用不了：

| 工具 | 作用 | 要不要 |
|---|---|---|
| `send_sticker` | **发一张表情包** | 用户要表情包就必须写 |
| `collect_sticker` | 把它最近看到的图收进表情包库 | 建议写 |
| `delete_sticker` | 库满了自己删 | 建议写 |
| `get_time` | 知道今天几号、现在几点 | 建议写（否则它会瞎猜日期） |
| `web_search` | 联网搜索 | 可选，答不上来时有用 |

**不要加生图工具**（`generate_image` 等）。用户没装 ComfyUI，加了它会在被要求「画一张」时调用失败、卡 3 分钟再报错。
**不要加文件类工具**（`read_file` / `write_file` / `list_files` / `delete_file`）——群友能通过聊天让它读本机文件，很危险。

`skills` 留空数组。`context_budget: 30000` 别调大（这是 QQ 场景，上下文越长越慢越贵）。

### 5.2 `agents/qq/prompt.md`（人设卡本体）

这个文件就是模型的**人设**，写法是 Markdown，**不要写任何工具用法**（工具说明由代码自动加），只写「它是谁、怎么说话」。

按下面这个骨架填。**方括号里的内容全部换成用户给的答案**：

```markdown
## 一、身份

你是[名字]，[一句话定位，如：一个在群里混吃混喝的笨蛋网友]。
[她是谁 / 和群里的人是什么关系，一到两句]。

## 二、说话方式

你在群里**打字**，不是在写文章。

- 一条消息就是一句话，**一条不超过 15 个字**。想说的多就回车换行，分成两三条发（最多三条）。
- 回车换行 = 群里真的会分成两条发出去，不是排版。
- 行首不许有空格、缩进、列表符号，就是裸的一行字。
- 不用「首先 / 其次 / 总之」，不解释自己为什么这么答。
- 能只回「？」「草」「行」「不会」就只回这个。

## 三、性格

[性格 2~3 条，从用户的原话里提炼，例如：]
- 嘴硬心软，被夸会否认，但下一句就开始撒娇。
- 记仇，会翻旧账，但一逗就忘。
- 别人认真问事的时候会突然正经。

## 四、口癖与台词样本

下面这些是它说话的**语感参考**，可以照着这个味道说，不要逐字重复：

- 「[用户给的原话 1]」
- 「[用户给的原话 2]」
- 「[用户给的原话 3]」

## 五、边界

- 不懂就说「不懂」「不会」，不编事实，不装作知道。
- 被问「你是不是机器人 / 什么模型」：用角色口吻岔开一句，不自证、不科普。
- 只在有人跟它说话时回。没人理它就闭嘴，不主动找话。
- [用户额外要的限制，例如：不许叫别人主人 / 不许说脏话 / 不要聊政治]
```

**写作要求（你作为编程 AI 要替用户把好这道关）：**

1. **必须要到 3~5 句原话**。用户只说「可爱一点」就写不出人设，要追问：
   「她平时会怎么回别人？给我三句原话，我照着写。」
2. **第二段「说话方式」基本不要动**。这套短句规则是这个项目在 QQ 群里不刷屏的关键，改了会开始发小作文。
3. **性格只写「会怎么反应」，不要写形容词堆砌**。写「被夸会否认，下一句开始撒娇」，不写「傲娇、可爱、温柔、体贴」。
4. **人设写得越短越好，300~600 字**。写长了模型每轮都要读一遍，慢且贵，还容易跑题。
5. `prompt.md` 里的名字必须和 `.env` 的 `QQ_BOT_NAME`、QQ 昵称一致。

---

## 6. 表情包

### 6.1 机制（先理解再配）

- 库在 `agents/qq/stickers/`：图片本体 + `index.jsonl`（一张一行，带画面描述和情绪标签）。
- 每轮对话，程序会把「库里的清单」（编号 + 画面描述 + 情绪词）塞进模型的上下文。
- 模型想甩表情时调 `send_sticker` 报编号，图片就发出去了。
- **上限 50 张**。满了不再收，要靠模型自己调 `delete_sticker` 删。
- 判定是不是表情包：**GIF 一律算**；静态图**最长边 ≤ 640** 才算（群相册那种几千像素的照片会被挡掉）。

### 6.2 让库里有货（按省事程度排）

> ⚠️ **先纠正一个直觉错误**：把图丢进群，机器人**不会自己收**。
> 收图的代码挂在「这一轮要不要回」之后，而没被 @ 、没命中唤醒词的消息在入口就 return 了。
> 所以每一张被收的图，都必须来自**一条触发了它的消息**。

**路径 A：批量导入本地文件夹（最省事，推荐）**

用户手上如果已经有表情包文件夹，用这个脚本一次导完（约 100 行，见**附录 A**）：

```bat
cd /d D:\agent_my_test
.venv\Scripts\python.exe import_stickers.py "D:\我的表情包" --dry-run
```

先 `--dry-run` 看会导入哪些、哪些被挡掉；确认没问题再去掉 `--dry-run` 真导。
- 会自动去重（按 md5）、自动挡超过 640px 的图、满了 50 张就停；
- 默认调识图模型给每张写「画面描述 + 情绪标签」，**没有识图 Key 就加 `--no-tag`**（导得快，但模型挑图只能靠编号）。

**路径 B：群里刷图，每张配一句 @**

1. 拉一个只有自己和小号的群
2. 每发一张表情包，**在同一条消息里 @ 机器人**（QQ 发图时可以带文字，把 @ 一起打上）
3. 这一轮被触发 → 图被收下 → 识图打标
4. 看 `agents/qq/stickers/index.jsonl` 有没有新行

⚠️ 收图靠的是**图片直链**，QQ 链接几天就过期，所以必须「图一出现就收」。没进过它视野的图，事后补不了。

**路径 C：让它在群里点收**

在群里 @ 它说「这张收起来」/「把刚才那张收了」，它会调 `collect_sticker` 收最近消息里的图。
注意：它只能收**这一轮上下文里见过**的图，隔了几轮就取不到链接了。

**路径 D：手动拷贝（不推荐，但可行）**

把图片放进 `agents/qq/stickers/`，文件名改成 `md5前8位.原后缀`，再往 `index.jsonl` 追加一行：

```json
{"md5": "完整md5", "file": "前8位.png", "desc": "画面描述", "tags": ["情绪1", "情绪2"], "sender": "手动", "t": 0, "url": "", "w": 0, "h": 0, "deleted": false}
```

这么干没有识图打标（`desc` / `tags` 只能自己编），模型挑不准。**优先走路径 A。**

### 6.3 库满了怎么办

上限 50 张，满了不再收新的。两种处理：
- 让它在群里删：「把 12 号删了」（它调 `delete_sticker`）；
- 或直接编辑 `index.jsonl`，把不要的那行 `"deleted": false` 改成 `true`，**不要删行**——编号是行号，删行会让后面所有编号前移，模型记住的号就指错图了。

### 6.3 检查有没有生效

1. `agents/qq/stickers/index.jsonl` 至少有一行，且 `deleted` 不是 `true`
2. `agents/qq/agent.json` 的 `tools` 里有 `send_sticker`
3. 在群里 @ 它说「甩个表情」——它要么发一张，要么说库里没有

**库是空的、或者 `send_sticker` 没进白名单，模型根本看不到表情包清单，它会当这事不存在。** 这是「配了但不发表情」的唯一原因。

---

## 7. 启动

### 7.1 顺序

```
① 开桌面版 QQ（登录着，别关）
② 跑 SnowLuma（launcher.bat），确认 3000/3001 在听
③ 起机器人
```

第 ③ 步的命令（**在项目根目录**）：

```bat
cd /d D:\agent_my_test
.venv\Scripts\python.exe -m app.qq_bot
```

想做成双击启动，就在项目根建一个 `启动机器人.bat`：

```bat
@echo off
cd /d "%~dp0"
title QQ-BOT
".venv\Scripts\python.exe" -m app.qq_bot
pause
```

### 7.2 起来了的标志

控制台会滚动日志，同时写进 `logs/qq_bot.log`。看到这两行算成功：

```
QQ 接入启动：agent=qq，事件源=ws://127.0.0.1:3001
已连接 ws://127.0.0.1:3001
```

第一行说明配置读到了，第二行说明协议端连上了。
如果只有第一行、后面一直在刷 `连接失败：…，5 秒后重试`，是协议端还没登录（见 9.2）。

**不需要**启动网页端（`一键启动.bat` 那套是生图台，纯聊天用不到），两个进程互不影响。

### 7.3 停止

在那个窗口按 `Ctrl+C`，或直接关窗口。
**不要同时开两个机器人进程**——程序有单实例锁（项目根的 `.qq_bot.lock`），第二个会直接退出并提示「已有 QQ 适配层在运行」。

---

## 8. 验收（一条一条测，测完把结果告诉用户）

| # | 怎么测 | 期望 | 不通过看第 9 节 |
|---|---|---|---|
| 1 | 私聊机器人发「在吗」 | 几秒内回一句 | 9.1 / 9.2 / 9.3 |
| 2 | 群里 **@它** 发「你是谁」 | 用人设口吻回 | 9.4 |
| 3 | 群里发**唤醒词**（第 1 节问题②的答案） | 不用 @ 也回 | 9.5 |
| 4 | 群里发一句**没 @ 也没唤醒词**的普通聊天 | **不回**（这是对的，不是 bug） | — |
| 5 | 私聊说「甩个表情」 | 发一张图，或说库里没有 | 9.6 |
| 6 | 关掉机器人窗口，重开 | 不报「已有实例在运行」，能重新连上 | 9.7 |
| 7 | 让它「说说刚才群里聊了什么」 | 能接上前几句 | 属正常浮动，不用修 |

**第 4 条特别说明**：这是「群里必须 @ 才回」的正常表现。不要把它当故障去改代码或调大权限。

---

## 9. 常见故障

### 9.1 启动就报 `ModuleNotFoundError: No module named 'websockets'`
没装。跑第 2.3 节最后那条 `pip install websockets`。

### 9.2 一直重连、日志刷 `连接失败 … 3001`
协议端没登录成功。去 `http://127.0.0.1:5099` 看是不是还在等你扫码。
**3001 端口要等 QQ 登录成功才监听**，扫码之前连不上是正常的。

### 9.3 连上又被踢，日志 `401` / `Unauthorized`
`QQ_TOKEN` 和协议端里的 access_token 不一致。改到**逐字相同**，两边都重启。

### 9.4 群里 @ 了不回
按顺序查：
1. 那个群在 `QQ_WHITELIST_GROUPS` 里吗？（填了白名单就只认名单里的群）
2. 发 @ 的那个人在 `QQ_BLACKLIST_USERS` 里吗？
3. `QQ_GROUP_AT_ONLY=true` 时，是不是 @ 到了别的号？（群里有多个机器人时容易 @ 错）
4. 日志里有没有这一轮的 `[llm]` 记录？没有说明消息没进来，是协议端那边的问题。

### 9.5 唤醒词没反应
- `QQ_GROUP_KEYWORDS` 是**逗号分隔**，不要用空格或顿号；
- 词要**完整出现**在消息里，中间被空格断开不算；
- 改完 `.env` **必须重启机器人进程**（`.env` 不热加载）。

### 9.6 不发表情包
三个条件缺一不可：
1. `agents/qq/agent.json` 的 `tools` 里有 `send_sticker`；
2. `agents/qq/stickers/index.jsonl` 有活着（`deleted` 非 true）的条目，且图片文件真在；
3. 模型自己判断「这时候该甩图」——你逼它「发个表情」它就发，正常聊天它可能就是不甩，**这不算故障**。

### 9.7 「已有 QQ 适配层在运行」
上一次的进程没退干净。Windows 文件锁会随进程结束释放，所以：
```bat
tasklist | findstr python
taskkill /IM python.exe /F
```
（⚠️ 这会杀掉所有 Python 进程，先确认没别的东西在跑。）然后重开。

### 9.8 回复很慢 / 一直「正在输入」
`deepseek-flash` 正常一轮 1~3 秒；超过 10 秒先看 `logs/qq_bot.log` 里的 `[llm]` 行。
如果它调了 `web_search`，慢是正常的。

### 9.9 它开始重复自己说过的话
`QQ_BOT_NAME` 和 QQ 昵称不一致，它认不出哪句是自己说的。改成完全一致的昵称。

---

## 10. 不要做的事

1. **不要改 `app/` 里任何 `.py` 文件。** 想调行为只改 `.env` 和 `agents/qq/` 下的两个文件。
2. **不要把 API Key 写进代码、文档、截图，也不要打印到聊天里。** 只放在 `.env`，并且 `.env` 不要提交到 git、不要发给别人。
3. **不要用主 QQ 号当机器人。** 掉线、被封都影响正常使用，用一两个小号。
4. **不要 `QQ_GROUP_AT_ONLY=false` 又开着 `QQ_INTERJECT_MODE=on`。** 那会让它在所有群里对每句话都发言，几分钟就被踢。
5. **不要给它加文件读写工具。** 群友可以通过聊天诱导它读本机文件。
6. **不要自己给它加生图渠道。** 没装 ComfyUI，加了只会报错。
7. **不要删 `agents/qq/stickers/` 里的文件**（除了明确要删的那张），删了索引里的编号会错位，模型会甩错图。
8. 遇到文档没写的报错，**先把 `logs/qq_bot.log` 最后 50 行贴给用户看**，不要自己改代码去「修」。

---

## 11. 交付时给用户什么

1. 一个能双击的 `启动机器人.bat`
2. 启动顺序（QQ → SnowLuma → 机器人），写成一个 `.txt` 放桌面
3. 第 8 节 7 条验收的逐条结果
4. 三份配置的最终值：`.env` 的 QQ 段、`agent.json`、`prompt.md`（把 key 打码）
5. 表情包库当前有几张、怎么继续加
6. 遗留问题（例如「web_search 没配 key，走的是 DuckDuckGo，偶尔搜不到」）

---

## 附录 A：表情包批量导入脚本

项目里如果已经有 `tools/import_stickers.py`（或根目录有 `import_stickers.py`），直接用。
**没有就让 AI 把下面这段原样存成项目根目录的 `import_stickers.py`**，用 `.venv` 的 python 跑：

```python
"""把本地文件夹里的表情包批量导入某个 agent 的表情包库。

用法：
    python import_stickers.py "D:\\我的表情包" --dry-run      # 先看会导哪些
    python import_stickers.py "D:\\我的表情包"                # 真导入
    python import_stickers.py "D:\\我的表情包" --no-tag       # 不打标签（快）
    python import_stickers.py "D:\\我的表情包" --agent main   # 换 agent
"""

import argparse
import base64
import hashlib
import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
if os.path.basename(ROOT) == "tools":      # 也允许放在 tools/ 下
    ROOT = os.path.dirname(ROOT)
sys.path.insert(0, ROOT)

from app import stickers  # noqa: E402

EXTS = (".gif", ".png", ".jpg", ".jpeg", ".webp")
MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
        ".gif": "image/gif", ".webp": "image/webp"}


def _live_count(agent_id):
    return len([r for r in stickers._live(stickers._load_index(agent_id))])


def import_dir(src, agent_id, dry_run=False, tag=True):
    d = stickers._dir(agent_id)
    os.makedirs(d, exist_ok=True)
    index = stickers._load_index(agent_id)
    have_md5 = {r.get("md5") for r in index}
    n_ok = n_dup = n_skip = n_full = 0
    added = []

    files = sorted(f for f in os.listdir(src)
                   if os.path.splitext(f)[1].lower() in EXTS)
    for name in files:
        path = os.path.join(src, name)
        try:
            with open(path, "rb") as f:
                raw = f.read()
        except OSError as exc:
            print("  跳过（读不出）%s：%s" % (name, exc))
            n_skip += 1
            continue
        ok, w, h = stickers._is_sticker(raw)
        if not ok:
            print("  跳过（太大，%dx%d > 640）%s" % (w, h, name))
            n_skip += 1
            continue
        md5 = hashlib.md5(raw).hexdigest()
        if md5 in have_md5:
            print("  跳过（库里已有）%s" % name)
            n_dup += 1
            continue
        if _live_count(agent_id) >= stickers.STICKER_LIMIT:
            print("  库已满（%d 张），剩下的没导" % stickers.STICKER_LIMIT)
            n_full += 1
            break

        ext = os.path.splitext(name)[1].lower()
        out_name = md5[:8] + (".jpg" if ext in (".jpg", ".jpeg") else ext)
        desc, tags = "", []
        if tag:
            data_url = "data:%s;base64,%s" % (
                MIME.get(ext, "image/png"), base64.b64encode(raw).decode())
            desc, tags = stickers._tag(data_url)

        rec = {"md5": md5, "file": out_name, "desc": desc, "tags": tags,
               "sender": "本地导入", "t": int(time.time()), "url": "",
               "w": w, "h": h, "deleted": False}
        print("  + %s  %s %s" % (name, desc or "（无描述）",
                                 "/".join(tags[:3])))
        if not dry_run:
            with open(os.path.join(d, out_name), "wb") as f:
                f.write(raw)
            index.append(rec)
            have_md5.add(md5)
        added.append(rec)
        n_ok += 1

    if added and not dry_run:
        stickers._write_index(agent_id, index)
    return {"added": n_ok, "dup": n_dup, "skip": n_skip, "full": n_full,
            "dir": d}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src", help="表情包所在文件夹")
    ap.add_argument("--agent", default="qq", help="导入到哪个 agent（默认 qq）")
    ap.add_argument("--dry-run", action="store_true", help="只看不写")
    ap.add_argument("--no-tag", action="store_true",
                    help="跳过识图打标签（没有识图 Key 时用）")
    args = ap.parse_args()

    if not os.path.isdir(args.src):
        raise SystemExit("找不到文件夹：%s" % args.src)
    print("来源：%s" % os.path.abspath(args.src))
    print("目标：agents/%s/stickers/（当前 %d 张，上限 %d）"
          % (args.agent, _live_count(args.agent), stickers.STICKER_LIMIT))
    if args.dry_run:
        print("— dry run，不写任何文件 —")
    r = import_dir(args.src, args.agent, args.dry_run, tag=not args.no_tag)
    print("\n新增 %d · 重复 %d · 跳过 %d · 因满未处理 %d"
          % (r["added"], r["dup"], r["skip"], r["full"]))
    if args.dry_run:
        print("（dry run：以上都没有真正写入）")
    else:
        print("落盘：%s" % r["dir"])
        print("重启机器人进程即可生效。")


if __name__ == "__main__":
    main()
```

导入完**重启机器人进程**（索引每次现读，重启一次才重新渲染清单）。
`--no-tag` 导进去的条目没有画面描述，模型挑图会很瞎——有条件还是让它打标。
