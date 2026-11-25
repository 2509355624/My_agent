"""moss 漫画台 · 逐页对话修正（2026-11-25）

用户原话：「我会在每一页这边，当我在第一页里面发现有什么问题，我就在下面
有一个输入栏，开始打字说这一页什么什么什么问题……点发送，AI 去修改……

相当于 AI 指定输出一个格式代码去解析之后进行一个替换。……会说某个格子有问题，
上面那个格子有问题，或者是左下角或者是右下角那格子有问题，到时候 AI 它就
针对性地去优化那一格就行了，要么就是整体地进行一个优化。」

## 为什么不是复用 `comic_agent.chat_revise`

那条链路改的是 **script.md（散文分镜稿）**，改完再由编译器重编 Layout JSON。
moss 台早就没有散文稿了：出图直接吃每页的 `prompt_override`（一整段 Layout
JSON 字符串）。把用户这句话塞进那条链，等于绕一大圈再回来，中途还会把已经
调好的 bbox / 落格丢掉。

所以这里直接改 `prompt_override` 本身：**拿这一页的 Layout JSON 当输入，
模型交回同一份 JSON 的改版，代码只判「结构还能不能用」**。

## 采纳判据（和仓库既有哲学一致：只判格式，不判内容）

1. 能解析成 JSON 对象；
2. `items` 是列表、非空；
3. 每个 character / text 条目都还有 `caption`；
4. 版式骨架和原来一致（items 数量不变）。

1~4 全过才采纳。**改得对不对不判**——那是作者在对话里看的，界面会显示
前后差异，不满意可以撤销回上一版。判不过就把原文当答复摆出来，让作者重说一句，
代码绝不自己糊一份。

## 为什么骨架不许变

`bbox` 决定这一格画在纸上的位置。模型一改 items 数量，页码 / 分格就和这一本
其它页对不上（翻页时格子跳位）。要换版式是页头那个 layout 下拉的事，不是
聊天框的事——这里明确拒掉，并在 `reply` 里告诉作者去改版式。

## 历史

同 `comic_agent.chat_revise`：`moss_chat_<task>.json`，只留最近几轮。
"""

from __future__ import annotations

import json
import os
import re
import time
import logging

log = logging.getLogger(__name__)

# 一次对话最多带回几轮（作者-助手各算一轮的一半）
HISTORY_KEEP = 8

# 「这是改稿」还是「这是答复」——形态分流，和 comic_agent 同一套。
# 模型先说「答复：」就是在讨论，不动稿。
_REPLY_RE = re.compile(r"^\s*(?:答复|回答|Reply)\s*[：:]\s*", re.I)

_SYSTEM = """你是这本漫画的分镜修正助手。用户在**某一页**下面提了意见，你要么
交回改好的那一页，要么回一句话说明。

# 你手里的东西

· 这一本的原始需求（作者写的大纲/剧情）——**它是最高优先级**。大纲点名要求的
  衣着状态、动作、有没有某样东西，一律照大纲，不许自作主张。
· 这一页当前的 Layout JSON。字段含义：
  - `bbox`：这一格在纸上的位置 `[x, y, 宽, 高]`，**绝对不许改**。
  - `items[].label`：`character`（人物）/ `object`（物件、空镜）/ `text`（气泡、旁白）。
  - `items[].caption`：这一格的画面描述（英文）。人物格末尾可能有
    `girl wears: …` 和 `Author's requirement for this page: …`，这两段是
    系统钉上去的衣着状态和作者原话，**你改画面时要把它们一起考虑进去**。
  - `items[].text`：中文台词（只有 text 条目有）。
  - `summary.caption`：整页说明，末尾通常有配色串和本页衣着状态。
· 用户这句话：**可能点名某一格**（「右上那格」「第一格」「左下角」），也可能
  说的是整页。点名了就只改那一格，别顺手改别的；没点名就按上下文判断。

# 两条路，二选一

**路一：改稿。** 你确实知道怎么改，就直接输出**完整的**这一页 Layout JSON：
· 第一个字符必须是 `{`，最后一个必须是 `}`，**不要 markdown 围栏、不要解释**。
· `items` 的数量和顺序，以及每一项的 `bbox`，**一个字都不许动**——分格定死了。
· 只改该改的 `caption` / `text`。没被提到的格子**原样保留**，一个字符都别动。
· 画面描述仍然用英文，仍然守规矩：不写画风词（anime / 8k / photorealistic）、
  不写采样参数；人物格写清外形 + 衣着 + 动作 + 神态；一格里的衣服只提一次。

**路二：只是问问 / 需要作者补充。** 第一行写 `答复：`，后面一句话说清。
这时**不要**输出 JSON。

# 用户说「这页不对，重来」

那就整页重写所有 `caption`（bbox 和 items 数量还是不许动），但依然要守住
上面的原始需求。

# 衣服这块特别当心（用户反复强调过）

· 作者说「不要外套 / 脱掉 / 光着 / 不穿」，就把这页所有格子里那件衣服真的删掉，
  连 `wears:` 那段一起改成**正向描述**（`bare skin` / `a plain white underlayer
  only` / 宠物就 `bare fur`），**绝不写 `no shirt` 这种否定句**——出图模型读
  否定句只会看见 shirt。
· 作者说「上一页的外套要褪下来」，这页就不能再出现外套，哪怕上一页有。
· 反过来也一样：作者说这页穿什么，就一定写什么，别沿用上一页。
"""


