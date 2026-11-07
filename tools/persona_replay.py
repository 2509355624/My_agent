#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""扮演测试台：把一条真实会话冻成「母本」，映射副本上跑，测完删副本。

为什么这么设计
--------------
2026-11-07 用户提出：拿现成的一条 57 轮会话当基线，反复测「AI 演得好不好」，
不想每次从零开始聊；测完把副本删掉，母本一直留着。

    母本  state/persona_mothers/<name>.json      一条线的完整快照，只读不改
    副本  state/ 里一条新线（key 前缀 `t_`）      从母本映射出来，随便折腾

一条扮演线的状态散在四个地方，所以母本必须四样一起冻：

    ① state    `state/persona_sessions.json` 里的那一条
                （账本 note / 好感 / 天数 / 情绪 / 状态行 / 怀孕）
    ② window   `agents/qq/sessions/persona_<key>.jsonl`
                滑动窗口 12 条 = 模型真正看见的历史
    ③ archive  `agents/qq/sessions/persona_archive_<key>.jsonl`
                全量归档，查看台、补压、回退都要用
    ④ rag      向量记忆（`persona_<sha256(key)[:16]>` 这个 collection）
                只冻文本的话，副本一开口就没有回忆，测出来和线上不是一回事

副本 key 一律 `t_` 开头，`drop` 只认这个前缀 —— 手滑也删不到真人的线。

常用
----
    # 1. 冻母本（从现有会话，默认连向量记忆一起冻）
    python tools/persona_replay.py freeze web_546587874__junior --name junior-56

    # 2. 映射副本（会打印副本 key）
    python tools/persona_replay.py new junior-56

    # 3. 跑几轮
    python tools/persona_replay.py run t_junior-56_101530                 # 交互
    python tools/persona_replay.py run t_junior-56_101530 --say "..."     # 单轮
    python tools/persona_replay.py run t_junior-56_101530 --script a.txt  # 逐行台词

    # 4. 看记录 / 删副本
    python tools/persona_replay.py show t_junior-56_101530
    python tools/persona_replay.py drop t_junior-56_101530
    python tools/persona_replay.py drop --all          # 清掉所有副本

`run --dry` 不调模型，只把这一轮**会注入什么**打出来（账本 / 发展线 / 天数 /
窗口条数），改完提示词先用它对一眼，不烧 token。
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

MOTHER_DIR = os.path.join(BASE, "state", "persona_mothers")
REPLAY_DIR = os.path.join(BASE, "state", "persona_replay")
COPY_PREFIX = "t_"          # 副本 key 前缀；drop 只认它
SAFE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


# ── 基础设施 ────────────────────────────────────────────

def _persona():
    """延迟导入：`--model` 要先写进环境变量，config 才会读到。"""
    from app import persona
    return persona


def _ragmod():
    from app import persona_rag
    return persona_rag


