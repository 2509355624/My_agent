# -*- coding: utf-8 -*-
"""把项目打包成「给别人部署」的干净 zip（2026-10-19）。

## 为什么要有这个脚本

直接 `zip -r 整个项目` 会把本机的东西一起带走：`.env` 里的真实密钥、QQ 群的
会话与记忆、几十个渠道的 workflow、还有根目录那一堆 `_xxx.py` 实验脚本和
几十 MB 的对比图。手工挑一次可以，改完代码再挑一次就会漏。

所以这里把「哪些该给」写成白名单，`python tools/build_export.py` 重跑一次
就是一版新的包。目标机只需要一个生图渠道（默认 anima-line）也能跑通全部网页端
功能；渠道由 `.env` 的 `IMAGE_DEFAULT_SKILL` 决定，代码不用改。

## 不带走的东西（逐条都有理由）

- `.env` / `agents/*/session*` / `agents/*/memory` —— 真实密钥与私人对话
- `agents/{qq,draw,writing,self_233}` —— 别的 agent，对方只要一个通用助手
- `skills/` 下除白名单以外的渠道 —— 对方的 ComfyUI 没有那些工作流
- `state/ usage/ logs/ comics/ documents/ vector_store/`
- `tests/` —— 里面的用例依赖本机 36 个渠道，给出去只会让对方的 AI 困惑
- 根目录 `_*.py` / `_*.png` / `*.log` / `presets_*.json` —— 实验件与私人数据

## 这个脚本只写不删

中间目录和 zip 名都带时间戳，跑多少次都是新建，**不会覆盖也不会清掉上一次的
产物**，更不会碰项目里的任何文件（脚本对项目本体是只读的，只往外复制）。
`dist/` 攒多了自己手动清理，或者先看 `--list` 打出来的清单。

用法：
    python tools/build_export.py              # 生成 dist/agent_my_test-friend-<时间>.zip
    python tools/build_export.py --list       # 只列出打包范围，不写任何文件
"""

import argparse
import io
import os
import shutil
import sys
import time
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(ROOT, "dist")
NAME = "agent_my_test-friend"

# 目录：原样整棵复制（仍会跳过 __pycache__ 与会话）
DIRS = [
    "app",
    "web",
    "agents/main",
    "agents/writing",     # 写作助手：100 万上下文 + 只挂 writing skill
]

# 单个文件
FILES = [
    "agent.py",
    "requirements.txt",
    "requirements-rag.txt",
    "一键启动.bat",
    "安装依赖.bat",
]

# skills 只给这几个：他要的渠道 + 标签库 + 剧情/写作 + 两个内部渠道
SKILLS = [
    "anima-line",        # 他唯一的工作流
    "anima-tags",        # search_tags / random_tags 的数据源，硬引用
    "storyboard-prompt",  # 漫画编剧（剧情）
    "writing",           # 写作
    "_comic_batch",      # 漫画台内部批量渠道
    "_persona_fast",     # 角色扮演快图渠道
]
SKILL_FILES = ["SKILL.md"]   # skills 根目录的索引说明

# 复制时跳过：缓存、我们的会话与记忆、运行产物
SKIP_DIRS = {"__pycache__", "sessions", "memory", "recent", "interject",
             "stickers", "sessions.zip"}
SKIP_EXT = {".pyc", ".pyo", ".log"}

# 按**文件名**跳过（不看扩展名）。这三类即使不在 sessions/ 里也必须拦住：
#   session.jsonl  我们的真实对话记录（之前漏过一次，27 KB 群聊原文进了包）
#   settings.json  群号、审核词、静音名单；包里会按 agent 重写一份干净版
#   *.bak*         改人设留下的备份，纯属噪音
SKIP_NAMES = {"session.jsonl", "settings.json"}
SKIP_NAME_PREFIX = ("session.",)
SKIP_NAME_MARKS = (".bak",)

