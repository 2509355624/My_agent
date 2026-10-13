"""漫画工作台的「生图模式」：单人写真集（2026-10-13 用户拍板四项）。

和剧本模式的关系：共用 ① 卡的输入框和模型选择，但**不共用提示词规则**——
剧本模式是 POV 多格叙事（规则 14–20 全量），生图模式是**单人写真**：画面里
只有女主一个人，没有观众、没有剧情、没有台词，就是一条条纯画面提示词各出
一张图。所以这里另写一套提示词，绝不拼 comic_agent 的 POV 规则块。

流水线（用户拍板）：
  1. 渠道 anima-line（与漫画场景同渠道，渲染参数卡直接复用，画风一致）；
  2. N 条变体各一张（提示词员一次写 N 条不同画面：场景/服装/表情/构图）；
  3. 女主设定独立框（外观一次填好，每条提示词原样拼前头，人物不跑样）；
  4. 独立任务目录（没有漫画任务也能用）：comics/<TASK>/portraits/。

存储（都在任务目录下）：
  portrait.json   {brief, heroine, n, created}——这批写真的需求快照
  prompts.json    [{no, prompt, seed, state, error}]——逐条账本（换种子重跑靠它）
  portraits/NN.png  成图

约定与漫画侧一致：`_submit_retry` 只重试超时、`_download` 只重取文件；
渲染参数（render.json）开跑时读一次，整批同一套。NAI 云端不走这里——
生图模式锁 anima-line（用户 2026-10-13 选定）。
"""

import json
import logging
import os
import time

from app import comic, comic_render_opts, image_jobs
from app.tools.normal import generate_image
from app.skills import load_skill

log = logging.getLogger("comic")

# 生图模式锁定的渠道（与漫画场景同一个：渲染参数卡就是它的，直接复用）
SKILL = comic.SCENE_SKILL

# 一批最多几张（界面默认 4，上限留宽点）
MAX_SHOTS = 12


class PortraitError(Exception):
    """提示词员没交齐作业（空 / 解析不出 / 条数不够）——提示重跑，不猜不补。"""


_PORTRAIT_SYSTEM = """你是写真集提示词员。你写的是**单人写真**的生图提示词，不是故事。

画面里从头到尾只有女主一个人（提示词以 1girl 开头）。没有观众、没有第二个人、
没有「我」的视角、没有手部交互、没有剧情、没有对白。

每条提示词就是一张独立的写真，写清楚：
- 场景（在哪儿：教室/花田/天台/海边……）
- 服装（具体到款式和颜色，颜色你自己拍板；同一个设定里同一件衣服颜色保持一致）
- 发型发色、表情、姿势
- 构图（全身 / 半身 / 特写，机位高度）
- 光线和氛围（午后阳光 / 黄昏逆光 / 室内暖光……）

女主的外观设定会原样贴在每条提示词最前面，你不要改动它，只在后面接画面内容。

铁律：
- 只写「要什么」，不写否定句（不写「没有」「不要」「避免」）。
- 不写序号、不写标题、不写解释，一条一行，纯提示词。
- N 条之间必须有区分度：场景 / 服装 / 构图 / 表情，每条至少换一样。
- 用英文写（Danbooru 风格标签串或自然语句都行），这是给生图模型看的。"""


def write_prompts(brief, heroine, n, provider=None, model=None, progress=None):
    """让提示词员写 N 条单人写真提示词，返回 list[str]（长度必须 == n）。

    条数不齐 = 没交齐作业：报 PortraitError 让上层提示重跑，**不猜不补**
    （用户 2026-10-12 拍板的哲学：代码只做检测，判断权留给 AI）。
    """
    from app import llm

    n = max(1, min(MAX_SHOTS, int(n)))
    heroine = (heroine or "").strip() or "1girl"
    brief = (brief or "").strip()
    if not brief:
        raise PortraitError("需求是空的——想拍什么总得写一句")
    user = ("女主设定：%s\n\n需求：%s\n\n请写 %d 条单人写真提示词，每条一行。"
            % (heroine, brief, n))
    if progress:
        progress("提示词员动笔（%d 条）" % n)
    text = llm.call_llm([{"role": "system", "content": _PORTRAIT_SYSTEM},
                         {"role": "user", "content": user}],
                        provider=provider, model=model)
    lines = parse_lines(text)
    if len(lines) < n:
        raise PortraitError(
            "提示词员只交了 %d 条，要 %d 条——重跑一次（它偶尔会少写几行）"
            % (len(lines), n))
    return lines[:n]


