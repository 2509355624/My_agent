"""按**真实生图链路**跑一张，用来验收渠道配置（`moss-qunyou` / `moss8`）。

跟 `_run_moss_layout_once.py` 的区别：那个自己拼一份工作流做实验；这个直接读
`skills/<渠道>/workflow.json`，走 `image_jobs.process` —— 也就是 QQ 和漫画台
真正走的那条路（占位符替换、输出目录 stamp、进度监听、**采样降级自动重启**）。

用法：
    python tools/_verify_moss_channel.py moss-qunyou tools/_prompt_fox_girl.json
    python tools/_verify_moss_channel.py moss8 - [可选提示词文件]

Job 用 `target=None, quiet=True`：跑完只把结果留在 `job.entry`，不发 QQ、不进
任务账本——验收不打扰任何人。
"""

import json
import os
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from app import comfy_progress, image_jobs, skills          # noqa: E402
from app.tools.normal.generate_image import (              # noqa: E402
    apply_prompt_placeholders)

# moss8 的普通单图提示词（QQ 侧就是这种：标签堆，不排字、不写 Layout JSON）
MOSS8_PROMPT = (
    "1girl, solo, fox girl, blonde hair, green eyes, white nightgown, "
    "sitting on bed, bedroom, soft window light, looking at viewer, "
    "anime illustration, clean lineart, high quality"
)


def _load_workflow(skill):
    wf = skills.load_skill(skill)["workflow"]
    return json.loads(json.dumps(wf))       # 深拷贝，别污染缓存


def _set_steps(wf, steps):
    for node in wf.values():
        if node.get("class_type") == "KSampler":
            node["inputs"]["steps"] = steps
            return True
    return False


def main():
    skill = sys.argv[1] if len(sys.argv) > 1 else "moss-qunyou"
    prompt_arg = sys.argv[2] if len(sys.argv) > 2 else "-"
    if prompt_arg == "-":
        prompt = MOSS8_PROMPT
    else:
        with open(prompt_arg, encoding="utf-8") as fh:
            prompt = fh.read().strip()
    if skill.startswith("moss-qunyou"):
        json.loads(prompt)                  # Layout JSON：坏了现在就报

    wf = _load_workflow(skill)
    steps = None
    for node in wf.values():
        if node.get("class_type") == "KSampler":
            steps = node["inputs"]["steps"]
    if prompt_arg == "-":
        _set_steps(wf, 8)

    wf = apply_prompt_placeholders(json.dumps(wf, ensure_ascii=False),
                                   prompt, 20261028)
    wf_steps = [n["inputs"]["steps"] for n in wf.values()
                if n.get("class_type") == "KSampler"]
    on = [n["inputs"].get("lora_%d" % i) for n in wf.values()
          for i in range(1, 11)
          if n.get("class_type") == "MOSSLoRAToggle"
          and n["inputs"].get("on_%d" % i)]
    print("渠道 %s | 步数 %s | LoRA %s | 提示词 %d 字符"
          % (skill, wf_steps, [o for o in on if o] or "无", len(prompt)))

    comfy_progress.ensure_started("http://127.0.0.1:8188")
    job = image_jobs.Job(None, None, wf, skill=skill, quiet=True,
                         tag=time.strftime("VERIFY-%Y%m%d-%H%M%S"))
    t0 = time.time()
    image_jobs.process(job)
    cost = time.time() - t0

    if job.error:
        print("失败 %.1f 秒：%s: %s" % (cost, type(job.error).__name__, job.error))
        return 1
    names = image_jobs.output_images(job.entry)
    print("完成 %.1f 秒 | %s" % (cost, names))
    return 0


if __name__ == "__main__":
    sys.exit(main())
