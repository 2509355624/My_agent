"""扮演历史的读取视图：归档全文、轮次统计、窗口裁剪。"""

import logging

log = logging.getLogger("persona")


def load_archive(session_key):
    """读**完整**归档（`persona_archive_*.jsonl`）。网页回看历史读的是这一份。

    2026-11-04：以前网页读的是 `svc._load_hist` 的 12 条滑动窗口，所以「超过 6 轮
    就翻不到更早的」——数据一直在归档里，只是接口没给。这里不加新存储，只把
    已有的那份读出来。

    没有归档（功能上线前的老线，或归档写失败）就退回窗口原文——那时能看到的
    只有窗口内的这些，救不回来。**不调用 `trim()`**：回看要全文，截到 900 字
    加个省略号就不是「回顾」了。
    """
    from app.features.persona import service as svc

    rows = svc._read_archive_file(session_key)
    if not rows:
        rows = [m for m in svc._load_hist(session_key)
                if isinstance(m, dict) and m.get("role") in ("user", "assistant")]
    # 2026-11-06：老归档里的 `【心声】` 标签只在这份**返回副本**上剥掉，
    # 网页回看和前情提取拿到的都是干净文字；磁盘上的归档原样保留。
    out = []
    for r in rows:
        if isinstance(r, dict) and r.get("role") == "assistant" and r.get("content"):
            r = dict(r)
            r["content"] = svc._strip_monologue_tag(r["content"])
        out.append(r)
    return out


def archive_len(session_key):
    """归档一共多少条。给 `chat` 回包算「这一轮是第几条」用。"""
    from app.features.persona import service as svc

    return len(load_archive(session_key))


def _archive_assistant_count(session_key):
    """归档里她说过多少条——老线补算「累计轮次」用。"""
    from app.features.persona import service as svc

    try:
        return sum(1 for m in load_archive(session_key)
                   if isinstance(m, dict) and m.get("role") == "assistant")
    except Exception:                                    # noqa: BLE001
        return 0


def turn_total(session_key):
    """这条线累计交流了多少轮（网页「轮次」显示用，2026-11-06 用户要求）。

    和小记压缩的 `turns` 分开：那个每 `NOTE_EVERY_TURNS` 轮被 `compress_note`
    清零，玩家真正想看的是总量。新线由 `_bump_turn` 维护 `turn_total`；
    老线没有这个字段时按归档里她的回复条数补算（只读不写盘）。
    """
    from app.features.persona import service as svc

    key = svc.resolve_line(session_key)
    ent = svc._entry(key)
    if not isinstance(ent, dict):
        return 0
    if ent.get("turn_total") is not None:
        try:
            return max(0, int(ent.get("turn_total") or 0))
        except (TypeError, ValueError):
            return 0
    return _archive_assistant_count(key)


def history_page(session_key, before=None, limit=None):
    """归档里 `[before-limit, before)` 这一段，返回 (start, chunk, total)。

    `before=None` → 最后 limit 条（首屏）；否则往前翻一页。
    下标就是归档**行号**，也是前端气泡的 id —— 归档只增不删，所以它稳定，
    不会像「窗口内第几条」那样聊一轮整体前移。
    """
    from app.features.persona import service as svc

    rows = load_archive(session_key)
    total = len(rows)
    try:
        end = total if before is None else int(before)
    except (TypeError, ValueError):
        end = total
    end = max(0, min(total, end))
    try:
        lim = int(limit or svc.ARCHIVE_PAGE)
    except (TypeError, ValueError):
        lim = svc.ARCHIVE_PAGE
    lim = max(1, min(200, lim))
    start = max(0, end - lim)
    return start, rows[start:end], total


