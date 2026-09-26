# QQ 机器人笔记本迁移手册（给笔记本上的 AI 助手看）

这个 zip 是一台台式机上正在运行的 QQ 机器人完整副本（代码 + 配置 + 人设 + 知识库）。
你的任务：在笔记本上把它跑起来，并把机器人换成一个**新人设**。照着下面的步骤做即可。

## 包内容速览

| 路径 | 说明 |
|---|---|
| `app/` `web/` `tests/` `agent.py` | 引擎代码（Flask 主服务 + QQ 适配层） |
| `一键启动QQ机器人.bat` `启动QQ机器人.bat` 等 | 启动脚本（GBK 编码，**别用普通文本编辑器改**） |
| `.env` | 全部配置：模型密钥、降级链、QQ 触发词、白名单（**含密钥，勿外传**） |
| `agents/qq/` | 机器人人设（prompt.md / agent.json / settings.json）+ 表情包库 stickers/ |
| `skills/` | 技能包（表情包、写作等） |
| `documents/` + `vector_store/` | 知识库文档 + 向量索引（RAG 用） |
| `napcat_ref/onebot11.json` | NapCat 网络配置模板（3000/3001 端口 + token 已配好） |
| `requirements.txt` | Python 依赖清单 |

**没有带**：台式机的聊天历史（sessions/ recent/ memory/），笔记本是干净的全新会话。

## 第 1 步：解压 + 装环境

```
解压到一个固定目录，例如 D:\AI\agent_my_test\（后面脚本按这个路径举例，换了路径记住同步改）
cd D:\AI\agent_my_test
python -m venv D:\AI\confyui_env        # 名字随意，与台式机一致最省心
D:\AI\confyui_env\Scripts\pip install -r requirements.txt
```

## 第 2 步：装 NapCat 并放网络配置

1. 安装 NapCat（https://napcat.napneke.icu 下载对应版本，或 Shell/Launcher 一键版），装好后找到它的 `napcat/config/` 目录。
2. 把包里的 `napcat_ref/onebot11.json` **复制为** `napcat/config/onebot11.json`（无账号后缀的模板，新号首登自动继承）。
   - 里面有 HTTP 3000 端口（机器人收消息用）和 WS 3001，token 已与 `.env` 配好对，不用改。
3. 启动 NapCat，用**笔记本专用的那个 QQ 小号**扫码登录（别用台式机机器人正在用的号，同一账号不能两处同时登录）。
4. 登录成功标志：`netstat -ano | findstr "3000 3001"` 能看到 LISTENING。

## 第 3 步：检查 .env（换人设的关键都在这）

用记事本打开 `.env`，按需改这几行：

| 配置项 | 作用 | 笔记本要做什么 |
|---|---|---|
| `QQ_GROUP_KEYWORDS=小小怪` | **群聊触发词**（免 @ 命中即回） | **改成新人设的名字**，如 `QQ_GROUP_KEYWORDS=新名字` |
| `QQ_WHITELIST_GROUPS` | 群白名单（空=不限制） | 按需填笔记本机器人要服务的群号 |
| `QQ_WHITELIST_USERS` | 私聊白名单（空=全放行） | 按需；管理页里也能改（热更新） |
| `QQ_BLACKLIST_USERS` | 黑名单 | 保留或清空 |
| `LLM_FALLBACK_CHAIN` 等 | 模型密钥与降级链 | 已配好，**不用动** |
| `COMFYUI_URL=localhost:8188` | 生图服务地址 | 笔记本没装 ComfyUI，**不用动**——只是生图工具会报错，聊天不受影响 |

## 第 4 步：换人设（两个文件 + 一个触发词）

1. `agents/qq/prompt.md` —— 人设正文，整份重写成新角色（说话风格、称呼、表情包使用习惯等）。
2. `agents/qq/agent.json` —— 把 `"name"` 改成新名字（管理页显示用）。
3. `.env` 的 `QQ_GROUP_KEYWORDS` —— 和人设名字保持一致（第 3 步已做）。
4. 可选：`agents/qq/stickers/` 里是台式机的表情包，不想要就清空（AI 检测到没有表情包就自动不发表情）。

改完 prompt.md 不用重启（每轮自动同步），但改 `.env` 和 agent.json 后要重启 qq_bot。

## 第 5 步：启动

方式 A（推荐，注意路径）：`一键启动QQ机器人.bat` 会依次拉起 NapCat 和机器人适配层。
**但它硬编码了两条台式机路径**，记事本打开把这两行改成笔记本的实际路径：

```
set "NAPCAT_BAT=D:\AI\NapCat\启动NapCat.bat"      → 改成笔记本 NapCat 的启动 bat
set "BOT_BAT=D:\AI\agent_my_test\启动QQ机器人.bat" → 改成解压目录里的 启动QQ机器人.bat
```

方式 B（手动）：先启动 NapCat 并登录，再双击解压目录里的 `启动QQ机器人.bat`。

**bat 文件是 GBK+中文编码，用记事本编辑没问题，但不要用会改行尾/编码的工具。**

## 第 6 步：验证

1. 机器人窗口（标题 QQBOT-ADAPTER）日志无报错、显示已连接 WS。
2. 用另一个 QQ 在白名单群里 @ 它说话 → 有回复。
3. 不 @ 但打出触发词（新人设名字）→ 有回复。
4. 管理页：浏览器开 `http://127.0.0.1:<AGENT_PORT，默认看 .env>` 可看到会话管理/私聊白名单界面。

## 测试完之后（以后想完整复刻生图）

- 笔记本装 ComfyUI + 把台式机 `D:\AI\ComfyUI-master\...\custom_nodes\ComfyUI-BatchPromptGenerator\` 整个目录拷过去，`.env` 的 `COMFYUI_URL` 指向本地 8188 即可。
- ⚠️ 两台机器人的号别加同一个群，会互相接话刷屏。
