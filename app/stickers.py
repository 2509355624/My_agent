"""表情包收藏库：群里出现的好图趁链接活着存下来，打上标签，回头能甩出去。

为什么必须趁热存：QQ 群图的直链（multimedia.nt.qq.com.cn）带时效签名，
过几天就 404。想攒一个"像真人一样随时甩表情"的图库，唯一的办法是图片
一出现就下载落盘——所以收藏挂在每轮处理的开头（worker 线程，阻塞无妨），
跟「这一轮要不要回复」完全解耦：接话被拒的轮次，图也照收。

判定"是不是表情包"用尺寸就行：动图（GIF）一律算；静态图最长边不超过
STICKER_MAX_EDGE 的算。群相册照片动辄几千像素，这条线能把照片挡在外面。

标签用识图模型打一次（每张图只打一次，md5 去重后不会重复调）。打的是
「画面内容 + 情绪标签」：模型选表情靠的是每轮注入的清单一行字，只有情绪词
撑不起"看图挑图"，得让它知道画面里画的是什么。失败不拦收藏——没标签的图
照样入库，只是清单里只能干列标签。

存哪：agents/<agent_id>/stickers/
    <md5前8位>.<后缀>                    图片本体（原始字节，不压缩——
                                         甩出去的就是群里看到的那张）
    index.jsonl                           一行一条：
    {"md5","file","desc","tags","sender","t","url","w","h","deleted"}

删：模型自己觉得哪张不好用，调 delete_sticker 按编号删。删的是「索引行
打 deleted 标记 + 删图片文件」而不是删行——编号是行号，删行会让后面的
号全部前移，模型记住的号就指错图了。上限按活着的条目算，所以删出来的
位置能让新图进来。
"""

import hashlib
import io
import json
import logging
import os
import re
import threading
import time

from app import agents as agent_store
from app.vision import fetch_image, sniff_mime

log = logging.getLogger("stickers")

# 静态图最长边上限：超过按照片处理，不入库
STICKER_MAX_EDGE = 640

# 收藏上限：库存满了就不再收新的（用户口径：最多给 AI 20 个选择——
# 清单每轮都全量注入，20 条是「够挑」和「不烧 token」的平衡点）。
# 不做自动淘汰——哪张该删没有判断依据，交给模型用 delete_sticker 自己删。
STICKER_LIMIT = 20

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


def _live(entries):
    """过滤掉已删除的条目（墓碑）。库存计数、去重都只看活着的。"""
    return [r for r in entries if not r.get("deleted")]


def _write_index(agent_id, entries):
    """整份重写索引（打删除标记时用）。调用方需持有 _lock。

    先写 .tmp 再 os.replace：重写途中崩了也不会留下半份索引。
    """
    path = _index_path(agent_id)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for r in entries:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
        return True
    except OSError as exc:
        log.warning("表情包索引重写失败：%s", exc)
        return False