ENV_TEMPLATE = """\
# ── 必填：一个 DeepSeek API Key ──────────────────────
# 申请：https://platform.deepseek.com/api_keys
DEEPSEEK_API_KEY=sk-在这里填你自己的
DEEPSEEK_BASE_URL=https://api.deepseek.com
# 官方可用值：deepseek-chat（通用）/ deepseek-reasoner（推理，慢）
DEEPSEEK_MODEL=deepseek-chat

# ── 主模型 ───────────────────────────────────────────
# 先用 DeepSeek，挂了再退到本地 llama.cpp（两者都是 OpenAI 兼容接口）
LLM_PROVIDER=deepseek
# 链里只写 llama 不写模型名 = 直接用下面 LLAMA_MODEL 的值（改名只改一处）
LLM_FALLBACK_CHAIN=deepseek:deepseek-chat,llama

# ── 本地 llama.cpp（llama-server）───────────────────
# 启动示例：llama-server -m 你的模型.gguf --port 8081 -c 32768
# 注意 base_url 要带 /v1。
# 单模型方式启动（-m xxx.gguf）时这个名字不校验，填 gguf 文件名即可；
# 用 router 方式启动（--models-dir，一个进程挂多个 gguf）时必须**一字不差**
# 写模型 id，否则服务端找不到模型，直接 404。
LLAMA_BASE_URL=http://127.0.0.1:8081/v1
LLAMA_MODEL=Qwen3.5-9B-The-Defiant-Fable-Uncnr-Heretic-NEO-MAX-MTP-Q5_K_M
# 必须和启动参数 -c 填同一个数：代码用它给上下文预算封顶。
# 填小了，预算再高也会被按这个窗口打折（16384×0.7≈1.1 万 token）。
LLAMA_NUM_CTX=32768

# 想反过来「本地为主、DeepSeek 兜底」就改成：
#   LLM_PROVIDER=llama
#   LLM_FALLBACK_CHAIN=llama,deepseek:deepseek-chat

# ── 识图（引用图反推提示词用）────────────────────────
# DeepSeek 官方没有视觉能力，这里必须指向一个多模态模型：
#   llama-server 带 --mmproj 起的模型 → 填 llama；
#   ollama 拉过视觉模型                → 填 ollama，并把 OLLAMA_VISION_MODEL 填上；
# 都没有就留空 —— 引用图反推会失败，其它功能不受影响。
VISION_PROVIDER=
VISION_MODEL=
# OLLAMA_BASE_URL=http://127.0.0.1:11434
# OLLAMA_VISION_MODEL=qwen2.5vl:7b

# ── ComfyUI ─────────────────────────────────────────
COMFYUI_URL=http://127.0.0.1:8188
# 填 ComfyUI 的 output 目录可以多一层「HTTP 下载失败时从磁盘兜底」
# COMFYUI_OUTPUT_DIR=D:\\ComfyUI\\output
# 单张图的上限（秒），冷启动第一次要留余量
IMAGE_GEN_TIMEOUT=3600

# ── 生图渠道 ─────────────────────────────────────────
# 默认走哪个渠道。skills/ 里有的才算数；改了这里不用改代码。
# 你手上只有「anima线条」这一个工作流，所以填 anima-line。
IMAGE_DEFAULT_SKILL=anima-line

# ── 上下文（写长文的关键）─────────────────────────────
# 单位是 token。系统提示 + 工具说明每轮都要随请求发出去，先占掉一部分，
# 剩下的才是聊天史与读进来的文件。默认 32000 只够中短篇：
# 写作规范里一份 write-structure.md 就 22 KB（约 1.3 万 token），读两份就满。
# DeepSeek 的窗口是 100 万，装得下 10 万，所以这里直接给 100000。
# 代价：写长篇时每轮重新发送的长史按 token 计费，一篇下来几块钱量级。
CONTEXT_BUDGET=100000
# 工具描述只留首句、砍掉用法提示。系统提示从约 1.6 万字符压到 5 千，
# 给写作用腾出空间。本地小模型（16K 窗口）也建议保持 1。
PROMPT_BRIEF=1

# ── RAG 知识库（可选）────────────────────────────────
# 没装 requirements-rag.txt 就保持 0，那几个工具不会注册
ENABLE_RAG=0
LOCAL_EMBEDDING_MODEL=BAAI/bge-m3
RAG_EMBED_DEVICE=cpu
RAG_AUTO_DOWNLOAD=0
RAG_RETRIEVE_K=4

# ── 服务 ────────────────────────────────────────────
AGENT_PORT=5174
MAX_TURNS=10

# ── QQ 机器人（默认关，用不到就别开）────────────────
QQ_ENABLE=false
QQ_WS_URL=ws://127.0.0.1:3001
QQ_HTTP_URL=http://127.0.0.1:3000
QQ_AGENT_ID=main
"""

