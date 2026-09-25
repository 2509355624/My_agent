"""生图后台投递：QQ 会话里提交即返回，图出来自己发回原会话。

## 它解决什么

generate_image 原先是同步的：工具不返回，agent 循环就走不下去，文本回复
也得跟着等显卡。而这一等还占着适配层的并发槽（QQ_MAX_CONCURRENCY，默认 2），
一张图几分钟，等于别的群陪着一起干等。

## 做法

提交拿到 prompt_id 就返回给模型，轮询等出图这件事挪到后台线程：出图后直接
调 qq_api 发回原来那个群 / 那个人，不经过 agent 循环，也不占并发槽。文本
走文本的队，图片走图片的队。

## 关键取舍

- **会话在提交那一刻快照**：qq_api 的上下文是线程本地的，后台线程取不到，
  所以 target / target_id 必须当参数带进来，不能靠回头猜（猜错就发错群）。
- **同一会话最多同时 MAX_INFLIGHT 张**：防一个人连环点单把显卡全占走。超了
  返回 False，让模型自己去说「前面还有几张在画」。
- **失败在群里说一句**：对方点了单，图没了却一声不吭会让人干等。发送失败
  只记日志——那已经是无话可说的场面了。
- **后台线程不查取消信号**：取消事件绑在请求线程上，后台线程本来就没有；
  而且中断的是「等待」，不该中断「出图」——图会在 ComfyUI 里照常跑完
  （与 generate_image 里放弃等待而不去 /interrupt 同一个理由）。
"""

import logging
import threading
import time
from urllib.parse import quote

import requests

from app.config import COMFYUI_URL, IMAGE_GEN_TIMEOUT

log = logging.getLogger("image_jobs")

# 同一会话同时在画的张数上限
MAX_INFLIGHT = 2

# 轮询间隔（秒）
POLL_INTERVAL = 2

_lock = threading.Lock()
_inflight = {}          # (target, target_id) -> [prompt_id, ...]


def _key(target, target_id):
    return (str(target or ""), str(target_id or ""))


def inflight_count(target, target_id):
    """这个会话还有几张在画。"""
    with _lock:
        return len(_inflight.get(_key(target, target_id), []))


def poll_once(prompt_id):
    """问一次 ComfyUI 出图没有；没出或问不到返回 None。

    网络抖动、半截 JSON 一律当「还没好」——轮询本来就是容错手段，一次失败
    不影响大局，真出不了图最后由超时兜住。
    """
    try:
        resp = requests.get(COMFYUI_URL + "/history/" + str(prompt_id),
                            timeout=10)
        resp.raise_for_status()
        history = resp.json()
    except Exception:
        return None
    if isinstance(history, dict) and prompt_id in history:
        return history[prompt_id]
    return None


def wait_done(prompt_id, timeout=None):
    """轮询等到出图，返回 history entry；超时抛 TimeoutError。

    后台线程专用。与 generate_image._wait_for_completion 的差别只有一处：
    它不查取消信号（见模块 docstring）。
    """
    limit = timeout if timeout and timeout > 0 else IMAGE_GEN_TIMEOUT
    start = time.time()
    while time.time() - start < limit:
        entry = poll_once(prompt_id)
        if entry is not None:
            return entry
        time.sleep(POLL_INTERVAL)
    raise TimeoutError("生成超时 (" + str(limit) + "s)")


def output_images(entry):
    """从 history entry 里掏出文件名列表；没有返回空列表。"""
    out = []
    for node in (entry.get("outputs") or {}).values():
        if not isinstance(node, dict):
            continue
        for img in (node.get("images") or []):
            if isinstance(img, dict) and img.get("filename"):
                out.append(img["filename"])
    return out


def submit(target, target_id, prompt_id):
    """登记一个在途任务并起后台线程。返回 (是否接下, 当前在途张数)。

    接不下（同一会话已经排满）时返回 (False, 在途数)——调用方拿这个数去
    跟模型说「前面还有几张」。
    """
    key = _key(target, target_id)
    with _lock:
        cur = _inflight.setdefault(key, [])
        if len(cur) >= MAX_INFLIGHT:
            return False, len(cur)
        cur.append(prompt_id)
    try:
        threading.Thread(target=_run, args=(target, target_id, prompt_id),
                         daemon=True, name="image-job").start()
    except Exception:
        _release(key, prompt_id)     # 线程没起来就别占着名额
        raise
    return True, inflight_count(target, target_id)


def _release(key, prompt_id):
    with _lock:
        cur = _inflight.get(key)
        if not cur:
            return
        if prompt_id in cur:
            cur.remove(prompt_id)
        if not cur:
            _inflight.pop(key, None)


def _reason(exc):
    """给群友看的一句失败原因：超时单独说，其余照抄异常（截断防刷屏）。"""
    if isinstance(exc, TimeoutError):
        return "超时"
    return (str(exc) or type(exc).__name__)[:30]


def _send_text(target, target_id, text):
    from app import qq_api
    if target == "group":
        qq_api.send_group(target_id, text)
    else:
        qq_api.send_private(target_id, text)


def _send_image(target, target_id, filename):
    from app import qq_api
    url = COMFYUI_URL.rstrip("/") + "/view?filename=" + quote(filename)
    qq_api.send_image(target, target_id, url)


def _run(target, target_id, prompt_id):
    """后台线程：等出图 → 发回原会话；失败说一句。"""
    key = _key(target, target_id)
    try:
        entry = wait_done(prompt_id)
        names = output_images(entry)
        if not names:
            raise RuntimeError("跑完了但没找到图片")
        for name in names:
            _send_image(target, target_id, name)
        log.info("生图完成已发回 %s %s：%d 张", target, target_id, len(names))
    except Exception as exc:
        log.warning("生图任务失败 %s %s：%s", target, target_id, exc)
        try:
            _send_text(target, target_id, "图没画出来（%s）" % _reason(exc))
        except Exception:
            log.exception("生图失败说明也发不出去 %s %s", target, target_id)
    finally:
        _release(key, prompt_id)
