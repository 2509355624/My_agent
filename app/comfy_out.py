"""ComfyUI 输出目录分流：谁喊的图，就存进谁自己的文件夹。

## 它解决什么

`output/` 平铺着两千多张图，找「上周那个群画的那张」只能靠文件名猜。
ComfyUI 的 `SaveImage` 本来就支持 `filename_prefix` 带子目录（`qq/群_1/anima/Anima`），
目录不存在它会自己建 —— 所以不用我们搬文件，只要在**提交前**把前缀改掉。

## 目录长什么样

```
output/
├─ qq/
│  ├─ 群_885143276/anima-line/Anima_00042_.png
│  └─ 私聊_2596056376/qwen_image_v1/QwenI2I_00013_.png
├─ web/
│  ├─ 生图页/silver/Anima_00043_.png
│  └─ 漫画_C20261018-01/anima-line/Anima_00044_.png
└─ Anima_00045_.png        ← 你自己在 ComfyUI 终端手跑的（不带前缀改动）
```

## 关键取舍

- **只改保存节点**（`SaveImage` / `BatchPromptImageGenerator` 这三类）的前缀，
  不搬文件。搬完 `/view` 就找不到它了，还得给每一处取图逻辑补「去哪个目录找」；
  让 ComfyUI 自己往子目录里写，磁盘上一步到位，也没有「还没归档」的中间态。
- **改写必须幂等**：静默重抽 / 超时重跑会把同一个 workflow 再提交一次，
  每次都只取原前缀的 `basename` 再拼目录，不会叠成 `a/b/a/b/Anima`。
- **目录名只用号码**（`群_123` / `私聊_456`）：群名会改、会带 `:` `?` 这类
  非法字符，号码不会。要名字的那种做法见 `_safe` 的注释。
- **取图跟着要带 `subfolder`**：`output_images()` 直接返回相对路径
  （`qq/群_1/anima/Anima_0001_.png`），下游拿到的还是**一个字符串**，
  函数签名一个都不用改；真正拆开只在打 `/view` 那几处（`split_rel`）。
"""

import copy
import os
import re

# Windows 路径里的非法字符 + 控制字符。群号是数字，其实碰不到；留着是为了
# 哪天把群名放进来（`_safe("原神:交流")` → `原神_交流`）也不会炸。
_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# 相对路径的层级上限：防着哪天拼出个七八层的怪路径
_MAX_DEPTH = 4


def _safe(seg):
    """把一段路径文字变成能放进 Windows 文件名的样子。"""
    s = _BAD.sub("_", str(seg or "")).strip()
    s = s.strip(". ")                       # Windows 不允许结尾是点/空格
    return s or "_"


def split_rel(name):
    """`"qq/群_1/anima/Anima_0001_.png"` → `("Anima_0001_.png", "qq/群_1/anima")`。

    没有子目录时 subfolder 返回空串，正好是 ComfyUI `/view` 的默认值。
    反斜杠一并认（掉进日志/账本里的路径可能是 Windows 形态）。
    """
    s = str(name or "").replace("\\", "/").strip("/")
    if "/" not in s:
        return s, ""
    d, f = s.rsplit("/", 1)
    return f, d


def _join(*parts):
    segs = []
    for p in parts:
        for seg in str(p or "").replace("\\", "/").split("/"):
            seg = _safe(seg) if seg else ""
            if seg:
                segs.append(seg)
    return "/".join(segs[:_MAX_DEPTH])


def session_base(target, target_id):
    """会话 → 顶层目录。`group`/`private` 是 image_jobs 的两种 target。"""
    if target == "group":
        return _join("qq", "群_%s" % target_id)
    if target == "private":
        return _join("qq", "私聊_%s" % target_id)
    return _join("web", "生图页")


def base_for(job):
    """这个任务该落哪个目录（不含渠道那一层）。

    `job.out_dir` 优先：漫画工作台把它设成 `web/漫画_<任务号>`，因为任务号只有
    它在入队时知道（见 `comic._submit`）。其余情况按 target 推断：
    `None` 就是网页侧（生图页 / 网页对话生成）。
    """
    out = str(getattr(job, "out_dir", "") or "").strip()
    if out:
        return _join(out)
    return session_base(getattr(job, "target", None),
                        getattr(job, "target_id", None))


def body_for_target(target, target_id, skill):
    """按 target 直接算目录体，不经过 Job（重启补发那条路要用）。"""
    base = session_base(target, target_id)
    skill = str(skill or "").strip()
    return _join(base, skill) if skill else base


def body_for(job):
    """完整的目录体：`<会话>/<渠道>`，作为 `filename_prefix` 的前半截。"""
    base = base_for(job)
    skill = str(getattr(job, "skill", "") or "").strip()
    return _join(base, skill) if skill else base


# 会往 output 目录写图的节点（skills/ 里现存的三种）。判据不用「等于
# SaveImage」是因为 `_persona_fast` / 漫画批量用的是
# `BatchPromptImageGenerator`（一次出一串），漏掉它那一整类图就还躺在根目录。
_SAVERS = ("SaveImage", "BatchPromptImageGenerator",
           "DualModelBatchPromptImageGenerator")


def _is_saver(node):
    ct = str((node or {}).get("class_type") or "")
    return ct in _SAVERS or ct.startswith("SaveImage")


def stamp(workflow, body):
    """把 workflow 里所有保存节点的前缀改成 `<body>/<原名>`，返回**新**对象。

    新对象而不是原地改：`process` 会拿同一份 `job.workflow` 反复提交（重抽、
    重跑），原地改虽然幂等但会把「原名前缀」丢在 job 上，出问题不好追。
    找不到保存节点时原样返回（云端渠道 / 非常规工作流，不该因此卡住）。
    """
    if not isinstance(workflow, dict) or not body:
        return workflow
    out = copy.deepcopy(workflow)
    hit = False
    for node in out.values():
        if not isinstance(node, dict) or not _is_saver(node):
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        cur = str(inputs.get("filename_prefix") or "ComfyUI")
        # 只留最后一段再拼目录：重复调用不会叠成 a/b/a/b/Anima。
        name = os.path.basename(cur.replace("\\", "/")) or "ComfyUI"
        inputs["filename_prefix"] = "%s/%s" % (str(body).strip("/"), name)
        hit = True
    return out if hit else workflow


def describe(job):
    """日志用：这张图会落到哪个目录。"""
    return body_for(job) or "output 根目录"
