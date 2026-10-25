"""打「QQ 聊天机器人」包给群友用（2026-10-22）。

两种规格：

    --chat-only   纯聊天最小包（~1 MB）：只有聊天必需的代码与人设，
                  不带 skills / tools / tests / 其他 agent。她要生图再单独给。
    （默认）       接近完整项目（~8 MB）：代码全量 + skills 最小集（6 个）+
                  tools/ tests/ agents/main agents/writing + 启动与安装 bat。

## 为什么 skills 只带 6 个

`skills/` 完整目录 252 MB，其中 `ai-prompts` 165 MB **代码零引用**、`anima-tags`
13 MB 只是标签文本。旧 `build_export.py` 里那 6 个是「生图 + 漫画 + 写作」真正
会被 import 到的全部内容，带上足够跑通所有网页端功能。渠道再多也没用——对方
的 ComfyUI 里没有那些工作流。

## 不带走的东西

- `.env`（真实密钥）、`agents/*/session*.jsonl`、`agents/*/memory*`、`agents/*/sessions`
- `agents/*/settings.json`（带作者群号、审核词、静音名单）→ 按 agent 重写一份干净版
- `agents/*/prompt.md.bak-*`（改人设留下的备份）
- `web/实测_*` 等出图产物（`web/` 138 MB 全是测试图，只取 html/js/css）
- `documents/` `vector_store/`（RAG 数据库）、`data/custom_providers.json`（明文 key）
- 根目录 `_*.py` 实验脚本、`dist/` `state/` `usage/` 里的运行数据
- `启动QQ套装.bat` 等「带本机绝对路径」的 bat（`D:\\AI\\NapCat\\...`）

用法：
    python tools/build_qq_pack.py              # 接近完整项目
    python tools/build_qq_pack.py --chat-only  # 纯聊天最小包
    python tools/build_qq_pack.py --dry        # 只列清单不出 zip
"""

import argparse
import json
import os
import shutil
import time
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(ROOT, "dist")
NAME_FULL = "大肥鱼QQ机器人-完整版"
NAME_CHAT = "大肥鱼QQ机器人-纯聊天"

SKIP_DIRS = {"__pycache__", "sessions", "memory", "memory_archive", "recent",
             "interject", "stickers", "archive"}
SKIP_NAMES = {"session.jsonl", "settings.json", "sessions.zip"}
SKIP_EXT = {".pyc", ".pyo", ".log"}

# 完整版才带的东西
SKILLS_MIN = ["anima-line",      # 生图渠道（唯一工作流）
              "anima-tags",      # search_tags / random_tags 的数据源，被代码硬引用
              "storyboard-prompt",  # 漫画编剧规范
              "writing",         # 写作助手
              "_comic_batch",    # 漫画台内部批量渠道
              "_persona_fast"]   # 角色扮演快图渠道
AGENTS_EXTRA = ("main", "writing")
EXTRA_BATS = ("安装依赖.bat", "启动网页端.bat")

ENV_TEMPLATE = """\
# ══════ 必填：只需要这一个 Key ══════
# 申请：https://platform.deepseek.com/api_keys
DEEPSEEK_API_KEY=sk-在这里填你自己的
LLM_PROVIDER=deepseek
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-flash

# ══════ QQ 接入 ══════
QQ_ENABLE=true
QQ_WS_URL=ws://127.0.0.1:3001
QQ_HTTP_URL=http://127.0.0.1:3000
# 必须和 SnowLuma / NapCat 里设的 access_token 逐字相同
QQ_TOKEN=改成和协议端一致的令牌
QQ_WEBUI_PORT=5099
QQ_AGENT_ID=qq
# 必须和这个 QQ 号在群里的昵称一致，否则它会认不出自己说过的话
QQ_BOT_NAME=大肥鱼

# ══════ 触发规则（按部署文档第 1 节的问答改）══════
QQ_PRIVATE_ENABLE=true
QQ_GROUP_AT_ONLY=true
QQ_GROUP_KEYWORDS=

# 名单：留空＝不限；黑名单优先于白名单
QQ_WHITELIST_GROUPS=
QQ_WHITELIST_USERS=
QQ_BLACKLIST_USERS=

# ══════ 节奏 ══════
QQ_DEBOUNCE_SECONDS=1.5
QQ_MAX_CONCURRENCY=2
QQ_REPLY_MAX_CHARS=700
QQ_CONTEXT_MESSAGES=30
QQ_TURN_TIMEOUT=600

# ══════ 长记忆（群聊摘要，默认就开着）══════
# 每轮注入几条摘要 / 总字数上限。调大记得更细，但每轮多花 token。
QQ_MEMORY_INJECT_LIMIT=20
QQ_MEMORY_INJECT_MAX_CHARS=3000

# ══════ 主动插话（默认关；说错撤不回来）══════
QQ_INTERJECT_MODE=off
QQ_INTERJECT_GROUPS=

# ══════ 网页端 ══════
AGENT_PORT=5174
MAX_TURNS=10
# 聊天场景够用；agents/writing 自己带 1000000，不受这里影响
CONTEXT_BUDGET=32000

# ══════ 看图（可选）══════
# DeepSeek 官方没有视觉能力。想让机器人「看懂群里发的图」才需要填一个多模态模型：
# 本机 ollama 拉过视觉模型 → VISION_PROVIDER=ollama + OLLAMA_VISION_MODEL=qwen2.5vl:7b
# 留空不影响聊天。
VISION_PROVIDER=
VISION_MODEL=

# ══════ 生图 / 漫画（代码在包里，默认不连）══════
# 没有 ComfyUI 就保持原样，聊天完全不受影响。
COMFYUI_URL=http://127.0.0.1:8188
IMAGE_DEFAULT_SKILL=anima-line
IMAGE_GEN_TIMEOUT=3600
# COMFYUI_OUTPUT_DIR=D:\\\\ComfyUI\\\\output

# ══════ RAG 知识库（可选，装了才生效）══════
# 没装 requirements-rag.txt 就保持 0，那几个工具不会注册
ENABLE_RAG=0
LOCAL_EMBEDDING_MODEL=BAAI/bge-m3
RAG_EMBED_DEVICE=cpu
RAG_AUTO_DOWNLOAD=0
RAG_RETRIEVE_K=4
"""