def parse_lines(text):
    """把模型回复解析成提示词行：剥代码围栏 / 序号 / 列表符 / 空行。

    只做「格式事实」清理，不改内容。解析出 0 行 = 没交作业。
    """
    import re

    out = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("```"):
            continue
        line = re.sub(r"^(?:#{1,6}\s*|\[?\d+\]\s*|[-*]\s+|\d+[.、)]\s*)", "",
                      line).strip()
        line = re.sub(r"^(?:提示词|画面|写真)\s*[:：]\s*", "", line).strip()
        if line:
            out.append(line)
    return out


# ─── 存储 ───────────────────────────────────────────────

def portrait_path(task_dir):
    return os.path.join(task_dir, "portrait.json")


def prompts_path(task_dir):
    return os.path.join(task_dir, "prompts.json")


def shots_dir(task_dir):
    return os.path.join(task_dir, "portraits")


def shot_path(task_dir, no):
    return os.path.join(shots_dir(task_dir), "%s.png" % str(no).zfill(2))


def load_prompts(task_dir):
    """读账本；没有 / 读坏 → 空列表（界面显示「还没有写真」）。"""
    fp = prompts_path(task_dir)
    if not os.path.isfile(fp):
        return []
    try:
        with open(fp, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def save_prompts(task_dir, shots):
    with open(prompts_path(task_dir), "w", encoding="utf-8") as f:
        json.dump(shots, f, ensure_ascii=False, indent=1)


def save_meta(task_dir, brief, heroine, n):
    with open(portrait_path(task_dir), "w", encoding="utf-8") as f:
        json.dump({"brief": brief, "heroine": heroine, "n": n,
                   "created": time.strftime("%Y-%m-%d %H:%M:%S")},
                  f, ensure_ascii=False, indent=1)


# ─── 跑图 ───────────────────────────────────────────────

def _render_one(task_dir, no, prompt, seed, skill=SKILL, opts=None):
    """一条提示词 → 一张图，落 portraits/NN.png。返回更新后的账本条目。"""
    if opts is None:
        opts = comic_render_opts.load(task_dir, skill)
    wf = generate_image.build_t2i_workflow(skill, prompt, seed)
    if wf is None:
        raise RuntimeError("skill %s 没有文生图工作流" % skill)
    wf = comic_render_opts.apply(wf, opts)
    os.makedirs(shots_dir(task_dir), exist_ok=True)
    name = comic._submit_retry(
        lambda: comic._submit(wf, skill, prompt, seed), "[写真 %s]" % no)
    comic._download(name, shot_path(task_dir, no))
    return {"no": no, "prompt": prompt, "seed": seed,
            "state": "rendered", "error": None}


def run(task_dir, brief, heroine, n, provider=None, model=None,
        skill=SKILL, progress=None):
    """一批写真：写 N 条提示词 → 逐条出图 → 账本落盘。

    单条失败不拖垮整批：那一条记 error，其余照跑（失败态在图里看得见原因，
    和漫画格一个规矩——失败态要「能看见原因」+「有出口按钮」）。
    """
    prompts = write_prompts(brief, heroine, n, provider=provider, model=model,
                            progress=progress)
    comic._require_comfy()
    opts = comic_render_opts.load(task_dir, skill)
    save_meta(task_dir, brief, heroine, len(prompts))
    shots = []
    for i, prompt in enumerate(prompts, 1):
        no = "%02d" % i
        seed = comic._new_seed()
        if progress:
            progress("第 %s 张出图中（%s）" % (no, prompt[:24] + "…"))
        try:
            shot = _render_one(task_dir, no, prompt, seed, skill=skill, opts=opts)
        except Exception as e:
            log.warning("写真 %s 失败：%s", no, e)
            shot = {"no": no, "prompt": prompt, "seed": seed,
                    "state": "error", "error": str(e)}
        shots.append(shot)
        save_prompts(task_dir, shots)      # 每条一落，刷新页面不丢进度
    return shots


def reseed_shot(task_dir, no, skill=SKILL):
    """换种子重跑某一张：提示词不变，种子重抽。返回更新后的账本条目。"""
    shots = load_prompts(task_dir)
    hit = next((s for s in shots if str(s.get("no")) == str(no).zfill(2)), None)
    if hit is None:
        raise PortraitError("没有 %s 这张写真" % no)
    if not (hit.get("prompt") or "").strip():
        raise PortraitError("第 %s 条没有提示词，不能重跑" % no)
    comic._require_comfy()
    seed = comic._new_seed()
    try:
        new = _render_one(task_dir, hit["no"], hit["prompt"], seed, skill=skill)
    except Exception as e:
        hit.update(state="error", error=str(e), seed=seed)
        save_prompts(task_dir, shots)
        raise
    hit.update(new)
    save_prompts(task_dir, shots)
    return hit


def has_portraits(task_dir):
    return bool(load_prompts(task_dir))
