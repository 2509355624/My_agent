"""群友漫画台：整本跑完把成品打成 zip，从本机直接私聊发给群友。

## 为什么走 QQ 而不是网页下载

图在 `D:\\AI\\...` 本机磁盘，QQ 协议端（SnowLuma）也在本机，收件人在国内。
走 5175 的 Cloudflare 免费隧道下载，等于「本机 → 海外节点 → 回国 → 浏览器」
绕一圈，还吃隧道的长连接与大文件限制。QQ 私聊文件走的是本机带宽与腾讯
国内节点，最快。网页只留几十 KB 的缩略图做预览（见 `app/comic_thumb.py`）。

## 两个包分开

- **无气泡** `raw/NN_raw.png`：跳过 2x 放大后单张约 4MB。
- **有气泡** `final/NN_final.png`：加气泡/文字之后的图。
- 写真任务另发一个「写真」包 `portraits/NN.png`。

QQ 里收到的名字是「<作品名> 无气泡 / 有气泡」，不用「底图包 / 成品包」
这种内部叫法（2026-11-10 实测反馈）。内部 dict 的 key 仍沿用老名字，
指纹去重与历史 `delivery.json` 才不会因为改文案而失效。

用户各取所需，不用一次传 80MB。哪个包的文件变了就只重发那个包
（换种子重跑、改画面词都只吃 GPU 不花 Token，重发也不额外收费）。

## 落盘与生命周期

`output/群友漫画台/<QQ>/<作品>/zip/`。**不自动删**：协议端拉本机文件是异步的
（`app/image_out.py:KEEP_SECONDS` 那条血泪教训），发完立刻删会发出空文件；
用户也明确说「服务端不持久化由我自己清」。zip 留着还顺手支持手动重发。

状态写任务目录 `delivery.json`，网页轮询显示；失败只在页面提示与管理页重发，
**不回退网关下载**。
"""

import json
import os
import re
import threading
import time
import zipfile

from app.config import BASE_DIR, COMFYUI_OUTPUT_DIR

_LOCK = threading.Lock()
_COOLDOWN = 60.0            # 手动重发冷却，防连点刷屏
_last_manual = {}           # {任务目录: 上次手动重发时刻}

# 给用户看的一句话规则（2026-11-10 改）：底图/写真跑完只通知，用户检查确认后
# 点「一键加气泡」或「一键发送 zip」才发高清包。全站单张串行，正常排队。
# 长篇说明一律不写（用户实测反馈：看不进也看不到）。
RULE = "跑完通知 · 检查后再发送 · 正常排队"

# 内部 key → 给用户看的名字。key 不动，指纹去重和老状态文件才不受影响。
_DISPLAY = {"底图包": "无气泡", "成品包": "有气泡", "写真包": "写真"}


def _work_name(task_dir):
    """作品名，用来给 zip 命名：`C20261110-01_学姐的饮料` → `学姐的饮料`。

    剧本目录本来就带标题（`comic._next_task_dir`）；老任务退剧本里的
    `# 标题：`；写真任务没剧本，用 `portrait.json` 需求快照第一行；
    都不行才用目录名。只做文件名清洗，不改动任何数据。
    """
    base = os.path.basename(str(task_dir).rstrip("\\/")) or "作品"
    m = re.match(r"^C\d{8}-\d+_(.+)$", base)
    title = (m.group(1) if m else "").strip()
    if not title:
        try:
            from app import comic
            meta, _ = comic.parse_script(
                os.path.join(str(task_dir), "script.md"))
            title = str((meta or {}).get("title") or "").strip()
        except Exception:
            pass
    if not title:
        # 写真任务没有剧本标题，目录名只有编号；用需求快照第一行当作品名，
        # 别让用户收到一个 `C20261110-02 写真.zip`。
        try:
            with open(os.path.join(str(task_dir), "portrait.json"),
                      encoding="utf-8") as f:
                brief = str((json.load(f) or {}).get("brief") or "").strip()
            title = brief.splitlines()[0][:12] if brief else ""
        except (OSError, ValueError, AttributeError, IndexError):
            pass
    title = re.sub(r'[\\/:*?"<>|\r\n\t]', "", (title or base)).strip()
    return title[:40] or "作品"


def pack_name(task_dir, label):
    """`<作品名> 无气泡` —— zip 文件名、QQ 提示、网页状态共用这一个名字。"""
    return "%s %s" % (_work_name(task_dir), _DISPLAY.get(label, label))


def delivery_dir(task_dir, qq):
    """`output/群友漫画台/<QQ>/<作品>/zip/`。output 没配就退回 state 下。"""
    root = (COMFYUI_OUTPUT_DIR or "").strip()
    if not root:
        root = os.path.join(BASE_DIR, "state", "comic_delivery")
    name = os.path.basename(str(task_dir).rstrip("\\/")) or "作品"
    return os.path.join(root, "群友漫画台", str(qq), name, "zip")


