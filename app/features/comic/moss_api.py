"""moss 漫画台 API（2026-10-21 用户拍板新建）。

和 `comic_api.py` **完全两套，互不调用**：那边是 Anima 的 tag 流水线（编剧 →
内容补正 → 逐格渲染 → qwen 加气泡），这边是 moss 的「一句话 → 编剧写整页 →
一次出图」。两边唯一共用的是 `image_jobs` 的出图队列。

为什么这边没有补正、没有逐格、没有后期贴字：

  · 补正治的是「漏 `1girl` / 犯 POV / 角色串味」这些 tag 硬规则。moss 的文本
    编码器是 Qwen3.5-9B，吃自然语言，没有这些规则——`moss_comic.check()` 只
    数格数、报字数，**只报不拦**。
  · 一页一次生成，格与格共享上下文，角色一致性天然成立；代价是改一格只能
    整页重抽（所以这里只有「整页换 seed」，没有逐格重渲）。
  · 文字（2026-11-08 起）：默认走 **Soda 程序排字**（`app/soda_letter.py`）——
    底图用 `build_page_prompt(with_text=False)` 生成（画面里声明无文字），
    工作流里 SaveImage 前插 `SodaLetterPage` 节点画气泡 + 竖排中文。台词
    不再让模型画，根治气泡位置随机 / 贴边乱码。`MOSS_SODA_LETTER=0` 退回
    老链路（模型画字）。

端点：
  POST /api/moss/write    一句话剧情 → 编剧写 spec.json（同步，几秒）
  POST /api/moss/save     手改后的 spec 落盘（重算每页提示词）
  POST /api/moss/render   后台出图（整页；可指定页号、可换 seed）
  GET  /api/moss/loras    当前 LoRA 槽位（`MOSSLoRAToggle` 的 1~7 槽）
  GET  /api/moss/status   轮询：spec + 体检 + 提示词 + 每页出图状态
  GET  /api/moss/img      出图（state.json 里记的相对 ComfyUI output 的路径）
  GET  /api/moss/list     历史任务

状态为什么不放内存：一张图 2~3 分钟，Flask 重启不能丢进度 → `state.json` 是
唯一事实来源，和 `comic_api` 一个道理。

⚠️ `image_jobs` 内部会 `raise SystemExit`（`_require_comfy` 等处），它不继承
Exception——线程里必须 `except BaseException`，否则失败是静默的，前端只会看到
永远转圈的「正在画」。
"""

import json
import logging
import os
import re
import threading
import time

from flask import Blueprint, jsonify, request, send_file

from app.features.comic import soda_letter
from app.features.image import lora_slots
from app.features.comic import moss_comic
from app.features.comic import moss_agent
from app.features.comic import moss_series
from app.features.comic import moss_twopass
from app.integrations import llm
from app.features.image import jobs as image_jobs
from app.config import BASE_DIR, COMFYUI_OUTPUT_DIR
from app.core.skills import load_workflow
from app.tools.normal.generate_image import apply_prompt_placeholders

bp = Blueprint("moss_api", __name__)
log = logging.getLogger(__name__)

# 任务根目录钉死绝对路径：Flask 的启动 cwd 可能不是项目根。
STATE_ROOT = os.path.join(BASE_DIR, "state", "moss_comic")


def _friend_qq():
    """当前请求的群友 QQ；站主（直连 5174）返回空串。

    `comic_api` 是靠 `before_request` 把身份挂到 `g` 上的，moss 是另一个
    blueprint、没有那段钩子，所以这里直接问一次 `friend_qq()`。它只认 5175
    转发时才带的那枚内部令牌，站主自己点页面天然是空串。
    """
    try:
        from app.features.comic import quota as comic_quota
        return comic_quota.friend_qq()
    except Exception:                      # noqa: BLE001 - 拿不到就按站主处理
        return ""

# 出图渠道与参数（2026-10-28 用户拍板：**完全照群友原版，不自创参数**）。
# 依据 `skills/_moss_comic/CHARTER.md` §5：参数**只认这里**——workflow.json 里
# 的 steps / 宽高会被下面三行覆盖，改参数改这里，别去改 workflow（改了也白改）。
#
# 群友原版实测值（`工作流MOSS-Image-SFT .json`）：KSampler 的 widgets 显示 8/1，
# 但 inputs 实际接的是 PrimitiveInt(23)=50 和 PrimitiveFloat(25)=3 —— 界面上
# 看到的是被覆盖前的旧值。所以群友正式档是 **50 步 / cfg 3**，
# 8 步是 turbo 档（他那份文件里 SaveImage 的 prefix 就叫 SFT_8step_toggle，
# 同时 MOSS-turbo-lora 节点是 bypass 状态）。
#
# 2026-10-28 三次定档：**25 步**（8 步 → 50 步 → 25 步，前两档都被实测推翻）。
#
# 第一版定 8 步，理由是「50 步单格 300 秒以上，等不起」——但那个 300 秒是
# **内存压力下的数字**（ram_free 只剩 2GB，每步都在把权重换页），不是步数成本。
# 干净环境实测（1024×1536 / maliker @0.4 / seed 20261028 / 同一份 Layout JSON）：
#
#     步数  重启后第一张   与 50 步像素差   >48 占比   文字
#      8      35.4 s          ——           ——       两支中文框全崩，读不通
#     20      66.1 s        11.64         4.26%      全对
#     25      74.1 s         7.22         2.03%      全对
#     30      86.1 s         6.19         1.98%      全对
#     50     ~102 s（热态）    0            0         全对
#
# 关键读法：25→30 多花 12 秒只把差值降 1.03（>48 占比仅降 0.05 个百分点），
# 肉眼看不出；而 20→25 之差是 9.56，那才是「跨台阶」（发丝从成块到条条分开、
# 裙摆花边从平到有褶皱层次）。所以取 25：74 秒一页，画质约等于 30 步，
# 比 50 步省约 28 秒（27%），比 20 步只多 8 秒但质感另一个档。
#
# 2026-10-28 晚用户拍板「以后 moss 就一个 25 步长的渠道」——8 步（`moss8`）、
# 50 步（`moss50`）、整页档（`moss_page`）全部进 `skills.ARCHIVED_SKILLS`，
# 目录保留只为让历史任务重画 / 旧图显示，不再是可点名的渠道。
#
# 2026-10-28 深夜改 25 → 30（挂上群友 moss-nai5 后作者建议 30-40 步）。
# 2026-10-29 用户改回 **25**（25 步一页约 74 秒，跟跑图台渠道模板一致）。
# 同日晚再改：漫画台仍走 `moss-qunyou`，而那套四 LoRA 组合已经搬到
# `moss-light`（moss光影），现在 `moss-qunyou` 只挂 nai5 一个——漫画台跟着
# 变成 nai5 单挂，要四件套得另指 `moss-light`。
SKILL = "moss-qunyou"
# 2026-10-31：moss 小漫画频道定档 **30 步 / cfg 3.5**（用户拍板）。
#
# 25 → 30 的依据是上面那张实测表：25→30 多花约 12 秒，与 50 步的像素差从
# 7.22 降到 6.19，肉眼看不出差别，但**一格文字的面积越小越吃步数**——2×2
# （每格 512 宽）在 25 步就够，3×2（每格 341 宽）留一点余量更稳。
#
# ⚠️ cfg 从 3.0 提到 3.5 **不是纯画质参数**。`skills/moss-qunyou/skill.md`
# 第 122 行记着：`<|pose_and_clothing_neg|>` 那段结构化负向**只在 cfg>1 时
# 起作用**。提 cfg 等于把整个负向池的拽力一起加大，构图可能被往「床上/脱衣」
# 方向带。跑几张如果发现人物姿态被拽跑，第一件事是把这里退回 3.0。
STEPS = 30
CFG = 3.5
# 默认画风（2026-11-22 用户按「回家」成图定档，原串照抄）：现有全彩锁下这句
# 会渲染成冷蓝夜色 + 暖灯、清晰墨线的安静生活向。agent 每本会自己写一句
# style，在 two_agent 出口写死覆盖它，保证跨本不漂；换画风只改这一处。
DEFAULT_STYLE = ("black and white manga page, crisp inked lineart, screentone dot shading, "
                 "high contrast evening light, quiet slice-of-life mood")
# 分辨率：群友原版 1024×1536。实测这个尺寸在 12GB 卡上能出图（8 步 72 秒），
# 降到 896×1344 反而在采样阶段出纯噪声，所以保持原版尺寸不动。
# 改这里必须同步 `moss_comic._CANVAS_W/_CANVAS_H`，否则 bbox 会整体偏。
WIDTH, HEIGHT = 1024, 1536
SEED_BASE = 20261501
OUT_ROOT = "web/moss漫画"
# 群友的图另起一棵树（2026-11-24 用户拍板）：**只分「站主 / 群友」两层**，
# 群友全堆在一起，不按 QQ 细分。站主仍是 OUT_ROOT，老任务原地不动——判据
# 是任务目录里有没有 owner.json，有就把根换成这里。
#
# 2026-11-24 晚第二次拍板：群友的东西一律收进 `工作台/`
# （「只要不是我这边的东西，都给我放到工作台的文件夹里面」），所以根从
# `群友moss漫画` 变成 `工作台/群友漫画台/moss`——和群友漫画台
# （`工作台/群友漫画台/<QQ>/`）同在一棵 `群友漫画台` 下，用户翻文件夹时
# 「群友的画」只有一个入口。老任务在 `群友moss漫画/` 原地不动，不搬。
OUT_ROOT_FRIEND = "工作台/群友漫画台/moss"
# 上一版给群友用的根（2026-11-24 白天定过 `群友moss漫画`）。只在
# 「同一个任务以前出过图」时拿来兜底读，免得刚跑完的那本看起来是空的。
OUT_ROOT_FRIEND_OLD = "群友moss漫画"
# 老任务出图所在的历史渠道目录（换渠道后旧图还得能显示；新的排前面）
LEGACY_SKILLS = ("moss-qunyou", "moss8", "moss50")