def _strip_fence(text):
    """剥掉 ```json 围栏。模型偶尔会加，加了就当没加。"""
    t = (text or "").strip()
    m = re.match(r"^```(?:json)?\s*\n?(.*?)\n?```$", t, re.S)
    return (m.group(1).strip() if m else t)


def parse_prompt(page):
    """这一页的 prompt_override → dict。解析不了返回 None（旧链路那种散文提示词）。"""
    raw = (page or {}).get("prompt_override") or ""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        js = json.loads(raw)
    except Exception:                       # noqa: BLE001
        return None
    return js if isinstance(js, dict) else None


def _skeleton(js):
    """版式骨架：每一格的 bbox，按顺序。用来判「有没有偷偷改分格」。"""
    out = []
    for it in (js.get("items") or []):
        if isinstance(it, dict):
            b = it.get("bbox")
            out.append(tuple(b) if isinstance(b, list) else None)
        else:
            out.append(None)
    return out


def _shape_ok(new, old):
    """新稿的结构还能不能用。返回 (ok, 原因)。只判格式，不判内容。"""
    if not isinstance(new, dict):
        return False, "交回来的不是 JSON 对象"
    items = new.get("items")
    if not isinstance(items, list) or not items:
        return False, "items 是空的"
    old_items = old.get("items") or []
    if len(items) != len(old_items):
        return False, ("格子数变了（%d → %d）——分格是定死的，要换版式请用页头的"
                       "「版式」下拉，不是在聊天框里改" % (len(old_items), len(items)))
    for i, (a, b) in enumerate(zip(items, old_items)):
        if not isinstance(a, dict):
            return False, "第 %d 格不是对象" % (i + 1)
        if a.get("bbox") != b.get("bbox"):
            return False, "第 %d 格的位置被挪动了（bbox 不许改）" % (i + 1)
        if str(b.get("caption") or "").strip() and not str(a.get("caption") or "").strip():
            return False, "第 %d 格丢了 caption" % (i + 1)
    return True, ""


def _diff(old, new):
    """逐格差异，给界面显示「改了哪几格」。"""
    out = []
    oi = old.get("items") or []
    ni = new.get("items") or []
    for i in range(max(len(oi), len(ni))):
        a = oi[i] if i < len(oi) else {}
        b = ni[i] if i < len(ni) else {}
        picked = []
        for k in ("caption", "text"):
            if str(a.get(k) or "") != str(b.get(k) or ""):
                picked.append(k)
        if picked:
            out.append({"panel": i + 1,
                        "fields": picked,
                        "before": str(a.get("caption") or a.get("text") or "")[:160],
                        "after": str(b.get("caption") or b.get("text") or "")[:160]})
    so = (old.get("summary") or {}).get("caption") or ""
    sn = (new.get("summary") or {}).get("caption") or ""
    if so != sn:
        out.append({"panel": 0, "fields": ["summary"],
                    "before": str(so)[:160], "after": str(sn)[:160]})
    return out


