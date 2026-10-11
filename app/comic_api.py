"""漫画工作台 API：挂给 app/main.py 的 Blueprint。

复用 app/comic.py 的全部调度能力（run / render_scene / render_bubble / Task），
把 CLI 的手动动作变成 HTTP 端点 + 后台线程：

  POST /api/comic/script    编剧写剧本 → 建任务目录（comics/C<日期>-<序号>/）
  POST /api/comic/save      用户编辑剧本后保存（确认开跑前的修改都走这里）
  POST /api/comic/start     确认开跑：后台线程跑阶段一（场景图，不碰气泡）
  GET  /api/comic/status    槽位轮询：剧本 + state.json → 每格状态与图 URL
  POST /api/comic/rerender  换种子重跑某格（重置该格 → 后台只跑阶段一）
  POST /api/comic/bubble    加气泡：单格，或全部「待加气泡」的格（阶段二）
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
import zipfile

from flask import Blueprint, jsonify, request, send_file, send_from_directory

from app import comic, comic_writer
from app.config import BASE_DIR

bp = Blueprint("comic_api", __name__)

# 任务根目录：CLI 里用相对路径 comics/（cwd 在项目根），API 这里钉死绝对，
# 免得 Flask 的启动 cwd 不同把任务散到别处。
_COMICS_ROOT = os.path.join(BASE_DIR, "comics")

# 运行中的后台任务：{任务目录绝对路径: {"phase": "scene"|"bubble", "error": str}}
_JOBS = {}
_JOBS_LOCK = threading.Lock()


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
    """编剧写剧本。同步调用（一次 LLM，前端转圈等待），产出新任务目录。"""
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

    try:
        text = comic_writer.write_script(brief, panels=panels_n,
                                         character=character)
    except Exception as exc:
        return jsonify({"ok": False, "error": "编剧失败：%s" % exc}), 502

    path = comic._next_task_dir(_COMICS_ROOT)
    os.makedirs(path, exist_ok=True)
    script_path = os.path.join(path, "script.md")
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(text + "\n")
    return jsonify({"ok": True, "task": os.path.basename(path),
                    "script": text})


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
    """槽位轮询。图片 URL 由前端拼时间戳防缓存。"""
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
                "label": p.label, "bubble": p.bubble, "scene": p.scene,
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
                    "panels": panels})


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


@bp.route("/api/comic/bubble", methods=["POST"])
def api_comic_bubble():
    """加气泡（阶段二，qwen 图生图）。no=单格；no=all → 全部「待加气泡」
    （rendered）的格。只挑 rendered：pending 的格场景都没有，气泡无从加起；
    done 的格不动（重做某格先 rerender 或改台词让它自动退回 rendered）。"""
    data = request.get_json(silent=True) or {}
    try:
        path = _task_dir(data.get("task"))
        task = _load_task(path)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    no = str(data.get("no") or "").strip()
    if no == "all":
        targets = [p.no for p in task.panels if p.state == "rendered"]
        if not targets:
            return jsonify({"ok": False,
                            "error": "没有待加气泡的格（都还没出场景图？）"}), 400
        only = set(targets)
        phase = "bubble"
    else:
        no = no.zfill(2)
        panel = next((p for p in task.panels if p.no == no), None)
        if panel is None:
            return jsonify({"ok": False, "error": "没有 %s 这格" % no}), 404
        if panel.state != "rendered":
            return jsonify({"ok": False,
                            "error": "格 %s 还没有场景图（%s）" % (
                                no, panel.state)}), 400
        only = {no}
        phase = "bubble"
    started = _spawn(path, phase, lambda: comic.run(
        os.path.join(path, "script.md"), only=only))
    if not started:
        return jsonify({"ok": False, "error": "这个任务已经在跑了"}), 409
    return jsonify({"ok": True, "panels": sorted(only)})


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