def trim(hist, limit=None):
    """取最近 N 条（默认 svc.HISTORY_LIMIT），单条超 svc.MSG_MAX_CHARS 截断。

    `limit`（2026-11-10）：给前缀缓存的滞回窗口用。传窗口的实际长度时，
    **只做单条截断、不砍条数**，历史前缀才能逐轮只追加。不传时行为与
    以前完全一致（编剧 / 打分器 / 回看等内部调用仍只看最近 12 条）。

    裁的是**送进模型的副本**；落盘保留原文（2026-11-05 起，`run_turn` 直接存
    `_load_hist` 的原始窗口，不再把截断版写回去）——网页回看和归档要的是全貌。

    2026-11-02：assistant 那几条**保留结构化尾巴**。截断只砍正文，最后三行
    （画面 / 心情 / 状态）原样留着——它们是 few-shot 示范，历史里全是纯对话
    的话，模型第 2 轮起就会跟着历史把格式丢掉（实测第 1 轮完美、第 2 轮全空）。
    """
    from app.features.persona import service as svc

    msgs = [m for m in (hist or [])
            if isinstance(m, dict) and m.get("role") in ("user", "assistant")]
    n = svc.HISTORY_LIMIT if not limit else max(1, int(limit))
    msgs = msgs[-n:]
    out = []
    for m in msgs:
        c = str(m.get("content") or "").strip()
        if len(c) > svc.MSG_MAX_CHARS:
            if m["role"] == "assistant":
                body, tail = _split_meta(c)
                room = max(20, svc.MSG_MAX_CHARS - len(tail) - 1)
                c = body[:room].rstrip() + "…" + tail
            else:
                c = c[:svc.MSG_MAX_CHARS] + "…"
        out.append({"role": m["role"], "content": c})
        # 分段（2026-11-05）：送到模型前还要再渲染一次，所以 `trim` 不能把
        # `seg` 丢掉。展示/归档读的是 `content`，不受影响。
        if m.get("role") == "user" and m.get("seg"):
            out[-1]["seg"] = m["seg"]
            out[-1]["addressed"] = m.get("addressed", True)
    return out


def cache_rows(hist):
    """前缀缓存窗口：逐轮只追加，攒过 svc.HISTORY_CACHE_HIGH 才一次性砍回低水位。

    返回的是**原始行**（带着 `seg` 等字段），既能送模型（外面再 `trim(...
    , limit=len(win))` 做单条截断），也能原样落盘。两条路径必须走同一个函数：
    落盘的内容要和上一轮发给模型的历史序列逐字节对得上，下一轮才接得上前缀。

    砍回那一次会断一次缓存，这是滞回的代价；换来的是其余 9 轮都能整段命中，
    比每轮滑动（每轮都断）划算得多。
    """
    from app.features.persona import service as svc

    rows = [m for m in (hist or [])
            if isinstance(m, dict) and m.get("role") in ("user", "assistant")]
    if len(rows) > svc.HISTORY_CACHE_HIGH:
        rows = rows[-svc.HISTORY_LIMIT:]
    return rows


def trim_chars(msgs, budget, min_rows=6):
    """按总字数从最老的一头丢历史，保留最近几轮，且让开头落在用户消息上。

    为什么按字数而不是条数：扮演回复长短差很大，一条 1,400 字的回复和
    一条 60 字的「嗯」占的 token 完全不同。按条数截会让「6 轮」变成
    8,000~9,000 token 的盲盒；按字数截才是真正的预算。

    `min_rows` 是保底：预算再小也至少留最近 6 条（3 轮），避免刚聊两句
    就只剩一句话。若最后一条自己就超预算，仍然保留它——空历史更糟。

    只用于**送模型**的副本；落盘/归档仍走 `cache_rows`，一条不少。
    """
    rows = list(msgs or [])
    if not rows or budget <= 0:
        return rows
    keep = max(0, min(len(rows), int(min_rows or 0)))
    idx = len(rows) - keep
    total = sum(len(str(m.get("content") or "")) for m in rows[idx:])
    while idx > 0:
        n = len(str(rows[idx - 1].get("content") or ""))
        if total + n > budget:
            break
        idx -= 1
        total += n
    # 截出来的第一条如果是 assistant，说明它对应的用户消息被丢了；
    # 从用户消息开始，模型看到的才是一组完整的「问 → 答」。
    while idx < len(rows) and rows[idx].get("role") != "user":
        idx += 1
    return rows[idx:] or rows[-max(1, keep):]


def _split_meta(text):
    """把一条 assistant 回复切成「正文」和「结构化的三行」。

    返回 `(正文, "\\n画面：…\\n心情：…\\n状态：…")`；没有结构化行时尾巴为空串。
    用行首匹配而不是别处出现的词，避免正文里提到「画面」被误切。
    """
    from app.features.persona import service as svc

    lines = str(text or "").split("\n")
    keep, idx = [], len(lines)
    # 从尾巴往回找连续的三类元数据行（允许中间夹空行）
    metas, i = [], len(lines) - 1
    while i >= 0:
        ln = lines[i].strip()
        if not ln or any(rx.match(ln) for rx in (svc._SCENE_RE, svc._MOOD_RE, svc._STATE_RE)):
            if ln and any(rx.match(ln) for rx in (svc._SCENE_RE, svc._MOOD_RE, svc._STATE_RE)):
                metas.insert(0, lines[i])
                idx = i
            i -= 1
            continue
        break
    if not metas:
        return str(text or ""), ""
    keep = lines[:idx]
    return "\n".join(keep).rstrip() + "\n", "\n" + "\n".join(metas)