def _read_jsonl(path):
    if not path or not os.path.exists(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _write_jsonl(path, rows):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def _append_jsonl(path, row):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _read_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _write_json(path, obj):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _slug(text):
    s = re.sub(r"[^A-Za-z0-9_-]+", "-", str(text or "")).strip("-_")
    return (s or "mother")[:32]


def _mother_path(name):
    return os.path.join(MOTHER_DIR, _slug(name) + ".json")


def _load_mother(name):
    path = _mother_path(name)
    if not os.path.exists(path):
        # 允许直接传带 .json 的文件名
        alt = os.path.join(MOTHER_DIR, str(name))
        if os.path.exists(alt):
            path = alt
        else:
            _die("找不到母本：%s\n先跑 `freeze`，或 `list` 看看有哪些。" % name)
    return _read_json(path), path


def _mothers():
    if not os.path.isdir(MOTHER_DIR):
        return []
    out = []
    for fn in sorted(os.listdir(MOTHER_DIR)):
        if not fn.endswith(".json"):
            continue
        try:
            out.append((fn[:-5], _read_json(os.path.join(MOTHER_DIR, fn))))
        except Exception:                                # noqa: BLE001
            continue
    return out


def _die(msg, code=2):
    print("✗ " + msg, file=sys.stderr)
    raise SystemExit(code)


def _ok(msg):
    print("✓ " + msg)


# ── persona 状态读写（复用 persona 自己的落盘逻辑）────────────
#
# 路径规则只维护在 `app/persona.py` 一处，这里调它的 `_hist_path` 等方法，
# 免得以后改目录时脚本还往老地方写。`_save_state` 自带「先合并盘上其他线」
# 和天数单调保护，比自己 open/write 安全得多。

def _state_set(key, ent, deleted=None):
    persona = _persona()
    st = persona._load_state()
    st[key] = ent
    persona._save_state(st, deleted=deleted)


def _state_pop(key):
    persona = _persona()
    st = persona._load_state()
    if key not in st:
        return False
    st.pop(key, None)
    persona._save_state(st, deleted=[key])
    return True


# ── 向量记忆 ────────────────────────────────────────────

def _rag_dump(key):
    """把一条线的向量记忆整份读出来。返回 [] 表示这条线还没有记忆。"""
    pr = _ragmod()
    if not pr.enabled_for(key):
        return []
    try:
        client = pr._client()
        resp = client.list_entries(pr.collection_name(key), timeout=180)
    except Exception as exc:                             # noqa: BLE001
        print("  ! 读不到向量记忆（%s）—— 这一份母本只含文本记忆" % exc)
        return []
    if not resp.get("ok"):
        print("  ! 读不到向量记忆（%s）" % resp.get("error"))
        return []
    return resp.get("entries") or []


def _rag_restore(key, entries):
    """把母本的记忆灌进副本的 collection。分批 embed，几百条约半分钟。"""
    if not entries:
        return 0
    pr = _ragmod()
    client = pr._client()
    coll = pr.collection_name(key)
    try:
        client.delete_kb(coll)               # 幂等：副本大概率是空的
    except Exception:                        # noqa: BLE001
        pass
    done = 0
    step = 40
    for i in range(0, len(entries), step):
        chunk = []
        for e in entries[i:i + step]:
            content = str(e.get("content") or "").strip()
            if not content:
                continue
            chunk.append({
                "id": e.get("id") or ("m-%d" % (i + len(chunk))),
                "content": content,
                "metadata": e.get("metadata") or {},
            })
        if not chunk:
            continue
        resp = client.ingest(coll, chunk)
        if not resp.get("ok"):
            raise RuntimeError(resp.get("error") or "ingest 失败")
        done += int(resp.get("count") or 0)
        print("    记忆 %d/%d" % (min(i + step, len(entries)), len(entries)),
              end="\r", flush=True)
    print(" " * 32, end="\r")
    return done


def _rag_drop(key):
    pr = _ragmod()
    if not pr.enabled_for(key):
        return False
    try:
        resp = pr._client().delete_kb(pr.collection_name(key))
        return bool(resp.get("ok"))
    except Exception:                        # noqa: BLE001
        return False


def _warm_rag(key, timeout=90):
    """等记忆引擎就绪再开跑。

    脚本是新进程，扮演用的 RAG daemon 每次都要重新加载 bge-small（约 10 秒）。
    不等的话第一轮 `build_block` 直接返回空——测「记不记得前面」时会误判成
    「记忆没生效」。
    """
    pr = _ragmod()
    if not pr.enabled_for(key):
        return False
    try:
        client = pr._client()
        t0 = time.time()
        ok = client.start()
        if ok:
            print("  记忆引擎就绪（%.1fs）" % (time.time() - t0))
        else:
            print("  ! 记忆引擎没起来，这几轮不带回忆")
        return bool(ok)
    except Exception as exc:                             # noqa: BLE001
        print("  ! 记忆引擎异常：%s" % exc)
        return False


def _pending_pop(key):
    """清掉待提炼缓冲：副本从头攒，别继承母本的半截批次。"""
    path = os.path.join(BASE, "state", "persona_rag_pending.json")
    if not os.path.exists(path):
        return
    try:
        data = _read_json(path)
    except Exception:                        # noqa: BLE001
        return
    if isinstance(data, dict) and key in data:
        data.pop(key, None)
        _write_json(path, data)


# ── freeze ─────────────────────────────────────────────

def cmd_freeze(args):
    persona = _persona()
    src = args.src_key
    if not SAFE_KEY.match(src or ""):
        _die("会话 key 不合法：%r" % src)
    ent = persona._entry(src)
    if not ent:
        _die("这条线在 persona_sessions.json 里不存在：%s" % src)

    window_path = persona._hist_path(src)
    archive_path = persona._archive_path(src)
    window = _read_jsonl(window_path)
    archive = _read_jsonl(archive_path)
    if not archive and window:
        archive = list(window)               # 老线没归档时拿窗口兜底

    card = str(ent.get("card") or "")
    turns = len([r for r in archive if r.get("role") == "assistant"])
    name = args.name or ("%s-%d" % (_slug(card or "line"), turns))

    print("母本：%s" % name)
    print("  源线   %s（角色 %s）" % (src, card or "?"))
    print("  窗口   %d 条" % len(window))
    print("  归档   %d 条 / %d 轮" % (len(archive), turns))
    print("  账本   %d 字" % len(str(ent.get("note") or "")))

    rag = []
    if args.no_rag:
        print("  记忆   跳过（--no-rag）")
    else:
        print("  记忆   读取中…")
        rag = _rag_dump(src)
        print("  记忆   %d 条" % len(rag))

    mother = {
        "kind": "persona-mother",
        "version": 1,
        "name": name,
        "src_key": src,
        "card": card,
        "frozen_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "turns": turns,
        "day": int((ent.get("gameday") or {}).get("day") or 1),
        "state": copy.deepcopy(ent),
        "window": window,
        "archive": archive,
        "rag": rag,
    }
    path = _mother_path(name)
    if os.path.exists(path) and not args.force:
        _die("母本已存在：%s\n换一个 --name，或加 --force 覆盖。" % path)
    _write_json(path, mother)
    _ok("母本已冻结 → %s" % path)
    print("  下一步：python tools/persona_replay.py new %s" % name)
    return 0


# ── new（映射副本）──────────────────────────────────────

def cmd_new(args):
    persona = _persona()
    mother, path = _load_mother(args.mother)
    name = mother.get("name") or os.path.basename(path)[:-5]

    key = args.key or ("%s%s_%s" % (COPY_PREFIX, _slug(name),
                                    time.strftime("%H%M%S")))
    key = key[:64]
    if not SAFE_KEY.match(key):
        _die("副本 key 不合法：%r" % key)
    if not key.startswith(COPY_PREFIX):
        _die("副本 key 必须以 %r 开头（drop 靠这个前缀保护真人的线）" % COPY_PREFIX)
    if persona._entry(key) and not args.force:
        _die("副本 key 已存在：%s（加 --force 覆盖）" % key)

    window = copy.deepcopy(mother.get("window") or [])
    archive = copy.deepcopy(mother.get("archive") or [])
    ent = copy.deepcopy(mother.get("state") or {})
    if not ent:
        _die("母本里没有 state，无法映射")

    # 运行期游标重置：note_at 指到归档末尾，压缩只吃新轮次，不会把整份
    # 历史重写成一遍账本；note_due 归零，节奏从副本第一轮重新数。
    ent["note_at"] = len(archive)
    ent["note_due"] = 0

    _state_set(key, ent)

    hist_path = persona._hist_path(key)
    arch_path = persona._archive_path(key)
    if not hist_path:
        _die("副本 key 拿不到历史路径，key 可能不合法：%s" % key)
    _write_jsonl(hist_path, window)
    _write_jsonl(arch_path, archive)

    n_rag = 0
    if not args.no_rag and mother.get("rag"):
        print("  映射向量记忆 %d 条…" % len(mother["rag"]))
        n_rag = _rag_restore(key, mother["rag"])
    _pending_pop(key)

    meta = {
        "copy_key": key,
        "mother": name,
        "src_key": mother.get("src_key"),
        "card": mother.get("card"),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "window": len(window),
        "archive": len(archive),
        "rag": n_rag,
    }
    _write_json(os.path.join(REPLAY_DIR, key + ".meta.json"), meta)

    _ok("副本已建好：%s" % key)
    print("  窗口 %d 条 / 归档 %d 条 / 记忆 %d 条" % (len(window), len(archive), n_rag))
    print("  跑一轮：python tools/persona_replay.py run %s --say \"…\"" % key)
    print("  删副本：python tools/persona_replay.py drop %s" % key)
    return 0


# ── run（真的跑）────────────────────────────────────────

def _state_line(key):
    persona = _persona()
    ent = persona._entry(key) or {}
    gd = ent.get("gameday") or {}
    aff = ent.get("affection") or {}
    bits = [
        "第 %s 天" % (gd.get("day") or 1),
        str(gd.get("phase") or "—"),
    ]
    if gd.get("place"):
        bits.append(str(gd["place"])[:24])
    bits.append("累计 %s 轮" % persona.turn_total(key))
    if ent.get("mood"):
        bits.append("%s %s" % (ent.get("mood"), ent.get("intensity") or 0))
    if aff.get("mode") == "raid":
        bits.append("好感 %s(%s)" % (aff.get("value"), aff.get("tier")))
    print("  状态：" + " · ".join(bits))
    if ent.get("state"):
        print("  在场：" + str(ent["state"])[:100])


def _dry(key, text):
    """不调模型，把这一轮会注入的东西打出来。"""
    persona = _persona()
    ent = persona._entry(key) or {}
    note = str(ent.get("note") or "")
    gd = ent.get("gameday") or {}
    window = _read_jsonl(persona._hist_path(key))
    head = "第 %s 天 · %s · 累计 %s 轮" % (
        gd.get("day") or 1, gd.get("phase") or "—", persona.turn_total(key))
    print("【dry】" + head)
    print("  窗口 %d 条（%d 轮）" % (len(window),
                                 len([r for r in window if r["role"] == "assistant"])))
    print("  账本 %d 字" % len(note))
    tl = persona.story_line(gd, note)
    print("  发展线 %d 字：%s" % (len(tl), (tl.replace("\n", " / ") or "（空）")))
    gb = persona._gameday_block(gd) or ""
    print("  时间块 %d 字：%s" % (len(gb), gb.replace("\n", " / ")[:160] or "（空）"))
    try:
        from app import writing_style
        # `card` 在这里不在作用域里（这是 dry 分支，没走完整 run_turn）。
        # 从副本那条线自己的 state 里取卡名，取不到就空着——尾注只是预览。
        cname = str((persona._entry(key) or {}).get("card") or "")
        tail = writing_style.tail(turn_no=persona.turn_total(key), name=cname)
        print("  尾注 %d 字：%s" % (len(tail), tail.replace("\n", " / ")))
    except Exception as exc:                            # noqa: BLE001
        print("  尾注  取不到（%s）" % exc)
    if text:
        try:
            from app import persona_rag
            rblk = persona_rag.build_block(key, text)
        except Exception as exc:                        # noqa: BLE001
            rblk = ""
            print("  回忆  取不到（%s）" % exc)
        else:
            print("  回忆 %d 字：%s" % (len(rblk), rblk.replace("\n", " / ")[:240] or "（空）"))
    print("  （dry 不调模型。想真跑就去掉 --dry）")
    return 0


def _settle_note(key, timeout=120):
    """等这一轮触发的账本压缩落盘再继续。

    `_bump_turn` 攒够 `NOTE_EVERY_TURNS` 轮会起一个 daemon 线程去压缩——
    生产服务常驻，没问题；脚本跑完 5 轮就退出，那个线程直接被掐死，账本
    停在上一版，`note_due` 还挂在 5。批量测长期记忆时会误判成「账本没更新」。

    做法：先等后台线程（它在 `_NOTE_RUNNING` 里挂着），超时或线程已经不在
    而节拍仍满，就同步补压一次。
    """
    persona = _persona()
    due = getattr(persona, "NOTE_EVERY_TURNS", 5)
    running = getattr(persona, "_NOTE_RUNNING", None)
    if running is not None:
        t0 = time.time()
        while key in running and time.time() - t0 < timeout:
            time.sleep(0.2)
    st = persona._entry(key) or {}
    if int(st.get("note_due") or 0) < due:
        return False
    if running is not None and key in running:
        print("  ! 账本压缩超时未完成，先往下走")
        return False
    print("  账本压缩中（第 %s 轮这一批）…" % st.get("note_due"))
    t0 = time.time()
    ok = False
    try:
        ok = persona.compress_note(key)
    except Exception as exc:                             # noqa: BLE001
        print("  ! 账本压缩失败：%s" % exc)
        return False
    st = persona._entry(key) or {}
    print("  账本 %s（%d 字，%.1fs）"
          % ("已更新" if ok else "没变",
             len(str(st.get("note") or "")), time.time() - t0))
    return ok


def _one_turn(key, text, idx=None):
    persona = _persona()
    t0 = time.time()
    turn = persona.run_turn(key, text, "private", "")
    dt = time.time() - t0
    reply = (turn.reply or "").strip()
    tag = "[%d] " % idx if idx else ""

    print("\n%s你：%s" % (tag, text))
    if not reply:
        print("  ！她这轮没说话（空回复）")
    else:
        print("  她（%d 字，%.1fs）：" % (len(reply), dt))
        for line in reply.splitlines():
            print("    " + line)
    _state_line(key)

    ent = persona._entry(key) or {}
    aff = ent.get("affection") or {}
    _append_jsonl(os.path.join(REPLAY_DIR, key + ".jsonl"), {
        "i": idx,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "user": text,
        "reply": reply,
        "chars": len(reply),
        "elapsed": round(dt, 2),
        "day": persona.game_day(key),
        "phase": persona.game_phase(key),
        "mood": ent.get("mood"),
        "intensity": ent.get("intensity"),
        "state": ent.get("state"),
        "turns": persona.turn_total(key),
        "tier": aff.get("tier"),
        "value": aff.get("value"),
    })
    _settle_note(key)
    return reply


def cmd_run(args):
    key = args.copy_key
    if not key.startswith(COPY_PREFIX):
        _die("只跑副本（key 必须 %r 开头）：%s\n母本是只读快照，直接在真线上跑会改掉真实记录。"
             % (COPY_PREFIX, key))
    persona = _persona()
    if not persona._entry(key):
        _die("副本不存在或已删：%s\n先 `new` 一个。" % key)

    _state_line(key)
    _warm_rag(key)
    if args.dry:
        return _dry(key, args.say[0] if args.say else "")

    lines = list(args.say or [])
    if args.script:
        with open(args.script, "r", encoding="utf-8") as fh:
            for raw in fh:
                s = raw.strip()
                if not s or s.startswith("#"):
                    continue
                lines.append(s)

    idx = 0
    if lines:
        for text in lines:
            idx += 1
            _one_turn(key, text, idx)
            if args.max and idx >= args.max:
                break
        print("\n共 %d 轮。记录：%s" % (idx, os.path.join(REPLAY_DIR, key + ".jsonl")))
        return 0

    # 交互：一行一轮，/quit 退出，/state 看状态
    print("\n一行一轮，/quit 退出，/state 看当前状态，/note 看账本。")
    while True:
        if args.max and idx >= args.max:
            print("到 --max %d 轮，停。" % args.max)
            break
        try:
            text = input("\n你> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text:
            continue
        if text in ("/quit", "/exit", "/q"):
            break
        if text == "/state":
            _state_line(key)
            continue
        if text == "/note":
            print((persona._entry(key) or {}).get("note") or "（账本还是空的）")
            continue
        idx += 1
        _one_turn(key, text, idx)
    print("本轮共 %d 轮。记录：%s" % (idx, os.path.join(REPLAY_DIR, key + ".jsonl")))
    return 0


# ── show / list / drop ──────────────────────────────────

def cmd_show(args):
    key = args.copy_key
    meta_path = os.path.join(REPLAY_DIR, key + ".meta.json")
    if os.path.exists(meta_path):
        meta = _read_json(meta_path)
        print("副本 %s（母本 %s，建于 %s）" % (key, meta.get("mother"),
                                          meta.get("created_at")))
        print("  起点：窗口 %s / 归档 %s / 记忆 %s"
              % (meta.get("window"), meta.get("archive"), meta.get("rag")))
    rows = _read_jsonl(os.path.join(REPLAY_DIR, key + ".jsonl"))
    if not rows:
        print("  还没跑过。")
        return 0
    chars = [r.get("chars") or 0 for r in rows]
    print("  跑过 %d 轮，字数 %d~%d，均 %.0f，总耗时 %.0fs"
          % (len(rows), min(chars), max(chars), sum(chars) / len(chars),
             sum(r.get("elapsed") or 0 for r in rows)))
    _state_line(key)
    if args.verbose:
        for r in rows:
            print("\n[%s] 你：%s" % (r.get("i"), r.get("user")))
            print("    她（%s 字）：%s" % (r.get("chars"), (r.get("reply") or "")[:300]))
    return 0


def cmd_list(args):
    ms = _mothers()
    print("母本（%s）" % MOTHER_DIR)
    if not ms:
        print("  （还没有）")
    for name, m in ms:
        print("  %-20s 源线 %-30s %s 轮 · %s · 记忆 %d 条"
              % (name, m.get("src_key"), m.get("turns"),
                 m.get("frozen_at", "")[:16], len(m.get("rag") or [])))

    persona = _persona()
    st = persona._load_state()
    keys = sorted(k for k in st if str(k).startswith(COPY_PREFIX))
    print("\n副本（%s）" % REPLAY_DIR)
    if not keys:
        print("  （没有存活的副本）")
    for k in keys:
        rows = _read_jsonl(os.path.join(REPLAY_DIR, k + ".jsonl"))
        print("  %-28s %d 轮记录" % (k, len(rows)))
    return 0


def cmd_drop(args):
    persona = _persona()
    if args.all:
        st = persona._load_state()
        targets = [k for k in st if str(k).startswith(COPY_PREFIX)]
    else:
        targets = [args.copy_key] if args.copy_key else []
    if not targets:
        _die("没指定副本，也没有存活的副本（drop --all 清全部）")
    for key in targets:
        if not str(key).startswith(COPY_PREFIX):
            print("跳过 %s（不是副本）" % key)
            continue
        hist = persona._hist_path(key)
        arch = persona._archive_path(key)
        for p in (hist, arch):
            if p and os.path.exists(p):
                os.remove(p)
        removed = _state_pop(key)
        rag_ok = _rag_drop(key)
        _pending_pop(key)
        for suf in (".jsonl", ".meta.json"):
            p = os.path.join(REPLAY_DIR, key + suf)
            if os.path.exists(p):
                os.remove(p)
        _ok("已删副本 %s（状态 %s / 记忆 %s）"
            % (key, "有" if removed else "无", "有" if rag_ok else "无"))
    return 0


# ── main ───────────────────────────────────────────────

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="扮演测试台：母本冻结 / 副本映射 / 跑轮 / 清理",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("freeze", help="把一条真实会话冻成母本")
    p.add_argument("src_key", help="会话 key，如 web_546587874__junior")
    p.add_argument("--name", help="母本名（默认 <角色>-<轮数>）")
    p.add_argument("--no-rag", action="store_true", help="只冻文本，不带向量记忆")
    p.add_argument("--force", action="store_true", help="覆盖同名母本")
    p.set_defaults(func=cmd_freeze)

    p = sub.add_parser("new", help="从母本映射出一个副本")
    p.add_argument("mother", help="母本名（freeze 时的 --name）")
    p.add_argument("--key", help="指定副本 key（默认 t_<母本>_<时分秒>）")
    p.add_argument("--no-rag", action="store_true", help="不映射向量记忆")
    p.add_argument("--force", action="store_true", help="覆盖已存在的副本 key")
    p.set_defaults(func=cmd_new)

    p = sub.add_parser("run", help="在副本上跑轮")
    p.add_argument("copy_key")
    p.add_argument("--say", action="append", help="跑一轮，可重复")
    p.add_argument("--script", help="台词文件：一行一轮，# 开头忽略")
    p.add_argument("--max", type=int, default=0, help="最多跑几轮")
    p.add_argument("--dry", action="store_true", help="不调模型，只看注入内容")
    p.add_argument("--model", help="临时换文本模型（写进 PERSONA_MODEL）")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("show", help="看副本跑过的记录")
    p.add_argument("copy_key")
    p.add_argument("-v", "--verbose", action="store_true", help="逐轮打印")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("list", help="列母本与副本")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("drop", help="删副本（母本不动）")
    p.add_argument("copy_key", nargs="?")
    p.add_argument("--all", action="store_true", help="删掉所有 t_ 开头的副本")
    p.set_defaults(func=cmd_drop)

    args = ap.parse_args(argv)
    if not getattr(args, "func", None):
        ap.print_help()
        return 1
    # `--model` 必须在 import app.config 之前落到环境里
    if getattr(args, "model", None):
        os.environ["PERSONA_MODEL"] = args.model
        print("（本轮文本模型 → %s）" % args.model)
    return args.func(args)


def _ensure_env():
    """当前解释器没装依赖时，自动切到项目环境。

    本机有多个 Python（`.venv` / `my_env` / `confyui_env`），直接 `python
    tools/persona_replay.py` 很可能是一个裸解释器，连 dotenv 都没有。这里按
    启动脚本同样的顺序找一个能跑的，`execv` 换掉自己——用户记不住路径也不卡。
    """
    try:
        import dotenv  # noqa: F401
        return
    except ImportError:
        pass
    cands = [
        os.path.join(BASE, ".venv", "Scripts", "python.exe"),
        os.path.join(BASE, "venv", "Scripts", "python.exe"),
        r"D:\AI\my_env\Scripts\python.exe",
        r"D:\AI\confyui_env\Scripts\python.exe",
    ]
    me = os.path.abspath(sys.executable)
    for p in cands:
        if os.path.exists(p) and os.path.abspath(p) != me:
            os.execv(p, [p, os.path.abspath(__file__)] + sys.argv[1:])
    _die("这个 python 没装项目依赖（缺 dotenv），也没找到可用的项目环境。\n"
         "试试：D:\\AI\\confyui_env\\Scripts\\python.exe tools/persona_replay.py …")


if __name__ == "__main__":
    _ensure_env()
    sys.exit(main())
