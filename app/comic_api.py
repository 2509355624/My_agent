"""漫画工作台 API：挂给 app/main.py 的 Blueprint。

复用 app/comic.py 的全部调度能力（run / render_scene / render_bubble / Task），
把 CLI 的手动动作变成 HTTP 端点 + 后台线程：

  POST /api/comic/script    编剧写剧本 → 建任务目录（POV 仍走补正；free/third 走 silver 一次 API）
  POST /api/comic/save      用户编辑剧本后保存（确认开跑前的修改都走这里）
  POST /api/comic/fix       重跑内容补正（从 script_original.md 重来，直接落地）
  POST /api/comic/chat_revise  和修订助手聊一句：改剧本（落地+备份）或答疑
  GET  /api/comic/chat_status  轮询那句话的结果
  GET  /api/comic/chat_history 任务里的对话历史（刷新页面显示回来）
  POST /api/comic/chat_undo 撤销最近一次对话改稿（script_chat_backup.md → script.md）
  POST /api/comic/start     确认开跑：后台线程跑阶段一（场景图，不碰气泡）
  GET  /api/comic/status    槽位轮询：剧本 + state.json → 每格状态与图 URL
  GET  /api/comic/render_opts  读这个任务的渲染参数覆盖（render.json）+ ComfyUI 可用清单
  POST /api/comic/render_opts  保存 / 重置（reset=true）渲染参数覆盖
  POST /api/comic/rerender  换种子重跑某格（重置该格 → 后台只跑阶段一）
  POST /api/comic/portrait  生图模式：提示词员写 N 条单人写真 → 逐条出图（后台）
  GET  /api/comic/portrait_status  轮询这批写真的进度（shots 逐条落盘）
  POST /api/comic/portrait_shot  换种子重跑某一张写真
  POST /api/comic/bubble    加气泡：单格（含已完成的格＝从底图重画），或全部
  GET  /api/comic/prompt    取出「这一格原样发给 qwen 的那段文字」，供界面显示与手改
  POST /api/comic/manual    手动修复：拿当前成品当源图，按手输的指令跑一遍 qwen
  POST /api/comic/upload    手动放底图：外部跑好的底图顶替这一格的 raw
  GET  /api/comic/img       服务 raw/ final/ portraits/ 的图；群友版改走 thumb/ 缩略图
  GET  /api/comic/download  成品 final/ 打包 zip（站主版）
  GET  /api/comic/friend_config  群友页启动配置：额度 / 模型白名单 / 画幅预设
  GET  /api/comic/delivery_status  QQ 直发状态（哪个包发了、失败原因）
  POST /api/comic/send_qq   手动重发成品包到 QQ（失败不回退网页下载）
  GET|POST /api/comic/admin  站主 5174 管理页：模型白名单 / 按账号额度 / 发送记录

群友态（2026-11-10 开放，详见 app/comic_quota.py 模块头）：5175 带内部令牌 +
登录 Cookie 解出的 qq 转发过来才算群友。站主直连 5174 没有那层令牌，**全部放行、
一个限制都不加**，老页面 /comic 一个字节都不用改。群友才走：按账号配额、作品
归属校验、网页缩略图、强制跳过最终 2x 放大、output 按账号/作品分流、跑完 QQ
直发 zip。高清原图不经 Cloudflare 隧道，网页只看服务端磁盘上的 WEBP 缩略图。

为什么状态不用内存而读 state.json：comic.run 本来就逐格落盘 + 断点续跑，
`pending → rendered → done` 的进度全在里面——轮询它就是唯一事实来源，
重启 Flask 也不丢。同一任务同时只允许一个后台线程（_JOBS 里的运行标记），
防止两个 run 互相踩 state.json。

⚠️ comic.run 抛的是 SystemExit（_require_comfy 等处 raise SystemExit），
它不继承 Exception——线程里必须用 `except BaseException` 接住记进 _JOBS，
否则失败是静默的，前端只会看到永远不结束。
"""

import io
import json
import os
import re
import threading
import time
import uuid
import zipfile

from flask import (Blueprint, Response, g, jsonify, request, send_file,
                   send_from_directory)

from app import (comic, comic_agent, comic_delivery, comic_portrait,
                 comic_quota, comic_render_opts, comic_thumb, image_jobs,
                 silver_comic)
from app import config as _cfg
from app.config import BASE_DIR

bp = Blueprint("comic_api", __name__)

# ─── 群友态（2026-11-10 开放漫画台）──────────────────────
#
# 5175 的 `_forward()` 带 `X-Friend-Token` 内部令牌并把登录 Cookie 解出的 QQ
# 写死塞进请求；站主直连 5174 没有那个头。`comic_quota.friend_qq()` 只认这个
# 组合，所以**站主版 /comic 的所有行为完全不变**，前端一个字节都不用动。
# 群友身份挂在 `g` 上：后台线程没有 request 上下文，但 `_task_dir` 的归属校验
# 只在请求线程里跑，不冲突。

@bp.before_request
def _friend_context():
    try:
        g.comic_qq = comic_quota.friend_qq()
    except Exception:
        g.comic_qq = ""


def _qq():
    """当前请求的群友 QQ；站主返回空串（= 全部放行）。"""
    try:
        return getattr(g, "comic_qq", "") or ""
    except RuntimeError:          # 没有请求上下文（后台线程/CLI）
        return ""


def _job_visible(task_value):
    """群友能不能轮询这个 job。job 号是随机的，但剧本正文也在响应里，
    猜到一个 id 就该只能看到自己的。`task` 还是 None = 后台还没建目录，
    那时无从判断，放行（前端只拿得到自己刚提交的 job）。
    """
    qq = _qq()
    name = os.path.basename(str(task_value or ""))
    if not qq or not name:
        return True
    return comic_quota.can_read(os.path.join(_COMICS_ROOT, name), qq)


def _img_url(task_id, rel, thumb=None):
    """同一张图的 URL：群友版指服务端缩略图，站主版照旧指高清原图。

    缩略图不是塞进浏览器 Local Storage 的字节，而是任务目录 `thumb/` 下按需
    生成、落盘缓存的 WEBP（见 `app/comic_thumb`）。这样换设备、清缓存、刷新
    页面都能重新拉回来；高清原图走 QQ zip，网页一次都不用碰 9MB 的 PNG。
    """
    f = thumb if (_qq() and thumb) else rel
    return "/api/comic/img?task=%s&f=%s" % (task_id, f)


def _need(kind):
    """群友请求前检查额度 → 额度不足返回错误响应；站主永远返回 None。

    只检查不扣：真正扣在各端点确认启动成功后，失败的调用不白扣次数。
    """
    qq = _qq()
    if not qq:
        return None
    if comic_quota.remaining(qq, kind) <= 0:
        label = {"script": "剧本生成", "revise": "修订助手",
                 "comic": "漫画生成", "portrait": "写真集"}.get(kind, kind)
        return jsonify({"ok": False,
                        "error": "免费的%s次数用完了，找站主加次数吧" % label,
                        "quota": comic_quota.snapshot(qq)}), 403
    return None


def _refund(kind):
    """端点没真正启动（参数错、任务在跑、建目录失败）时退还预扣。"""
    qq = _qq()
    if qq:
        comic_quota.refund(qq, kind)


# ─── 群友编剧的「中转站优先」计费（2026-11-11 用户拍板）───────────────
#
# 免费层 = 公用中转站 V4 Pro（与角色扮演同一家，config.PERSONA_*），站主零成本：
# 请求成功就免费、不限次、不扣任何额度。中转站挂了才动用「代充次数」——就是
# comic_quota 现有的 script / revise 计数器（站主在管理页按账号加），走站主付费
# 官方并扣 1 次；没次数就报错引导找站长。默认额度因此全部为 0（见
# comic_quota.DEFAULTS，state/comic_friend.json 里也已是 0）。
#
# 免费尝试一律 strict=True：只打中转站、绝不接降级链——全局降级链的第一项就是
# 付费官方，静默接上去等于绕过次数闸门白烧站主的余额（llm.candidates 文档）。
# 下层函数（write_script / fix_script / chat_revise / silver）在 strict 模式下
# 把调用失败**抛出来**而不是吞掉，这里才探测得到「中转站挂了」。

# 免费层的 (provider, model)。读属性而非 import 常量：测试想换掉它时 patch
# config 模块属性即可，不用重载本模块。
def _relay_pair():
    return ((_cfg.PERSONA_PROVIDER or "").strip(),
            (_cfg.PERSONA_MODEL or "").strip())


def _relay_err(exc):
    return (str(exc) or exc.__class__.__name__)[:120]


def _friend_llm(qq, kind, label, run):
    """群友 LLM 作业：先免费中转站，挂了才扣代充次数走站主官方。

    `run(provider, model, strict)` → 任意结果；抛异常 = 这一次没成（调用方的
    「补正失败不拖垮编剧」这类兜底要在 run 内部处理好，别把业务失败混成网络
    失败）。返回 (结果, 是否扣了次数)；扣了次数但官方也失败时在这里退回。
    """
    def _paid():
        comic_quota.consume(qq, kind)
        try:
            return run(None, None, False), True
        except Exception:
            comic_quota.refund(qq, kind)
            raise

    relay_p, relay_m = _relay_pair()
    if not relay_p:
        # 免费层没配置：等同「中转站不在」——有次数扣着走官方，没有就明说。
        if comic_quota.remaining(qq, kind) <= 0:
            raise RuntimeError("中转站没有配置，免费的%s跑不了；"
                               "找站长加次数就能用站主的额度继续" % label)
        return _paid()
    try:
        return run(relay_p, relay_m, True), False
    except Exception as relay_exc:
        if comic_quota.remaining(qq, kind) <= 0:
            raise RuntimeError(
                "中转站出问题（%s），免费的%s暂时跑不了；"
                "找站长加次数，就能用站主的额度继续"
                % (_relay_err(relay_exc), label)) from relay_exc
        comic.log.info("中转站没跑成（%s），扣 1 次代充改走站主官方", _relay_err(relay_exc))
        return _paid()


@bp.route("/api/comic/friend_config", methods=["GET"])
def api_comic_friend_config():
    """群友页启动配置：剩余额度、可用编剧模型、画幅预设、发送状态说明。"""
    qq = _qq()
    return jsonify({"ok": True, "friend": bool(qq),
                    "quota": comic_quota.snapshot(qq) if qq else None,
                    "models": comic_quota.models() if qq else None,
                    "panel_tiers": comic_quota.PANEL_TIERS,
                    "ratio_presets": [
                        {"key": "2:3", "label": "竖屏 2:3", "w": 832, "h": 1216},
                        {"key": "3:4", "label": "竖屏 3:4", "w": 960, "h": 1280},
                        {"key": "1:1", "label": "方块 1:1", "w": 1024, "h": 1024},
                        {"key": "3:2", "label": "横屏 3:2", "w": 1216, "h": 832},
                        {"key": "16:9", "label": "横屏 16:9", "w": 1344, "h": 768},
                    ]})

# 任务根目录：CLI 里用相对路径 comics/（cwd 在项目根），API 这里钉死绝对，
# 免得 Flask 的启动 cwd 不同把任务散到别处。
_COMICS_ROOT = os.path.join(BASE_DIR, "comics")

# 运行中的后台任务：{任务目录绝对路径: {"phase": "scene"|"bubble", "error": str}}
_JOBS = {}
_JOBS_LOCK = threading.Lock()

# ComfyUI 在线状态（给页面顶部显示，2026-10-18）。
# **不能每次轮询都真探活**：前端 2 秒问一次，而探活失败时要等满 timeout，
# 一挂就整页跟着卡。缓存 5 秒，够用且不会漏掉「正在重启」这件事。
_COMFY_CACHE = {"at": 0.0, "up": True}
_COMFY_TTL = 5.0


def _comfy_state():
    now = time.time()
    if now - _COMFY_CACHE["at"] >= _COMFY_TTL:
        _COMFY_CACHE["up"] = bool(image_jobs.comfy_up(timeout=1))
        _COMFY_CACHE["at"] = now
    return _COMFY_CACHE["up"]

# ─── 视角模式（pov / free，2026-10-22）────────────────────────
#
# 存在任务目录的 `mode.json`（`{"pov_mode": "free"}`），不用 state.json：
# state.json 由 `comic.Task` 管、字段有它自己的口径（渲染进度），而视角模式是
# **创作口径**，在剧本还没写完时就定了，塞进去得先建 Task 对象，反而绕。
#
# ⚠️ 为什么要落盘而不是只从前端带：「重新补正」和「修订助手」都是之后单独触发
# 的请求，那时前端不一定会把模式带上。不读这个文件，补正员会拿着 POV 硬规则把
# 作者刚写好的双人剧本又改回单人——这是模式切换最容易踩的坑。
# 三档：pov 全篇第一人称 / free 机位自选 / third 第三人称旁观（双人为常态）。
_POV_MODES = ("pov", "free", "third")


def _norm_pov_mode(v):
    """归一成一个合法模式，认不出来就当 pov（默认永远是保守那一侧）。"""
    v = str(v or "").strip().lower()
    return v if v in _POV_MODES else "pov"


def _write_pov_mode(path, mode):
    """把这一话的视角模式记进任务目录。写不进去只记日志——不影响出稿。"""
    try:
        with open(os.path.join(path, "mode.json"), "w", encoding="utf-8") as f:
            json.dump({"pov_mode": mode}, f, ensure_ascii=False)
    except OSError as exc:
        comic.log.warning("写 mode.json 失败 %s：%s", path, exc)


def _read_pov_mode(path):
    """读任务的视角模式；没有 / 坏了 / 值不认识都退回 pov（老任务全是 POV）。"""
    try:
        with open(os.path.join(path, "mode.json"), encoding="utf-8") as f:
            m = json.load(f).get("pov_mode")
    except (OSError, ValueError):
        return "pov"
    return _norm_pov_mode(m)


