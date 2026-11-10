"""群友漫画台：整本跑完把成品打成 zip，从本机直接私聊发给群友。

## 为什么走 QQ 而不是网页下载

图在 `D:\\AI\\...` 本机磁盘，QQ 协议端（SnowLuma）也在本机，收件人在国内。
走 5175 的 Cloudflare 免费隧道下载，等于「本机 → 海外节点 → 回国 → 浏览器」
绕一圈，还吃隧道的长连接与大文件限制。QQ 私聊文件走的是本机带宽与腾讯
国内节点，最快。网页只留几十 KB 的缩略图做预览（见 `app/comic_thumb.py`）。

## 两个包分开

- **底图包** `raw/NN_raw.png`：跳过 2x 放大后单张约 4MB。
- **成品包** `final/NN_final.png`：加气泡/文字之后的图。
- 写真任务另发一个写真包 `portraits/NN.png`。

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
import threading
import time
import zipfile

from app.config import BASE_DIR, COMFYUI_OUTPUT_DIR

_LOCK = threading.Lock()
_COOLDOWN = 60.0            # 手动重发冷却，防连点刷屏
_last_manual = {}           # {任务目录: 上次手动重发时刻}

_HINT = ("作品跑好了，文件正在打包并通过 QQ 发给你，可能要等一会儿才收得到，"
         "不用守着网页。成品服务端不长期保存，收到后请及时下载保存。")


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


def _pack(task_dir, qq, label, files):
    """打一个 zip，返回绝对路径。PNG 本身已压缩，ZIP_STORED 省 CPU。"""
    dst_dir = delivery_dir(task_dir, qq)
    os.makedirs(dst_dir, exist_ok=True)
    work = os.path.basename(str(task_dir).rstrip("\\/")) or "作品"
    name = "%s_%s.zip" % (work, label)
    dst = os.path.join(dst_dir, name)
    tmp = dst + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:
        for fp in files:
            z.write(fp, os.path.basename(fp))
    os.replace(tmp, dst)
    return dst


def deliver(task_dir, qq, force=False, auto=False):
    """打包 → QQ 私聊直发。返回本次实际发了哪几个包。

    `auto=True` 时按内容指纹去重：整本没变化就不重复发（阶段一只发底图包、
    加完气泡再发成品包，正好两个包各自一次）。
    `force=True` 忽略指纹，用于手动「重发到 QQ」/ 管理页补发。
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
                                  "files": len(files), "at": _now(),
                                  "size": os.path.getsize(path)}
                sent.append(label)
            except Exception as exc:
                results[label] = {"fingerprint": fp, "state": "failed",
                                  "files": len(files), "at": _now(),
                                  "error": str(exc)[:300]}
                st["error"] = str(exc)[:300]
        st["packages"] = results
        st["state"] = ("sent" if sent and all(
            v.get("state") == "sent" for v in results.values())
            else "failed" if any(v.get("state") == "failed"
                                 for v in results.values()) else "idle")
        st["at"] = _now()
        st["qq"] = qq
        if "error" not in st:
            st["error"] = ""
        elif st["state"] == "sent":
            st["error"] = ""
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
        # 一条说明就够，别每个包再刷一条消息。
        try:
            from app import qq_api
            qq_api.send_private(qq, _HINT)
        except Exception:
            pass
    return sent


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


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
        packs.append({"label": label, "state": v.get("state"),
                      "files": v.get("files"), "at": v.get("at"),
                      "size": v.get("size"), "error": v.get("error")})
    return {"state": st.get("state") or "idle", "at": st.get("at") or "",
            "error": st.get("error") or "", "packages": packs,
            "hint": _HINT}