AGENT_JSON = {
    "name": "大肥鱼",
    "description": "QQ 群聊 / 私聊：聊天、发表情包、联网搜索",
    "tools": ["send_sticker", "collect_sticker", "delete_sticker",
              "get_time", "web_search"],
    "skills": [],
    "provider": "deepseek",
    "model": "deepseek-flash",
    "context_budget": 30000,
}

PROMPT_MD = """## 一、身份

你是大肥鱼，一个在群里混吃混喝的笨蛋网友。
（这一行以上请按部署文档第 5.2 节改成用户要的人设——名字、身份、和群友的关系。）

## 二、说话方式

你在群里**打字**，不是在写文章。

- 一条消息就是一句话，**一条不超过 15 个字**。想说的多就回车换行，分成两三条发（最多三条）。
- 回车换行 = 群里真的会分成两条发出去，不是排版。
- 行首不许有空格、缩进、列表符号，就是裸的一行字。
- 不用「首先 / 其次 / 总之」，不解释自己为什么这么答。
- 能只回「？」「草」「行」「不会」就只回这个。

## 三、性格

（按用户给的原话提炼 2~3 条，写「会怎么反应」，别堆形容词。）
- 例：嘴硬心软，被夸会否认，但下一句就开始撒娇。

## 四、口癖与台词样本

下面这些是语感参考，可以照着这个味道说，不要逐字重复：

- （用户给的原话 1）
- （用户给的原话 2）
- （用户给的原话 3）

## 五、边界

- 不懂就说「不懂」「不会」，不编事实，不装作知道。
- 被问「你是不是机器人 / 什么模型」：用角色口吻岔开一句，不自证、不科普。
- 只在有人跟它说话时回。没人理它就闭嘴，不主动找话。
"""

SETTINGS_JSON = '{\n  "image_send_format": "jpg"\n}\n'

# ⚠️ bat 必须 GBK + CRLF：cmd 只认这套，UTF-8 会显示乱码、LF 会让 goto 失效
BAT = """@echo off\r
cd /d "%~dp0"\r
title QQ-BOT\r
\r
set "PY="\r
if exist ".venv\\Scripts\\python.exe" set "PY=.venv\\Scripts\\python.exe"\r
if not defined PY set "PY=python"\r
\r
echo ==================================================\r
echo   QQ 机器人（独立进程）\r
echo ==================================================\r
echo.\r
echo   前置：桌面版 QQ 已登录，协议端已起且 3000/3001 在监听\r
echo   停止：Ctrl+C 或直接关这个窗口\r
echo.\r
\r
"%PY%" -m app.qq_bot\r
echo.\r
echo [进程已退出] 返回码 %ERRORLEVEL%\r
pause\r
"""