# 编剧任务：{job_id: {...}}。与 _JOBS 分开——_JOBS 以**任务目录**为键（要等
# 剧本落盘才知道目录名），而编剧任务在剧本写出来之前就得有个句柄给前端轮询。
_SCRIPT_JOBS = {}
_SCRIPT_LOCK = threading.Lock()
_SCRIPT_KEEP = 12          # 只留最近这么多条，防长期运行攒内存

# 对话修订任务：{job_id: {...}}。同一次只许一句话在跑（前端发送按钮置灰 +
# 后端 job 检查双保险）；同一任务也不许并发改稿（跑渲染时后端直接拒绝）。
_CHAT_JOBS = {}
_CHAT_LOCK = threading.Lock()
_CHAT_KEEP = 12
_CHAT_HIST_MAX = 40        # chat_history.json 里最多留多少条（20 轮对话）

# 写真批次任务：{job_id: {...}}。与 _SCRIPT_JOBS 同型——写提示词要点时间，
# 前端要个句柄轮询；同一任务同时只许一批在跑（prompts.json 会被互相踩）。
_PORTRAIT_JOBS = {}
_PORTRAIT_LOCK = threading.Lock()
_PORTRAIT_KEEP = 12


def _read_chat_history(path):
    """读任务的对话历史 chat_history.json；没有/坏了返回空列表（老任务目录）。"""
    fp = os.path.join(path, "chat_history.json")
    if not os.path.isfile(fp):
        return []
    try:
        with open(fp, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        comic.log.warning("chat_history.json 读不动：%s", fp)
        return []


def _append_chat_history(path, user_msg, assistant_msg):
    """把一轮对话追加进历史。LLM 上下文里只带最近几条，文件里留全一点，
    刷新页面后对话还能整段显示回来。"""
    hist = _read_chat_history(path)
    hist.append({"role": "user", "content": user_msg})
    hist.append({"role": "assistant", "content": assistant_msg})
    del hist[:-_CHAT_HIST_MAX]
    with open(os.path.join(path, "chat_history.json"), "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False, indent=1)
    return hist


def _task_dir(task_id):
    """task id → 目录绝对路径。只认一级目录名，堵路径穿越。

    群友请求额外验归属：每个任务目录里的 `owner.json` 是谁建的就只给谁看。
    不这么拦的话，`/api/comic/tasks` 一把 `os.listdir` 就能把站主所有漫画和
    别人的作品全列出来，改个 task 参数连原图都能拉走。
    """
    name = os.path.basename(str(task_id or "").strip())
    if not name:
        raise ValueError("缺 task")
    path = os.path.join(_COMICS_ROOT, name)
    if not os.path.isdir(path):
        raise ValueError("任务不存在：%s" % name)
    qq = _qq()
    if qq and not comic_quota.can_read(path, qq):
        raise ValueError("任务不存在：%s" % name)
    return path


def _load_task(path):
    """目录 → (Task, script_path)。state.json 里的进度贴回面板。"""
    script = os.path.join(path, "script.md")
    if not os.path.isfile(script):
        raise ValueError("任务里没有 script.md")
    meta, panels = comic.parse_script(script)
    task = comic.Task(script, meta, panels)
    task.load_state()
    return task


# ─── 阶段一渠道（2026-10-19 多渠道）─────────────────────
#
# 渠道跟着**任务**走：state.json 里一个 `scene_skill` 字段。纯写真任务没有
# script.md、也就没有 state.json，那条线写在 portrait.json 的同名字段里。
# 三条读取路径（status / render_opts / start）都走 _resolve_skill，避免出现
# 「界面显示 A、实际跑 B」。

_SKILL_FILES = ("state.json", "portrait.json")


def _state_skill(path):
    """任务目录里记着的阶段一渠道：state.json > portrait.json > 空串。"""
    if not path:
        return ""
    for name in _SKILL_FILES:
        fp = os.path.join(path, name)
        if not os.path.isfile(fp):
            continue
        try:
            with open(fp, "r", encoding="utf-8") as f:
                v = (json.load(f) or {}).get("scene_skill")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _resolve_skill(path, wanted=""):
    """这次用哪个渠道：请求显式给的 > 任务存过的 > 全局默认。"""
    return (str(wanted or "").strip() or _state_skill(path)
            or comic.SCENE_SKILL)


# 群友渠道锁定（2026-11-11）：群友页只给 anima-line 一个渠道（用户拍板「最快
# 的当默认，其他先隐藏」）。前端隐藏只是 UI 层，这里在服务端收敛：带 qq 的
# 请求无论传什么 scene_skill 都落回 anima-line，直连 5174 的 API 调用也绕不过。
# 站主（qq 为空）不受影响，多渠道照旧。
FRIEND_SKILL = "anima-line"


def _skill_for(path, wanted=""):
    """所有「渠道从请求里来」的入口统一走这里：群友锁死 anima-line。"""
    if _qq():
        return FRIEND_SKILL
    return _resolve_skill(path, wanted)


def _save_task_skill(path, skill):
    """把渠道记回任务目录。

    优先写已存在的账本文件；两个都没有（刚建的写真任务）就建 portrait.json——
    随后 `comic_portrait.save_meta` 会合并写，不会把这个字段冲掉。
    """
    skill = str(skill or "").strip()
    if not (path and skill):
        return
    for name in _SKILL_FILES:
        fp = os.path.join(path, name)
        if not os.path.isfile(fp):
            continue
        try:
            with open(fp, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        data["scene_skill"] = skill
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return
    try:
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, "portrait.json"), "w", encoding="utf-8") as f:
            json.dump({"scene_skill": skill}, f, ensure_ascii=False, indent=1)
    except OSError as exc:
        comic.log.warning("记不下阶段一渠道（%s）：%s", path, exc)


# 下拉框分组（只是显示用，不影响任何调度）
_GROUPS = (
    ("anima-line", "anima"), ("anima", "anima"), ("silver", "silver"),
    ("hd_", "高清"), ("krea2", "krea2"), ("qwen", "qwen"), ("moss", "moss"),
    ("miao", "miao"), ("image_gen", "sd"), ("nffa", "nffa"),
    ("cunny", "cunny"), ("jank", "jank"), ("nai", "云端"),
)


def _group_of(name):
    for pre, label in _GROUPS:
        if name.startswith(pre):
            return label
    return "其他"


def _channels():
    """参数卡能选的阶段一渠道：**有 workflow.json 的 skill** + NAI 云端。

    有 workflow 就是能文生图（i2i-only 的 skill 没有这个文件）。写作用 skill
    也因此天然被挡掉，不用去解析 frontmatter。
    """
    from app.skills import SKILLS_DIR, list_skills
    out = []
    for name in sorted(list_skills()):
        if os.path.isfile(os.path.join(SKILLS_DIR, name, "workflow.json")):
            out.append({"name": name, "group": _group_of(name)})
    for name in image_jobs.NAI_SKILLS:
        out.append({"name": name, "group": "云端", "cloud": True})
    out.sort(key=lambda c: (c["group"], c["name"]))
    return out


def _rev(path):
    """图片指纹（毫秒 mtime + 字节数），文件不存在给 "0"。

    前端是**逐格 diff** 刷新的：签名没变就不重建 DOM，图也不会重新加载。
    手动跑 Qwen 是「就地覆盖成品」——`state` 还是 done、`seed` 没变、`final_url`
    字符串一模一样，光靠原来那几个字段签名不变，于是新图永远不显示（用户
    2026-10-11 报的「重跑结果没给到界面」）。所以签名必须带上**文件本身变了没**。
    用毫秒 mtime（NTFS 100ns 精度）而不是秒，避免同一秒内连跑两次判成没变。
    """
    try:
        st = os.stat(path)
    except OSError:
        return "0"
    return "%d.%d" % (int(st.st_mtime * 1000), st.st_size)


def _save_upload(fs, dst):
    """上传的图统一转成 PNG 再落盘。

    不按原后缀直接存：存成 `.png` 却塞 JPEG 字节，会让 /api/comic/img 的
    content-type 撒谎（它按扩展名给 image/png），浏览器有时就不认了。顺便这
    一步也是「上传的到底是不是图」的校验。EXIF 方向要摆正——手机/截图工具出的
    jpg 常带 Orientation，不正过来会躺倒。
    """
    from PIL import Image, ImageOps
    im = Image.open(io.BytesIO(fs.read()))
    try:
        im.load()
        im = ImageOps.exif_transpose(im) or im
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGBA" if "A" in im.getbands() else "RGB")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        im.save(dst, "PNG")
    finally:
        im.close()


def _write_fix_report(path, fix):
    """把补正结果落成 `fix.json`（不放全文，只放清单与逐格差异）。

    落盘而不是只放内存：补正是编剧之后的**独立一步**，页面靠槽位轮询读它，
    重启 Flask 后也要还在（和 state.json 一个道理）。
    """
    report = {k: v for k, v in fix.items() if k != "text"}
    report["at"] = time.strftime("%H:%M:%S")
    with open(os.path.join(path, "fix.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    return report


def _read_fix_report(path):
    """读 `fix.json`；没有就返回 None（老任务目录没有这个文件）。"""
    fp = os.path.join(path, "fix.json")
    if not os.path.isfile(fp):
        return None
    try:
        with open(fp, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        comic.log.warning("fix.json 读不动：%s", fp)
        return None


def _spawn(path, phase, fn, on_error=None, finish=None):
    """起后台线程跑 fn()。同一任务已有线程在跑就拒绝（409 语义）。

    `finish` 是这个任务跑完后的收尾动作，由**调用方显式给**，不靠 phase 猜：
      - None      什么都不做（单格重跑 / 改画面词 / 传底图 / 补正，默认）
      - "notify"  整本底图跑完：只通知「待检查」，不发 zip
      - "send"    整批加完气泡：把底图包 + 成品包一起发到 QQ
    2026-11-10 用户改的规则：阶段一刚跑完的图可能有瑕疵，用户会换种子重跑，
    一跑完就自动发等于先把废包发出去、重跑再发一次，所以只有用户确认之后
    （漫画点「一键加气泡」、写真点「发送 zip」）才真正发包。

    `on_error(exc)`：线程整体挂掉时的收尾（2026-10-18 加）。原来失败只记进
    内存 `_JOBS`，state.json 一个字都不动——而 `rerender` / `edit_scene` 进门
    第一件事就是把该格写成 `pending`，于是线程死后那格永远停在 pending，前端
    badge 显示「排队」，看着像卡死（用户 18:46 实报：ComfyUI 重启时点重跑，
    探活失败，"重启完还在排队"）。收尾由调用方给，因为只有它知道是哪几格。
    """
    with _JOBS_LOCK:
        running = _JOBS.get(path)
        if running and running.get("phase"):
            return False
        _JOBS[path] = {"phase": phase, "error": None}

    # 归属要在**请求线程**取：flask.g 是 thread-local，后台 worker 里读不到。
    owner = comic_quota.owner_of(path)

    def _worker():
        try:
            fn()
        except BaseException as exc:            # SystemExit 不是 Exception！
            with _JOBS_LOCK:
                _JOBS[path] = {"phase": None, "error": str(exc)}
            comic.log.exception("后台任务失败 %s", path)
            if on_error is not None:
                try:
                    on_error(exc)
                except Exception:
                    comic.log.exception("失败状态落盘时又出错 %s", path)
            return
        with _JOBS_LOCK:
            _JOBS[path] = {"phase": None, "error": None}
        # 群友任务的收尾由调用方用 finish 指定，默认什么都不做。
        # 单格重跑 / 改词 / 传底图都不再自动发（2026-11-10 用户拍板）。
        if owner and finish == "send":
            comic_delivery.spawn_auto(path, owner)
        elif owner and finish == "notify":
            comic_delivery.spawn_notify(path, owner)

    threading.Thread(target=_worker, daemon=True, name="comic-studio").start()
    return True


def _mark_pending_failed(path, no=None):
    """后台线程挂了以后的收尾：把还停在 `pending` 的格标成出错并落盘。

    只动 pending —— 已经 `rendered` / `done` 的格是跑之前就完成的，不该被这
    次失败牵连。`no` 给了就只标那一格（重跑单格的入口），没给就是整批
    （`/start`，此时所有没跑完的格都不会有人再来跑）。

    error 写的是异常原文（如「ComfyUI 等了 120 秒还没在线…」），前端会把它
    显示在格子里，badge 从「排队」变「出错」——两处都是现成渲染，不用改前端。
    """
    def _apply(exc):
        task = _load_task(path)
        hit = 0
        for p in task.panels:
            if p.state != "pending":
                continue
            if no and p.no != no:
                continue
            p.error = str(exc) or "后台任务失败"
            hit += 1
        if hit:
            task.save_state()
            comic.log.info("已把 %d 个未跑完的格标成出错（%s）", hit, path)
    return _apply


def _is_test_shell(path, name):
    """一个任务目录是不是「测试空壳」——只跑过编剧、从没出图的联调残留。

    背景（2026-11-10）：历史上有联调脚本拿固定 brief「公交站被抓包」直接打
    运行中的 /api/comic/script，剧本落进了真实 comics/ 却从没点「确认开跑」，
    攒出 76 个只有 5 个小文件的空壳，把历史任务下拉撑爆，站主找续集得疯狂下滑。

    **按内容判、不按名字判**：目录名 `_测试` 后缀能抓 75 个，但漏网的
    `C20261109-55_公交站被抓包` 名字里没有「测试」，只靠名字会漏。真正的共同
    指纹是「只有剧本、没有任何出图/状态、且 brief 是那句固定测试语」。判据宁可
    窄（只认这一组），也不能把用户刚生成、还没来得及开跑的真实剧本误藏掉——
    那种目录 brief 是用户自己写的，不会等于固定测试语。
    磁盘上的空壳已另行移动到归档目录；这道过滤是防复发兜底：以后再有联调
    残留，也自动不进下拉。带 `?include_test=1` 可临时看回全部（排查用）。
    """
    # 出过图 / 记过状态 / 是群友任务的，一律不是空壳（先排除， widest 保护）
    if os.path.isfile(os.path.join(path, "state.json")):
        return False
    if os.path.isdir(os.path.join(path, "raw")) or os.path.isdir(os.path.join(path, "final")):
        return False
    if os.path.isfile(os.path.join(path, "owner.json")):
        return False
    brief = os.path.join(path, "brief.txt")
    try:
        with open(brief, encoding="utf-8", errors="ignore") as f:
            if f.read().strip() == "公交站被抓包":
                return True
    except OSError:
        pass
    # 名字带 _测试 后缀、且确实没有任何出图痕迹的，也算（兼容早期没写 brief 的）
    return name.endswith("_测试")


@bp.route("/api/comic/tasks")
def api_comic_tasks():
    """列出 comics/ 下的剧本任务，给页面顶部的「历史任务」下拉用（2026-10-18）。

    为什么需要这个接口：剧本一直都在存（每个任务目录的 `script.md`），但页面
    只记得 localStorage 里那一个任务名——新建一集之后就再也翻不回去了，用户
    真要找前情只能去磁盘上翻目录。

    只收**有 script.md** 的目录：纯写真任务（生图模式新建、没剧本）不进这个
    列表，它们由 `portraits` 那条链路自己管。

    默认隐藏「测试空壳」（`_is_test_shell`）：联调残留不该占着历史任务下拉。
    站主排查时可带 `?include_test=1` 临时看回全部，不影响正常创作流程。
    """
    out = []
    qq = _qq()
    include_test = request.args.get("include_test") == "1"
    try:
        names = os.listdir(_COMICS_ROOT)
    except OSError:
        names = []
    for name in names:
        path = os.path.join(_COMICS_ROOT, name)
        if not os.path.isdir(path):
            continue
        # 群友只看到自己建的任务；站主（qq 空）照旧看全部，含没有 owner.json
        # 的老任务。开放前建的站主作品不带 owner，天然对群友不可见。
        if qq and comic_quota.owner_of(path) != qq:
            continue
        script = os.path.join(path, "script.md")
        if not os.path.isfile(script):
            continue
        # 测试空壳默认不进历史下拉；磁盘本身没删（已移归档），include_test 能看回
        if not include_test and _is_test_shell(path, name):
            continue
        item = {"task": name, "title": "", "panels": 0, "done": 0,
                "updated": "", "prev": ""}
        try:
            meta, panels = comic.parse_script(script)
            item["title"] = (meta or {}).get("title", "")
            item["panels"] = len(panels)
        except Exception:
            item["title"] = "（剧本解析失败）"
        try:
            with open(os.path.join(path, "state.json"), encoding="utf-8") as f:
                st = json.load(f)
            item["updated"] = st.get("updated", "") or ""
            item["done"] = sum(1 for p in (st.get("panels") or {}).values()
                               if isinstance(p, dict) and p.get("state") == "done")
        except (OSError, ValueError):
            pass
        try:
            with open(os.path.join(path, "prev.txt"), encoding="utf-8") as f:
                item["prev"] = f.read().strip()
        except OSError:
            pass
        # 这一集编剧时用的视角模式（老任务没有 mode.json → pov）
        item["pov_mode"] = _read_pov_mode(path)
        out.append(item)

    def _key(it):
        # 任务号 CYYYYMMDD-NN[_标题]：按「日期 + 序号数值」排，不能直接比字符串
        # （"C20261018-10" 会排到 "C20261018-9" 前面）。**不锚 `$`**：带标题的
        # 目录 `C20261018-06_那杯饮料` 也得按 6 参与排序，否则它们全掉到最后。
        m = re.match(r"^C(\d{8})-(\d+)", it["task"])
        return (m.group(1), int(m.group(2))) if m else ("0", 0)

    out.sort(key=_key, reverse=True)
    return jsonify({"ok": True, "tasks": out})


@bp.route("/api/comic/script", methods=["POST"])
def api_comic_script():
    """编剧写剧本：起后台任务，立刻返回 job；前端轮询 /script_status 取结果。

    **为什么必须异步**：编剧现在是 Agent（app/comic_agent）——先把这一话要用
    的标签逐个查库（search_tags）、再写剧本，十几轮 LLM 跑几分钟是常态。
    而 Flask 是单线程跑的（app/main.py 的 app.run 没开 threaded），同步等待
    会把同一个页面每 2 秒一次的槽位轮询一起堵死，界面看起来完全卡住
    （2026-10-11 确认）。

    provider / model 由前端下拉传入：不传就走全局默认，与别的环节同口径。
    """
    data = request.get_json(silent=True) or {}
    qq = _qq()
    brief = str(data.get("brief") or "").strip()
    if not brief:
        return jsonify({"ok": False, "error": "缺 brief（一句话设定）"}), 400
    # 群友版计费（2026-11-11 改）：不再进门就查/扣次数——免费层是公用中转站，
    # 在线就免费不限次；中转站挂了才在 _work 里查/扣「代充次数」走站主官方
    #（见 _friend_llm）。站主直连照旧不查不扣。
    # 自带 key 依然不放行：provider/model 对群友直接忽略，不给自定义入口。
    try:
        panels_n = int(data.get("panels") or 10)
    except (TypeError, ValueError):
        panels_n = 10
    # 格数上限以 silver 编剧为准（2026-10-22 用户要 50 页，原来这里写死 30）。
    # pov 路线自己还有 comic_story.clamp_panels 收口，不用在这拦。
    panels_n = max(1, min(silver_comic.MAX_PANELS, panels_n))
    if qq:
        panels_n = min(panels_n, comic_quota.limits(qq)["max_panels"])
    character = str(data.get("character") or "").strip()
    provider = str(data.get("provider") or "").strip() or None
    model = str(data.get("model") or "").strip() or None
    # 自由 / 第三人称统一走 silver 连续漫画 skill；pov 保持原路线。
    # free / third 不再调用 comic_agent 的复杂规范或内容补正。
    silver_mode = str(data.get("silver_mode") or "no_bubble").strip().lower()
    if silver_mode not in ("no_bubble", "bubble"):
        silver_mode = "no_bubble"
    # 跳过查库（2026-10-22）：前端默认勾上。只跳过「先查标签库」那十几轮，
    # 规范 / POV 硬规则 / 长度限制照旧，补正那一步也照旧（它不查库、只改稿）。
    # 不传 = True（用户要的默认）；显式传 false 才走查库流程。字符串形态也认，
    # 避免 `bool("false") is True` 这种老坑。
    no_search = data.get("no_search", True)
    if isinstance(no_search, str):
        no_search = no_search.strip().lower() not in ("", "0", "false", "no", "off")
    else:
        no_search = bool(no_search)
    # 视角模式（2026-10-22）：pov = 全篇第一人称硬规则（默认，与之前完全一样）；
    # free = 不加 POV 硬性规则，机位由编剧按规范自己挑（可以双人同框）。
    pov_mode = _norm_pov_mode(data.get("pov_mode"))
    # 连载（2026-10-18）：prev = 上一集的任务号，给了就把它的气泡+旁白当前情。
    # 目录名过一遍 `_task_dir` 校验（它挡住 `..` 和绝对路径），指不到就当没给。
    prev = str(data.get("prev") or "").strip()
    # 沿用上一集服装（2026-11-07）：默认不带，前端勾了才取最后一格的画面行。
    carry_outfit = bool(data.get("carry_outfit"))
    prev_text = ""
    prev_outfit_text = ""
    if prev:
        try:
            prev_path = os.path.join(_task_dir(prev), "script.md")
        except ValueError:
            prev_path = ""
        if prev_path and os.path.isfile(prev_path):
            prev_text = comic_agent.prev_story_text(prev_path)
            if carry_outfit:
                prev_outfit_text = comic_agent.prev_outfit(prev_path)
        if not prev_text:
            prev = ""             # 目录不在 / 没有剧本：静默按全新一集处理
            prev_outfit_text = ""

    job = uuid.uuid4().hex[:12]
    with _SCRIPT_LOCK:
        _SCRIPT_JOBS[job] = {"state": "running", "task": None, "script": None,
                             "error": None, "log": []}
        # 清掉已经结束的旧任务（运行中的绝不动）
        for k in [k for k, v in _SCRIPT_JOBS.items()
                  if v["state"] != "running"][:-_SCRIPT_KEEP]:
            _SCRIPT_JOBS.pop(k, None)

    def _progress(msg):
        with _SCRIPT_LOCK:
            j = _SCRIPT_JOBS.get(job)
            if j is not None:
                j["log"].append(msg)
                del j["log"][:-8]           # 只留最近几条给界面滚动显示

    def _produce(provider_, model_, strict_):
        """「出稿」段（不含落盘）：silver 一次 API，或 POV 写作 + 补正。

        strict=True 是中转站优先模式：写作层的调用失败**抛出来**，让 _friend_llm
        探测得到「中转站挂了」并换道代充。补正失败仍按「绝不拖垮编剧」吞成
        fix 报告（稿子已经免费拿到手；用户点「重新补正」时那条链路自己有
        中转站优先 + 代充，挂了会自己扣次数走官方）。
        """
        if pov_mode in ("free", "third"):
            # silver 自由路线：一次 API，skill 原文直接给模型，输出「固定部分 +
            # --- + 动态部分」；这里只转成旧 script.md 契约，不查库、不补正。
            detail = silver_comic.write_script_detail(
                brief, panels=panels_n, character=character,
                provider=provider_, model=model_,
                mode=silver_mode, prev=prev_text, strict=strict_)
            text_ = detail["script"]
            # 固定部分 / 逐格动态部分单独留在 job 里：界面要分两栏看，
            # 开跑时仍然是「固定部分 + 该格动态」拼起来交给渲染。
            return text_, {"text": text_, "issues": [], "diff": [],
                           "adopted": False, "status": "silver", "warnings": [],
                           "audit": [], "skipped": True,
                           "model": model_ or provider_ or "",
                           "silver": {"title": detail["title"],
                                      "fixed": detail["fixed"],
                                      "dynamics": [p["scene"]
                                                   for p in detail["panels"]],
                                      "bubbles": [{"bubble": p["bubble"],
                                                   "bubble2": p["bubble2"]}
                                                  for p in detail["panels"]]}}
        text_ = comic_agent.write_script(
            brief, panels=panels_n, character=character,
            provider=provider_, model=model_, progress=_progress,
            prev=prev_text, no_search=no_search, pov_mode=pov_mode,
            prev_outfit_text=prev_outfit_text, strict=strict_)
        # POV 才保留现有内容补正链路。
        _progress("编剧交稿，开始补正")
        try:
            fix_ = comic_agent.fix_script(brief, text_, provider=provider_,
                                          model=model_, progress=_progress,
                                          pov_mode=pov_mode, strict=strict_)
        except Exception as exc:            # 补正绝不许拖垮编剧（strict 换道除外）
            comic.log.exception("补正失败 job=%s", job)
            fix_ = {"text": text_, "issues": [], "diff": [], "adopted": False,
                    "status": "error", "warnings": ["补正异常：%s" % exc],
                    "audit": [], "skipped": False, "model": ""}
        return text_, fix_

    def _work():
        charged = False
        try:
            if qq:
                # 中转站优先：在线免费不限次；挂了才扣「代充次数」走站主官方，
                # 没次数报「找站长加次数」（错误文案进 job，前端原样显示）。
                (text, fix), charged = _friend_llm(
                    qq, "script", "剧本生成", _produce)
            else:
                text, fix = _produce(provider, model, False)
            # 目录**不再在这里建**：名字要带剧本标题，得等拿到稿子（见下面
            # `final` 那一段）。失败的 job 仍然不留空目录。

            # silver 自由路线已经在上面的分支完成唯一一次 API 请求；POV 分支
            # 的 fix 也已经在对应的 else 中完成。这里统一只负责落盘，不能再补正。
            final = fix.get("text") or text
            # 目录带标题（2026-10-18）：`C20261018-06_加了料的饮料`。标题只有
            # 拿到剧本才知道，所以**建目录这一步要等在这里**——原来那句更靠前
            # 的 makedirs 已挪下来。解析不出标题就退回纯编号，不影响落盘。
            title = ""
            try:
                meta, _ = comic.parse_script_text(final)
                title = (meta or {}).get("title", "")
            except Exception:               # 解析失败不该挡住落盘
                comic.log.warning("取标题失败 job=%s，目录退回纯编号", job)
            path = comic._next_task_dir(_COMICS_ROOT, title=title)
            os.makedirs(path, exist_ok=True)
            # 群友任务一建目录就钉归属。后面所有取图 / 任务列表 / QQ 直发都
            # 靠这个文件认主人，早一步写能挡住「编剧还没完成就来拉图」的窗口。
            if qq:
                comic_quota.set_owner(path, qq)
            with open(os.path.join(path, "script_original.md"), "w",
                      encoding="utf-8") as f:
                f.write(text + "\n")
            with open(os.path.join(path, "script.md"), "w", encoding="utf-8") as f:
                f.write(final + "\n")
            # brief 落盘：手动重跑补正时要拿它当「原始需求」。
            with open(os.path.join(path, "brief.txt"), "w", encoding="utf-8") as f:
                f.write(brief + "\n")
            # 视角模式落盘：之后单独触发的「重新补正」「修订助手」靠它判断该用哪套
            # 视角规则（不落盘它们就只能退回 pov，把双人剧本改回单人）。
            _write_pov_mode(path, pov_mode)
            # 上一集是谁也记一笔：下一集接着接的时候，下拉里能看出这条链。
            if prev:
                with open(os.path.join(path, "prev.txt"), "w",
                          encoding="utf-8") as f:
                    f.write(prev + "\n")
            report = _write_fix_report(path, fix)

            with _SCRIPT_LOCK:
                _SCRIPT_JOBS[job].update(state="done", script=final,
                                         original=text, task=os.path.basename(path),
                                         fix=report, pov_mode=pov_mode,
                                         # 自由视角才有：固定部分 + 逐格动态部分，
                                         # 界面按两栏显示（POV 是 None）。
                                         silver=fix.get("silver"))
            comic.log.info("编剧完成 job=%s → %s（补正 %s）", job,
                           os.path.basename(path),
                           "采纳" if fix.get("adopted") else "未采纳")
        except BaseException as exc:        # SystemExit 不是 Exception（见模块头）
            with _SCRIPT_LOCK:
                _SCRIPT_JOBS[job].update(state="error", error=str(exc))
            comic.log.exception("编剧失败 job=%s", job)
            # 只有真扣过代充次数的（中转站挂了走官方）才要退；中转站免费路径
            # 本来就没扣，别把账退成负数。
            if qq and charged:
                comic_quota.refund(qq, "script")

    threading.Thread(target=_work, daemon=True, name="comic-script").start()
    return jsonify({"ok": True, "job": job})


@bp.route("/api/comic/script_status", methods=["GET"])
def api_comic_script_status():
    """轮询编剧进度：state=running/done/error，log 是最近几条查库动作。"""
    job = str(request.args.get("job") or "").strip()
    with _SCRIPT_LOCK:
        j = _SCRIPT_JOBS.get(job)
        if j is None:
            return jsonify({"ok": False,
                            "error": "没有这个编剧任务（可能已过期）"}), 404
        if not _job_visible(j.get("task")):
            return jsonify({"ok": False, "error": "没有这个编剧任务"}), 404
        snap = dict(j, log=list(j["log"]))
    snap["ok"] = True
    return jsonify(snap)


@bp.route("/api/comic/save", methods=["POST"])
def api_comic_save():
    """用户编辑剧本后保存。场景/台词改没改不用管——load_state 的契约会自动
    把受影响的格退回正确状态（改场景→整格重来，改台词→只重加气泡）。"""
    data = request.get_json(silent=True) or {}
    try:
        path = _task_dir(data.get("task"))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    text = str(data.get("script") or "")
    if not text.strip():
        return jsonify({"ok": False, "error": "剧本不能为空"}), 400
    # 群友：格数上限不能在「保存」这一步被绕过去。手写一份 30 格剧本再点
    # 保存，/start 也会拦，但让坏本子先落盘只会让人白改一场，不如进门就说清。
    qq = _qq()
    if qq:
        try:
            _, peek = comic.parse_script_text(text)
        except Exception:
            peek = []
        cap = comic_quota.limits(qq)["max_panels"]
        if len(peek) > cap:
            return jsonify({"ok": False,
                            "error": "这一本 %d 格，超过你的 %d 格上限，"
                                     "找站主开更高档位" % (len(peek), cap)}), 403
    # 先落盘再解析：解析失败说明改坏了契约，回滚不让坏本子进管道。
    script_path = os.path.join(path, "script.md")
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(text if text.endswith("\n") else text + "\n")
    try:
        meta, panels = comic.parse_script(script_path)
        if not panels:
            raise ValueError("没解析到任何格子")
    except Exception as exc:
        return jsonify({"ok": False,
                        "error": "剧本格式不对（%s），请修正后再保存" % exc}), 400
    # 自由视角的「固定部分 / 动态部分」分栏要跟着一起存（2026-10-22）：
    # 分栏是从 fix.json 还原的，只更新 script.md 的话——用户改完保存、一刷新，
    # 分栏退回**生成时**的原稿，再点一次保存就把改动覆盖掉了。这条链必须闭合。
    silver = data.get("silver")
    if isinstance(silver, dict) and silver.get("fixed"):
        report = _read_fix_report(path) or {}
        report["silver"] = {
            "title": str(silver.get("title") or "连续漫画"),
            "fixed": str(silver.get("fixed") or ""),
            "dynamics": [str(x) for x in (silver.get("dynamics") or [])],
            "bubbles": silver.get("bubbles") or [],
        }
        try:
            _write_fix_report(path, dict(report))
        except Exception:
            comic.log.warning("分栏内容没写回 fix.json：%s", path)
    return jsonify({"ok": True, "panels": len(panels)})


@bp.route("/api/comic/start", methods=["POST"])
def api_comic_start():
    """确认开跑：后台线程跑阶段一（所有格的场景图）。scene_skill 可选覆盖，
    默认就是 comic.SCENE_SKILL（anima-line）。"""
    data = request.get_json(silent=True) or {}
    qq = _qq()
    try:
        path = _task_dir(data.get("task"))
        task = _load_task(path)                    # 开跑前先验一遍剧本可解析
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    need_charge = False
    if qq:
        lim = comic_quota.limits(qq)
        if len(task.panels) > lim["max_panels"]:
            return jsonify({"ok": False,
                            "error": "这一本 %d 格，超过你的 %d 格上限，"
                                     "找站主开更高档位" % (
                                         len(task.panels), lim["max_panels"])}), 403
        # 一本 = 一次生成机会，按任务幂等：同一本里换种子 / 改词 / 从底图重画
        # 都只吃 GPU 不花 Token，随便重跑，不再扣次数。
        need_charge = not comic_quota.charged(qq, "comic",
                                              os.path.basename(path))
        if need_charge:
            deny = _need("comic")
            if deny:
                return deny
    scene_skill = _skill_for(path, data.get("scene_skill"))
    _save_task_skill(path, scene_skill)
    # 独占通道（2026-11-11）：把待跑底图格一次性推上队列，前 comic.EXCLUSIVE_MAX
    # 张登记优先票压后所有已排队任务。站主靠开跑旁的勾选框显式选择；**群友在
    # anima-line 渠道自动走独占**（同日拍板）：群友页没有这个勾选框，参数卡全
    # 局默认 + 优先票让一本底图一口气跑完；别的渠道照旧逐格（NAI 云端本来就
    # 管不着独木桥顺序）。群友请求必带 qq（_qq() 注入），站主直连 qq 为空。
    if qq:
        exclusive = scene_skill == "anima-line"
    else:
        exclusive = bool(data.get("exclusive"))
    started = _spawn(path, "scene", lambda: comic.run(
        os.path.join(path, "script.md"), silver_only=True,
        scene_skill=scene_skill, exclusive=exclusive), on_error=_mark_pending_failed(path),
        finish="notify")
    if not started:
        return jsonify({"ok": False, "error": "这个任务已经在跑了"}), 409
    # 扣次放在启动成功之后：409 / 404 的调用不该白吃一次机会。
    if qq and need_charge:
        comic_quota.consume(qq, "comic")
        comic_quota.mark_charged(qq, "comic", os.path.basename(path))
    return jsonify({"ok": True, "scene_skill": scene_skill,
                    "exclusive": exclusive})


@bp.route("/api/comic/rerun", methods=["POST"])
def api_comic_rerun():
    """换渠道重跑全部：把每一格清回待渲染，按请求里的渠道整批重画阶段一。

    和 `/start` 只差一步：`/start` 会跳过已经出过图的格子（那是给「续跑」用的），
    而换渠道测效果时，那些旧图恰恰是要被替换掉的——所以这里先把 state 清回
    pending，`comic.run` 才会每格都重画。

    剧本一个字不动，阶段二（气泡）也不重画：这条链只回答「换个渠道长什么样」。
    正在跑的任务直接 409，不叠第二个线程。
    """
    data = request.get_json(silent=True) or {}
    try:
        path = _task_dir(data.get("task"))
        task = _load_task(path)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    skill = _skill_for(path, data.get("scene_skill"))
    _save_task_skill(path, skill)
    # 独占判定与 /start 同一条规则（2026-11-11）：站主看请求、群友在
    # anima-line 渠道自动独占——重跑同样是整本底图批跑，不给优先的话
    # 「一本底图一口气跑完」在重跑时不成立。这个端点原本只给站主页用，
    # 群友页从 5175 反代过来会带 qq（_qq() 注入），这里第一次用到。
    qq = _qq()
    if qq:
        exclusive = skill == "anima-line"
    else:
        exclusive = bool(data.get("exclusive"))
    for p in task.panels:
        p.state, p.seed, p.error = "pending", None, None
    task.scene_skill = skill
    task.save_state()
    started = _spawn(path, "scene", lambda: comic.run(
        os.path.join(path, "script.md"), silver_only=True,
        scene_skill=skill, exclusive=exclusive),
        on_error=_mark_pending_failed(path), finish="notify")
    if not started:
        return jsonify({"ok": False, "error": "这个任务已经在跑了，先中断再重跑"}), 409
    comic.log.info("换渠道重跑 %s：%d 格，渠道 %s%s", os.path.basename(path),
                   len(task.panels), skill, "（独占）" if exclusive else "")
    return jsonify({"ok": True, "panels": len(task.panels),
                    "scene_skill": skill, "exclusive": exclusive})


@bp.route("/api/comic/abort", methods=["POST"])
def api_comic_abort():
    """中断漫画台的渲染：正在画的那张停掉、排队中的全部摘掉、逐格循环停下。

    2026-10-22 用户要的：「点击中断直接打断漫画台的排队全部任务，我可以重新
    修改参数再重新跑」。三处必须一起收，少一处就"停不干净"：

      1. **逐格循环**——`comic.run` 的每格开头看这个 Event，置位就 break。
         不置位的话当前这张画完它还会接着画下一格；
      2. **排队中的 job**——`image_jobs.cancel_batch("comic")` 摘掉。不摘的
         话队列名额和「前面还有 N 张」还挂着，改完参数重跑会被自己劝退；
      3. **正在画的那一张**——它在 `chan.running` 里，队列里已经没有它了，
         只能通过 `job.cancel` 让 worker 的 `wait_done` 看到，走定向
         `/interrupt`（不带 prompt_id 的全局中断会劈掉 QQ 机器人正在画的图）。

    **已经出好的图一张都不动**：`rendered` / `done` 的格子照旧，重跑时会被
    跳过。被中断那一格的状态清回 `pending`（run 里做的），不留「出错」——
    中断不是失败，不该在格子上挂一个红字。

    不重启 ComfyUI、不动 QQ 机器人排的图：只是「这一批我不画了」。
    """
    data = request.get_json(silent=True) or {}
    try:
        path = _task_dir(data.get("task"))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404

    ev = comic.abort_event(path)
    task_running = ev is not None
    if ev is not None:
        ev.set()

    # 排队中：整批摘掉（含这个任务之前点「跑这一格」留下的残兵）
    dropped = image_jobs.cancel_batch("comic", "用户中断了整批渲染")

    # 正在跑：cancel_batch 摘不到（它已经被 worker 取走了），单独置位
    running = image_jobs.running_batch("comic")
    for job in running:
        job.cancel.set()

    with _JOBS_LOCK:
        phase = (_JOBS.get(path) or {}).get("phase")
    log_line = "中断漫画任务 %s：%s，摘掉队列 %d 张，正在画 %d 张" % (
        os.path.basename(path), "有后台线程" if task_running else "没有后台线程",
        dropped, len(running))
    comic.log.info(log_line)
    return jsonify({"ok": True,
                    # 线程要等 /interrupt 生效（秒级）才真正退出，前端拿这两个
                    # 标志决定提示语：还在收尾 vs 已经停了。
                    "task_running": task_running,
                    "phase": phase,
                    "dropped": dropped,
                    "running": len(running)})


@bp.route("/api/comic/status", methods=["GET"])
def api_comic_status():
    """槽位轮询。图片 URL 由前端拼 `&v=<rev>` 做失效；每格另带 `rev`（图片
    文件指纹），前端拿它判断「图被重写过」，否则就地覆盖的场景（手动跑 Qwen）
    刷不出来。rev 稳定时 URL 稳定 → 浏览器走本地缓存，重绘不再重下整本图。

    纯写真任务（生图模式新建、没有 script.md）也认：panels 空 + portraits 照返，
    前端靠它把刷新后的写真卡恢复出来。"""
    try:
        path = _task_dir(request.args.get("task"))
        task = _load_task(path)
    except ValueError as exc:
        # 只有「目录存在但没有剧本」这一种情况放行（纯写真任务）；目录不存在
        # 照样 404。
        try:
            path = _task_dir(request.args.get("task"))
        except ValueError:
            return jsonify({"ok": False, "error": str(exc)}), 404
        if not os.path.isfile(os.path.join(path, "script.md")):
            qq = _qq()
            return jsonify({"ok": True, "task": os.path.basename(path),
                            "title": "", "running": None, "job_error": None,
                            "queue": image_jobs.queue_depth(),
                            "quota": comic_quota.snapshot(qq) if qq else None,
                            "delivery": comic_delivery.view(path) if qq else None,
                            "comfy": _comfy_state(),
                            "fix": None, "panels": [],
                            "scene_skill": _resolve_skill(path),
                            "portraits": _portrait_shots(path)})
        return jsonify({"ok": False, "error": str(exc)}), 404
    with _JOBS_LOCK:
        running = dict(_JOBS.get(path) or {})
    panels = []
    for p in task.panels:
        item = {"no": p.no, "state": p.state, "seed": p.seed,
                "label": p.label, "bubble": p.bubble, "bubble2": p.bubble2,
                "speaker": p.speaker, "speaker2": p.speaker2,
                "scene": p.scene,
                # rev 给前端做失效判断：图被重写（手动跑 Qwen / 加气泡 / 传新底图）
                # 后它必变，见 _rev() 的注释。
                "rev": "%s/%s" % (_rev(task.raw_path(p)), _rev(task.final_path(p))),
                "error": p.error,
                # 耗时（2026-11-11）：两阶段各 exec/wall 两个口径，老任务缺值
                # 为 None → 前端显示「—」。
                "scene_exec": p.scene_exec, "scene_wall": p.scene_wall,
                "bubble_exec": p.bubble_exec, "bubble_wall": p.bubble_wall}
        if os.path.isfile(task.raw_path(p)):
            item["scene_url"] = _img_url(task.task_id, "raw/%s_raw.png" % p.no,
                                         comic_thumb.rel(p.no, "raw"))
        if os.path.isfile(task.final_path(p)):
            item["final_url"] = _img_url(task.task_id, "final/%s_final.png" % p.no,
                                         comic_thumb.rel(p.no, "final"))
        panels.append(item)
    # 两段耗时汇总（2026-11-11）：各格求和 = 该阶段总耗时（漫画台是串行逐格
    # 提交，求和就约等于阶段墙钟，且单格重跑 / 断点续跑 / 单独加气泡都能正确
    # 反映）。exec 口径：只要有任意一格拿不到纯执行耗时，整段 exec 就标 None
    # ——宁可显示「—」，也不给一个「只算了一半格」的虚高数字。wall 同理带
    # 样本数 n，前端能显示「N 格合计」避免误读成整本。
    def _sum_stage(stage):
        ex = [getattr(p, stage + "_exec") for p in task.panels
              if isinstance(getattr(p, stage + "_exec"), (int, float))]
        wl = [getattr(p, stage + "_wall") for p in task.panels
              if isinstance(getattr(p, stage + "_wall"), (int, float))]
        return {"exec": round(sum(ex), 2) if len(ex) == len(task.panels) and ex
                else None,
                "wall": round(sum(wl), 2) if wl else None,
                "n": len(wl)}

    timing = {"scene": _sum_stage("scene"), "bubble": _sum_stage("bubble")}
    qq = _qq()
    return jsonify({"ok": True, "task": task.task_id,
                    "timing": timing,
                    # 全站共用的串行队列里还有几张在等，群友页顶部显示给用户。
                    # 漫画台本来就是单张串行提交，不会一次占满 10 个名额。
                    "queue": image_jobs.queue_depth(),
                    "quota": comic_quota.snapshot(qq) if qq else None,
                    "delivery": comic_delivery.view(path) if qq else None,
                    "title": task.meta.get("title", ""),
                    "running": running.get("phase"),
                    "job_error": running.get("error"),
                    # ComfyUI 在不在（页面顶部那行小字）。在等它重启的时候，
                    # 用户得知道「是在等 ComfyUI」还是「界面卡死了」。
                    "comfy": _comfy_state(),
                    # 补正报告（编剧之后那一步）落盘在 fix.json：页面拿它显示
                    # 「改了哪几格、改了什么」，没有这个文件就是老任务，返回 null。
                    "fix": _read_fix_report(path),
                    # 生图模式的写真（portraits/ + prompts.json）：刷新页面后
                    # 写真卡靠它恢复。没有就是空列表。
                    "portraits": _portrait_shots(path),
                    # 阶段一渠道（2026-10-19）：前端靠它把下拉框同步成任务真实用的
                    # 那个——刷新页面、切换任务都不会再显示成默认 anima-line。
                    "scene_skill": _resolve_skill(path),
                    "panels": panels})


@bp.route("/api/comic/fix", methods=["POST"])
def api_comic_fix():
    """重跑补正：**从编剧原稿（script_original.md）重来一遍**，直接落地。

    为什么底稿用原稿而不是当前 script.md：补正是脚本化的一步，重跑要幂等。
    拿补正过的稿再补一次，会把上一轮的改动当成"原稿"来比对，diff 越滚越乱，
    而且第二次的「未点名格不许动」判据会把上一轮的成果当成越权改动。所以底稿
    永远取 `script_original.md`（没有它就退回 script.md，兼容老任务目录）。

    异步跑（补正实测 9~80 秒，同步会堵死槽位轮询），前端轮询 /status 看结果——
    状态里带 fix.json。
    """
    data = request.get_json(silent=True) or {}
    qq = _qq()
    try:
        path = _task_dir(data.get("task"))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    # 补正要再跑一次编剧模型。群友计费同 gen_script（2026-11-11）：中转站在线
    # 免费，挂了才在 _work 里查/扣代充次数走站主官方；provider/model 对群友忽略。
    provider = str(data.get("provider") or "").strip() or None
    model = str(data.get("model") or "").strip() or None

    orig_path = os.path.join(path, "script_original.md")
    if not os.path.isfile(orig_path):
        orig_path = os.path.join(path, "script.md")
    with open(orig_path, encoding="utf-8") as f:
        original = f.read()
    brief = ""
    bf = os.path.join(path, "brief.txt")
    if os.path.isfile(bf):
        with open(bf, encoding="utf-8") as f:
            brief = f.read().strip()
    brief = str(data.get("brief") or "").strip() or brief
    if not brief:
        return jsonify({"ok": False,
                        "error": "这个任务没存原始需求（老目录缺 brief.txt），"
                                 "请在请求里带上 brief"}), 400

    def _fix_once(provider_, model_, strict_):
        return comic_agent.fix_script(
            brief, original, provider=provider_, model=model_, strict=strict_,
            progress=lambda m: comic.log.info("[comic-fix] %s", m),
            pov_mode=_read_pov_mode(path))

    def _work():
        charged = False
        try:
            if qq:
                fix, charged = _friend_llm(qq, "revise", "修订助手", _fix_once)
            else:
                fix = _fix_once(provider, model, False)
            final = fix.get("text") or original
            with open(os.path.join(path, "script.md"), "w", encoding="utf-8") as f:
                f.write(final + "\n")
            _write_fix_report(path, fix)
            comic.log.info("补正重跑完成 %s（%s）", os.path.basename(path),
                           "采纳" if fix.get("adopted") else "未采纳")
        except BaseException:
            # _friend_llm 在官方也失败时已经退过；这里兜的是「LLM 成功、落盘
            # 失败」——扣了次数却什么都没落地，退回才对。
            if charged:
                comic_quota.refund(qq, "revise")
            raise

    # 用 _spawn 而不是自己开线程：状态里的 `running: "fix"` 和「同一任务只允许
    # 一个后台线程」都是现成的，跟 rerender / bubble / manual 一个口径。
    if not _spawn(path, "fix", _work):
        return jsonify({"ok": False, "error": "这个任务已经有别的事在跑"}), 409
    return jsonify({"ok": True, "task": os.path.basename(path)})


@bp.route("/api/comic/script_text", methods=["GET"])
def api_comic_script_text():
    """读当前剧本正文（script.md）与编剧原稿（script_original.md）。

    **存在的理由**：重跑补正会就地改写 script.md，而页面 textarea 里还是旧文本
    ——用户随后点「确认开跑」会把旧文本存回去，**把补正结果整个覆盖掉**。
    所以补正落地后必须重新拉一次正文。status 里不带全文（每 2 秒轮询一次，
    没必要一直传几 KB），需要时才来取。
    """
    try:
        path = _task_dir(request.args.get("task"))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    out = {"ok": True, "pov_mode": _read_pov_mode(path)}
    for key, name in (("script", "script.md"), ("original", "script_original.md")):
        fp = os.path.join(path, name)
        if os.path.isfile(fp):
            with open(fp, encoding="utf-8") as f:
                out[key] = f.read()
    return jsonify(out)


def _render_resources():
    """ComfyUI 可用清单（LoRA / 采样器 / 调度器 / 放大模型），给渲染参数卡做下拉。

    拉不到（ComfyUI 没开 / 超时）就返回空清单 + online=False：界面退化成自由
    输入，后端也**不做名单校验**——没有证据源就不替调用方猜（用户 2026-10-12
    拍板的哲学：代码只做检测，缺证据时别当闸门）。名字写错的后果是那一格渲染
    失败、错误明文显示在格子里，看得见摸得着，比悄悄拒掉好。
    """
    out = {"loras": [], "samplers": [], "schedulers": [], "upscales": [],
           "online": False}
    try:
        from app.tools.normal import comfy_workflow as cw
        r = cw.resource_list()
        out.update(loras=r.get("loras") or [],
                   samplers=r.get("samplers") or [],
                   schedulers=r.get("schedulers") or [],
                   upscales=r.get("upscales") or [],
                   online=True)
    except Exception as e:
        comic.log.info("拉取 ComfyUI 资源清单失败（%s），渲染参数按自由输入处理", e)
    return out


@bp.route("/api/comic/render_opts", methods=["GET"])
def api_comic_render_opts_get():
    """这个任务当前的渲染参数 + ComfyUI 可用清单。**不带 task = 只看全局默认**。

    返回值永远是「现在拿去跑会用什么」（任务 render.json > 全局默认 >
    anima-line 模板当前值，见 comic_render_opts.load）。存没存过用两个标志
    区分：`saved` = 本任务存过，`defaults_saved` = 全局默认存过。
    """
    task_id = (request.args.get("task") or "").strip()
    path = ""
    if task_id:
        try:
            path = _task_dir(task_id)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
    skill = _skill_for(path, request.args.get("skill"))
    return jsonify({
        "ok": True,
        "skill": skill,
        # caps = 这个渠道让参数卡显示什么（几段采样 / 有没有 LoRA / 预计出图多大）
        "caps": comic_render_opts.caps_of(skill),
        "opts": comic_render_opts.load(path, skill),
        "resources": _render_resources(),
        "saved": bool(path) and os.path.isfile(comic_render_opts.path_of(path, skill)),
        "defaults_saved": comic_render_opts.has_defaults(skill),
    })


@bp.route("/api/comic/render_opts", methods=["POST"])
def api_comic_render_opts_save():
    """保存渲染参数：`scope="task"`（默认，写任务目录 render.json）或
    `scope="global"`（写 state/comic_render_defaults.json，**所有任务通用**）；
    `reset=true` 按同一个 scope 删掉对应那份。

    校验在 normalize 里：宽高不是 8 的倍数、LoRA/采样器 ComfyUI 里没有，这些是
    事实问题，直接 400 把话说清；越界的数字钳位 + 回 warnings（给人填的表，
    钳位比拒掉友好）。保存只影响**之后**渲染的格——已出的图不动。
    """
    data = request.get_json(silent=True) or {}
    qq = _qq()
    task_id = (data.get("task") or "").strip()
    scope = str(data.get("scope") or "task").strip().lower()
    if scope not in ("task", "global"):
        scope = "task"
    # 群友版：只能改自己这一本的画幅。不许碰「全局默认」（那会把站主的
    # 出图参数一起改掉），也不给 reset（reset 会删掉强制跳过放大的痕迹，
    # 而且群友版根本没有这个按钮）。
    if qq:
        scope = "task"
        if data.get("reset"):
            return jsonify({"ok": False, "error": "群友版不支持重置渲染参数"}), 403
    path = ""
    if task_id:
        try:
            path = _task_dir(task_id)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
    elif scope == "task" and not data.get("reset"):
        return jsonify({"ok": False, "error": "缺 task"}), 400

    skill = _skill_for(path, data.get("skill"))
    if data.get("reset"):
        if scope == "global":
            comic_render_opts.reset_defaults(skill)
        else:
            comic_render_opts.reset(path, skill)
        return jsonify({"ok": True, "reset": True, "scope": scope, "skill": skill,
                        "caps": comic_render_opts.caps_of(skill),
                        "opts": comic_render_opts.load(path, skill),
                        "saved": bool(path) and
                                 os.path.isfile(comic_render_opts.path_of(path, skill)),
                        "defaults_saved":
                            comic_render_opts.has_defaults(skill)})

    raw_opts = data.get("opts") or {}
    if qq:
        # 只认宽高，其余（LoRA / 采样 / 放大模型）一律沿用当前生效值。
        # 画幅预设由前端换成 width/height 数字发过来，服务端不信任别的字段。
        raw_opts = {"width": raw_opts.get("width"),
                    "height": raw_opts.get("height")}
    # base 传当前生效值：前端只提交**看得见**的字段（非 LoRA 渠道不提交 loras、
    # 单段渠道不提交 hires），缺的部分要沿用，不能变成空。
    opts, errors, warnings = comic_render_opts.normalize(
        raw_opts, base=comic_render_opts.load(path, skill),
        resources=_render_resources())
    if errors:
        return jsonify({"ok": False, "error": "；".join(errors)}), 400
    if qq:
        # 最后再钉一次：不管前端发了什么、render.json 里存了什么，群友任务
        # 落盘的参数必须跳过最终 2x 放大。`load()` 兜的是「没存过参数」，
        # 这里兜的是「存过之后又被手改」的情况。
        opts.setdefault("upscale", {})["final_skip"] = True
    if scope == "global":
        comic_render_opts.save_defaults(opts, skill)
    else:
        comic_render_opts.save(path, opts, skill)
    return jsonify({"ok": True, "opts": opts, "warnings": warnings,
                    "scope": scope, "skill": skill,
                    "caps": comic_render_opts.caps_of(skill),
                    "saved": bool(path) and
                             os.path.isfile(comic_render_opts.path_of(path, skill)),
                    "defaults_saved":
                        comic_render_opts.has_defaults(skill)})


@bp.route("/api/comic/channels", methods=["GET"])
def api_comic_channels():
    """参数卡的渠道下拉：哪些渠道能当阶段一 + 这个任务现在用的是哪个。

    群友只给 anima-line 一个（2026-11-11 用户拍板：最快的当默认，其他先隐藏；
    服务端也收敛，跟 _skill_for 同一条规则）。站主多渠道照旧。
    """
    task_id = (request.args.get("task") or "").strip()
    path = ""
    if task_id:
        try:
            path = _task_dir(task_id)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
    chans = _channels()
    if _qq():
        chans = [c for c in chans if c["name"] == FRIEND_SKILL] or \
                [{"name": FRIEND_SKILL, "group": _group_of(FRIEND_SKILL)}]
    return jsonify({"ok": True, "channels": chans,
                    "current": _resolve_skill(path)})


@bp.route("/api/comic/channel", methods=["POST"])
def api_comic_channel():
    """换这个任务的阶段一渠道（只影响**之后**渲染的格，已出的图不动）。"""
    data = request.get_json(silent=True) or {}
    task_id = str(data.get("task") or "").strip()
    skill = str(data.get("skill") or "").strip()
    if _qq():
        # 群友没有换渠道：传什么都落回 anima-line（前端下拉已隐藏，这里兜住
        # API 直调；_channels 一定含 anima-line，下面的白名单校验天然通过）。
        skill = FRIEND_SKILL
    if not skill:
        return jsonify({"ok": False, "error": "缺 skill"}), 400
    path = ""
    if task_id:
        try:
            path = _task_dir(task_id)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
    names = {c["name"] for c in _channels()}
    if skill not in names:
        return jsonify({"ok": False,
                        "error": "没有这个生图渠道：%s" % skill}), 400
    _save_task_skill(path, skill)
    return jsonify({"ok": True, "skill": skill,
                    "caps": comic_render_opts.caps_of(skill),
                    "opts": comic_render_opts.load(path, skill),
                    "saved": bool(path) and
                             os.path.isfile(comic_render_opts.path_of(path, skill))})


@bp.route("/api/comic/rerender", methods=["POST"])
def api_comic_rerender():
    """换种子重跑某格：把该格重置回 pending（种子作废），后台只跑阶段一。
    seed 不传新的——render_scene 里 _new_seed() 每次现抽。

    **渠道以请求里带的为准**（2026-10-22 修）：前端把下拉选中的渠道传上来，
    和 `/start` 同一个口径。原来这里不传、只靠 state.json 里记的，于是出现
    过「界面显示宝可梦、实际用 anima-line 画」——任务目录还没有 state.json
    （新任务刚出剧本）或者那份记录是旧的，`comic.run` 就静默回落到全局默认
    anima-line，用户看到的是一格风格完全不对的图。
    """
    data = request.get_json(silent=True) or {}
    try:
        path = _task_dir(data.get("task"))
        task = _load_task(path)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    no = str(data.get("no") or "").strip().zfill(2)
    panel = next((p for p in task.panels if p.no == no), None)
    if panel is None:
        return jsonify({"ok": False, "error": "没有 %s 这格" % no}), 404
    skill = _skill_for(path, data.get("scene_skill"))
    panel.state, panel.seed, panel.error = "pending", None, None
    task.scene_skill = skill          # save_state 会把它写回 state.json
    task.save_state()
    # 换种子重跑（2026-11-11 用户拍板「先删再跑、新图占原编号」）：
    # 旧图在**后台线程确实启动之后、开跑之前**清掉。放进 fn 而不是请求线程，
    # 是为了「任务已在跑 → 409」时一张图都不删；`comic.run` 自己会探活，
    # 不在线就整格标 error，此时该编号保持空，绝不拿上一版冒充新图。
    # 删完这一格只剩同编号的新底图，final 要再点「加气泡」。
    def _run():
        comic.discard_numbered_images(path, no, ("raw", "final"))
        comic.run(os.path.join(path, "script.md"), only={no},
                  silver_only=True, scene_skill=skill)

    started = _spawn(path, "scene", _run,
                     on_error=_mark_pending_failed(path, no))
    if not started:
        return jsonify({"ok": False, "error": "这个任务已经在跑了"}), 409
    return jsonify({"ok": True, "no": no, "scene_skill": skill})


# ─── 生图模式（单人写真集，2026-10-13）──────────────────

def _portrait_task_dir(data):
    """task 给了就校验存在；没给就新建一个（写真不依附剧本任务）。"""
    task_id = str(data.get("task") or "").strip()
    if task_id:
        return _task_dir(task_id)
    path = comic._next_task_dir(_COMICS_ROOT)
    os.makedirs(path, exist_ok=True)
    return path


@bp.route("/api/comic/portrait", methods=["POST"])
def api_comic_portrait():
    """生图模式：提示词员写 N 条单人写真提示词 → 逐条出图（后台线程）。

    与剧本模式共用 ① 卡的输入框和模型选择，但提示词规则完全另一套（无 POV、
    无剧情、无观众，见 app/comic_portrait 模块头）。task 不给就新建任务目录
    ——没有漫画任务也能直接跑写真。异步：写提示词 + N 张图要几分钟，同步会
    堵死轮询（和编剧同一个理由）。
    """
    data = request.get_json(silent=True) or {}
    qq = _qq()
    brief = str(data.get("brief") or "").strip()
    if not brief:
        return jsonify({"ok": False, "error": "想拍什么总得写一句"}), 400
    try:
        n = int(data.get("n") or 4)
    except (TypeError, ValueError):
        n = 4
    n = max(1, min(comic_portrait.MAX_SHOTS, n))
    if qq:
        n = min(n, comic_quota.limits(qq)["max_shots"])
    heroine = str(data.get("heroine") or "").strip()
    provider = str(data.get("provider") or "").strip() or None
    model = str(data.get("model") or "").strip() or None
    if qq:
        provider, model = comic_quota.resolve_model(provider, model)

    try:
        path = _portrait_task_dir(data)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    with _PORTRAIT_LOCK:
        busy = any(j.get("state") == "running" and j.get("task") == path
                   for j in _PORTRAIT_JOBS.values())
    if busy:
        return jsonify({"ok": False, "error": "这个任务的写真还在跑，等它跑完"}), 409
    charged_now = False
    if qq:
        comic_quota.set_owner(path, qq)
        tid = os.path.basename(path)
        if not comic_quota.charged(qq, "portrait", tid):
            deny = _need("portrait")
            if deny:
                return deny
            comic_quota.consume(qq, "portrait")
            comic_quota.mark_charged(qq, "portrait", tid)
            charged_now = True

    # 阶段一渠道（2026-10-19）：跟剧本模式同一套解析，写真也用参数卡选的那个。
    skill = _resolve_skill(path, data.get("scene_skill"))
    _save_task_skill(path, skill)

    job = uuid.uuid4().hex[:12]
    with _PORTRAIT_LOCK:
        _PORTRAIT_JOBS[job] = {"state": "running", "task": path, "log": [],
                               "error": None}
        for k in [k for k, v in _PORTRAIT_JOBS.items()
                  if v["state"] != "running"][:-_PORTRAIT_KEEP]:
            _PORTRAIT_JOBS.pop(k, None)

    def _progress(msg):
        with _PORTRAIT_LOCK:
            j = _PORTRAIT_JOBS.get(job)
            if j is not None:
                j["log"].append(msg)
                del j["log"][:-8]

    def _work():
        try:
            comic_portrait.run(path, brief, heroine, n, provider=provider,
                               model=model, progress=_progress, skill=skill)
            with _PORTRAIT_LOCK:
                _PORTRAIT_JOBS[job].update(state="done")
            if qq:
                comic_delivery.spawn_notify(path, qq, "portrait")
        except BaseException as exc:        # SystemExit 不是 Exception（见模块头）
            with _PORTRAIT_LOCK:
                _PORTRAIT_JOBS[job].update(state="error", error=str(exc))
            comic.log.exception("写真批次失败 job=%s", job)
            if qq and charged_now:
                # 只有这次新扣的机会才退；同一本换种子重跑失败不该白送一次。
                comic_quota.refund(qq, "portrait")

    threading.Thread(target=_work, daemon=True, name="comic-portrait").start()
    return jsonify({"ok": True, "job": job, "task": os.path.basename(path)})


@bp.route("/api/comic/portrait_status", methods=["GET"])
def api_comic_portrait_status():
    """轮询写真批次：state + log + shots（账本逐条落盘，进度实时）。"""
    job = str(request.args.get("job") or "").strip()
    with _PORTRAIT_LOCK:
        j = _PORTRAIT_JOBS.get(job)
        if j is None:
            return jsonify({"ok": False,
                            "error": "没有这个写真任务（可能已过期）"}), 404
        if not _job_visible(j.get("task")):
            return jsonify({"ok": False, "error": "没有这个写真任务"}), 404
        snap = dict(j, log=list(j["log"]))
    snap["shots"] = _portrait_shots(snap.get("task"))
    snap["ok"] = True
    return jsonify(snap)


def _portrait_shots(path):
    """账本 + 图 URL + 文件指纹（前端逐张 diff 刷新，签名不变不重建 DOM）。"""
    if not path:
        return []
    out = []
    for s in comic_portrait.load_prompts(path):
        no = str(s.get("no") or "").zfill(2)
        fp = comic_portrait.shot_path(path, s.get("no"))
        item = dict(s)
        item["url"] = _img_url(os.path.basename(path),
                               "portraits/%s.png" % no,
                               comic_thumb.rel(no, "portrait"))
        item["rev"] = _rev(fp)
        out.append(item)
    return out


@bp.route("/api/comic/portrait_shot", methods=["POST"])
def api_comic_portrait_shot():
    """换种子重跑某一张写真：提示词不变，种子重抽。

    同步跑（一张图几十秒，前端按钮转圈等）——和 rerender 走后台不同：这里
    没有「整批状态」要维护，prompts.json 落盘即完事。
    """
    data = request.get_json(silent=True) or {}
    try:
        path = _task_dir(data.get("task"))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    with _PORTRAIT_LOCK:
        busy = any(j.get("state") == "running" and j.get("task") == path
                   for j in _PORTRAIT_JOBS.values())
    if busy:
        return jsonify({"ok": False, "error": "这批写真还在跑，等它跑完再单张重跑"}), 409
    no = str(data.get("no") or "").strip().zfill(2)
    try:
        shot = comic_portrait.reseed_shot(
            path, no, skill=_resolve_skill(path, data.get("scene_skill")))
    except comic_portrait.PortraitError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    fp = comic_portrait.shot_path(path, no)
    # 单张重跑只吃 GPU：不再自动发 zip，等用户检查完自己点「发送 zip」。
    return jsonify({"ok": True, "shot": dict(
        shot, url=_img_url(os.path.basename(path),
                           "portraits/%s.png" % no,
                           comic_thumb.rel(no, "portrait")),
        rev=_rev(fp))})


@bp.route("/api/comic/chat_revise", methods=["POST"])
def api_comic_chat_revise():
    """和修订助手聊一句，改剧本 / 答疑。

    用户 2026-10-12：补正只治代码清单里的问题，作者自己的小修小改（加长发、
    背景换暖色）没有入口。这里给一个聊天框：作者说一句话 → comic_agent.chat_revise
    要么交回改好的剧本（**直接落地 script.md**，改前整份备份到 script_chat_backup.md，
    「撤销对话改动」可回退一级），要么回一段答复（剧本一字不动）。

    异步跑（一次 LLM 调用，几秒到一分多钟；Flask 单线程，同步会堵死槽位轮询），
    前端拿返回的 job 轮询 /chat_status。复用 _spawn 的互斥：渲染 / 补正还在跑时
    拒绝改稿——改稿落地会把正在渲染的格偷偷换掉，等跑完再说。
    """
    data = request.get_json(silent=True) or {}
    qq = _qq()
    try:
        path = _task_dir(data.get("task"))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    # 群友计费同 gen_script（2026-11-11）：中转站在线免费不限次；挂了才在
    # _work 里查/扣代充次数走站主官方。provider/model 对群友忽略（不给自带 key）。
    message = str(data.get("message") or "").strip()
    if not message:
        return jsonify({"ok": False, "error": "先写一句话再发送"}), 400
    provider = str(data.get("provider") or "").strip() or None
    model = str(data.get("model") or "").strip() or None

    with _JOBS_LOCK:
        running = (_JOBS.get(path) or {}).get("phase")
    if running:
        return jsonify({"ok": False,
                        "error": "这个任务正在跑（%s），等它结束再聊" % running}), 409

    script_path = os.path.join(path, "script.md")
    if not os.path.isfile(script_path):
        return jsonify({"ok": False, "error": "任务里没有 script.md"}), 404
    with open(script_path, encoding="utf-8") as f:
        script = f.read()
    brief = ""
    bf = os.path.join(path, "brief.txt")
    if os.path.isfile(bf):
        with open(bf, encoding="utf-8") as f:
            brief = f.read().strip()
    history = _read_chat_history(path)

    # 结算：中转站路径没扣过账；官方代充路径扣了 1 次，但「修订助手真的改了
    # 剧本才扣」的口径保留——反问/答疑/落地失败都退回去。扣发生在 _work 线程里
    #（中转站挂了才知道要走官方），原子的 quota RLock 保证并发扣不超。
    charged = {"v": False}
    settled = {"done": False}

    def _settle(landed):
        if settled["done"]:
            return
        settled["done"] = True
        if qq and charged["v"] and not landed:
            comic_quota.refund(qq, "revise")

    job = uuid.uuid4().hex[:12]
    with _CHAT_LOCK:
        # 同一任务同时只许一句话在跑：上面 _JOBS 只挡渲染，不挡两个改稿线程
        # 互相覆盖 script.md。放在锁里判，避免并发请求一起通过。
        if any(j.get("state") == "running" and
               j.get("task") == os.path.basename(path)
               for j in _CHAT_JOBS.values()):
            _settle(False)
            return jsonify({"ok": False, "error": "上一句还在改，等它回完再发"}), 409
        _CHAT_JOBS[job] = {"state": "running", "reply": "", "changed": False,
                           "diff": [], "warnings": [], "error": None,
                           "task": os.path.basename(path), "at": None,
                           "charged": False}
        for k in [k for k, v in _CHAT_JOBS.items()
                  if v["state"] != "running"][:-_CHAT_KEEP]:
            _CHAT_JOBS.pop(k, None)

    def _chat_once(provider_, model_, strict_):
        return comic_agent.chat_revise(script, message, brief=brief,
                                       history=history[-8:],   # 最近 4 轮够用了
                                       provider=provider_, model=model_,
                                       strict=strict_,
                                       pov_mode=_read_pov_mode(path))

    def _work():
        if qq:
            r, used = _friend_llm(qq, "revise", "修订助手", _chat_once)
            charged["v"] = used
        else:
            r = _chat_once(provider, model, False)
        reply = r.get("reply") or ""
        landed = bool(r.get("changed") and r.get("script"))
        if landed:
            # 单级回退：改前整份备份。改两次只有最近一次可撤销——再早的用
            # 「只保存」+ 手改都能到，聊天框的定位是小修小改，不是版本管理。
            with open(script_path, encoding="utf-8") as f:
                prev = f.read()
            with open(os.path.join(path, "script_chat_backup.md"), "w",
                      encoding="utf-8") as f:
                f.write(prev)
            with open(script_path, "w", encoding="utf-8") as f:
                f.write(r["script"] + "\n")
            comic.log.info("对话改稿落地 %s（%s）", os.path.basename(path),
                           "、".join(d["no"] for d in r.get("diff", [])))
        # 落实判定只认「剧本真的写回磁盘」。在这之后就算追加聊天历史出错，
        # 也不该把已落地的改动退成免费。
        _settle(landed)
        _append_chat_history(path, message, reply)
        with _CHAT_LOCK:
            _CHAT_JOBS[job].update(
                state="done", reply=reply, changed=landed, charged=bool(
                    qq and charged["v"] and landed),
                diff=r.get("diff") or [], warnings=r.get("warnings") or [],
                at=time.strftime("%H:%M:%S"))

    def _runner():
        try:
            _work()
        except BaseException as exc:        # SystemExit 不是 Exception
            comic.log.exception("对话改稿失败 %s", path)
            _settle(False)
            with _CHAT_LOCK:
                _CHAT_JOBS[job].update(state="error", error=str(exc))

    threading.Thread(target=_runner, daemon=True, name="comic-chat").start()
    return jsonify({"ok": True, "job": job})


@bp.route("/api/comic/chat_status", methods=["GET"])
def api_comic_chat_status():
    """轮询一句话的修订结果。changed=true 时前端要把剧本 textarea 刷新成落地的
    新稿（和 refix 一个道理，不然接着「确认开跑」会把旧文本存回去）。"""
    job = str(request.args.get("job") or "").strip()
    with _CHAT_LOCK:
        j = _CHAT_JOBS.get(job)
        if j is None:
            return jsonify({"ok": False,
                            "error": "没有这个对话任务（可能已过期）"}), 404
        if not _job_visible(j.get("task")):
            return jsonify({"ok": False, "error": "没有这个对话任务"}), 404
        snap = dict(j)
    snap["ok"] = True
    # 群友页轮询完这句话就顺手拿最新余额，不用再多发一个请求。
    # 「这次有没有扣」由 job 里的 charged 告诉前端：反问/答疑是不扣的。
    qq = _qq()
    if qq:
        snap["quota"] = comic_quota.snapshot(qq)
    return jsonify(snap)


@bp.route("/api/comic/chat_history", methods=["GET"])
def api_comic_chat_history():
    """整个任务的对话历史（刷新页面后把聊天框显示回来）+ 有没有可撤销的备份。"""
    try:
        path = _task_dir(request.args.get("task"))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    return jsonify({"ok": True, "history": _read_chat_history(path),
                    "backup": os.path.isfile(
                        os.path.join(path, "script_chat_backup.md"))})


@bp.route("/api/comic/chat_undo", methods=["POST"])
def api_comic_chat_undo():
    """撤销最近一次对话改稿：script_chat_backup.md → script.md，备份删掉
    （单级回退，撤完就没有了，再点提示无可撤销）。"""
    data = request.get_json(silent=True) or {}
    try:
        path = _task_dir(data.get("task"))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    backup = os.path.join(path, "script_chat_backup.md")
    if not os.path.isfile(backup):
        return jsonify({"ok": False, "error": "没有可撤销的对话改动"}), 404
    with _JOBS_LOCK:
        running = (_JOBS.get(path) or {}).get("phase")
    if running:
        return jsonify({"ok": False,
                        "error": "这个任务正在跑（%s），等它结束再撤" % running}), 409
    with open(backup, encoding="utf-8") as f:
        prev = f.read()
    with open(os.path.join(path, "script.md"), "w", encoding="utf-8") as f:
        f.write(prev)
    os.remove(backup)
    comic.log.info("对话改稿已撤销 %s", os.path.basename(path))
    return jsonify({"ok": True, "script": prev})


@bp.route("/api/comic/edit_scene", methods=["POST"])
def api_comic_edit_scene():
    """直接改某格的画面提示词（`画面:` 行）并重画这一格。

    用户 2026-10-12：补正把词改坏时（删了 navy pleated skirt，裙子消失），
    他要能「只改一个词就重画这一格」，而不是整份剧本重跑。流程：
      1. `comic.update_panel_scene` 只替换那一格的画面行，别的格一字不动；
      2. 后台只跑该格——load_state 的契约会因为 scene 变化自动把它退回
         pending、旧图作废，气泡后续照常加。
    任务正在跑时返回 409（剧本已改好，等当前跑完再点一次重跑即可）。
    """
    data = request.get_json(silent=True) or {}
    try:
        path = _task_dir(data.get("task"))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    no = str(data.get("no") or "").strip().zfill(2)
    scene = str(data.get("scene") or "").strip()
    if not scene:
        return jsonify({"ok": False, "error": "画面提示词不能为空"}), 400
    script_path = os.path.join(path, "script.md")
    try:
        if not comic.update_panel_scene(script_path, no, scene):
            return jsonify({"ok": False, "error": "没有 %s 这格（或它没有画面行）" % no}), 404
        # 改完解析一遍：改坏契约（画面行丢了/格子解析不出）就不进管道
        meta, panels = comic.parse_script(script_path)
        panel = next((p for p in panels if p["no"] == no), None)
        if panel is None or not panel["scene"].strip():
            raise ValueError("改完解析不到该格的画面行")
    except Exception as exc:
        return jsonify({"ok": False,
                        "error": "画面提示词改坏了（%s），请修正后再保存" % exc}), 400
    # 渠道跟 rerender 同一个口径：前端传什么就用什么（见那边注释）。
    skill = _resolve_skill(path, data.get("scene_skill"))
    started = _spawn(path, "scene", lambda: comic.run(
        script_path, only={no}, silver_only=True, scene_skill=skill),
        on_error=_mark_pending_failed(path, no))
    if not started:
        return jsonify({"ok": True, "no": no, "queued": False,
                        "notice": "任务正在跑，改动已保存；跑完后点「换种子重跑」生效"})
    return jsonify({"ok": True, "no": no, "queued": True})


@bp.route("/api/comic/prompt", methods=["GET"])
def api_comic_prompt():
    """把「这一格原样发给 qwen 的那段文字」交出来，供界面显示 + 手改。

    返回的就是 `Panel.bubble_prompt()` 的输出，**不加任何预览用的包装**——用户
    2026-10-11 明确要求「看到完整的发给 qwen 的文字，并进行修改」，所以界面上
    看到的一字不差就是提交给 ComfyUI 的那段。

    split 跟界面上的画法下拉一致（否则他看到的是 A 段、实际跑的是 B 段——一格
    有没有第二句台词，拼出来的文字是两套模板）。
    """
    try:
        path = _task_dir(request.args.get("task"))
        task = _load_task(path)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    no = str(request.args.get("no") or "").strip().zfill(2)
    panel = next((p for p in task.panels if p.no == no), None)
    if panel is None:
        return jsonify({"ok": False, "error": "没有 %s 这格" % no}), 404
    split = str(request.args.get("split") or "") not in ("", "0", "false", "False")
    return jsonify({"ok": True, "no": no, "split": split,
                    "prompt": panel.bubble_prompt(split=split)})


@bp.route("/api/comic/bubble", methods=["POST"])
def api_comic_bubble():
    """加气泡（阶段二，qwen 图生图）。no=单格；no=all → 全部「待加气泡」
    （rendered）的格。

    单格允许 done：那等于「用剧本里的提示词，从**底图**重画这一格的气泡」——
    底图保留，所以它是手动改坏之后的**回退路**（用户 2026-10-11 拍板接出来）。
    不接出来的话「完成」就是死路：只剩「换种子重跑」，而那个会把底图一起丢掉。
    pending 的格仍然拒绝——场景都没有，气泡无从加起。

    `split`（可选）：true 时这一格分两遍画（第一遍气泡1+旁白、第二遍补气泡2）。
    默认 false = 一遍把两个气泡和旁白全画完，省一次 50~70 秒；某格一遍画崩了，
    前端把它单独切回「分开画」重跑。

    `prompt`（可选）：**整段替换**这一格第一遍的提示词，源图仍是底图。界面上的
    「完整提示词」框走这条路——取 /prompt 看到的文字、改完直接发回来。它只对
    单格有意义（「全部」会把它套到每一格上），所以 no=all 时直接拒。
    """
    data = request.get_json(silent=True) or {}
    try:
        path = _task_dir(data.get("task"))
        task = _load_task(path)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    split = bool(data.get("split"))
    no = str(data.get("no") or "").strip()
    override = str(data.get("prompt") or "").strip()
    if override and no == "all":
        return jsonify({"ok": False,
                        "error": "手改的提示词只能给单格用——"
                                 "「全部」会把它原样套到每一格上"}), 400
    redo = False
    if no == "all":
        targets = [p.no for p in task.panels if p.state == "rendered"]
        if not targets:
            return jsonify({"ok": False,
                            "error": "没有待加气泡的格（都还没出场景图？）"}), 400
        only = set(targets)
    else:
        no = no.zfill(2)
        panel = next((p for p in task.panels if p.no == no), None)
        if panel is None:
            return jsonify({"ok": False, "error": "没有 %s 这格" % no}), 404
        if panel.state not in ("rendered", "done"):
            return jsonify({"ok": False,
                            "error": "格 %s 还没有场景图（%s）" % (
                                no, panel.state)}), 400
        only = {no}
        redo = panel.state == "done"      # done 的格要靠 redo 才进得了阶段二
        if override:
            # 手改的提示词走**单格直跑**：comic.run 是「按剧本逐格跑」的批处理，
            # 提示词由它自己按模板拼，没有「这一格换一段文字」的入口，所以这里
            # 直接调 render_bubble（自己补 save_state，run 里是它负责存的）。
            def _work():
                comic.render_bubble(task, panel, split=split, prompt=override)
                task.save_state()
            if not _spawn(path, "bubble", _work):
                return jsonify({"ok": False, "error": "这个任务已经在跑了"}), 409
            return jsonify({"ok": True, "panels": [no], "split": split,
                            "override": True})
    started = _spawn(path, "bubble", lambda: comic.run(
        os.path.join(path, "script.md"), only=only, redo=redo,
        split_bubbles=split), finish="send" if no == "all" else None)
    if not started:
        return jsonify({"ok": False, "error": "这个任务已经在跑了"}), 409
    return jsonify({"ok": True, "panels": sorted(only), "redo": redo,
                    "split": split})


@bp.route("/api/comic/manual", methods=["POST"])
def api_comic_manual():
    """手动修复：拿这一格的**当前成品**当源图，按用户手输的指令跑一遍 qwen。

    用户的实际用法（2026-10-11）：点开大图看到某几个字糊了 → 写「把『松手啦』
    重画成『放开了』」→ qwen 在图生图上就地改。所以源图是 final 不是 raw，
    这一遍是**累积**的；想退回干净底图就用 /api/comic/bubble。
    """
    data = request.get_json(silent=True) or {}
    try:
        path = _task_dir(data.get("task"))
        task = _load_task(path)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    no = str(data.get("no") or "").strip().zfill(2)
    panel = next((p for p in task.panels if p.no == no), None)
    if panel is None:
        return jsonify({"ok": False, "error": "没有 %s 这格" % no}), 404
    instruction = str(data.get("prompt") or "").strip()
    if not instruction:
        return jsonify({"ok": False, "error": "先写一句要 qwen 改什么"}), 400
    # 强度：不传就用管线默认（0.60）。改字调低、整格重来调高，由前端下拉给。
    denoise = str(data.get("denoise") or "").strip() or comic.I2I_DENOISE
    started = _spawn(path, "bubble", lambda: comic.render_manual(
        task, panel, instruction, denoise))
    if not started:
        return jsonify({"ok": False, "error": "这个任务已经在跑了"}), 409
    return jsonify({"ok": True, "no": no})


@bp.route("/api/comic/upload", methods=["POST"])
def api_comic_upload():
    """手动放底图：用户在别处跑好的底图，传进来顶替这一格的 raw。

    换了底图，原来那张成品就作废（它是拿旧底图加的）→ 删掉并退回「待加气泡」，
    接着点「加气泡」或直接手输指令跑 qwen。
    """
    try:
        path = _task_dir(request.form.get("task"))
        task = _load_task(path)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    no = str(request.form.get("no") or "").strip().zfill(2)
    panel = next((p for p in task.panels if p.no == no), None)
    if panel is None:
        return jsonify({"ok": False, "error": "没有 %s 这格" % no}), 404
    fs = request.files.get("file")
    if fs is None or not fs.filename:
        return jsonify({"ok": False, "error": "没收到图片"}), 400
    try:
        _save_upload(fs, task.raw_path(panel))
    except Exception as exc:                    # 不是图 / 坏了 / 写不进去
        return jsonify({"ok": False,
                        "error": "这张图读不出来（%s）" % exc}), 400
    final = task.final_path(panel)
    if os.path.isfile(final):
        os.remove(final)
    panel.state, panel.seed, panel.error = "rendered", None, None
    task.save_state()
    comic.log.info("格 %s 底图已手动替换 → %s", no, task._rel(task.raw_path(panel)))
    # 手动换底图也不发 zip，检查完由用户自己触发。
    return jsonify({"ok": True, "no": no})


@bp.route("/api/comic/img", methods=["GET"])
def api_comic_img():
    """服务 raw/ final/ portraits/ 的图，外加群友版专用的 WEBP 缩略图。

    `f` 只认四种形态（正则 / `comic_thumb.parse_rel` 钉死），别的一律 404——
    不做成通用静态目录服务。缩略图是懒生成：源文件比缓存新就重画，否则只读盘；
    万一 Pillow 打不开，退回高清原图，别让整页变成一排破图。
    """
    import re as _re
    f = str(request.args.get("f") or "")
    # 前端给每张图拼了 &v=<文件指纹>（图片 mtime+size，见 comic_studio.html
    # 的 imgUrl）：图一变 URL 就变，所以这类请求可以让浏览器长期缓存，
    # 重绘格子时连 304 条件请求都不发，手机端不用把整本图重下一遍。
    # 没带 v 的旧链接保持原来 ETag / 条件请求的行为，免得缓存住会变的图。
    # private 与 5175 反代层 _secure 对图片的口径一致：图片要过登录态，
    # 不能让 CDN 边缘缓存下来（5175 那层本来就会把图片头改成 private）。
    immutable = ("private, max-age=31536000, immutable"
                 if request.args.get("v") else None)
    thumb = comic_thumb.parse_rel(f)
    if thumb:
        src_rel = comic_thumb.src_rel(*thumb)
    elif _re.fullmatch(r"(?:(?:raw|final)/\d{2}_(?:raw|final)|portraits/\d{2})\.png",
                       f):
        src_rel = f
    else:
        return jsonify({"ok": False, "error": "非法文件名"}), 404
    try:
        path = _task_dir(request.args.get("task"))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    if thumb:
        cached = comic_thumb.ensure(path, *thumb)
        if cached and os.path.isfile(cached):
            # 几十 KB 的 WEBP，直接读成字节回：不把文件句柄挂在响应上，
            # 5175 反代 / 测试客户端都能一次收干净。ETag 用 mtime+size，
            # 换种子重跑后缩略图变了，浏览器不会接着用旧缓存。
            with open(cached, "rb") as f:
                data = f.read()
            resp = Response(data, mimetype="image/webp")
            resp.set_etag("%x-%x" % (os.stat(cached).st_mtime_ns, len(data)))
            if immutable:
                resp.headers["Cache-Control"] = immutable
            return resp.make_conditional(request)
        # ensure 返回 None（源图不在 / PIL 失败）时下面会自然落到 404 或原图
    full = os.path.join(path, src_rel.replace("/", os.sep))
    if not os.path.isfile(full):
        return jsonify({"ok": False, "error": "文件不存在"}), 404
    resp = send_from_directory(path, src_rel)
    if immutable:
        resp.headers["Cache-Control"] = immutable
    return resp


@bp.route("/api/comic/delivery_status", methods=["GET"])
def api_comic_delivery_status():
    """QQ 直发状态轮询：哪个包发了、几张、什么时候、失败原因。"""
    try:
        path = _task_dir(request.args.get("task"))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    return jsonify(dict(comic_delivery.view(path), ok=True))


@bp.route("/api/comic/send_qq", methods=["POST"])
def api_comic_send_qq():
    """手动把成品包重发到 QQ。发送失败只提示找站主，**不回退网页下载**。

    群友自己点（5175 注入 QQ）只能发自己的任务；站主在 5174 管理页点，
    QQ 从任务目录的 owner.json 取，也能显式给 qq。60 秒冷却防连点。
    """
    data = request.get_json(silent=True) or {}
    try:
        path = _task_dir(data.get("task"))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    qq = (_qq() or comic_quota.owner_of(path)
          or str(data.get("qq") or "").strip())
    if not qq.isdigit():
        return jsonify({"ok": False,
                        "error": "这个任务没有绑定 QQ，没法直发"}), 400
    # 点下去的这一瞬间就把「会发什么」说清楚，别让人对着按钮猜。
    packs = comic_delivery.preview(path)
    if not packs:
        return jsonify({"ok": False,
                        "error": "这本还没有任何图，等跑完底图再发"}), 409
    ok, wait = comic_delivery.manual_ok(path)
    if not ok:
        return jsonify({"ok": False,
                        "error": "刚发过，等 %d 秒再点（按钮下面能看到每个包的状态）" % wait}), 429
    # 先落「正在打包」：deliver() 要等 zip 打完 + 协议端回包才写状态，
    # 几十 MB 时网页轮询会一直看到旧状态，用户以为按钮坏了就会反复点。
    comic_delivery.mark_packing(path)

    def _run():
        try:
            comic_delivery.deliver(path, qq, force=True)
        except Exception as exc:
            comic.log.exception("手动重发 QQ 失败 %s", path)
            comic_quota.note_delivery(qq, os.path.basename(path),
                                      "failed", str(exc))
            comic_delivery.mark_packing(path, "failed", str(exc)[:300])

    threading.Thread(target=_run, daemon=True, name="comic-resend").start()
    what = "、".join("%s %d 张" % (k, v) for k, v in packs.items())
    return jsonify({"ok": True, "state": "packing", "packages": packs,
                    "delivery": comic_delivery.view(path),
                    "hint": "本次会发：" + what + "。" + comic_delivery.RULE})


def _works_img_url(task, f):
    """作品浏览页固定指向服务端缩略图。

    不走 `_img_url`：那个在站主态（qq 空）会退回 9MB 高清原图，作品墙一次
    铺几十张会把 5174 本地带宽也拖住。`/api/comic/img` 对站主一样认 thumb
    形态并懒生成 WEBP，所以这里直接拼 thumb URL。
    """
    from urllib.parse import quote
    return "/api/comic/img?task=%s&f=%s" % (
        quote(str(task), safe=""), quote(str(f), safe="/"))


def _work_summary(name, kind):
    """一个任务目录 → 作品墙一行。只读文件清单与元数据，不解码任何图片。

    kind 判据在调用处定（script.md → 漫画；prompts.json / portraits/ → 写真），
    这里只负责把标题、张数、缩略图清单、更新时间拼齐。标题写真取 brief 首行、
    漫画取剧本 `# 标题：`，都没有才退回目录名（去 C 前缀）。
    """
    import datetime
    path = os.path.join(_COMICS_ROOT, name)
    item = {"task": name, "title": "", "kind": kind, "count": 0,
            "updated": "", "thumbs": [], "cover": ""}
    if kind == "comic":
        try:
            meta, _ = comic.parse_script(os.path.join(path, "script.md"))
            item["title"] = str((meta or {}).get("title") or "").strip()
        except Exception:
            pass
    else:
        try:
            with open(os.path.join(path, "portrait.json"),
                      encoding="utf-8") as f:
                brief = str((json.load(f) or {}).get("brief") or "").strip()
            item["title"] = brief.splitlines()[0] if brief else ""
        except (OSError, ValueError, AttributeError, IndexError):
            pass
    if not item["title"]:
        m = re.match(r"^C\d{8}-\d+_(.+)$", name)
        item["title"] = (m.group(1) if m else name).strip()

    if kind == "comic":
        final_d = os.path.join(path, "final")
        raw_d = os.path.join(path, "raw")
        finals = sorted(n for n in os.listdir(final_d) if n.endswith("_final.png")) \
            if os.path.isdir(final_d) else []
        raws = sorted(n for n in os.listdir(raw_d) if n.endswith("_raw.png")) \
            if os.path.isdir(raw_d) else []
        use, ck = (finals, "final") if finals else (raws, "raw")
        for i in range(1, len(use) + 1):
            item["thumbs"].append(_works_img_url(name, comic_thumb.rel(i, ck)))
    else:
        pd = os.path.join(path, "portraits")
        shots = sorted(n for n in os.listdir(pd) if n.endswith(".png")) \
            if os.path.isdir(pd) else []
        for i in range(1, len(shots) + 1):
            item["thumbs"].append(_works_img_url(name, comic_thumb.rel(i, "portrait")))
    item["count"] = len(item["thumbs"])
    if item["thumbs"]:
        item["cover"] = item["thumbs"][0]
    # 更新时间取目录树里最新一个文件的 mtime（目录 mtime 在 Windows 上不随
    # 子目录内文件变化，不能用；写真也没有 state.json 的 updated 字段）。
    try:
        mt = max(os.path.getmtime(os.path.join(dp, f))
                 for dp, _, fns in os.walk(path) for f in fns)
        item["updated"] = datetime.datetime.fromtimestamp(mt).strftime("%Y-%m-%d %H:%M")
    except (OSError, ValueError):
        pass
    return item


@bp.route("/api/comic/works")
def api_comic_works():
    """站主「群友作品浏览」：按 QQ 账号 → 漫画台 / 写真台列作品集。

    和 `/api/comic/tasks` 的分工：那个给站主版 /comic 当历史下拉、只收有
    script.md 的漫画；这个给独立的 `/comic_works` 页，**漫画写真都收**、按
    owner.json 的 QQ 分组，一次返回全量嵌套（群友账号就那么几个，前端切
    Tab 不用再发请求）。站主自己的老作品没有 owner.json，仍归 /comic 自己管，
    不混进「账号 → 作品」这套（那套的主键就是群友 QQ）。

    ⚠ 5175 是 `/api/comic/<path>` 通配反代，群友带令牌也会打到这个路径，
    所以和 `/api/comic/admin` 一样显式拒掉群友态——不能让人一把列走所有
    账号的作品清单。
    """
    if _qq():
        return jsonify({"ok": False, "error": "没有权限"}), 403
    try:
        names = os.listdir(_COMICS_ROOT)
    except OSError:
        names = []
    groups = {}
    for name in names:
        path = os.path.join(_COMICS_ROOT, name)
        if not os.path.isdir(path):
            continue
        qq = comic_quota.owner_of(path)
        if not qq:                       # 站主作品不进「账号」体系
            continue
        if os.path.isfile(os.path.join(path, "script.md")):
            kind = "comic"
        elif (os.path.isfile(os.path.join(path, "prompts.json"))
              or os.path.isdir(os.path.join(path, "portraits"))):
            kind = "portrait"
        else:
            continue                     # 空壳目录（建了没跑）不算作品
        groups.setdefault(qq, {"comic": [], "portrait": []})[kind].append(
            _work_summary(name, kind))

    def _key(it):
        m = re.match(r"^C(\d{8})-(\d+)", it["task"])
        return (m.group(1), int(m.group(2))) if m else ("0", 0)

    accounts = []
    for qq, gs in groups.items():
        for k in ("comic", "portrait"):
            gs[k].sort(key=_key, reverse=True)
        accounts.append({"qq": qq, "comic": gs["comic"], "portrait": gs["portrait"],
                         "counts": {"comic": len(gs["comic"]),
                                    "portrait": len(gs["portrait"])}})
    accounts.sort(key=lambda a: max([_key(x) for x in
                                     a["comic"] + a["portrait"]] or [("0", 0)]),
                  reverse=True)
    return jsonify({"ok": True, "accounts": accounts})


@bp.route("/api/comic/admin", methods=["GET", "POST"])
def api_comic_admin():
    """独立「漫画台管理」页的数据：谁用了、各剩几次、加次数、直发补发。

    群友请求（带内部令牌）一律 403——哪怕猜到了这个路径。站主直连 5174
    本来就没有那层令牌，管理页与 /api/qq_bill 是同一套信任边界。
    编剧模型白名单后端仍认，但管理页不再让站主手填：空 = 全体走站主默认。
    """
    if _qq():
        return jsonify({"ok": False, "error": "没有权限"}), 403
    if request.method == "POST":
        return jsonify(dict(comic_quota.admin_update(
            request.get_json(silent=True) or {}), ok=True))
    return jsonify(dict(comic_quota.admin_view(), ok=True))


@bp.route("/api/comic/download", methods=["GET"])
def api_comic_download():
    """打包这一集的成品图（内存打包，任务就几十张图，不值得落临时文件）。

    **成品 = final/ 优先，没有就用 raw/**（2026-10-22 修）。原来只打 final/，
    而无气泡模式压根不会生成 final/——用户跑完 50 格点下载，拿到一个空 zip。
    对无气泡来说 raw/ 的场景图就是成品，没有理由不给他。
    """
    try:
        path = _task_dir(request.args.get("task"))
        task = _load_task(path)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    buf = io.BytesIO()
    n = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for p in task.panels:
            fp = task.final_path(p)
            if not os.path.isfile(fp):
                fp = task.raw_path(p)          # 还没加气泡：场景图就是成品
            if os.path.isfile(fp):
                z.write(fp, "%s_final.png" % p.no)
                n += 1
    buf.seek(0)
    # ⚠️ 不能拿 `buf.nbytes` 判空：空 zip 也有 22 字节（EOCD），永远非零。
    # 必须数真正收进去的文件数。
    if not n:
        return jsonify({"ok": False,
                        "error": "这一集还没有任何图（先跑阶段一生成场景图）"}), 404
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name="%s_final.zip" % task.task_id)