def delivery_state(task_dir):
    try:
        with open(os.path.join(str(task_dir), "delivery.json"),
                  encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(task_dir, data):
    fp = os.path.join(str(task_dir), "delivery.json")
    tmp = fp + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, fp)


def _fingerprint(files):
    """文件清单指纹：路径 + 大小 + mtime_ns。重跑后必变，单纯轮询不变。"""
    parts = []
    for fp in files:
        try:
            st = os.stat(fp)
            parts.append("%s|%d|%d" % (os.path.basename(fp), st.st_size,
                                       st.st_mtime_ns))
        except OSError:
            pass
    return "|".join(sorted(parts))


def _collect(task_dir):
    """→ {包名: [文件绝对路径...]}。只收当前生效的那几张，废弃图不进包。"""
    out = {}
    for label, sub, pat in (
            ("底图包", "raw", "_raw.png"),
            ("成品包", "final", "_final.png"),
            ("写真包", "portraits", ".png")):
        d = os.path.join(str(task_dir), sub)
        try:
            names = sorted(n for n in os.listdir(d) if n.endswith(pat))
        except OSError:
            continue
        files = [os.path.join(d, n) for n in names
                 if os.path.isfile(os.path.join(d, n))]
        if files:
            out[label] = files
    return out


def preview(task_dir):
    """这一刻会发哪几个包、各几张。给「点了发送」的那一瞬间回话用，
    别让用户对着一个按钮猜发出去的到底是什么。key 直接用用户看到的名字。"""
    return {pack_name(task_dir, label): len(files)
            for label, files in _collect(task_dir).items()}


def mark_packing(task_dir, state="packing", error=""):
    """手动重发先落一个「正在打包」的状态。

    `deliver()` 要等 zip 打完 + QQ 协议端回包才写 delivery.json，几十 MB 时
    这段可能好几分钟。不先写一笔，网页轮询看到的一直是上一次的旧状态，
    用户就会以为按钮坏了、反复点（2026-11-10 实测反馈）。
    """
    with _LOCK:
        st = delivery_state(task_dir)
        st["state"] = state
        st["at"] = _now()
        st["error"] = error
        try:
            _save_state(task_dir, st)
        except OSError:
            pass


def _pack(task_dir, qq, label, files):
    """打一个 zip，返回绝对路径。PNG 本身已压缩，ZIP_STORED 省 CPU。"""
    dst_dir = delivery_dir(task_dir, qq)
    os.makedirs(dst_dir, exist_ok=True)
    name = pack_name(task_dir, label) + ".zip"
    dst = os.path.join(dst_dir, name)
    tmp = dst + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:
        for fp in files:
            z.write(fp, os.path.basename(fp))
    os.replace(tmp, dst)
    return dst


