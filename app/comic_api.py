"""漫画工作台 API：挂给 app/main.py 的 Blueprint。

复用 app/comic.py 的全部调度能力（run / render_scene / render_bubble / Task），
把 CLI 的手动动作变成 HTTP 端点 + 后台线程：

  POST /api/comic/script    编剧写剧本 → 内容补正 → 建任务目录（comics/C<日期>-<序号>/）
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
  GET  /api/comic/img       服务 raw/ final/ 下的图片文件
  GET  /api/comic/download  成品 final/ 打包 zip

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

from flask import Blueprint, jsonify, request, send_file, send_from_directory

from app import comic, comic_agent, comic_portrait, comic_render_opts, image_jobs
from app.config import BASE_DIR

bp = Blueprint("comic_api", __name__)

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
    """task id → 目录绝对路径。只认一级目录名，堵路径穿越。"""
    name = os.path.basename(str(task_id or "").strip())
    if not name:
        raise ValueError("缺 task")
    path = os.path.join(_COMICS_ROOT, name)
    if not os.path.isdir(path):
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


def _spawn(path, phase, fn, on_error=None):
    """起后台线程跑 fn()。同一任务已有线程在跑就拒绝（409 语义）。

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


@bp.route("/api/comic/tasks")
def api_comic_tasks():
    """列出 comics/ 下的剧本任务，给页面顶部的「历史任务」下拉用（2026-10-18）。

    为什么需要这个接口：剧本一直都在存（每个任务目录的 `script.md`），但页面
    只记得 localStorage 里那一个任务名——新建一集之后就再也翻不回去了，用户
    真要找前情只能去磁盘上翻目录。

    只收**有 script.md** 的目录：纯写真任务（生图模式新建、没剧本）不进这个
    列表，它们由 `portraits` 那条链路自己管。
    """
    out = []
    try:
        names = os.listdir(_COMICS_ROOT)
    except OSError:
        names = []
    for name in names:
        path = os.path.join(_COMICS_ROOT, name)
        if not os.path.isdir(path):
            continue
        script = os.path.join(path, "script.md")
        if not os.path.isfile(script):
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
    brief = str(data.get("brief") or "").strip()
    if not brief:
        return jsonify({"ok": False, "error": "缺 brief（一句话设定）"}), 400
    try:
        panels_n = int(data.get("panels") or 10)
    except (TypeError, ValueError):
        panels_n = 10
    panels_n = max(1, min(30, panels_n))    # 与 comic_story.clamp_panels 同口径
    character = str(data.get("character") or "").strip()
    provider = str(data.get("provider") or "").strip() or None
    model = str(data.get("model") or "").strip() or None
    # 连载（2026-10-18）：prev = 上一集的任务号，给了就把它的气泡+旁白当前情。
    # 目录名过一遍 `_task_dir` 校验（它挡住 `..` 和绝对路径），指不到就当没给。
    prev = str(data.get("prev") or "").strip()
    prev_text = ""
    if prev:
        try:
            prev_path = os.path.join(_task_dir(prev), "script.md")
        except ValueError:
            prev_path = ""
        if prev_path and os.path.isfile(prev_path):
            prev_text = comic_agent.prev_story_text(prev_path)
        if not prev_text:
            prev = ""             # 目录不在 / 没有剧本：静默按全新一集处理

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

    def _work():
        try:
            text = comic_agent.write_script(
                brief, panels=panels_n, character=character,
                provider=provider, model=model, progress=_progress,
                prev=prev_text)
            # 目录**不再在这里建**：名字要带剧本标题，得等拿到稿子（见下面
            # `final` 那一段）。失败的 job 仍然不留空目录。

            # ── 独立第二步：内容补正（2026-10-11 十轮）──────────────────
            # 编剧的上下文长（十几轮查库），实测三格全漏发色；补正拿干净稿 +
            # 代码清单再补一遍。**直接落地**：采纳的补正稿就是 script.md，编剧
            # 原稿另存 script_original.md，页面拿 fix.json 显示逐格差异、可回退。
            # ⚠️ 2026-10-12 用户拍板：补正**不再过准入闸门**，一律采纳；代码判据
            # 只做检测（清单 + 采纳后的体检报告）。补正不采纳只有一种情形——
            # 模型返回空/解析不出剧本/格号对不上（`status` 非 ok），此时 text 原样
            # 返回、界面提示「请重新补正」。补正异常也绝不拖垮编剧任务。
            _progress("编剧交稿，开始补正")
            try:
                fix = comic_agent.fix_script(brief, text, provider=provider,
                                             model=model, progress=_progress)
            except Exception as exc:            # 补正绝不许拖垮编剧
                comic.log.exception("补正失败 job=%s", job)
                fix = {"text": text, "issues": [], "diff": [], "adopted": False,
                       "status": "error", "warnings": ["补正异常：%s" % exc],
                       "audit": [], "skipped": False, "model": ""}
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
            with open(os.path.join(path, "script_original.md"), "w",
                      encoding="utf-8") as f:
                f.write(text + "\n")
            with open(os.path.join(path, "script.md"), "w", encoding="utf-8") as f:
                f.write(final + "\n")
            # brief 落盘：手动重跑补正时要拿它当「原始需求」。
            with open(os.path.join(path, "brief.txt"), "w", encoding="utf-8") as f:
                f.write(brief + "\n")
            # 上一集是谁也记一笔：下一集接着接的时候，下拉里能看出这条链。
            if prev:
                with open(os.path.join(path, "prev.txt"), "w",
                          encoding="utf-8") as f:
                    f.write(prev + "\n")
            report = _write_fix_report(path, fix)

            with _SCRIPT_LOCK:
                _SCRIPT_JOBS[job].update(state="done", script=final,
                                         original=text, task=os.path.basename(path),
                                         fix=report)
            comic.log.info("编剧完成 job=%s → %s（补正 %s）", job,
                           os.path.basename(path),
                           "采纳" if fix.get("adopted") else "未采纳")
        except BaseException as exc:        # SystemExit 不是 Exception（见模块头）
            with _SCRIPT_LOCK:
                _SCRIPT_JOBS[job].update(state="error", error=str(exc))
            comic.log.exception("编剧失败 job=%s", job)

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
    return jsonify({"ok": True, "panels": len(panels)})