def build(full=True, dry=False):
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = NAME_FULL if full else NAME_CHAT
    out_dir = os.path.join(DIST, "%s-%s" % (name, stamp))
    zip_path = out_dir + ".zip"
    os.makedirs(out_dir, exist_ok=True)

    plan = []

    def put(rel, data=None):
        """把一个文件放进包：data 给了就写内容，没给就从项目里复制。"""
        src_rel = os.path.join(ROOT, rel)
        if data is None and not os.path.isfile(src_rel):
            print("  ! 缺文件，跳过：", rel)
            return
        dst = os.path.join(out_dir, rel)
        os.makedirs(os.path.dirname(dst) or out_dir, exist_ok=True)
        if data is None:
            shutil.copy2(src_rel, dst)
        else:
            mode = "wb" if isinstance(data, bytes) else "w"
            kw = {} if isinstance(data, bytes) else {"encoding": "utf-8",
                                                     "newline": "\n"}
            with open(dst, mode, **kw) as f:
                f.write(data)
        plan.append(rel)

    def put_tree(rel, only_ext=None):
        """整棵复制项目里的 rel 目录（跳过缓存 / 会话 / 备份）。"""
        src = os.path.join(ROOT, rel)
        if not os.path.isdir(src):
            print("  ! 缺目录，跳过：", rel)
            return
        for base, dirs, files in os.walk(src):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for f in files:
                ext = os.path.splitext(f)[1].lower()
                if ext in SKIP_EXT or f in SKIP_NAMES:
                    continue
                if ".bak" in f or f.startswith("session."):
                    continue
                if only_ext and ext not in only_ext:
                    continue
                sub = os.path.relpath(os.path.join(base, f), ROOT)
                put(sub.replace("\\", "/"))

    # 1) 代码：app/ 全量
    put_tree("app")

    # 2) 界面：只要源码，不要出图产物（web/ 光测试图就 138 MB）
    for f in sorted(os.listdir(os.path.join(ROOT, "web"))):
        if f.lower().endswith((".html", ".htm", ".js", ".css")):
            put("web/" + f)

    # 3) 入口、依赖、文档
    for rel in ("agent.py", "requirements.txt", "requirements-rag.txt",
                "如何部署QQ聊天机器人-给编程ai看的.md",
                "第一步-发给群友的编程AI.md"):
        put(rel)
    put("import_stickers.py",
        open(os.path.join(ROOT, "tools", "import_stickers.py"),
             encoding="utf-8").read())

    # 4) 人设（qq 是模板，按文档第 5 节改）
    put("agents/qq/agent.json",
        json.dumps(AGENT_JSON, ensure_ascii=False, indent=2) + "\n")
    put("agents/qq/prompt.md", PROMPT_MD)

    # 5) 启动脚本 + 配置模板
    put("启动机器人.bat", BAT.encode("gbk"))
    put(".env", ENV_TEMPLATE)

    empty_dirs = ["agents/qq/stickers"]

    if full:
        # 6) skills：只带会被代码 import 到的那几个
        for sk in SKILLS_MIN:
            put_tree(os.path.join("skills", sk))
        put("skills/SKILL.md")

        # 7) 其他两个 agent：只带配置，重写一份干净 settings.json
        for aid in AGENTS_EXTRA:
            for f in ("agent.json", "prompt.md"):
                put("agents/%s/%s" % (aid, f))
            put("agents/%s/settings.json" % aid, SETTINGS_JSON)

        # 8) 参考件：作者的实验脚本与单元测试（⚠️ 测试在对方机器上会大面积红）
        put_tree("tools")
        put_tree("tests", only_ext={".py"})

        # 9) 通用启动脚本（都是相对路径，不带本机绝对路径）
        for b in EXTRA_BATS:
            put(b)

        empty_dirs += ["state", "usage"]

    for d in empty_dirs:
        os.makedirs(os.path.join(out_dir, d), exist_ok=True)

    if dry:
        print("会打进包的 %d 个文件（%s）：" % (len(plan), name))
        for p in plan:
            print("  " + p)
        shutil.rmtree(out_dir, ignore_errors=True)
        return None

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for base, dirs, files in os.walk(out_dir):
            for f in sorted(files):
                path = os.path.join(base, f)
                zf.write(path, os.path.join(name, os.path.relpath(path, out_dir))
                         .replace("\\", "/"))
        # 空目录（state / usage / stickers）单独写目录条目，否则解压后不存在
        for d in empty_dirs:
            zf.writestr(zipfile.ZipInfo("%s/%s/" % (name, d)), "")
    size = os.path.getsize(zip_path) / 1024 / 1024
    print("打包完成：%s（%.1f MB，%d 个文件）" % (zip_path, size, len(plan)))
    shutil.rmtree(out_dir, ignore_errors=True)
    return zip_path


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--chat-only", action="store_true",
                    help="只打聊天最小包（约 1 MB），不带 skills/tools/tests")
    ap.add_argument("--dry", action="store_true", help="只列清单，不出 zip")
    args = ap.parse_args()
    build(full=not args.chat_only, dry=args.dry)
