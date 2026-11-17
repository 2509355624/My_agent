# -*- coding: utf-8 -*-
"""启动前真实场景自检。

6457 个单元测试证明不了「这台机器上真的有生图渠道 / 真的有角色扮演记录」。
这个脚本专门查运行时资产，并在 Flask test_client 上走真实 HTTP 接口。
只读，不提交任务、不写业务状态。

用法：
    D:\\AI\\confyui_env\\Scripts\\python.exe tools\\preflight_runtime.py
"""

import json
import os
import re
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

FAILS = []
WARNS = []


def check(name, fn):
    try:
        detail = fn()
        print("[PASS] %s%s" % (name, (" — " + detail) if detail else ""))
        return True
    except Exception as exc:                       # noqa: BLE001
        print("[FAIL] %s — %s: %s" % (name, type(exc).__name__, exc))
        FAILS.append(name)
        return False


def warn(name, fn):
    try:
        detail = fn()
        print("[PASS] %s%s" % (name, (" — " + detail) if detail else ""))
    except Exception as exc:                       # noqa: BLE001
        print("[WARN] %s — %s" % (name, exc))
        WARNS.append(name)


def _channels():
    from app.features.image import routes
    rows = routes.channels()
    if not rows:
        raise AssertionError("渠道列表为空")
    ids = {str(r.get("id") or "") for r in rows}
    if "silver" not in ids:
        raise AssertionError("默认渠道 silver 不在列表里")
    return "%d 个渠道，含 silver" % len(rows)


def _workflow():
    from app.tools.normal import generate_image
    d = generate_image.load_skill("silver") or {}
    wf = d.get("workflow") or {}
    if not wf:
        raise AssertionError("silver/workflow.json 没加载出节点")
    return "%d 个节点" % len(wf)


def _cards():
    from app.features.persona import service as persona
    cards = persona.load_cards()
    if not cards:
        raise AssertionError("characters.json 里没有角色卡")
    return "%d 张角色卡" % len(cards)


def _sessions():
    from app.features.persona import service as persona
    path = getattr(persona, "_STATE_PATH", "")
    if not os.path.isfile(path):
        raise AssertionError("找不到 %s" % path)
    data = json.loads(open(path, "r", encoding="utf-8").read())
    if not isinstance(data, dict) or not data:
        raise AssertionError("persona_sessions.json 不是非空 dict")
    return "%d 条会话键" % len(data)


def _llm():
    from app import config
    if not config.LLM_PROVIDER:
        raise AssertionError("LLM_PROVIDER 为空")
    if config.LLM_PROVIDER not in config.PROVIDERS:
        raise AssertionError(
            "LLM_PROVIDER=%s 没注册进 PROVIDERS（多半缺 data/custom_providers.json）"
            % config.LLM_PROVIDER)
    return "%s 已注册，共 %d 个 provider" % (
        config.LLM_PROVIDER, len(config.PROVIDERS))


def _http():
    from app.main import app
    c = app.test_client()

    r = c.get("/api/gen/channels")
    if r.status_code != 200:
        raise AssertionError("/api/gen/channels 返回 %s" % r.status_code)
    d = r.get_json() or {}
    if not d.get("channels"):
        raise AssertionError("/api/gen/channels 没有渠道")
    n_ch = len(d["channels"])

    # 从真实记录里挑一个带 QQ 号的网页会话，不写死个人信息
    path = os.path.join(ROOT, "state", "persona_sessions.json")
    keys = json.loads(open(path, "r", encoding="utf-8").read()).keys()
    qq = ""
    for k in keys:
        m = re.match(r"web_(\d+)$", str(k))
        if m:
            qq = m.group(1)
            break
    if not qq:
        raise AssertionError("persona_sessions.json 没有 web_<qq> 会话键")

    r = c.get("/api/persona/cards?qq=%s" % qq)
    if r.status_code != 200:
        raise AssertionError("/api/persona/cards 返回 %s" % r.status_code)
    cards = (r.get_json() or {}).get("cards") or []
    if not cards:
        raise AssertionError("/api/persona/cards 没有角色")

    r = c.get("/api/persona/session?qq=%s" % qq)
    if r.status_code != 200:
        raise AssertionError("/api/persona/session 返回 %s" % r.status_code)
    d = r.get_json() or {}
    if not d.get("messages"):
        raise AssertionError("/api/persona/session 没读到历史消息")
    return "渠道 %d / 角色 %d / 会话 %d 条" % (n_ch, len(cards), int(d.get("total") or 0))


def _env():
    path = os.path.join(ROOT, ".env")
    if not os.path.isfile(path):
        raise AssertionError("没有 .env")
    text = open(path, "r", encoding="utf-8-sig").read()
    active = "\n".join(
        line for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#"))
    m = re.search(r"^QQ_WEBUI_PORT\s*=\s*(\S+)", active, re.M)
    if not m:
        raise AssertionError(".env 没写 QQ_WEBUI_PORT")
    if m.group(1).strip() != "5099":
        raise AssertionError("QQ_WEBUI_PORT=%s，不是 5099" % m.group(1).strip())
    if "NAPCAT_QRCODE_PATH" in active:
        raise AssertionError(".env 还留着 NAPCAT_QRCODE_PATH")
    return "QQ_WEBUI_PORT=5099"


def _comfy():
    from app.config import COMFYUI_URL
    req = urllib.request.Request(COMFYUI_URL.rstrip("/") + "/system_stats")
    with urllib.request.urlopen(req, timeout=3) as resp:
        if resp.status != 200:
            raise RuntimeError("HTTP %s" % resp.status)
    return COMFYUI_URL


def main():
    print("运行时自检：%s" % ROOT)
    check("生图渠道 /api/gen/channels", _channels)
    check("默认渠道 silver 工作流", _workflow)
    check("角色卡库 characters.json", _cards)
    check("角色扮演记录 persona_sessions.json", _sessions)
    check("LLM provider 注册表", _llm)
    check("真实 HTTP 冒烟", _http)
    check(".env 协议端口", _env)
    warn("ComfyUI 在线", _comfy)

    print()
    if FAILS:
        print("结果：FAIL（%s）" % "、".join(FAILS))
        return 1
    print("结果：PASS%s" % ("（警告：%s）" % "、".join(WARNS) if WARNS else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