# 运行中的任务：{任务目录绝对路径: {"t0": 起跑时间, "n": 当前页}}
_RUNNING = {}
_RUN_LOCK = threading.Lock()
# 中止信号：{任务目录: threading.Event}。用户点「终止」就置位，
# 出图线程每页看一眼；正在跑的那张由 jobs 走定向 /interrupt。
_ABORT = {}
# state.json 的读写互斥：后台线程写、HTTP 线程读，同一个文件。
_STATE_LOCK = threading.Lock()
# 单页终止（2026-11-23 用户要的「单个格子也给我一个终止」）：
# {任务目录: set(页号)}。和 `_ABORT`（整批停）是**两回事**——这个只丢一页，
# 后面几页照画。出图线程每页开工前看一眼，正在跑的那一页由 `_draw` 把
# 同一个 Event 挂到 job 上，走定向 /interrupt。
_PAGE_STOP = {}


# ══════════════════════════════════════════════════════════
#  任务目录 / 落盘
# ══════════════════════════════════════════════════════════
def _slug(text, limit=14):
    """标题 → 能当目录名的短串。中文保留（Windows 下没问题），只清非法字符。"""
    s = re.sub(r"[\\/:*?\"<>|\s]+", "", text or "")
    return (s or "untitled")[:limit]


def _new_task_dir(spec, owner=None):
    """建任务目录 `<日期>_<时间>_<标题>`。同一分钟同名再加 `_2`，不静默复用。

    `owner`：群友 QQ（空串 = 站主）。给了就写一份 `owner.json`，这是
    「站主 / 群友」唯一的落盘判据——出图跑在后台线程里，那时候没有 request
    上下文，只能靠文件记（见 `out_root`）。

    为什么要传进来而不是在这里现查：建目录有三个入口（新开 / 两段式 /
    续集），只有调用方知道自己跑在请求线程里、能取到群友身份。
    """
    base = "%s_%s" % (time.strftime("%Y%m%d_%H%M"), _slug(spec.get("title")))
    d = os.path.join(STATE_ROOT, base)
    k = 2
    while os.path.isdir(d):
        d = os.path.join(STATE_ROOT, "%s_%d" % (base, k))
        k += 1
    os.makedirs(d, exist_ok=True)
    if str(owner or "").strip():
        try:
            from app.features.comic import quota as comic_quota
            comic_quota.set_owner(d, str(owner).strip())
        except Exception:                  # noqa: BLE001 - 归属写不进去也该能画
            log.warning("moss 任务 %s 写 owner 失败", os.path.basename(d),
                        exc_info=True)
    return d


def _task_dir(name):
    """任务名 → 绝对路径。只认 state/moss_comic 的**直接子目录**（挡路径穿越：
    `basename("..")` 还是 `..`，join 完就跑到上一级去了）。"""
    name = os.path.basename(str(name or "").strip())
    if not name or name in (".", ".."):
        raise ValueError("任务名不合法")
    d = os.path.realpath(os.path.join(STATE_ROOT, name))
    if os.path.dirname(d) != os.path.realpath(STATE_ROOT) or not os.path.isdir(d):
        raise ValueError("任务不存在：%s" % name)
    return d


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:                                   # noqa: BLE001
        return default


