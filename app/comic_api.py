"""漫画工作台 API：挂给 app/main.py 的 Blueprint。

复用 app/comic.py 的全部调度能力（run / render_scene / render_bubble / Task），
把 CLI 的手动动作变成 HTTP 端点 + 后台线程：

  POST /api/comic/script    编剧写剧本 → 内容补正 → 建任务目录（comics/C<日期>-<序号>/）
  POST /api/comic/save      用户编辑剧本后保存（确认开跑前的修改都走这里）
  POST /api/comic/fix       重跑内容补正（从 script_original.md 重来，直接落地）
  POST /api/comic/start     确认开跑：后台线程跑阶段一（场景图，不碰气泡）
  GET  /api/comic/status    槽位轮询：剧本 + state.json → 每格状态与图 URL
  POST /api/comic/rerender  换种子重跑某格（重置该格 → 后台只跑阶段一）
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
import threading
import time
import uuid
import zipfile

from flask import Blueprint, jsonify, request, send_file, send_from_directory

from app import comic, comic_agent
from app.config import BASE_DIR

bp = Blueprint("comic_api", __name__)

# 任务根目录：CLI 里用相对路径 comics/（cwd 在项目根），API 这里钉死绝对，
# 免得 Flask 的启动 cwd 不同把任务散到别处。
_COMICS_ROOT = os.path.join(BASE_DIR, "comics")

# 运行中的后台任务：{任务目录绝对路径: {"phase": "scene"|"bubble", "error": str}}
_JOBS = {}
_JOBS_LOCK = threading.Lock()

# 编剧任务：{job_id: {...}}。与 _JOBS 分开——_JOBS 以**任务目录**为键（要等
# 剧本落盘才知道目录名），而编剧任务在剧本写出来之前就得有个句柄给前端轮询。
_SCRIPT_JOBS = {}
_SCRIPT_LOCK = threading.Lock()
_SCRIPT_KEEP = 12          # 只留最近这么多条，防长期运行攒内存


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


def _spawn(path, phase, fn):
    """起后台线程跑 fn()。同一任务已有线程在跑就拒绝（409 语义）。"""
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
            return
        with _JOBS_LOCK:
            _JOBS[path] = {"phase": None, "error": None}

    threading.Thread(target=_worker, daemon=True, name="comic-studio").start()
    return True


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
                provider=provider, model=model, progress=_progress)
            # 目录在剧本写好后才建：失败的 job 不留空目录。
            path = comic._next_task_dir(_COMICS_ROOT)
            os.makedirs(path, exist_ok=True)

            # ── 独立第二步：内容补正（2026-10-11 十轮）──────────────────
            # 编剧的上下文长（十几轮查库），实测三格全漏发色；补正拿干净稿 +
            # 代码清单再补一遍。**直接落地**：采纳的补正稿就是 script.md，编剧
            # 原稿另存 script_original.md，页面拿 fix.json 显示逐格差异、可回退。
            # 补正失败/被代码闸门拒收时 text 原样返回，流程照常往下走——
            # 补正是加分项，不能因为它把整个编剧任务搞失败。
            _progress("编剧交稿，开始补正")
            try:
                fix = comic_agent.fix_script(brief, text, provider=provider,
                                             model=model, progress=_progress)
            except Exception as exc:            # 补正绝不许拖垮编剧
                comic.log.exception("补正失败 job=%s", job)
                fix = {"text": text, "issues": [], "diff": [], "adopted": False,
                       "rejects": ["补正异常：%s" % exc], "warnings": [],
                       "skipped": False, "model": ""}
            final = fix.get("text") or text
            with open(os.path.join(path, "script_original.md"), "w",
                      encoding="utf-8") as f:
                f.write(text + "\n")
            with open(os.path.join(path, "script.md"), "w", encoding="utf-8") as f:
                f.write(final + "\n")
            # brief 落盘：手动重跑补正时要拿它当「原始需求」。
            with open(os.path.join(path, "brief.txt"), "w", encoding="utf-8") as f:
                f.write(brief + "\n")
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
        scene_skill=scene_skill))
    if not started:
        return jsonify({"ok": False, "error": "这个任务已经在跑了"}), 409
    return jsonify({"ok": True, "scene_skill": scene_skill})


@bp.route("/api/comic/status", methods=["GET"])
def api_comic_status():
    """槽位轮询。图片 URL 由前端拼时间戳防缓存；每格另带 `rev`（图片文件指纹），
    前端拿它判断「图被重写过」，否则就地覆盖的场景（手动跑 Qwen）刷不出来。"""
    try:
        path = _task_dir(request.args.get("task"))
        task = _load_task(path)
    except ValueError as exc:
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
                    # 补正报告（编剧之后那一步）落盘在 fix.json：页面拿它显示
                    # 「改了哪几格、改了什么」，没有这个文件就是老任务，返回 null。
                    "fix": _read_fix_report(path),
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
        os.path.join(path, "script.md"), only={no}, silver_only=True))
    if not started:
        return jsonify({"ok": False, "error": "这个任务已经在跑了"}), 409
    return jsonify({"ok": True, "no": no})


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
    """服务 raw/ final/ 的图。f 只认 raw/NN_raw.png、final/NN_final.png 两种
    形态（正则钉死），别的一律 404——不做成通用静态目录服务。"""
    import re as _re
    f = str(request.args.get("f") or "")
    if not _re.fullmatch(r"(?:raw|final)/\d{2}_(?:raw|final)\.png", f):
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