def _label(rec):
    """一条记录渲染成清单里的一行说明（画面 + 头几个标签）。"""
    desc = (rec.get("desc") or "").strip()
    tags = [t for t in (rec.get("tags") or []) if t]
    if desc and tags:
        return "%s（%s）" % (desc, "/".join(tags[:3]))
    return desc or "、".join(tags) or "（没打上标签）"


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
    """让识图模型给表情包写「画面 + 情绪」，返回 (描述, 标签列表)；失败空表。

    只有情绪词撑不起选图：模型每轮看的是清单一行字，得知道画面里是什么
    （"猫瘫在桌上打滚"和"熊猫头瞪眼"都是"无语"，但用起来完全两码事）。
    """
    from app.config import VISION_TIMEOUT
    from app.vision import describe

    prompt = (
        "这是QQ聊天里的表情包。先用不超过12个字描述画面内容"
        "（例如：猫瘫在桌上打滚、熊猫头瞪眼），然后用｜隔开，"
        "再给3-5个情绪或使用场景标签（如：无语、大笑、摸鱼、好耶）。"
        "只输出这一行，不要解释。"
    )
    try:
        text = describe(data_url, timeout=VISION_TIMEOUT, prompt=prompt)
    except Exception as exc:
        log.warning("表情包打标签失败（存无标签版）：%s", exc)
        return "", []
    desc, _, tag_part = text.strip().partition("｜")
    if not tag_part:
        desc, _, tag_part = desc.partition("|")
    tags = [t.strip() for t in tag_part.replace("，", ",").split(",")]
    return desc.strip()[:20], [t for t in tags if t and len(t) <= 12][:6]


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
        # 快速路径；锁内还有一道 md5/URL 查重，防并发窗口里的重复）。
        # 只跟活着的条目比——删过的图再发一次，说明它还想进库，那就收。
        if url in {r.get("url") for r in _live(_load_index(agent_id))}:
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
            live = _live(index)
            if digest in {r.get("md5") for r in live}:
                continue
            if url in {r.get("url") for r in live}:
                continue
            # 上限按「活着的」算：删过的位置要让给新图，否则删了也白删
            if len(live) >= STICKER_LIMIT:
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
                desc, tags = _tag(to_data_url(raw))
            except Exception:
                desc, tags = "", []
            rec = {
                "md5": digest, "file": name, "desc": desc, "tags": tags,
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


def catalog(agent_id):
    """渲染给模型看的表情包清单（带稳定编号），空库返回空串。

    编号 = 该记录在 index.jsonl 里的行号（从 1 起）。索引只追加不重排，
    所以编号跨轮稳定——模型这轮报 7 号，下轮 7 号还是同一张。已删除的
    条目和文件被手动删掉的条目都不进清单，但编号照算（清单一出一变，
    不能让旧号错位）。

    每行 = 编号 + 画面描述 + 头几个情绪标签。挑图靠的是这一行字，描述
    加标签都比纯标签好使；旧条目没 desc 就退回标签拼接。
    """
    d = _dir(agent_id)
    lines = []
    for i, r in enumerate(_load_index(agent_id), 1):
        if r.get("deleted"):
            continue
        if not os.path.exists(os.path.join(d, r.get("file", ""))):
            continue
        lines.append("%d. %s" % (i, _label(r)))
    if not lines:
        return ""
    return ("[表情包库] 想甩表情就调 send_sticker 报编号"
            "（可一次报多个，如 3 或 3,7）：\n" + "\n".join(lines))


def records_by_numbers(agent_id, nums_text):
    """把模型报的编号解析成索引记录，返回 [(编号, 记录), ...]。

    编号语义与 catalog 严格一致：index.jsonl 行号。越界/无效编号跳过；
    已删除的条目、文件已被手动删掉的条目都当不存在。返回空列表 = 没一个
    号能用。
    """
    d = _dir(agent_id)
    entries = _load_index(agent_id)
    out = []
    for n in (int(s) for s in re.findall(r"\d+", str(nums_text or ""))):
        if not 1 <= n <= len(entries):
            continue
        rec = entries[n - 1]
        if rec.get("deleted"):
            continue
        if os.path.exists(os.path.join(d, rec.get("file", ""))):
            out.append((n, rec))
    return out


def delete(agent_id, nums_text):
    """按编号删表情包，返回 (删掉的[(编号, 说明)], 没删成的[编号])。

    删法：给索引行打 deleted 标记 + 删掉本地图片文件，**不删索引行**。
    编号 = 行号，删行会让后面所有号前移，模型这轮记住的号下轮就指错图了。
    打标记之后那个号永久空着，跟"文件被手动删掉"是同一种处理（清单里
    不再出现，编号照算）。删过的图再被发到群里，会当成新图重新收。
    """
    with _lock:
        entries = _load_index(agent_id)
        done, skipped = [], []
        for n in (int(s) for s in re.findall(r"\d+", str(nums_text or ""))):
            if not 1 <= n <= len(entries) or entries[n - 1].get("deleted"):
                skipped.append(n)
                continue
            rec = entries[n - 1]
            rec["deleted"] = True
            try:
                os.remove(abs_path(agent_id, rec))
            except OSError:
                pass              # 文件本来就不在也无所谓，标记生效即可
            done.append((n, _label(rec)))
        if done:
            _write_index(agent_id, entries)
            global _full_warned
            _full_warned = False  # 腾出位置了，下次满了再提醒一次
            log.info("表情包删除 %d 张：%s", len(done),
                     ",".join(str(n) for n, _ in done))
        return done, skipped