def _write_json(path, data):
    """先写 .tmp 再 os.replace——半截 JSON 会让轮询直接读到空任务。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _state_path(d):
    return os.path.join(d, "state.json")


def out_root(d):
    """这个任务目录的图该落在 ComfyUI `output/` 下的哪棵树上。

    - 站主：`web/moss漫画`
    - 群友：`工作台/群友漫画台/moss`（2026-11-24 晚起；白天那版是
      `群友moss漫画`，那个根仍在 `out_roots` 里兜底读，不搬）

    判据是任务目录里的 `owner.json`，跟漫画工作台共用同一个约定
    （`comic_quota.is_friend_task`）——**不看当前请求**：出图跑在后台线程里，
    没有 Flask 的 request 上下文，归属必须落盘才判得出来。

    老任务没有 owner.json，一律算站主，留在原来的 `web/moss漫画` 原地不动
    （2026-11-24 用户明确要求：旧的不动，新的才分）。
    """
    try:
        from app.features.comic import quota as comic_quota
        if comic_quota.is_friend_task(d):
            return OUT_ROOT_FRIEND
    except Exception:                      # noqa: BLE001 - 归属层异常不该拖垮出图
        pass
    return OUT_ROOT


def out_roots(d):
    """这个任务的图**可能**在哪几个根下（新根在前，老根在后兜底）。

    为什么要有这个：2026-11-24 一天之内群友的根搬过两次
    （`群友moss漫画` → `工作台/群友漫画台/moss`），已经出好的那几本还躺在
    老地方。按顺序挨个找，用户打开刚跑完的那本不会看到一片空白；**不搬文件**
    ——搬完 `/view` 就找不到它了，还得给每处取图逻辑补「去哪个目录找」。
    """
    roots = [out_root(d)]
    try:
        from app.features.comic import quota as comic_quota
        if comic_quota.is_friend_task(d) and OUT_ROOT_FRIEND_OLD not in roots:
            roots.append(OUT_ROOT_FRIEND_OLD)
    except Exception:                      # noqa: BLE001
        pass
    return roots


def _legacy_files(d):
    """老任务的出图（没有 state.json 的那些）。

    CLI 时代的图落在 `output/web/moss漫画/<任务名>/<渠道>/`，文件名带 `_P01_`。
    页面得能把它们显示出来，否则老任务打开全是空白。

    **渠道会换**（moss50 → moss-qunyou），所以按 `LEGACY_SKILLS` 逐个目录找，
    新渠道排在前面优先命中——同一个任务不可能同时有两个渠道的图，但按顺序
    找能保证换渠道后旧任务依然打得开。
    """
    if not COMFYUI_OUTPUT_DIR:
        return {}
    out = {}
    for skill in LEGACY_SKILLS:
        # 新根找过再找老根（见 out_roots）：搬过一次家的那几本也能打开
        folder = None
        for root in out_roots(d):
            probe = os.path.join(COMFYUI_OUTPUT_DIR, root.replace("/", os.sep),
                                 os.path.basename(d), skill)
            if os.path.isdir(probe):
                folder = probe
                break
        if not folder:
            continue
        for f in sorted(os.listdir(folder)):
            m = re.search(r"_P0*(\d+)_", f)
            if not m:
                continue
            if os.path.splitext(f)[1].lower() not in (".png", ".jpg", ".jpeg", ".webp"):
                continue
            n = int(m.group(1))
            if n not in out:                    # 先找到的优先，不覆盖
                out[n] = "/".join([root, os.path.basename(d), skill, f])
    return out


def _page_file(d, n, pages=None):
    """这一页的图（相对 ComfyUI output）。先认 state.json，再认老目录。"""
    pages = pages if pages is not None else (_load_state(d).get("pages") or {})
    rel = (pages.get(str(n)) or {}).get("file") or ""
    return rel or _legacy_files(d).get(int(n), "")


def _load_state(d):
    return _read_json(_state_path(d), {})


# ══════════════════════════════════════════════════════════
#  连载：任务目录 ↔ moss_series 的节点
# ══════════════════════════════════════════════════════════
#
# 2026-11-24 用户要「moss 漫画台增加续集，和漫画工作台一样」。
#
# **不另建一套线**：QQ 那边早就有 `moss_series` 那棵树（`moss续集` 用了
# 一个多月），漫画台再存一份只会变成两边各接各的、编号对不上。所以这里
# 只做一件事——把「任务目录」翻译成「树上的某一页」：
#
#   · 每个任务目录最多对应一个节点（`page.task`）；
#   · 老任务（漫画台这批，一个都没有系列）在第一次续集时补建，
#     历史页按页号顺序串成一条单链，只回填能拿到的事实；
#   · 续出来的新页**新开任务目录**（和 QQ 一致），旧的一本一个字不动。

_MOSS_WEB_OWNER = "web"


def _series_index():
    """`绝对任务路径 → (series_id, 页节点, series)`。一次扫全库，页面只要一次调用。

    只索引 `task` 非空的页：任务的绝对路径是它自己的主键，不靠标题猜。

    一本多页时**取这本里页号最大的那一页**：续集要接在它后面，不能接在
    这本的第一页后面（2026-11-24 实测：两页的本续集接在了树上第 3 页，
    但第 4 页才是这本真正的末页，接错分支）。
    """
    out = {}
    root = moss_series.STATE_ROOT
    if not os.path.isdir(root):
        return out
    for name in os.listdir(root):
        d = os.path.join(root, name)
        if not os.path.isdir(d):
            continue
        s = moss_series.load(name)
        if not s:
            continue
        for pg in s.get("pages") or []:
            t = str(pg.get("task") or "").strip()
            if not t:
                continue
            key = os.path.realpath(t)
            old = out.get(key)
            if old is None or int(pg.get("n") or 0) > int(old[1].get("n") or 0):
                out[key] = (s.get("id") or name, pg, s)
    return out


def _page_summary(spec):
    """一页 spec → 前情摘要（一行 50~80 字）。**只取 spec 里已有的事实**。

    补建时没有摘要就会让续集完全看不见前情，那样「续」出来的是另起一集。
    这里不调模型：`beat` / `link` / 每格 desc 本来就是这一页演了什么的
    书面记录，按顺序连起来就是一份够用的前情。
    """
    for page in spec.get("pages") or []:
        beats = [str(page.get("beat") or "").strip()]
        descs = [str((p or {}).get("desc") or "").strip()
                 for p in (page.get("panels") or [])]
        descs = [x for x in descs if x]
        text = "；".join(x for x in beats + descs if x)
        if text:
            return text[:160]
    return str(spec.get("title") or "").strip()[:80]


def _page_scene(spec):
    """一页 spec → 场景卡（画面锚）。就是这一页那串英文 scene。"""
    for page in spec.get("pages") or []:
        sc = str(page.get("scene") or "").strip()
        if sc:
            return sc
    return ""


def _series_for_task(d, spec=None):
    """任务目录 → `(sid, 页节点, series)`；没有就登记到树上再返回。

    **续集本**（`spec.line` 里有 `series` / `parent`，由 `/api/moss/continue`
    写进去）挂成**上一页的子节点**——这才是「续」；**老本**按页号顺序补建
    一条单链。补建规则刻意保守：只填 spec 里看得见的事实（summary / scene /
    seed / task），**不编造剧情**——拿不到摘要就让它缺着。

    返回的「页节点」是**最后一页**：续集接在它后面。
    """
    hit = _series_index().get(os.path.realpath(d))
    if hit:
        return hit
    spec = spec if spec is not None else _read_json(os.path.join(d, "spec.json"), {})
    if not spec:
        return "", None, None
    line = spec.get("line") if isinstance(spec.get("line"), dict) else {}
    sid = str(line.get("series") or "")
    prev_id = str(line.get("parent") or "")
    # 续集本的父页所属系列：父页不在就退回补建一条新的（不挂到错误的线上）
    if sid and prev_id:
        series = moss_series.load(sid)
        parent = moss_series.by_id(series or {}, prev_id)
        if not parent:
            sid, prev_id = "", ""
    else:
        sid, prev_id = "", ""
    if not sid:
        series = moss_series.open_new("private", _MOSS_WEB_OWNER,
                                      title=spec.get("title") or "",
                                      style=spec.get("style") or "",
                                      cast=spec.get("cast") or {})
        sid = series["id"]
    # 页号基准：**续集本的 spec 是从第 1 页起排的**（新的一本），但它接在
    # 树上的第 N 页后面，所以第一页应该是 N+1 而不是 1。老本没有 `line`，
    # 基准 0 → 第 1 页，就是它自己的页号。
    # 2026-11-24 实测踩到的坑：按 spec 的 n 直接挂，第 2 页的老本续一集
    # 会变成树上第 4 页（2+2），中间凭空少一页。
    # 基准取 `parent_n`：它记的就是**点续集那一刻这条线最高的页号**。
    # `added` 记下这次真正新建的节点 id：只有它们才能挪页号。`_series_index`
    # 是按 `page.task` 建索引的，一本多页时只记住最后一页，所以「这个任务
    # 已经挂过」和「这本里的第一页还没挂」要分开判断——否则第二次调用来
    # 会把已经对齐过的页再 +base 一遍（2026-11-24 实测：1,2,3,4 → 8,5）。
    base = int(line.get("parent_n") or 0) if line.get("series") else 0
    st = _load_state(d).get("pages") or {}
    # 「这本挂过没有」按任务查全树，不按 `_series_index`：那本索引一本多页时
    # 每个 task 只留一个节点，拿它判重会把第二页当成没挂过，再挂一遍
    # （2026-11-24 实测：1,2,3,4 变成 1,2,3,4,8,5）。
    def _already(x, _d=d):
        real = os.path.realpath(_d)
        return any(str(pg.get("task") or "") and
                   os.path.realpath(pg["task"]) == real
                   for pg in x.get("pages") or [])
    if moss_series.mutate(sid, _already):
        series = moss_series.load(sid) or {}
        pages = series.get("pages") or []
        log.info("moss 任务 %s 已经在系列 %s 里，不重复登记",
                 os.path.basename(d), sid)
        return (sid, pages[-1] if pages else None, series)
    added = []
    for page in spec.get("pages") or []:
        n = int(page.get("n") or 0)
        stp = st.get(str(n)) or {}
        new_id = moss_series.mutate(sid, lambda x, _p=prev_id, _s=stp, _pg=page: (
            moss_series.add_page(
                x, parent_id=_p, task=d, brief="", seed=_s.get("seed"),
                summary=_page_summary({"pages": [_pg]}),
                scene=_page_scene({"pages": [_pg]}),
                owner=_MOSS_WEB_OWNER) or {}).get("id") or "")
        if new_id:
            added.append(new_id)
            prev_id = new_id
    series = moss_series.load(sid) or {}
    # 整本对齐到基准上：续集本的 spec 从第 1 页起排，接到树上要变成 base+1。
    # `moss_series.fill` 明确不许改 `n`（页号是结构的一部分），所以这里直接在
    # 锁里改完再整份写回——只动**这次新建**的节点，别的分支不碰。
    if base and added:
        def _renumber(x, _ids=set(added), _base=base):
            for pg in x.get("pages") or []:
                if pg.get("id") in _ids and int(pg.get("n") or 0) <= _base:
                    pg["n"] = _base + int(pg.get("n") or 0)
            return None
        moss_series.mutate(sid, _renumber)
        series = moss_series.load(sid) or series
    pages = series.get("pages") or []
    log.info("moss 任务 %s 登记系列 %s（共 %d 页）",
             os.path.basename(d), sid, len(pages))
    return (sid, pages[-1] if pages else None, series)


def _rel_exists(rel):
    """state 里记的相对路径是否真的还在磁盘上。

    只认「路径 + 文件存在」两件事：图被手动清掉 / output 被清空后，state
    还写着 done，这时候既有 url 也打不开，又要按没画过处理重新排。
    """
    if not rel:
        return False
    full = rel if os.path.isabs(rel) else os.path.join(
        COMFYUI_OUTPUT_DIR or "", rel.replace("/", os.sep))
    return bool(full) and os.path.isfile(full)


def _set_page(d, n, **kw):
    with _STATE_LOCK:
        st = _read_json(_state_path(d), {})
        pages = st.setdefault("pages", {})
        pages.setdefault(str(n), {}).update(kw)
        st["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _write_json(_state_path(d), st)


def _normalize(spec):
    """把前端送来的 spec 收拾成能拼提示词的样子。

    **不做准入判断**——只补默认值、去空白、重编号。格数对不对由
    `moss_comic.check()` 出报告，页面自己显示，不在这里拒绝。
    """
    spec = spec if isinstance(spec, dict) else {}
    cast = spec.get("cast") or {}
    spec["cast"] = {str(k).strip(): str(v).strip()
                    for k, v in cast.items() if str(k).strip()}
    spec["style"] = str(spec.get("style") or "").strip()
    spec["title"] = str(spec.get("title") or "").strip() or "未命名"
    pages = []
    for i, page in enumerate(spec.get("pages") or [], 1):
        if not isinstance(page, dict):
            continue
        page["n"] = i
        page["layout"] = str(page.get("layout") or "single").strip()
        page["scene"] = str(page.get("scene") or "").strip()
        panels = []
        for panel in page.get("panels") or []:
            if not isinstance(panel, dict):
                continue
            clean = {k: str(panel.get(k) or "").strip()
                     for k in ("desc", "bubble", "sfx", "caption")}
            # `bubbles` 是数组（2026-11-01 加），不能走上面那行的字符串清洗，
            # 漏掉它整页台词会在存盘这一步被静默丢掉。
            got = panel.get("bubbles")
            if isinstance(got, list):
                clean["bubbles"] = [str(x or "").strip() for x in got
                                    if str(x or "").strip()]
            panels.append(clean)
        page["panels"] = panels
        pages.append(page)
    spec["pages"] = pages
    return spec


def _prompts(spec):
    """{页号字符串: 最终提示词}。

    2026-10-28 起是 §15 官方 Layout JSON 字符串（不是散文），见
    `skills/_moss_comic/CHARTER.md` §4。
    """
    out = {}
    for page in spec.get("pages") or []:
        out[str(page.get("n"))] = moss_comic.build_page_prompt(spec, page)
    return out


def _pretty(p):
    """提示词落盘用：是 JSON 就缩进展开，不是就原样返回。

    prompts.txt 是给人排查用的，一行几 KB 的 JSON 挤在一起没人看得下去。
    """
    try:
        return json.dumps(json.loads(p), ensure_ascii=False, indent=1)
    except Exception:                           # noqa: BLE001
        return p


def _save(d, spec, raw=None):
    """spec.json / prompts.txt 落盘（spec_raw.txt 只在初次编剧时写）。"""
    spec = _normalize(spec)
    with _STATE_LOCK:
        _write_json(os.path.join(d, "spec.json"), spec)
        if raw is not None:
            with open(os.path.join(d, "spec_raw.txt"), "w", encoding="utf-8") as fh:
                fh.write(raw)
    ps = _prompts(spec)
    with open(os.path.join(d, "prompts.txt"), "w", encoding="utf-8") as fh:
        for page in spec.get("pages") or []:
            n = page.get("n")
            fh.write("── 第 %s 页 [%s] %d 格\n%s\n\n"
                     % (n, page.get("layout"), len(page.get("panels") or []),
                        _pretty(ps.get(str(n), ""))))
    return spec, ps


# ══════════════════════════════════════════════════════════
#  组装返回体
# ══════════════════════════════════════════════════════════
def _running(d):
    with _RUN_LOCK:
        r = _RUNNING.get(d)
        if not r:
            return None
        return {"page": r.get("n"),
                "waited": round(time.time() - r.get("t0", time.time()), 1)}


def _payload(d, spec=None, prompts=None):
    if spec is None:
        spec = _read_json(os.path.join(d, "spec.json"), {})
    if prompts is None:
        prompts = _prompts(spec)
    st = (_load_state(d).get("pages") or {})
    legacy = _legacy_files(d)
    task = os.path.basename(d)
    pages = []
    for page in spec.get("pages") or []:
        n = page.get("n")
        s = st.get(str(n)) or {}
        rel = s.get("file") or legacy.get(int(n)) or ""
        status = s.get("status") or ("done" if rel else "pending")
        # 「有图就是已出图」（2026-11-24 用户：「第一页已经画过的显示准备中，
        # 这些标识必须解决」）。单页终止 / 整批终止会把**已经有图的页**也写成
        # canceled，那样页面上就出现「已终止」的徽标配一张完好的图，或者刷新后
        # 当作没画过又排一次。图还在磁盘上就是画过的，状态以图为准。
        if rel and _rel_exists(rel):
            status = "done"
        item = dict(page)
        item.update({
            "prompt": prompts.get(str(n), ""),
            "status": status,
            "seed": s.get("seed"),
            "secs": s.get("secs"),
            "error": s.get("error") or "",
            # 带 v= 是为了换 seed 后浏览器不复用旧图缓存
            "url": ("/api/moss/img?task=%s&n=%s&v=%s"
                    % (task, n, s.get("rev") or s.get("seed") or 0))
                   if rel else "",
        })
        pages.append(item)
    # 双段链路的页自带 prompt_override；旧链路的体检 moss_comic.check() 是拿
    # 「散文分镜规则」去查一份 Layout JSON，只会吐假错（没有 plan / 景别单一 / 缺旁白）。
    # 2026-11-21：双段模式直接跳过，不给页面添噪音。
    two_pass = any(p.get("prompt_override") for p in (spec.get("pages") or []))
    # 这一本在连载里是哪一页（2026-11-24）。只读树上**已经有**的节点，不补建：
    # 打开一本老任务不该顺手写一个系列档案出来（列表里每一本都会做这次调用）。
    # 续集本刚编剧完还没出图时树上还没有它，就用 `spec.line` 里记的位置
    # （上一页第几页 + 自己是第几页），页面照样能显示「第 02 页」。
    line = None
    try:
        hit = _series_index().get(os.path.realpath(d))
        if hit:
            sid, node, series = hit
            line = {"id": sid,
                    "page": node.get("id"),
                    "n": node.get("n"),
                    "pages": len(series.get("pages") or []),
                    "title": series.get("title") or ""}
        else:
            raw = spec.get("line") if isinstance(spec.get("line"), dict) else {}
            if raw.get("series"):
                line = {"id": raw["series"],
                        "page": "",
                        "n": int(raw.get("parent_n") or 0) + 1,
                        "pages": 0,
                        "title": "",
                        "pending": True}
    except Exception:                           # noqa: BLE001 - 定位失败不该挡住看图
        log.exception("moss 任务 %s 定位系列失败", task)
    return {
        "ok": True,
        "task": task,
        "title": spec.get("title"),
        "style": spec.get("style"),
        "cast": spec.get("cast") or {},
        "ratio": "%dx%d" % (WIDTH, HEIGHT),
        "steps": int(spec.get("steps") or STEPS),
        "cfg": CFG,
        "running": _running(d),
        "issues": [] if two_pass else moss_comic.check(spec),
        "pages": pages,
        "line": line,
    }


# ══════════════════════════════════════════════════════════
#  编剧
# ══════════════════════════════════════════════════════════
@bp.route("/api/moss/write", methods=["POST"])
def api_moss_write():
    """一句话剧情 → 整篇 spec。同步跑：实测 deepseek-flash 5 秒，够快。

    返回的 `task` 是新建的任务目录名，后面 save / render / status 都靠它。
    """
    data = request.get_json(silent=True) or {}
    brief = str(data.get("brief") or "").strip()
    if not brief:
        return jsonify({"ok": False, "error": "先写一句剧情"}), 400
    try:
        pages = max(1, min(12, int(data.get("pages") or 1)))
    except (TypeError, ValueError):
        pages = 1
    # 输入可能是「男主 / 女主 / 剧情」三栏拼的（2026-11-22）。四格这条链没有
    # 「男主台词」这个概念，但至少要把「男主：无」变成一句人话，不能把标签
    # 当剧情文字喂进去。旧的一句话输入原样透传，一个字都不动。
    raw_brief = brief
    brief = moss_agent.compose_brief(moss_agent.parse_brief(brief), fallback=brief)
    spec, raw, err = moss_comic.write(
        brief, pages=pages,
        style=str(data.get("style") or "").strip(),
        ratio="%dx%d" % (WIDTH, HEIGHT),
        layout=str(data.get("layout") or "").strip(),
        provider=str(data.get("provider") or "").strip() or None,
        model=str(data.get("model") or "").strip() or None)
    brief = raw_brief
    if spec is None:
        # 502 = 模型侧没交作业（解析失败），不是用户的错，页面提示重来一次
        return jsonify({"ok": False,
                        "error": "编剧没写出能解析的剧本：%s" % err,
                        "raw": (raw or "")[:1500]}), 502
    d = _new_task_dir(spec, _friend_qq())
    spec, ps = _save(d, spec, raw)
    out = _payload(d, spec, ps)
    out["brief"] = brief
    return jsonify(out)


def _twopass_call(system, user, max_tokens=8000, temperature=None, tag="",
                  provider=None, model=None):
    """把台子的 LLM 链路包成 moss_twopass 要的 call(system, user, …)。

    `tag` 只用于日志对账，不参与请求。
    `provider` / `model` 是页面选的模型，透传给 llm：选中项当链头、失败自动降级；
    都不传时走全局默认链，和加下拉之前的行为一致。
    """
    text = llm.call_llm(
        [{"role": "system", "content": system},
         {"role": "user", "content": user}],
        max_tokens=max_tokens,
        temperature=temperature,
        timeout=300,
        provider=provider,
        model=model,
    )
    return text or ""


@bp.route("/api/moss/two_agent", methods=["POST"])
def api_moss_two_agent():
    """整本一条线：写 → 代码体检 → 定向重修 → 再体检 → 每页 autofix。

    2026-11-21 合并：原来是「编剧 AI 拆幕 + 每页编译 AI」共 1+N 次请求，
    7000 字编译契约每页重发一遍，3 页约 2.3 万 token 输入。现在一个 AI 直接
    产出整本 Layout JSON，中间那份 Markdown 稿不再存在。

    自查是真检查：`moss_agent.audit()` 只比事实（格子对不对得上骨架、文字有没有
    落进格子、外形串有没有原样粘上、尾句跟本页有几种人的话是否一致、台词有没
    重复），不通过就把清单交回模型定向重修，最多 2 轮；仍不过就 autofix 兜底，
    不阻断出图。

    旧两段链路保留：请求体 `{"legacy": true}` 或环境变量 `MOSS_TWOPASS_LEGACY=1`
    可切回去对比。产物结构完全一致（每页带 `prompt_override`），前端不用改。
    """
    data = request.get_json(silent=True) or {}
    brief = str(data.get("brief") or "").strip()
    if not brief:
        return jsonify({"ok": False, "error": "先写一句剧情"}), 400
    try:
        pages = max(1, min(12, int(data.get("pages") or 3)))
    except (TypeError, ValueError):
        pages = 3
    provider = str(data.get("provider") or "").strip() or None
    model = str(data.get("model") or "").strip() or None
    legacy = bool(data.get("legacy")) or os.environ.get("MOSS_TWOPASS_LEGACY") == "1"
    # 群像模式（2026-11-22）：多角色同屏合照，走 moss_agent 的群像覆写段。
    # legacy 双段链路是写死单女主 POV 的，群像必须走 agent 链路。
    group = bool(data.get("group"))
    if group:
        legacy = False

    # 把页面选的模型绑进 call：链路内部多次调用（成稿 / 重修）都走它。
    def bound_call(system, user, max_tokens=8000, temperature=None, tag=""):
        return _twopass_call(system, user, max_tokens=max_tokens,
                             temperature=temperature, tag=tag,
                             provider=provider, model=model)

    engine = "twopass" if legacy else "agent"
    try:
        if legacy:
            # 双段链路自己不会拆三段式，这里先归一成一段人话再给它。
            # agent 链路在 generate 内部自己拆（还要把「有没有男主」传给体检）。
            spec, report = moss_twopass.generate(
                moss_agent.compose_brief(moss_agent.parse_brief(brief), fallback=brief),
                pages, bound_call)
        else:
            spec, report = moss_agent.generate(brief, pages, bound_call, group=group)
    except BaseException as exc:                # noqa: BLE001 - 模型侧异常一律转 502
        return jsonify({"ok": False,
                        "error": "MOSS 链路失败：%s" % (str(exc) or exc.__class__.__name__)}), 502

    # 画风由代码定档，不用模型每本自填的 scenes[1]：那句随剧情变化，跨本会漂。
    # 写在出口而不是 generate 内部，用户手改 spec 前的第一份事实就统一。
    spec["style"] = DEFAULT_STYLE

    d = _new_task_dir(spec, _friend_qq())
    spec, ps = _save(d, spec, None)
    if report.get("script_md"):
        with open(os.path.join(d, "script.md"), "w", encoding="utf-8") as fh:
            fh.write(report.get("script_md") or "")
    rep = {k: v for k, v in report.items() if k != "script_md"}
    with open(os.path.join(d, "twopass_report.json"), "w", encoding="utf-8") as fh:
        json.dump(rep, fh, ensure_ascii=False, indent=1)
    out = _payload(d, spec, ps)
    out["brief"] = brief
    out["two_agent"] = {"engine": engine, "group": group,
                        "secs": report.get("secs"),
                        "pass": report.get("pass"),
                        "rounds": report.get("rounds") or [],
                        "pages": report.get("pages") or [],
                        "notes": report.get("script_issues") or []}
    return jsonify(out)


@bp.route("/api/moss/continue", methods=["POST"])
def api_moss_continue():
    """从某一本往下接一集（2026-11-24 用户要的「续集」）。

    页面只传 `task`（从哪一本接）+ `brief`（接下来发生什么）。其余全部由代码
    从树上取：前情摘要、上一页场景卡、人物外形。**和 QQ 的 `moss续集` 是
    同一套东西**（`moss_series` 那棵树），所以这里续出来的页在群里引用一下
    就能接着画，编号也是同一个。

    三种来源统一成一个「父页」：
      · 这本本来就是树上某一页的图（QQ 画过 / 网页续过）→ 就是那一页；
      · 老任务（漫画台改版前画的，没有系列）→ 先补建单链，取最后一页；
      · 都不是 → 报错，不猜。
    """
    data = request.get_json(silent=True) or {}
    try:
        d = _task_dir(data.get("task"))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    brief = str(data.get("brief") or "").strip()
    if not brief:
        return jsonify({"ok": False,
                        "error": "续集要写接下来的剧情，比如「她发现伞被人拿走了」"}), 400
    try:
        pages = max(1, min(12, int(data.get("pages") or 1)))
    except (TypeError, ValueError):
        pages = 1
    spec_old = _read_json(os.path.join(d, "spec.json"), {})
    if not spec_old:
        return jsonify({"ok": False, "error": "这一本的剧本不见了"}), 404

    sid, page, series = _series_for_task(d, spec_old)
    if not sid or not page:
        return jsonify({"ok": False, "error": "这一本接不上（系列档案没建起来）"}), 400

    # 同一条线同时只画一张：第二张接在同一页上会炸出几乎一样的分支
    # （QQ 那边 `_take` 管的是同一件事，那边是进程内占位，这边直接看队列）。
    # 新的一集从**这条线最高的页号**往后接：`page` 是分支父页，它不一定是最末一页。
    # 2026-11-24 实测：从「第一集」续出第 3、4 页后再接，父页还是第 3 页，
    # 整本挂树上会撞成两个第 4 页。
    head = 0
    for p in series.get("pages") or []:
        if p.get("dead"):
            continue
        head = max(head, int(p.get("n") or 0))
    nxt = head + 1
    busy = _line_busy(sid)
    if busy:
        return jsonify({"ok": False, "busy": True,
                        "error": "第 %02d 页还在画，等它出来再续" % busy,
                        "page": busy}), 409

    prev = moss_series.prev_lines(series, page.get("id") or "")
    prev_scene = moss_series.prev_scene(series, page.get("id") or "")
    cast = series.get("cast") or {}
    # 人物外形从树上取；树上没有（补建的老任务）就用原剧本里的那套
    cast = cast or (spec_old.get("cast") or {})

    # 剧情输入可能是「男主/女主/剧情」三段拼的（页面原样传），和 /write 一个口径
    plain = moss_agent.compose_brief(moss_agent.parse_brief(brief), fallback=brief)
    ctx = plain
    if prev or prev_scene:
        bricks = ["**这一集要接着上面的剧情往下写，不是另起一集。**", ""]
        if prev:
            bricks += ["**这条线前面已经画过的**：", prev, ""]
        if prev_scene:
            bricks += ["**上一页的画面**（这一集默认就在同一个地方、同一批东西里发生）：",
                       prev_scene, ""]
        bricks += ["这一集要写的事情：", plain]
        ctx = "\n".join(bricks)

    provider = str(data.get("provider") or "").strip() or None
    model = str(data.get("model") or "").strip() or None

    def bound_call(system, user, max_tokens=8000, temperature=None, tag=""):
        return _twopass_call(system, user, max_tokens=max_tokens,
                             temperature=temperature, tag=tag,
                             provider=provider, model=model)

    group = bool(data.get("group"))
    try:
        spec, report = moss_agent.generate(ctx, pages, bound_call, group=group)
    except BaseException as exc:                # noqa: BLE001 - 模型侧异常一律转 502
        return jsonify({"ok": False,
                        "error": "MOSS 链路失败：%s" % (str(exc) or exc.__class__.__name__)}), 502

    spec["style"] = DEFAULT_STYLE
    # 角色设定必须和上一页逐字一致，否则同一张脸下一集就变了样。
    if cast:
        spec["cast"] = dict(cast)
    # 标题没写就沿用这条线的标题，列表里才分得清是哪条线
    if not str(spec.get("title") or "").strip():
        spec["title"] = series.get("title") or "续集"
    # 这一本属于哪条线、接在哪一页后面。出图线程跑完按它把本子挂到树上
    # （`_series_for_task` 认这两个字段），刷新页面时也靠它显示「第 N 页」。
    # `parent_n` 记的是**这条线最高的页号**（`head`）：多页续集挂树时整本
    # 对齐到它后面，不能按本内页号 1、2 挂（会撞进已占的号里）。
    spec["line"] = {"series": sid, "parent": page.get("id") or "",
                    "parent_n": head, "from_n": page.get("n") or 0}

    nd = _new_task_dir(spec, _friend_qq())
    spec, ps = _save(nd, spec, None)
    rep = {k: v for k, v in report.items() if k != "script_md"}
    with open(os.path.join(nd, "twopass_report.json"), "w", encoding="utf-8") as fh:
        json.dump(rep, fh, ensure_ascii=False, indent=1)

    out = _payload(nd, spec, ps)
    out["brief"] = brief
    out["line"] = {"id": sid, "title": series.get("title") or "",
                   "parent": page.get("n"), "next": nxt,
                   "pages": len(series.get("pages") or []), "here": False}
    out["two_agent"] = {"engine": "agent", "group": group,
                        "secs": report.get("secs"),
                        "pass": report.get("pass"),
                        "rounds": report.get("rounds") or [],
                        "pages": report.get("pages") or [],
                        "notes": report.get("script_issues") or []}
    return jsonify(out)


def _line_busy(sid):
    """这条线现在是不是还有一页在画。在画就返回那一页的页号，否则 0。

    只看**能对上这条线的任务**：`_RUNNING` 的键是任务目录，返回体里带
    `n`。树上的页都能翻译成任务目录，所以逐个任务对一遍即可，不新增状态。
    """
    series = moss_series.load(sid) or {}
    dirs = {os.path.realpath(str(p.get("task") or "")): (p.get("n") or 0)
            for p in series.get("pages") or [] if p.get("task")}
    with _RUN_LOCK:
        running = list(_RUNNING.items())
    for d, r in running:
        n = dirs.get(os.path.realpath(d))
        if n:
            return int(n)
    return 0


@bp.route("/api/moss/save", methods=["POST"])
def api_moss_save():
    """手改后的 spec 落盘。改完提示词会重算，页面拿新的 prompt 预览。"""
    data = request.get_json(silent=True) or {}
    try:
        d = _task_dir(data.get("task"))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    spec = data.get("spec")
    if not isinstance(spec, dict):
        return jsonify({"ok": False, "error": "spec 不是对象"}), 400
    spec, ps = _save(d, spec)
    return jsonify(_payload(d, spec, ps))


# ══════════════════════════════════════════════════════════
#  出图
# ══════════════════════════════════════════════════════════
def _seed_for(d, n, given):
    """定这一页的种子：给了就用；否则沿用上次的（重画同一张）；再否则默认。"""
    if given:
        return int(given)
    st = (_load_state(d).get("pages") or {}).get(str(n)) or {}
    return int(st.get("seed") or (SEED_BASE + n - 1))


def _node_id(wf, cls, field):
    """按 `class_type` 找节点 id。找不到直接抛，**不许退回写死编号**。

    上一版把节点号写死成 `wf["30"]` / `wf["40"]` / `wf["60"]`，那是 `moss50`
    的结构。换渠道（`moss-qunyou` 是 60/70/90）之后同一串数字指向完全不同的
    节点：宽度写进 latent 碰巧还对，步数写进编码器、文件名写进 KSampler 就
    会静默产出坏图。宁可抛错让人看见，也不要画一张看不出问题的错图。
    """
    for nid, node in wf.items():
        if node.get("class_type") == cls and field in (node.get("inputs") or {}):
            return nid
    raise RuntimeError("工作流里找不到 %s.%s（渠道结构变了？）" % (cls, field))


def _draw(page, prompt, base_wf, seed, out_dir, title, letter_info="",
          steps=None, abort=None):
    """一页 → 一张图。返回 (相对 ComfyUI output 的路径 or None, 错误串)。

    `letter_info` 非空 = Soda 程序排字模式（`soda_letter.page_info` 的产物）：
    在 SaveImage 前插 `SodaLetterPage`，存盘的就是排好字的整页。

    `steps` 空＝用默认 30 步；用户可以在页面上改（2026-11-21）。
    `abort` 是 threading.Event：置位后 job.wait() 抛 Cancelled，
    并走定向 /interrupt + /queue delete，不留僵尸任务。
    """
    wf = json.loads(json.dumps(base_wf))
    lat = _node_id(wf, "EmptyFlux2LatentImage", "width")
    wf[lat]["inputs"]["width"] = WIDTH
    wf[lat]["inputs"]["height"] = HEIGHT
    try:
        use_steps = int(steps) if steps else STEPS
    except (TypeError, ValueError):
        use_steps = STEPS
    use_steps = min(max(use_steps, 1), 150)
    wf[_node_id(wf, "KSampler", "steps")]["inputs"]["steps"] = use_steps
    # cfg 同样只认这里的常量，不吃模板里的 3.0（`_node_id` 找不到就抛，
    # 不退回写死编号）。
    wf[_node_id(wf, "KSampler", "cfg")]["inputs"]["cfg"] = CFG
    n = page.get("n")
    # 落到独立子目录（2026-10-31 用户要求）：moss 的图单独一块地方，
    # 一眼能认出来，免得跟生图混在 output 根目录里被误删。
    # `filename_prefix` 带 `/` 时 ComfyUI 会自动建子目录；`output_images`
    # 返回的相对路径本来就支持子目录（现在就在处理 `qq/群_1/anima/...`），
    # 所以 `/view` 那条路不用改。
    wf[_node_id(wf, "SaveImage", "filename_prefix")]["inputs"]["filename_prefix"] = (
        "qq-moss漫画/MossComic_%s_P%02d" % (_slug(title), n))
    if letter_info:
        wf = soda_letter.rewire(wf, letter_info)
    # nai5 触发词跟着模板的 8 号槽走（2026-10-28）：模板开着就拼，用户「存为
    # 默认」关掉后这里自动就不拼了，两处永不脱节。
    prompt = lora_slots.nai5_prefix(SKILL) + prompt
    wf = apply_prompt_placeholders(json.dumps(wf, ensure_ascii=False), prompt, seed)

    # 账本记的提示词：JSON 前 200 字符全是结构骨架（`{"summary": {"canvas"...`），
    # 记进 image_log 等于没记。能解析就记 summary.caption——那句是人看得懂的
    # 画风+场景，以后查「这张图当时怎么描述的」才有用。
    ledger = prompt[:200]
    try:
        ledger = (json.loads(prompt).get("summary") or {}).get("caption") or ledger
        ledger = ledger[:200]
    except Exception:                           # noqa: BLE001  散文/坏 JSON 都走这里
        pass

    # tag 用「任务页号」而不是随机编号（2026-11-23）：页面上要单页终止、
    # 要显示这一页到底在排队还是在画，得有一个能对上号的字段。
    job, why = image_jobs.enqueue(None, None, wf, skill=SKILL, prompt=ledger,
                                  seed=seed, out_dir=out_dir)
    if job is None:
        return None, why or "队列没接"
    # 让 `cancel_tag` 认得这一页（2026-11-23 单页终止）。用「任务名#页号」：
    # 同一个任务重抽时页号不变，两条任务不会撞。
    try:
        job.tag = "%s#%s" % (os.path.basename(out_dir), n)
    except Exception:                           # noqa: BLE001
        pass
    # 中止信号转交：置位后 wait() 抛 Cancelled → process() 走定向 /interrupt
    if abort is not None:
        job.cancel = abort
    try:
        job.wait()
    except BaseException as exc:                # noqa: BLE001  见文件头 ⚠️
        # 用户点了终止：`job.wait()` 只是放弃等待，ComfyUI 里那张还在跑，
        # 白占显存、跑完还会把图写进 output。定向掐掉这一张
        # （只带这个 prompt_id，不会误伤 web/qq_bot 同机的别的任务）。
        if abort is not None and abort.is_set() and job.prompt_id:
            try:
                image_jobs._abort(job.prompt_id)
            except BaseException:               # noqa: BLE001  清不掉不算错
                pass
        return None, str(exc) or exc.__class__.__name__
    files = image_jobs.output_images(job.entry, job=job)
    if not files:
        return None, "跑完了但没找到图"
    # Windows 上 output_images 拼出来是反斜杠，统一成 `/` 存盘，
    # 免得 state.json 里出现 `web\moss漫画\xxx/moss50/` 这种混合分隔符
    return files[0].replace("\\", "/"), ""


def _run(d, spec, nums, seed_base, loras=None, steps=None, abort=None):
    """后台线程：逐页出图，每页状态落 state.json（页面靠它轮询）。

    `loras` 是这一批的 LoRA 槽位（见 `lora_slots`）：只改**这批的**工作流副本，
    模板不动。空 = 照模板现有配置。
    `steps` 空＝默认 30 步。
    `abort` 是 threading.Event：用户在页面上点了终止就置位，
    正在跑的那一页走定向 /interrupt，后面没开始的页直接标成 canceled。
    """
    title = spec.get("title") or "未命名"
    try:
        base_wf = load_workflow(os.path.join(BASE_DIR, "skills", SKILL, "workflow.json"))
        if not base_wf:
            raise RuntimeError("skills/%s/workflow.json 读不出来" % SKILL)
        if loras:
            lora_slots.apply_slots(base_wf, loras)
        prompts = _prompts(spec)
        # 排字模式：提交的底图提示词去掉文字条目（文字归 SodaLetterPage 画）。
        # prompts.txt 里仍保留 with_text=True 的版本——那是一份「字放哪」的
        # 布局图，排查排版时有用。
        lettering = soda_letter.enabled()
        by_n = {p.get("n"): p for p in spec.get("pages") or []}
        # 群友的根现在是 `工作台/群友漫画台/moss`（2026-11-24 晚挪进工作台），
        # 拼上任务名就已经四段；`comfy_out.body_for` 还要再加一层渠道。
        # 走 `_join`（不是字符串 %）才有那道层级上限兜着。
        from app.features.image import comfy_out as _co
        out_dir = _co._join(out_root(d), os.path.basename(d))
        for i, n in enumerate(nums):
            page = by_n.get(n)
            if not page:
                continue
            if abort is not None and abort.is_set():
                _set_page(d, n, status="canceled", error="用户终止")
                continue
            # 单页终止（2026-11-23）：这一页被点名了就直接跳过，**后面的页照画**
            # ——这正是「单个格子单独终止」和「整批中断」的区别。
            if _page_stopped(d, n):
                _set_page(d, n, status="canceled", error="用户终止了这一页")
                continue
            with _RUN_LOCK:
                if d in _RUNNING:
                    _RUNNING[d]["n"] = n
            seed = _seed_for(d, n, seed_base)
            _set_page(d, n, status="running", error="", rev=int(time.time()))
            t0 = time.time()
            try:
                info = ""
                draw_prompt = prompts.get(str(n), "")
                if lettering:
                    info = soda_letter.page_info(spec, page)
                    draw_prompt = moss_comic.build_page_prompt(spec, page,
                                                               with_text=False)
                rel, err = _draw(page, draw_prompt, base_wf, seed,
                                 out_dir, title, letter_info=info,
                                 steps=steps, abort=abort)
            except BaseException as exc:        # noqa: BLE001
                rel, err = None, str(exc) or exc.__class__.__name__
            if rel:
                _set_page(d, n, status="done", file=rel, seed=seed,
                          secs=round(time.time() - t0, 1), error="",
                          rev=int(time.time()))
            elif abort is not None and abort.is_set():
                # 当前这页被打断 + 后面的页都不画了，一起标 canceled
                _set_page(d, n, status="canceled", error="用户终止",
                          seed=seed, secs=round(time.time() - t0, 1),
                          rev=int(time.time()))
                for m in nums[i + 1:]:
                    _set_page(d, m, status="canceled", error="用户终止")
                break
            elif _page_stopped(d, n):
                # 只是这一页被终止：标掉它，**接着画下一页**（不 break）。
                # 和上面整批中断那条严格区分——混在一起就成了「停一格全停」。
                _set_page(d, n, status="canceled", error="用户终止了这一页",
                          seed=seed, secs=round(time.time() - t0, 1),
                          rev=int(time.time()))
            else:
                _set_page(d, n, status="error", error=err or "没出图", seed=seed,
                          secs=round(time.time() - t0, 1), rev=int(time.time()))
    except BaseException as exc:                # noqa: BLE001
        for n in nums:
            st = (_load_state(d).get("pages") or {}).get(str(n)) or {}
            if st.get("status") != "done":
                _set_page(d, n, status="error",
                          error=str(exc) or exc.__class__.__name__)
    finally:
        with _RUN_LOCK:
            _RUNNING.pop(d, None)
            _ABORT.pop(d, None)
            _PAGE_STOP.pop(d, None)
        # 出一批就把这本挂到连载树上（2026-11-24）。放在 finally 而不是
        # 循环里：中途被打断也是「这一本画过了」，下次点续集要用它的前情，
        # 而且已经出来的那几页在群里引用得到。
        # 新本（续集）→ 挂成上一页的子节点；老本 → 补建单链。
        _publish_line(d, spec)


def _publish_line(d, spec):
    """确保任务目录在树上有个位置。失败只记日志——出图已经成功了。"""
    try:
        _series_for_task(d, spec)
    except Exception:                           # noqa: BLE001
        log.exception("moss 任务 %s 挂连载树失败", os.path.basename(d))


def _page_stopped(d, n):
    """这一页是不是被用户点名终止了（2026-11-23 单页「终止」按钮）。"""
    with _RUN_LOCK:
        return n in (_PAGE_STOP.get(d) or ())


@bp.route("/api/moss/abort_page", methods=["POST"])
def api_moss_abort_page():
    """**只终止这一页**（2026-11-23 用户：「单个格子给我单独加一个终止可以吗」）。

    和 `/api/moss/abort`（整批停）是两回事：
      · 排队中的这一页 → 从 `image_jobs` 队列里摘掉；
      · 正在画的这一页 → 置位它自己的 Event / stopped，worker 走定向
        /interrupt 并等它退场，**图不会被投递**；
      · 后面还没开始的页 → **照画**（这就是「单格」的意义）。

    页号在 state.json 里落成 canceled，所以刷新页面看到的也是「终止了」，
    而不是又回到 pending 让人以为还要跑。
    """
    data = request.get_json(silent=True) or {}
    try:
        d = _task_dir(data.get("task"))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    try:
        n = int(data.get("page"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "没指定哪一页"}), 400
    tag = "%s#%s" % (os.path.basename(d), n)
    hit = False
    try:
        hit = image_jobs.cancel_tag(tag, "用户终止了这一页")
    except Exception:                           # noqa: BLE001
        log.warning("moss 第 %s 页：掐 image_jobs 里那张失败", n, exc_info=True)
    with _RUN_LOCK:
        _PAGE_STOP.setdefault(d, set()).add(n)
    # 已经有图的页：这一页本来就不用「停」，图也不该被标成终止（2026-11-24）。
    # 队列里掐掉是必要的（可能残留上一批的同页号条目），但状态保持 done，
    # 不覆盖 file / rev——否则页面上会出现「已终止」的徽标配一张好图。
    has_img = _rel_exists(((_load_state(d).get("pages") or {}).get(str(n)) or {})
                          .get("file"))
    if not has_img:
        _set_page(d, n, status="canceled", error="用户终止了这一页",
                  rev=int(time.time()))
    log.info("moss 任务 %s：终止第 %s 页（队列命中=%s，已有图=%s）",
             os.path.basename(d), n, hit, has_img)
    if has_img:
        return jsonify({"ok": True, "page": n, "hit": hit, "keep": True,
                        "message": "这一页已经画好了，不用停"})
    return jsonify({"ok": True, "page": n, "hit": hit,
                    "message": "这一页已终止，后面的照画"} if hit else
                   {"ok": True, "page": n, "hit": False,
                    "message": "这一页已经标掉了"})


@bp.route("/api/moss/pages_state", methods=["GET"])
def api_moss_pages_state():
    """每一页**此刻到底在哪**（2026-11-23）。

    state.json 里的 status 是出图线程写的，只能说明「开始画了 / 画完了」；
    用户真正想知道的是「它还在队列里排着？在 ComfyUI 手里画着？还是两边都
    没它了（ComfyUI 重启过）」——这三种情况原先都显示成同一句「画图中」。
    这里按页号去 `image_jobs` 现查，并顺手问一次 ComfyUI：它认不认识这个
    prompt_id。问不到就说 `unknown`，不猜。
    """
    try:
        d = _task_dir(request.args.get("task"))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    spec = _read_json(os.path.join(d, "spec.json"), {})
    st = (_load_state(d).get("pages") or {})
    base = os.path.basename(d)
    try:
        snap = image_jobs.snapshot()
    except Exception:                           # noqa: BLE001
        snap = {}
    running = {j.get("tag"): j for j in
               ([snap.get("running")] if snap.get("running") else [])}
    queued = {j.get("tag"): j for j in (snap.get("queued") or [])}
    out = {}
    comfy_up = None
    for page in spec.get("pages") or []:
        n = page.get("n")
        tag = "%s#%s" % (base, n)
        rec = st.get(str(n)) or {}
        phase = ""
        ahead = None
        if _rel_exists(rec.get("file")):
            # 已经有图 = 这一页画完了（2026-11-24）。队列里可能还残留着上一批
            # 同页号的旧条目，不能拿它去盖「已完成」——那正是用户看到的
            # 「第一页画过了还写准备中」。
            out[str(n)] = {"phase": "done", "ahead": None,
                           "error": "", "secs": rec.get("secs")}
            continue
        if tag in running:
            phase, ahead = "submit", 0
            pid = running[tag].get("pid")
            if pid:
                if comfy_up is None:
                    try:
                        comfy_up = image_jobs._comfy_up()
                    except Exception:           # noqa: BLE001
                        comfy_up = None
                if comfy_up is False:
                    phase = "lost"
                elif image_jobs._comfy_queue_has(pid) or image_jobs.poll_once(pid):
                    phase = "running"
                else:
                    phase = "lost"
        elif tag in queued:
            phase = "queued"
            ahead = queued[tag].get("ahead")
        else:
            phase = {"done": "done", "error": "error",
                     "canceled": "canceled"}.get(rec.get("status") or "", "idle")
        out[str(n)] = {"phase": phase, "ahead": ahead,
                       "error": rec.get("error") or "",
                       "secs": rec.get("secs")}
    return jsonify({"ok": True, "task": base,
                    "comfy_online": comfy_up, "pages": out})


@bp.route("/api/moss/render", methods=["POST"])
def api_moss_render():
    """后台出图。`pages` 不传＝全部；`seed` 明确给就用，`reroll=true` 换随机。

    不点名页号时**跳过已经画好的页**（2026-11-24）：只补没画的、失败的和被
    终止的。要整本重画带 `force=true`（或给 seed / reroll），要单页重画就
    点名 `pages=[n]`。

    同一任务同时只跑一个线程——两批一起跑会在 `image_jobs` 队列里抢同一套
    模型，白等两遍冷启动。
    """
    data = request.get_json(silent=True) or {}
    try:
        d = _task_dir(data.get("task"))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    spec = _read_json(os.path.join(d, "spec.json"), {})
    all_n = [p.get("n") for p in spec.get("pages") or []]
    if not all_n:
        return jsonify({"ok": False, "error": "剧本是空的"}), 400

    want = data.get("pages")
    nums = all_n
    picked = False                              # 是否点名了页号
    if isinstance(want, list) and want:
        keep = set()
        for x in want:
            try:
                keep.add(int(x))
            except (TypeError, ValueError):
                pass
        nums = [n for n in all_n if n in keep]
        picked = True
    if not nums:
        return jsonify({"ok": False, "error": "没有要画的页"}), 400

    # 「全部出图」只补没画的（2026-11-24 用户：「重新跑不会跑已经画过的」）。
    # 已 done 的页有图，重排一遍要白等两三分钟还会把旧图覆盖掉。
    # 只有两种情况照旧重画：点名了页号（单页「出图」）、或者显式 force。
    # 想整本重画就带 force=true——前端「换 seed」走的就是这条路。
    # 「换 seed」是整本重抽：只有真的指了 seed 或 reroll 才整本重画。
    def _as_int(v):
        try:
            return int(v or 0)
        except (TypeError, ValueError):
            return 0
    force = bool(data.get("force")) or bool(data.get("reroll")) or bool(
        _as_int(data.get("seed")))
    skipped = []
    if not picked and not force:
        st = (_load_state(d).get("pages") or {})
        todo = []
        for n in nums:
            s = st.get(str(n)) or {}
            if _rel_exists(s.get("file")):
                skipped.append(n)
            else:
                todo.append(n)
        nums = todo
        if not nums:
            return jsonify({"ok": True, "task": os.path.basename(d), "pages": [],
                            "skipped": skipped,
                            "message": "都画完了，没有要补的"})

    try:
        seed_base = int(data.get("seed") or 0)
    except (TypeError, ValueError):
        seed_base = 0
    if data.get("reroll") and not seed_base:
        seed_base = int(time.time() * 1000) % 900000000 + 100000000

    # LoRA 槽位：不传 = 照模板现有配置；传了就先校验再进线程（400 早退，
    # 别让半套参数跑到出图线程里才炸）。
    slots, errs, _warn = lora_slots.normalize(data.get("loras"))
    if errs:
        return jsonify({"ok": False, "error": "；".join(errs)}), 400

    # 步长：不传＝用 spec 里存过的，再没有就默认 30。范围 1–150。
    try:
        steps = int(data.get("steps") or 0)
    except (TypeError, ValueError):
        steps = 0
    steps = min(max(steps or int(spec.get("steps") or 0) or STEPS, 1), 150)
    spec["steps"] = steps

    with _RUN_LOCK:
        if _RUNNING.get(d):
            cur = _RUNNING[d].get("n")
            return jsonify({"ok": False, "busy": True, "page": cur,
                            "error": "这一本已经在画第 %s 页了，这一批跑完再点" %
                                     (cur if cur else "?")}), 409
        _RUNNING[d] = {"t0": time.time(), "n": nums[0]}
        ev = _ABORT[d] = threading.Event()

    # 步长记进 spec.json：下次打开这个任务还是这个步数，不用每次重填
    try:
        _save(d, spec)
    except BaseException:                       # noqa: BLE001  存盘失败不该拦出图
        pass

    threading.Thread(target=_run,
                     args=(d, spec, nums, seed_base, slots, steps, ev),
                     daemon=True,
                     name="moss-comic-%s" % os.path.basename(d)).start()
    return jsonify({"ok": True, "task": os.path.basename(d), "pages": nums,
                    "skipped": skipped, "force": force,
                    "seed": seed_base or None, "steps": steps})


@bp.route("/api/moss/abort", methods=["POST"])
def api_moss_abort():
    """终止这一批出图。

    不是杀进程：置位 Event → 出图线程每页看一眼；正在跑的那一页由出图层
    走定向 `/interrupt` + `/queue delete`（只有当前跑的就是这张时才停），
    后面没开始的页直接标成「已终止」。ComfyUI 本身不动。
    """
    data = request.get_json(silent=True) or {}
    try:
        d = _task_dir(data.get("task"))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    with _RUN_LOCK:
        ev = _ABORT.get(d)
        running = bool(_RUNNING.get(d))
    if not ev or not running:
        return jsonify({"ok": True, "aborted": False,
                        "message": "这个任务没在跑"})
    ev.set()
    # 整批停要**同时**把队列里这一本还没开始的那几页摘掉（2026-11-23）：
    # 光置位 Event 的话，正在画的那张要等它收尾，队列里那几张还占着名额，
    # 用户重新点「开始画」会被自己刚才那批挡住。逐页按 tag 掐，别的任务
    # 一张都不碰。
    spec = _read_json(os.path.join(d, "spec.json"), {})
    dropped = 0
    for page in spec.get("pages") or []:
        try:
            if image_jobs.cancel_tag("%s#%s" % (os.path.basename(d), page.get("n")),
                                     "用户终止了这一批"):
                dropped += 1
        except Exception:                       # noqa: BLE001
            pass
    return jsonify({"ok": True, "aborted": True, "dropped": dropped,
                    "message": "已全部终止：当前这一页停下，后面没开始的都不画了"})


# ══════════════════════════════════════════════════════════
#  LoRA 槽位（2026-10-28 用户要求「自己选开哪个」）
# ══════════════════════════════════════════════════════════
#
# `MOSSLoRAToggle` 是「一个节点里并联 10 个槽」，改它只改字段、不重建链。
# 读写逻辑在 `app/lora_slots.py`，跑图台 / 漫画台 / 这里共用同一份。


@bp.route("/api/moss/loras", methods=["GET"])
def api_moss_loras():
    wf = load_workflow(os.path.join(BASE_DIR, "skills", SKILL, "workflow.json"))
    if not wf:
        return jsonify({"ok": False,
                        "error": "读不到 skills/%s/workflow.json" % SKILL}), 500
    return jsonify({"ok": True, "skill": SKILL, "kind": "toggle",
                    "slots": lora_slots.read_slots(wf)})


# 「存为默认」不在这里开第二个口子：三个台统一走 `POST /api/lora/save`
# （gen_api），落到同一个 `lora_slots.save_default`。两条路径做同一件事，
# 迟早分叉成「在 A 台存了、B 台不认」。


# ══════════════════════════════════════════════════════════
#  读：状态 / 图 / 历史
# ══════════════════════════════════════════════════════════
@bp.route("/api/moss/status", methods=["GET"])
def api_moss_status():
    try:
        d = _task_dir(request.args.get("task"))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    return jsonify(_payload(d))


@bp.route("/api/moss/img", methods=["GET"])
def api_moss_img():
    """出图。只认 state.json 里记过的那条相对路径，不做通用静态目录服务。"""
    try:
        d = _task_dir(request.args.get("task"))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    try:
        n = int(request.args.get("n"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "n 不是数字"}), 400
    rel = _page_file(d, n)
    if not rel:
        return jsonify({"ok": False, "error": "这一页还没出图"}), 404
    full = rel if os.path.isabs(rel) else os.path.join(
        COMFYUI_OUTPUT_DIR or "", rel.replace("/", os.sep))
    if not os.path.isfile(full):
        return jsonify({"ok": False, "error": "图不见了：%s" % rel}), 404
    return send_file(full, max_age=0)


@bp.route("/api/moss/list", methods=["GET"])
def api_moss_list():
    """历史任务（按 spec.json 的修改时间倒序）。

    `line_n` / `line_of`（2026-11-24）：这本在连载里是第几页、那条线一共几页。
    续集出来的本子标题往往和上一本一样，光看标题分不清顺序，下拉里要靠这个
    标出「第 02 页」。一次扫全库算一次（`_series_index`），不逐本去读档案。
    """
    items = []
    idx = {}
    from app.features.comic import quota as comic_quota   # 函数内 import，避开加载期循环
    try:
        idx = {k: (v[0], v[1].get("n"), len(v[2].get("pages") or []))
               for k, v in _series_index().items()}
    except Exception:                           # noqa: BLE001 - 列表不该被连载拖垮
        log.exception("moss 列表读连载位置失败")
    if os.path.isdir(STATE_ROOT):
        for name in os.listdir(STATE_ROOT):
            d = os.path.join(STATE_ROOT, name)
            sp = os.path.join(d, "spec.json")
            if not os.path.isfile(sp):
                continue
            spec = _read_json(sp, {})
            pages = spec.get("pages") or []
            st = _load_state(d).get("pages") or {}
            have = [(p.get("n"), _page_file(d, p.get("n"), st)) for p in pages]
            have = [(n, rel) for n, rel in have if rel]
            first = have[0] if have else (None, "")
            ln, lof = 0, 0
            hit = idx.get(os.path.realpath(d))
            if hit:
                ln, lof = int(hit[1] or 0), int(hit[2] or 0)
            else:
                raw = spec.get("line") if isinstance(spec.get("line"), dict) else {}
                if raw.get("series"):
                    ln = int(raw.get("parent_n") or 0) + 1
            items.append({
                "task": name,
                "title": spec.get("title") or name,
                "pages": len(pages),
                "layout": (pages[0].get("layout") if pages else "") or "",
                "done": len(have),
                "line_n": ln,
                "line_of": lof,
                # 站主 / 群友（2026-11-24）：老任务没有 owner.json，一律算站主。
                # 只做二分，不返回群友 QQ —— 用户要的就是「我的」和「群友的」。
                "owner": "friend" if comic_quota.is_friend_task(d) else "owner",
                "mtime": time.strftime("%m-%d %H:%M",
                                       time.localtime(os.path.getmtime(sp))),
                "thumb": ("/api/moss/img?task=%s&n=%s" % (name, first[0]))
                         if first[1] else "",
                "_ts": os.path.getmtime(sp),
            })
    items.sort(key=lambda x: x.get("_ts") or 0, reverse=True)
    for it in items:
        it.pop("_ts", None)
    # ?owner=owner / friend 只看一半；不给 = 全部（老页面行为不变）
    want = str(request.args.get("owner") or "").strip()
    if want in ("owner", "friend"):
        items = [it for it in items if it.get("owner") == want]
    return jsonify({"ok": True, "items": items})
