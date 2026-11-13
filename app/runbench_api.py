# -*- coding: utf-8 -*-
"""随心配「预设」存储层（2026-11-13 用户拍板「存预设」功能）。

用户玩随心配抽到喜欢的配方，想存下来以后一直用。一条预设 = 一次抽卡结果
（2 个底模 + 5 个 LoRA + seed）+ 名字 + 可选封面图。

和 `lora_presets`（zdy- 前缀、LoRA 槽位组合、按 QQ 隔离）是**两套东西**：
那边管「在某渠道上调 LoRA 槽」，这边管「随心配抽出来的一整份配方」。不复用
它的前缀/额度/隔离规则——预设是全局共用、自由命名的。

数据：`state/runbench_presets.json`，结构：
    {"名字": {"models": [...], "loras": [...], "seed": int, "cover": "", "ts": ...}}

规则（2026-11-13 用户拍板）：
- **全局共用**：不分 QQ，所有用户看到同一份预设库。
- **同名覆盖**：存同名预设直接覆盖旧的（简单；用户拍板）。
- 名字：1~16 个汉字/字母/数字/下划线，**不许空格**（QQ 消息断词边界）。
- 封面图可选：QQ 里 /存预设 自动抓刚出的图当封面；也可以只存配方留空。

「代码只登记事实」：save/get/delete/list 只做读写，不判断预设好坏。
"""

import json
import logging
import os
import re
import shutil
import threading
import time

from app.config import state_path

from PIL import Image

log = logging.getLogger("runbench_api")

# 名字约束：跟 lora_presets 同口径——1~16 个汉字/字母/数字/下划线，不许空格。
NAME_RE = re.compile(r"^[0-9A-Za-z_\u4e00-\u9fa5]{1,16}$")

_LOCK = threading.Lock()


def _path():
    # 不缓存：state_path 在测试进程里会切到临时目录，模块级缓存会串味
    return state_path("runbench_presets.json")


def load():
    """读整份预设库。文件不存在/读坏返回 {}。"""
    try:
        with open(_path(), encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:                             # noqa: BLE001
        pass
    return {}


def _write(data):
    """整份写回。RLock + 临时文件 + os.replace，多进程/多线程并发安全。"""
    try:
        os.makedirs(os.path.dirname(_path()), exist_ok=True)
        tmp = _path() + ".%d.tmp" % os.getpid()
        with _LOCK:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, _path())
    except Exception as exc:                      # noqa: BLE001
        log.warning("写预设库失败：%s", exc)


def clean_name(raw):
    """把用户打的预设名折成合法键。返回 (名字, 错话)；错话非空就是没过校验。"""
    name = (raw or "").strip()
    if not name:
        return "", "预设名不能为空。"
    if not NAME_RE.match(name):
        return "", ("预设名只能 1~16 个汉字/字母/数字/下划线，不能有空格或符号"
                    "（比如「可爱画风」可以，「可爱 画风」不行）。")
    return name, ""


def get(name):
    """按名字取一条预设。没有返回 None。"""
    return load().get(name)


def save(name, models, loras, seed=None, cover=""):
    """存一条预设（同名覆盖）。返回 (名字, 错话)；错话非空就是没存上。

    models/loras 是文件名列表（带 .safetensors），seed 是 int 或 None。
    """
    name, err = clean_name(name)
    if err:
        return "", err
    if not models and not loras:
        return "", "配方是空的，没东西可存。"
    data = load()
    data[name] = {
        "models": list(models or []),
        "loras": list(loras or []),
        "seed": seed,
        "cover": cover or "",
        "ts": int(time.time()),
    }
    _write(data)
    return name, ""


def delete(name):
    """删一条预设。返回是否真删了。"""
    data = load()
    if name not in data:
        return False
    del data[name]
    _write(data)
    return True


def list_names():
    """所有预设名，按存的时间从新到旧排。"""
    data = load()
    items = sorted(data.items(), key=lambda kv: kv[1].get("ts", 0), reverse=True)
    return [k for k, _ in items]


def resolve(raw_name):
    """把用户打的预设名折成库里真实存在的名字。返回 (名字, 错话)。

    用户可能打错字/多打空格，这里先做 clean_name 的规范化，再看库里有没有。
    不猜近似——猜错了存的是别人起惯的名字，动错了就是另一张配方。
    """
    name, err = clean_name(raw_name)
    if err:
        return "", err
    if name not in load():
        return "", "没有叫「%s」的预设。用 /预设列表 看看有哪些。" % name
    return name, ""


