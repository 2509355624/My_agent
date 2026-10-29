"""跑图台「自定义步数」：只改工作流里的采样步数，不碰别的字段。

## 为什么单独一层

步数散在各渠道模板的 `KSampler.inputs.steps` 里，而且**不是每个渠道都只有一段**：
`silver` 是 8 + 12 两段（先粗后细）、`hd_3_*` 是 15 + 15、`moss-qunyou` 是单段 30。
跑图台要的是「这次用 N 步」——所以约定是**所有带 `steps` 的采样节点都改成 N**，
多段的渠道等于两段同步，不做比例缩放（缩放会让人猜不出实际跑了多少步）。

只认键名恰好是 `steps` 的整数字段：
- `hires_steps` 之类不改——那是二次放大的独立开关，改了就不是「这次用 N 步」；
- 值是列表（节点连线）的不改——那是上游节点喂的，不是可调数字。

和 `lora_slots` 同一条理由：模板里有 `__SEED__` / `__MULTI_PROMPTS__` 这种
非 JSON 字面量，**绝不能 json.dump 回写**；本模块只改内存里的 dict（提交前
覆盖），要落盘请走 `comic_render_opts.apply` 那套定点替换。
"""

STEPS_RANGE = (1, 100)


def _iter_steps_slots(wf):
    """产出 (节点 dict, 键名) —— 只挑值是整数、键名恰好是 `steps` 的输入。"""
    if not isinstance(wf, dict):
        return
    for node in wf.values():
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        if "steps" not in inputs:
            continue
        val = inputs["steps"]
        # bool 是 int 的子类，得单独挡掉（`true` 不是步数）
        if isinstance(val, bool) or not isinstance(val, int):
            continue
        yield inputs


def read_steps(wf):
    """模板里当前所有采样步数（按节点顺序）。没有返回空列表。"""
    return [int(i["steps"]) for i in _iter_steps_slots(wf)]


def describe(wf):
    """给界面看的默认步数：全都一样就 "30"，多段不同就 "8+12"。

    跑图台的输入框用它当 placeholder——用户留空就是「按渠道默认」。
    """
    vals = read_steps(wf)
    if not vals:
        return ""
    uniq = sorted(set(vals))
    if len(uniq) == 1:
        return str(uniq[0])
    return "+".join(str(v) for v in vals)


def normalize(value):
    """把前端传来的步数规整成 int。

    返回 `(步数, 错误)`：
    - 空值 / None → `(None, None)`，表示「不覆盖，按模板」；
    - 非法或越界 → `(None, 错误话术)`，调用方据此 400。
    """
    if value is None:
        return None, None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None, None
        try:
            value = int(s)
        except ValueError:
            return None, "步数要填整数，收到 %r" % s
    if isinstance(value, bool) or not isinstance(value, int):
        return None, "步数要填整数"
    lo, hi = STEPS_RANGE
    if not lo <= value <= hi:
        return None, "步数超出 %d~%d（收到 %d）" % (lo, hi, value)
    return value, None


def apply_steps(wf, steps):
    """把工作流里所有采样步数改成 `steps`，返回改了几个节点。

    改了 0 个不代表失败——NAI 这类没有本地工作流的渠道压根不会调到这里，
    而带 `steps` 的渠道正常都 ≥1。调用方按返回值为 0 打条警告即可。
    """
    steps, err = normalize(steps)
    if err or steps is None:
        return 0
    n = 0
    for inputs in _iter_steps_slots(wf):
        if inputs["steps"] != steps:
            inputs["steps"] = steps
            n += 1
    return n