def deliver(task_dir, qq, force=False, auto=False):
    """打包 → QQ 私聊直发。返回本次实际发了哪几个包。

    `auto=True` 时按内容指纹去重：整本没变化就不重复发。现在只在用户确认后
    （漫画整批加完气泡）调用一次，底图包与成品包一起发。
    `force=True` 忽略指纹，用于手动「发送 zip」/ 管理页补发。
    跑在后台线程里，任何异常都只写进 delivery.json，绝不抛回渲染线程。
    """
    task_dir, qq = str(task_dir), str(qq)
    with _LOCK:
        st = delivery_state(task_dir)
        packages = _collect(task_dir)
        if not packages:
            return []
        sent = []
        results = dict(st.get("packages") or {})
        for label, files in packages.items():
            fp = _fingerprint(files)
            old = results.get(label) or {}
            if auto and not force and old.get("fingerprint") == fp \
                    and old.get("state") == "sent":
                continue
            try:
                path = _pack(task_dir, qq, label, files)
                from app import qq_api
                qq_api.send_private_file(qq, path, os.path.basename(path))
                results[label] = {"fingerprint": fp, "state": "sent",
                                  "name": pack_name(task_dir, label),
                                  "files": len(files), "at": _now(),
                                  "size": os.path.getsize(path)}
                sent.append(label)
            except Exception as exc:
                results[label] = {"fingerprint": fp, "state": "failed",
                                  "name": pack_name(task_dir, label),
                                  "files": len(files), "at": _now(),
                                  "error": str(exc)[:300]}
                st["error"] = str(exc)[:300]
        st["packages"] = results
        # 只看「本次涉及的包」：手动 force 会把三个包全重发，但只要有一个历史
        # 失败记录还留在 results 里，按全表 any(failed) 判就会永远显示失败。
        failed_now = [str(exc_label) for exc_label in packages
                      if (results.get(exc_label) or {}).get("state") == "failed"]
        st["state"] = ("failed" if failed_now else
                       "sent" if sent else "idle")
        st["at"] = _now()
        st["qq"] = qq
        st["error"] = (str((results.get(failed_now[0]) or {}).get("error")
                           or "QQ 发送失败")[:300]) if failed_now else ""
        try:
            _save_state(task_dir, st)
        except OSError:
            pass
        from app import comic_quota
        comic_quota.note_delivery(
            qq, os.path.basename(task_dir),
            "sent" if st["state"] == "sent" else "failed",
            ",".join(sent) or st.get("error", ""))
    if sent:
        # 一行一个包，只说「发了什么」，不写「正在打包 / 正在发送」这类长句。
        try:
            from app import qq_api
            qq_api.send_private(qq, "\n".join(
                "%s 已发送" % pack_name(task_dir, lab) for lab in sent))
        except Exception:
            pass
    return sent


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def notify_ready(task_dir, qq, kind="comic"):
    """底图 / 写真跑完：**只通知、不发 zip**，等用户检查完自己触发发送。

    阶段一刚跑完的图可能有瑕疵，用户会换种子重跑；一跑完就自动发，收到的
    就是废包，重跑还会再打扰一次（2026-11-10 用户实报并拍板改流程）。
    这里只落一个 `waiting_check` 状态 + 发一条 QQ 文字，网页轮询到就弹一次窗。

    同一本只通知一次：换渠道整本重跑会再次走到这里，但 `ready_notified`
    已经写过就不再重复发 QQ（用户此刻正在网页上，自己看得到结果）。
    """
    task_dir, qq = str(task_dir), str(qq)
    with _LOCK:
        st = delivery_state(task_dir)
        if st.get("ready_notified"):
            return False
        is_portrait = kind == "portrait"
        packs = _collect(task_dir)
        n = len(packs.get("写真包" if is_portrait else "底图包") or [])
        st.update({"ready_notified": _now(), "ready_kind": kind,
                   "ready_count": n, "state": "waiting_check",
                   "at": _now(), "qq": qq})
        try:
            _save_state(task_dir, st)
        except OSError:
            pass
    # 三行短句，不写「正在打包 / 不用守着网页」这类长文案（用户明确嫌长）。
    text = ("%d 张%s已完成\n去网页检查，有瑕疵可换种子重跑\n确认后点「%s」发到 QQ"
            % (n, "写真" if is_portrait else "底图",
               "一键发送 zip" if is_portrait else "一键加气泡"))
    try:
        from app import qq_api
        qq_api.send_private(qq, text)
    except Exception:
        pass
    return True


def spawn_notify(task_dir, qq, kind="comic"):
    """后台线程做完成通知 + 预热缩略图（用户马上要回网页逐张检查）。"""
    def _run():
        try:
            from app import comic_thumb
            comic_thumb.ensure_all(task_dir)
        except Exception:
            pass
        try:
            notify_ready(task_dir, qq, kind)
        except Exception as exc:
            import logging
            logging.getLogger("comic_delivery").warning(
                "完成通知失败 %s：%s", task_dir, exc)
    threading.Thread(target=_run, daemon=True, name="comic-notify").start()


def spawn_auto(task_dir, qq):
    """渲染线程收尾调用：起独立 daemon 线程发送，不占渲染线程、不阻塞轮询。

    顺手预热缩略图，群友回网页刷新时槽位墙不用逐张现算。
    """
    def _run():
        try:
            from app import comic_thumb
            comic_thumb.ensure_all(task_dir)
        except Exception:
            pass
        try:
            deliver(task_dir, qq, auto=True)
        except Exception as exc:
            import logging
            logging.getLogger("comic_delivery").warning(
                "自动投递失败 %s：%s", task_dir, exc)
    threading.Thread(target=_run, daemon=True, name="comic-deliver").start()


def manual_ok(task_dir):
    """手动重发冷却。"""
    now = time.time()
    with _LOCK:
        last = _last_manual.get(str(task_dir), 0.0)
        if now - last < _COOLDOWN:
            return False, int(_COOLDOWN - (now - last) + 0.5)
        _last_manual[str(task_dir)] = now
        return True, 0


def view(task_dir):
    """给网页轮询的发送状态。"""
    st = delivery_state(task_dir)
    packs = []
    for label, v in sorted((st.get("packages") or {}).items()):
        # 老状态文件没有 name，按当前命名规则补一个，别把「底图包」露给用户。
        packs.append({"label": label,
                      "name": v.get("name") or pack_name(task_dir, label),
                      "state": v.get("state"),
                      "files": v.get("files"), "at": v.get("at"),
                      "size": v.get("size"), "error": v.get("error")})
    return {"state": st.get("state") or "idle", "at": st.get("at") or "",
            "error": st.get("error") or "", "packages": packs,
            "ready_kind": st.get("ready_kind") or "",
            "ready_count": int(st.get("ready_count") or 0),
            "hint": RULE}