# ─── 封面图（2026-11-13）─────────────────────────────────
#
# 跑图台卡片要一张封面缩略图。封面图**和主图库分离**：单独放
# `state/runbench_covers/<key>.png`，不混进 ComfyUI 的 output 目录
# （否则会被 `/api/output/tree` 当产出列出来、被定期清理、还跟生图目录纠缠）。
#
# `key` 两种身份（2026-11-13 用户拍板）：
#   - **官方渠道封面**：key = 渠道 id（如 `anima-2s`、`silver`），图片管理页
#     「存为封面」从图 rel 里读出渠道名，存成该渠道的默认封面。跑图台官方
#     渠道卡片显示它。
#   - **预设封面**：key = 预设名（如「可爱画风」），/存预设 时自动抓大大怪
#     刚出的图当封面。跑图台预设卡片显示它。
# 两者共用同一目录，靠 key 区分；渠道 id 可能带 `-`（预设名不许），所以
# `save_cover` 不做 clean_name 的预设名校验——合法性由调用方各自把关。
# 「代码只登记事实」：这里只做「复制文件」，不判断图好不好、合不合适。

_COVER_EXT = ".png"          # 统一转 PNG（来源图可能是 jpg/webp，复制前不转码，
                             # 只按扩展名存；真正要转码由图片管理页后端负责）

_THUMB_EXT = ".thumb.jpg"    # 封面缩略图：JPEG，长边压到 480px。跑图台卡片
                             # 用它，原图 PNG（8~16MB、2464×3680）只留给灯箱看
                             # 大图。懒生成 + 磁盘缓存（见 thumb_path / ensure_thumb）。

_THUMB_LONG = 480            # 缩略图长边像素。480 足够卡片显示，单张 ~30KB。
_THUMB_QUALITY = 82          # JPEG 质量，够清晰又压得住体积。


def covers_dir():
    """封面目录。不缓存——state_path 测试进程会切临时目录。"""
    return state_path("runbench_covers")


def cover_path(name):
    """某个 key（渠道 id 或预设名）的封面绝对路径（无论文件在不在）。"""
    return os.path.join(covers_dir(), name + _COVER_EXT)


def save_cover(name, src_abs):
    """把一张图复制成某 key 的封面。返回 (ok, 错话)。

    同名覆盖（用户拍板）。src_abs 必须是已存在的文件；内容校验（是不是图）
    交给调用方（图片管理页后端）先做，这里只负责「复制到封面目录」。
    name 可以是渠道 id（含 `-`）或预设名，不做预设名校验（合法性调用方把关）。
    """
    name = (name or "").strip()
    if not name:
        return False, "封面名不能为空。"
    if not os.path.isfile(src_abs):
        return False, "来源图不存在。"
    try:
        os.makedirs(covers_dir(), exist_ok=True)
        dst = cover_path(name)
        # 先写 .tmp 再 os.replace：复制中途被打断不会留下半张封面。
        tmp = dst + ".%d.tmp" % os.getpid()
        shutil.copyfile(src_abs, tmp)
        os.replace(tmp, dst)
        return True, ""
    except OSError as exc:
        log.warning("存封面失败 %s：%s", name, exc)
        return False, "封面写不进去（磁盘满/权限？）"


def delete_cover(name):
    """删某 key 的封面。文件不在也当成功（幂等）。"""
    try:
        if os.path.isfile(cover_path(name)):
            os.remove(cover_path(name))
        return True
    except OSError as exc:
        log.warning("删封面失败 %s：%s", name, exc)
        return False


def has_cover(name):
    """这个 key 有没有封面文件。"""
    return os.path.isfile(cover_path(name))


def thumb_path(name):
    """某 key 的封面缩略图绝对路径（无论文件在不在）。"""
    return os.path.join(covers_dir(), name + _THUMB_EXT)


def ensure_thumb(name):
    """懒生成封面缩略图。返回缩略图路径；生成失败/原图不在返回 ""。

    - 原图 PNG（`cover_path`）在 → 长边压到 `_THUMB_LONG` 存 `thumb_path`。
    - 缩略图已存在且不比原图旧（mtime 不早于原图）→ 直接复用，不重压。
    - 缩略图是缓存，坏了/原图更新了就重压；不 care 旧缩略图残留（幂等覆盖）。
    """
    src = cover_path(name)
    if not os.path.isfile(src):
        return ""
    dst = thumb_path(name)
    try:
        if os.path.isfile(dst) and os.path.getmtime(dst) >= os.path.getmtime(src):
            return dst
        with Image.open(src) as im:
            im = im.convert("RGB")          # PNG 可能是 RGBA/P，JPEG 不认透明
            w, h = im.size
            scale = min(1.0, _THUMB_LONG / max(w, h))
            if scale < 1.0:
                nw = max(1, int(round(w * scale)))
                nh = max(1, int(round(h * scale)))
                im = im.resize((nw, nh), Image.LANCZOS)
            # 先写 .tmp 再 os.replace：压缩中途被打断不留半张缩略图。
            tmp = dst + ".%d.tmp" % os.getpid()
            im.save(tmp, "JPEG", quality=_THUMB_QUALITY, optimize=True)
            os.replace(tmp, dst)
        return dst
    except Exception:                         # noqa: BLE001
        log.warning("生成封面缩略图失败 %s", name, exc_info=True)
        return ""