SETTINGS_JSON = """{
  "image_send_format": "jpg"
}
"""


def _skip_file(name):
    """这个文件该不该带进包。"""
    low = name.lower()
    if low in SKIP_NAMES:
        return True
    if low.startswith(SKIP_NAME_PREFIX) or low.endswith((".bak", ".orig")):
        return True
    return any(m in low for m in SKIP_NAME_MARKS)


def _copy_tree(src, dst):
    """复制目录，跳过缓存 / 会话 / 日志。返回复制的文件数。"""
    n = 0
    for base, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        rel = os.path.relpath(base, src)
        target = dst if rel == "." else os.path.join(dst, rel)
        os.makedirs(target, exist_ok=True)
        for f in files:
            if os.path.splitext(f)[1].lower() in SKIP_EXT or _skip_file(f):
                continue
            shutil.copy2(os.path.join(base, f), os.path.join(target, f))
            n += 1
    return n


def build(out_dir=DIST):
    # ⚠️ 只写不删：目录名带时间戳，永远新建，不覆盖也不清掉上一次的产物。
    # 早先这里会 rmtree 上一次的中间目录，一是沙箱会拦（拦了就是半途退出、
    # 留下脏状态），二是「打包脚本会删目录」这件事本身就不该存在。
    stamp = time.strftime("%Y%m%d-%H%M%S")
    stage = os.path.join(out_dir, "%s-%s" % (NAME, stamp))
    os.makedirs(stage, exist_ok=True)

    total = 0
    for rel in DIRS:
        src = os.path.join(ROOT, rel)
        if not os.path.isdir(src):
            print("  ! 缺目录，跳过：", rel)
            continue
        total += _copy_tree(src, os.path.join(stage, rel))

    for rel in FILES:
        src = os.path.join(ROOT, rel)
        if not os.path.isfile(src):
            print("  ! 缺文件，跳过：", rel)
            continue
        os.makedirs(os.path.dirname(os.path.join(stage, rel)) or stage,
                    exist_ok=True)
        shutil.copy2(src, os.path.join(stage, rel))
        total += 1

    for sk in SKILLS:
        src = os.path.join(ROOT, "skills", sk)
        if not os.path.isdir(src):
            print("  ! 缺 skill，跳过：", sk)
            continue
        total += _copy_tree(src, os.path.join(stage, "skills", sk))
    for f in SKILL_FILES:
        src = os.path.join(ROOT, "skills", f)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(stage, "skills", f))
            total += 1

    # 干净的运行时目录：state/usage/logs 由程序自己建，这里只保证 state 存在
    os.makedirs(os.path.join(stage, "state"), exist_ok=True)
    os.makedirs(os.path.join(stage, "usage"), exist_ok=True)

    # .env：全占位，不含任何真实密钥
    with io.open(os.path.join(stage, ".env"), "w", encoding="utf-8",
                 newline="\r\n") as f:
        f.write(ENV_TEMPLATE)

    # 干净的 settings.json（我们的那份带着群号、审核词、静音名单）。
    # 每个 agent 都要有一份：没有这个文件，网页上的设置页会读本机默认值。
    for aid in ("main", "writing"):
        d = os.path.join(stage, "agents", aid)
        if not os.path.isdir(d):
            continue
        with io.open(os.path.join(d, "settings.json"), "w",
                     encoding="utf-8", newline="\r\n") as f:
            f.write(SETTINGS_JSON)

    # 两份文档：一份是给对面编程 AI 的说明书，一份是让人类用户复制粘贴的
    # 开场白（不给他这段，他的 AI 不知道该去读说明书）。
    for name in ("如何完成项目部署-给编程ai看的.md",
                 "第一步-发给编程AI的话.md"):
        doc = os.path.join(ROOT, name)
        if os.path.isfile(doc):
            shutil.copy2(doc, stage)
            total += 1
        else:
            print("!! 缺文档，打包不完整：", name)

    # 打包（zip 同样带时间戳，上一次的包一律留着）
    zip_path = os.path.join(out_dir, "%s-%s.zip" % (NAME, stamp))
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for base, _dirs, files in os.walk(stage):
            for f in files:
                full = os.path.join(base, f)
                z.write(full, os.path.relpath(full, stage))
    size = os.path.getsize(zip_path) / 1024.0 / 1024.0
    print("打包完成：%s（%.1f MB，%d 个文件）" % (zip_path, size, total))
    return stage, zip_path


