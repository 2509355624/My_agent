"""表情包收藏库：群里出现的好图趁链接活着存下来，打上标签，回头能甩出去。

为什么必须趁热存：QQ 群图的直链（multimedia.nt.qq.com.cn）带时效签名，
过几天就 404。想攒一个"像真人一样随时甩表情"的图库，唯一的办法是图片
一出现就下载落盘——所以收藏挂在每轮处理的开头（worker 线程，阻塞无妨），
跟「这一轮要不要回复」完全解耦：接话被拒的轮次，图也照收。

判定"是不是表情包"用尺寸就行：动图（GIF）一律算；静态图最长边不超过
STICKER_MAX_EDGE 的算。群相册照片动辄几千像素，这条线能把照片挡在外面。

标签用识图模型打一次（每张图只打一次，md5 去重后不会重复调）。失败不拦
收藏——没标签的图照样入库，只是 send_sticker 挑不到它。

存哪：agents/<agent_id>/stickers/
    <md5前8位>.<后缀>                    图片本体（原始字节，不压缩——
                                         甩出去的就是群里看到的那张）
    index.jsonl                           一行一条：
    {"md5","file","tags","sender","t","url","w","h"}
"""

import hashlib
import io
import json
import logging
import os
import random
import threading
import time

from app import agents as agent_store
from app.vision import fetch_image, sniff_mime

log = logging.getLogger("stickers")

# 静态图最长边上限：超过按照片处理，不入库
STICKER_MAX_EDGE = 640

# 收藏上限：库存满了就不再收新的（用户口径：最多给 AI 100 个选择）。
# 不做淘汰——删旧留新需要"哪张好"的判断，现在没有这个信号，先简单停收。
STICKER_LIMIT = 100

_EXT = {"image/jpeg": "jpg", "image/png": "png",
        "image/gif": "gif", "image/webp": "webp"}

# 并发保护：索引的「读-判-写」必须整体原子，否则两轮同时收藏会重复入库
_lock = threading.Lock()

# 「库存已满」只提醒一次的标志（lock 对象挂不了属性，单独放）
_full_warned = False


def _dir(agent_id):
    aid = agent_store.safe_agent_id(agent_id) or "main"
    return os.path.join(agent_store.AGENTS_DIR, aid, "stickers")


def _index_path(agent_id):
    return os.path.join(_dir(agent_id), "index.jsonl")


def _load_index(agent_id):
    """读全量索引。文件不存在/坏行跳过——收藏是锦上添花，不该报错。"""
    try:
        with open(_index_path(agent_id), "r", encoding="utf-8") as f:
            return [json.loads(l) for l in f if l.strip()]
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        log.warning("表情包索引读不出来（忽略）：%s", exc)
        return []


def _is_sticker(raw):
    """GIF 一律算表情包；其他格式看尺寸。返回 (是否, 宽, 高)。"""
    if sniff_mime(raw) == "image/gif":
        return True, 0, 0
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(raw))
        w, h = im.size
    except Exception:
        return False, 0, 0
    return max(w, h) <= STICKER_MAX_EDGE, w, h


def _tag(data_url):
    """让识图模型给表情包打标签，返回标签列表；失败返回空表。"""
    from app.config import VISION_TIMEOUT
    from app.vision import describe

    prompt = (
        "这是QQ聊天里的表情包。给它打3-5个标签，表示它的情绪和使用场景"
        "（例如：无语、大笑、摸鱼、害怕、点赞、好耶、裂开）。"
        "只输出标签本身，用逗号分隔，不要解释。"
    )
    try:
        text = describe(data_url, timeout=VISION_TIMEOUT, prompt=prompt)
    except Exception as exc:
        log.warning("表情包打标签失败（存无标签版）：%s", exc)
        return []
    tags = [t.strip() for t in text.replace("，", ",").split(",")]
    return [t for t in tags if t and len(t) <= 12][:6]


def collect(agent_id, items):
    """收藏一批图。items = [(url, 发送者昵称), ...]。

    每张图：下载 → md5/URL 去重 → 尺寸判定 → 落盘 → 打标签 → 记索引。
    单张失败只记日志，不影响其他张。返回收藏张数（给日志用）。
    """
    saved = 0
    for url, sender in items:
        if not url:
            continue
        # 下载前先看一眼索引：同一个 URL 之前收过就不用再下（下载前查是
        # 快速路径；锁内还有一道 md5/URL 查重，防并发窗口里的重复）
        if url in {r.get("url") for r in _load_index(agent_id)}:
            continue
        try:
            raw = fetch_image(url)
        except Exception as exc:
            log.info("表情包下载失败，跳过：%s", exc)
            continue

        digest = hashlib.md5(raw).hexdigest()
        # 「查重-落盘-记索引」整体持锁：两条会话线程同时进来不会重复入库
        with _lock:
            index = _load_index(agent_id)
            if digest in {r.get("md5") for r in index}:
                continue
            if url in {r.get("url") for r in index}:
                continue
            if len(index) >= STICKER_LIMIT:
                # 库满了就停收，不淘汰——哪张该删没有判断依据，别瞎删
                global _full_warned
                if not _full_warned:
                    log.info("表情包库存已满（%d 张），不再收藏", STICKER_LIMIT)
                    _full_warned = True
                continue
            ok, w, h = _is_sticker(raw)
            if not ok:
                continue
            os.makedirs(_dir(agent_id), exist_ok=True)
            name = "%s.%s" % (digest[:8], _EXT[sniff_mime(raw)])
            try:
                with open(os.path.join(_dir(agent_id), name), "wb") as f:
                    f.write(raw)
            except OSError as exc:
                log.warning("表情包落盘失败：%s", exc)
                continue
            try:
                from app.vision import to_data_url
                tags = _tag(to_data_url(raw))
            except Exception:
                tags = []
            rec = {
                "md5": digest, "file": name, "tags": tags,
                "sender": sender or "", "t": int(time.time()),
                "url": url, "w": w, "h": h,
            }
            try:
                with open(_index_path(agent_id), "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                saved += 1
            except OSError as exc:
                log.warning("表情包索引写入失败：%s", exc)
    if saved:
        log.info("表情包收藏 %d 张", saved)
    return saved


def abs_path(agent_id, rec):
    """索引记录 → 图片本体的绝对路径（file 已不在磁盘时自行判断存在性）。"""
    return os.path.join(_dir(agent_id), rec.get("file", ""))


def pick(agent_id, query):
    """按标签挑一张表情包，返回索引记录；空库返回 None。

    query 是模型用自然语言说的情绪/场景（"大笑""无语"）。匹配规则宽松：
    标签和 query 互含就算命中。query 带"随便/随机"时不挑直接随机。
    标签匹配不上也随机兜底一张——甩表情不是精准检索，真人经常乱甩，
    频繁甩的场合里"有图可用"比"图完全对题"要紧。
    文件已被手动删掉的条目跳过。
    """
    d = _dir(agent_id)
    entries = [r for r in _load_index(agent_id)
               if os.path.exists(os.path.join(d, r.get("file", "")))]
    if not entries:
        return None
    q = (query or "").strip()
    if not q or any(w in q for w in ("随便", "随机", "来一个", "整一个")):
        return random.choice(entries)
    scored = []
    for r in entries:
        hit = sum(1 for t in r.get("tags") or [] if t and (t in q or q in t))
        if hit:
            scored.append((hit, r))
    if not scored:
        return random.choice(entries)
    best = max(h for h, _ in scored)
    return random.choice([r for h, r in scored if h == best])