@bp.route("/api/comic/start", methods=["POST"])
def api_comic_start():
    """确认开跑：后台线程跑阶段一（所有格的场景图）。scene_skill 可选覆盖，
    默认就是 comic.SCENE_SKILL（anima-line）。"""
    data = request.get_json(silent=True) or {}
    try:
        path = _task_dir(data.get("task"))
        _load_task(path)                    # 开跑前先验一遍剧本可解析
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    scene_skill = str(data.get("scene_skill") or "").strip() or comic.SCENE_SKILL
    started = _spawn(path, "scene", lambda: comic.run(
        os.path.join(path, "script.md"), silver_only=True,
        scene_skill=scene_skill), on_error=_mark_pending_failed(path))
    if not started:
        return jsonify({"ok": False, "error": "这个任务已经在跑了"}), 409
    return jsonify({"ok": True, "scene_skill": scene_skill})


@bp.route("/api/comic/status", methods=["GET"])
def api_comic_status():
    """槽位轮询。图片 URL 由前端拼时间戳防缓存；每格另带 `rev`（图片文件指纹），
    前端拿它判断「图被重写过」，否则就地覆盖的场景（手动跑 Qwen）刷不出来。

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
            return jsonify({"ok": True, "task": os.path.basename(path),
                            "title": "", "running": None, "job_error": None,
                            "comfy": _comfy_state(),
                            "fix": None, "panels": [],
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
                "error": p.error}
        if os.path.isfile(task.raw_path(p)):
            item["scene_url"] = "/api/comic/img?task=%s&f=raw/%s_raw.png" % (
                task.task_id, p.no)
        if os.path.isfile(task.final_path(p)):
            item["final_url"] = "/api/comic/img?task=%s&f=final/%s_final.png" % (
                task.task_id, p.no)
        panels.append(item)
    return jsonify({"ok": True, "task": task.task_id,
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
    try:
        path = _task_dir(data.get("task"))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
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

    def _work():
        fix = comic_agent.fix_script(
            brief, original, provider=provider, model=model,
            progress=lambda m: comic.log.info("[comic-fix] %s", m))
        final = fix.get("text") or original
        with open(os.path.join(path, "script.md"), "w", encoding="utf-8") as f:
            f.write(final + "\n")
        _write_fix_report(path, fix)
        comic.log.info("补正重跑完成 %s（%s）", os.path.basename(path),
                       "采纳" if fix.get("adopted") else "未采纳")

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
    out = {"ok": True}
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
    return jsonify({
        "ok": True,
        "opts": comic_render_opts.load(path, comic.SCENE_SKILL),
        "resources": _render_resources(),
        "saved": bool(path) and os.path.isfile(comic_render_opts.path_of(path)),
        "defaults_saved": os.path.isfile(comic_render_opts.defaults_path()),
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
    task_id = (data.get("task") or "").strip()
    scope = str(data.get("scope") or "task").strip().lower()
    if scope not in ("task", "global"):
        scope = "task"
    path = ""
    if task_id:
        try:
            path = _task_dir(task_id)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
    elif scope == "task" and not data.get("reset"):
        return jsonify({"ok": False, "error": "缺 task"}), 400

    if data.get("reset"):
        if scope == "global":
            comic_render_opts.reset_defaults()
        else:
            comic_render_opts.reset(path)
        return jsonify({"ok": True, "reset": True, "scope": scope,
                        "opts": comic_render_opts.load(path, comic.SCENE_SKILL),
                        "saved": bool(path) and
                                 os.path.isfile(comic_render_opts.path_of(path)),
                        "defaults_saved":
                            os.path.isfile(comic_render_opts.defaults_path())})

    opts, errors, warnings = comic_render_opts.normalize(
        data.get("opts") or {}, resources=_render_resources())
    if errors:
        return jsonify({"ok": False, "error": "；".join(errors)}), 400
    if scope == "global":
        comic_render_opts.save_defaults(opts)
    else:
        comic_render_opts.save(path, opts)
    return jsonify({"ok": True, "opts": opts, "warnings": warnings,
                    "scope": scope,
                    "saved": bool(path) and
                             os.path.isfile(comic_render_opts.path_of(path)),
                    "defaults_saved":
                        os.path.isfile(comic_render_opts.defaults_path())})


@bp.route("/api/comic/rerender", methods=["POST"])
def api_comic_rerender():
    """换种子重跑某格：把该格重置回 pending（种子作废），后台只跑阶段一。
    seed 不传新的——render_scene 里 _new_seed() 每次现抽。"""
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
    panel.state, panel.seed, panel.error = "pending", None, None
    task.save_state()
    started = _spawn(path, "scene", lambda: comic.run(
        os.path.join(path, "script.md"), only={no}, silver_only=True),
        on_error=_mark_pending_failed(path, no))
    if not started:
        return jsonify({"ok": False, "error": "这个任务已经在跑了"}), 409
    return jsonify({"ok": True, "no": no})


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
    brief = str(data.get("brief") or "").strip()
    if not brief:
        return jsonify({"ok": False, "error": "想拍什么总得写一句"}), 400
    try:
        n = int(data.get("n") or 4)
    except (TypeError, ValueError):
        n = 4
    n = max(1, min(comic_portrait.MAX_SHOTS, n))
    heroine = str(data.get("heroine") or "").strip()
    provider = str(data.get("provider") or "").strip() or None
    model = str(data.get("model") or "").strip() or None

    try:
        path = _portrait_task_dir(data)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    with _PORTRAIT_LOCK:
        busy = any(j.get("state") == "running" and j.get("task") == path
                   for j in _PORTRAIT_JOBS.values())
    if busy:
        return jsonify({"ok": False, "error": "这个任务的写真还在跑，等它跑完"}), 409

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
                               model=model, progress=_progress)
            with _PORTRAIT_LOCK:
                _PORTRAIT_JOBS[job].update(state="done")
        except BaseException as exc:        # SystemExit 不是 Exception（见模块头）
            with _PORTRAIT_LOCK:
                _PORTRAIT_JOBS[job].update(state="error", error=str(exc))
            comic.log.exception("写真批次失败 job=%s", job)

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
        fp = comic_portrait.shot_path(path, s.get("no"))
        item = dict(s)
        item["url"] = ("/api/comic/img?task=%s&f=portraits/%s.png"
                       % (os.path.basename(path), str(s.get("no")).zfill(2)))
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
        shot = comic_portrait.reseed_shot(path, no)
    except comic_portrait.PortraitError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    fp = comic_portrait.shot_path(path, no)
    return jsonify({"ok": True, "shot": dict(
        shot, url=("/api/comic/img?task=%s&f=portraits/%s.png"
                   % (os.path.basename(path), no)), rev=_rev(fp))})


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
    try:
        path = _task_dir(data.get("task"))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
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

    job = uuid.uuid4().hex[:12]
    with _CHAT_LOCK:
        _CHAT_JOBS[job] = {"state": "running", "reply": "", "changed": False,
                           "diff": [], "warnings": [], "error": None,
                           "task": os.path.basename(path), "at": None}
        for k in [k for k, v in _CHAT_JOBS.items()
                  if v["state"] != "running"][:-_CHAT_KEEP]:
            _CHAT_JOBS.pop(k, None)

    def _work():
        r = comic_agent.chat_revise(script, message, brief=brief,
                                    history=history[-8:],        # 最近 4 轮够用了
                                    provider=provider, model=model)
        reply = r.get("reply") or ""
        if r.get("changed") and r.get("script"):
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
        _append_chat_history(path, message, reply)
        with _CHAT_LOCK:
            _CHAT_JOBS[job].update(
                state="done", reply=reply, changed=bool(r.get("changed")),
                diff=r.get("diff") or [], warnings=r.get("warnings") or [],
                at=time.strftime("%H:%M:%S"))

    def _runner():
        try:
            _work()
        except BaseException as exc:        # SystemExit 不是 Exception
            comic.log.exception("对话改稿失败 %s", path)
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
        snap = dict(j)
    snap["ok"] = True
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
    started = _spawn(path, "scene", lambda: comic.run(
        script_path, only={no}, silver_only=True),
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
        split_bubbles=split))
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
    return jsonify({"ok": True, "no": no})


@bp.route("/api/comic/img", methods=["GET"])
def api_comic_img():
    """服务 raw/ final/ portraits/ 的图。f 只认 raw/NN_raw.png、final/NN_final.png、
    portraits/NN.png 三种形态（正则钉死），别的一律 404——不做成通用静态目录服务。"""
    import re as _re
    f = str(request.args.get("f") or "")
    if not _re.fullmatch(r"(?:(?:raw|final)/\d{2}_(?:raw|final)|portraits/\d{2})\.png",
                         f):
        return jsonify({"ok": False, "error": "非法文件名"}), 404
    try:
        path = _task_dir(request.args.get("task"))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    full = os.path.join(path, f.replace("/", os.sep))
    if not os.path.isfile(full):
        return jsonify({"ok": False, "error": "文件不存在"}), 404
    return send_from_directory(path, f)


@bp.route("/api/comic/download", methods=["GET"])
def api_comic_download():
    """成品 final/ 打包 zip（内存打包，任务就十几张图，不值得落临时文件）。"""
    try:
        path = _task_dir(request.args.get("task"))
        task = _load_task(path)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for p in task.panels:
            fp = task.final_path(p)
            if os.path.isfile(fp):
                z.write(fp, "%s_final.png" % p.no)
    buf.seek(0)
    if not buf.getbuffer().nbytes:
        return jsonify({"ok": False, "error": "还没有任何成品图"}), 404
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name="%s_final.zip" % task.task_id)