def _check_no_secret(stage):
    """兜底自检：包里的 .env 不许带真实 key。

    只扫 `.env`，而且**按变量名判**——名字里带 KEY / TOKEN / SECRET / PASSWORD
    的才算凭据。早先按「值够长就可疑」判，`DEEPSEEK_BASE_URL=https://api.deepseek.com`
    这种 URL、`LLM_FALLBACK_CHAIN=deepseek:...,llama:local` 这种链全被判成泄漏，
    一屏红字反而让人忽略真问题（2026-10-19 实测）。
    """
    import re as _re

    secret_key = _re.compile(r"(API_?KEY|_KEY|TOKEN|SECRET|PASSWORD|COOKIE)", _re.I)
    bad = []
    for base, _dirs, files in os.walk(stage):
        for f in files:
            if not (f == ".env" or f.startswith(".env.")):
                continue
            fp = os.path.join(base, f)
            for line in io.open(fp, encoding="utf-8", errors="ignore"):
                s = line.strip()
                if not s or s.startswith("#") or "=" not in s:
                    continue
                key, val = s.split("=", 1)
                key, val = key.strip(), val.strip().strip('"').strip("'")
                if not secret_key.search(key):
                    continue
                if len(val) < 8:
                    continue          # 空值 / 开关
                if val.startswith(("sk-在这里", "your_", "<", "${")):
                    continue
                bad.append("%s: %s=%s..." % (os.path.relpath(fp, stage),
                                             key, val[:8]))
    return bad


def _plan():
    """只打印打包范围，不创建任何文件。"""
    print("会复制这些目录：")
    for rel in DIRS:
        ok = os.path.isdir(os.path.join(ROOT, rel))
        print("   %s %s" % ("✓" if ok else "✗", rel))
    print("会复制这些文件：")
    for rel in FILES:
        ok = os.path.isfile(os.path.join(ROOT, rel))
        print("   %s %s" % ("✓" if ok else "✗", rel))
    print("会复制这些 skill：")
    for sk in SKILLS:
        ok = os.path.isdir(os.path.join(ROOT, "skills", sk))
        print("   %s skills/%s" % ("✓" if ok else "✗", sk))
    print("会复制这些文档：")
    for rel in ("如何完成项目部署-给编程ai看的.md", "第一步-发给编程AI的话.md"):
        ok = os.path.isfile(os.path.join(ROOT, rel))
        print("   %s %s" % ("✓" if ok else "✗", rel))
    print("会新写：.env（全占位）、agents/main/settings.json、state/、usage/")
    print("不会删除、不会覆盖任何已有文件。")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="只列出打包范围，不写文件")
    ap.add_argument("--out", default=DIST)
    args = ap.parse_args()

    if args.list:
        _plan()
        return 0

    stage, zip_path = build(args.out)
    bad = _check_no_secret(stage)
    if bad:
        print("!! 疑似密钥泄漏，先处理再发：")
        for b in bad:
            print("   ", b)
        return 1
    print("密钥自检：通过（没发现真实 key）")
    print("中间目录：", stage)
    print("成品 zip：", zip_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