def checkin(js, page_no, message, brief="", outline_line="", history=None,
            provider=None, model=None, call=None):
    """和「这一页」聊一句。返回 dict，契约同 `comic_agent.chat_revise`：

    {"reply": 给作者看的那段话,
     "prompt_override": 采纳的新整页 JSON 字符串（没改 = None）,
     "changed": 是否改了, "diff": 逐格差异,
     "warnings": [], "model": ""}
    """
    out = {"reply": "", "prompt_override": None, "changed": False,
           "diff": [], "warnings": [], "model": ""}
    msg = (message or "").strip()
    if not msg:
        out["reply"] = "这句话是空的。"
        out["warnings"].append("空消息")
        return out
    if not isinstance(js, dict) or not js:
        out["reply"] = ("这一页没有可改的 Layout JSON（可能是旧任务 / 旧链路产出）。"
                        "把它重画一遍就能聊了。")
        out["warnings"].append("没有 prompt_override")
        return out

    hist = []
    for h in (history or []):
        role = str(h.get("role") or "").strip()
        content = str(h.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            hist.append({"role": role, "content": content})

    ctx = ["【原始需求 / 大纲】", (brief or "（作者没给）").strip()]
    if (outline_line or "").strip():
        ctx += ["", "【作者对这一页的原话】", outline_line.strip()]
    # 之前几轮聊过什么：直接拼成一段文字塞进 user。
    # 不走 messages 多轮，是因为 `_twopass_call` 只吃 (system, user) 两条，
    # 而为这一件事另造一条调用链不值得。
    if hist:
        ctx += ["", "【之前几轮聊过（越长越早）】"]
        for h in hist:
            ctx.append(("作者：" if h["role"] == "user" else "你：") + h["content"])
    ctx += ["", "【这一页当前的 Layout JSON】",
            json.dumps(js, ensure_ascii=False),
            "", "【作者这句话（针对第 %s 页）】" % page_no, msg,
            "", "改稿就直接输出完整的这一页 JSON（第一个字符是 {）；"
                "只是答复就先写「答复：」。"]
    user = "\n".join(ctx)

    if call is None:
        out["reply"] = "没有可用的模型链路。"
        out["warnings"].append("call 为空")
        return out

    try:
        text = call(_SYSTEM, user, max_tokens=6000, temperature=0.4,
                    tag="moss_page_chat", provider=provider, model=model) or ""
    except Exception as exc:                # noqa: BLE001 - 调用失败转成人话
        out["reply"] = "修正助手调用失败：%s —— 请再发一次" % exc
        out["warnings"].append("调用失败")
        log.warning("[moss-chat] 调用失败：%s", exc)
        return out

    text = (text or "").strip()
    if not text:
        out["reply"] = "修正助手返回了空内容 —— 请再发一次。"
        out["warnings"].append("返回为空")
        return out

    # 形态一：讨论。不动稿。
    if _REPLY_RE.match(text):
        out["reply"] = _REPLY_RE.sub("", text, count=1).strip() or text
        return out

    # 形态二：改稿。先剥围栏再解析。
    body = _strip_fence(text)
    if not body.startswith("{"):
        # 既不是「答复：」也不是 JSON——大概率是模型忘写标记的一段人话。
        out["reply"] = text[:2000]
        out["warnings"].append("返回的内容既不是「答复：」也解析不成 JSON")
        return out
    try:
        new = json.loads(body)
    except Exception as exc:                # noqa: BLE001
        out["reply"] = "改动解析失败（JSON 不完整：%s）—— 请再说一次，或说得具体些。" % exc
        out["warnings"].append("JSON 解析失败")
        log.warning("[moss-chat] JSON 解析失败：%s", exc)
        return out

    ok, why = _shape_ok(new, js)
    if not ok:
        out["reply"] = "这一版没采纳：%s。\n说说你想改哪一格、改成什么样？" % why
        out["warnings"].append(why)
        return out

    d = _diff(js, new)
    if not d:
        out["reply"] = "这一版和原来一模一样，没有改动——是不是没说清要改哪里？"
        out["warnings"].append("没有差异")
        return out

    out["prompt_override"] = json.dumps(new, ensure_ascii=False)
    out["changed"] = True
    out["diff"] = d
    bits = "、".join("格%d" % x["panel"] if x["panel"] else "整页说明" for x in d[:6])
    out["reply"] = "改好了 %d 处（%s）。确认没问题就点这一页的「出图」重跑。" % (len(d), bits)
    return out


# ── 对话历史（按任务存一份） ────────────────────────────────
def _hist_path(task_dir):
    return os.path.join(task_dir, "moss_chat.json")


def read_history(task_dir):
    try:
        with open(_hist_path(task_dir), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, list) else []
    except Exception:                       # noqa: BLE001
        return []


def append_history(task_dir, page_no, question, reply):
    hist = read_history(task_dir)
    hist.append({"role": "user", "content": "第%s页：%s" % (page_no, question),
                 "at": time.strftime("%H:%M:%S")})
    hist.append({"role": "assistant", "content": reply,
                 "at": time.strftime("%H:%M:%S")})
    hist = hist[-HISTORY_KEEP:]
    try:
        with open(_hist_path(task_dir), "w", encoding="utf-8") as f:
            json.dump(hist, f, ensure_ascii=False, indent=1)
    except Exception:                       # noqa: BLE001 - 历史存不下不该挡改稿
        log.warning("[moss-chat] 历史写入失败 %s", task_dir)


def clear_history(task_dir):
    try:
        os.remove(_hist_path(task_dir))
    except OSError:
        pass
