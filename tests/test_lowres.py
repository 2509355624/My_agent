"""工作台低分辨率档（`app/features/image/lowres.py`）的算术守卫。

用户 2026-11-26 的原话：「为什么要先生成一个完整的七八兆的原图，然后再
压缩发送？为什么不干脆直接生成低分辨率的图片？」

这里守四件事：
1. 画布真降了一档，且是 8 的倍数（VAE 下采样要求）；
2. 成品尺寸**不变**——倍率补回来了，不是把图做小；
3. 认不出画布 / 没有放大链的工作流**一字不动**；
4. 只动延迟倍率和画布，别的节点碰都不碰。
"""
import copy
import json
import os
import unittest

from app.features.image import lowres

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(skill):
    """读真实渠道模板（带 __SEED__ 占位，先替换成数字再解析）。"""
    path = os.path.join(ROOT, "skills", skill, "workflow.json")
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        text = f.read()
    text = text.replace("__SEED__", "1").replace("__MULTI_PROMPTS__", "x")
    return json.loads(text)


def _canvas(wf):
    for node in wf.values():
        if not isinstance(node, dict):
            continue
        ins = node.get("inputs") or {}
        ct = node.get("class_type") or ""
        if "width" in ins and "height" in ins and (
                ct.startswith("Empty") or "PromptImageGenerator" in ct):
            return ins["width"], ins["height"]
    return None


class TestLowresPlan(unittest.TestCase):

    def test_silver_halves_canvas_and_keeps_output(self):
        wf = _load("silver")
        if wf is None:
            self.skipTest("skills/silver/workflow.json 不在")
        before = _canvas(wf)
        p = lowres.plan(wf)
        self.assertTrue(p["on"], p.get("why"))
        self.assertEqual(before, (1024, 1536))
        self.assertEqual(p["canvas"], (512, 768))
        # 成品不能变小：原来 1024×1.2×2 = 2457，降档后必须还是这个量级
        self.assertGreaterEqual(p["est"][0], before[0] * 2)
        self.assertGreater(p["latent"], 1.0, "画布降了，延迟倍率必须补上去")

    def test_canvas_is_multiple_of_8(self):
        for skill in ("silver", "krea2", "anima_curvy", "hd_3_clear"):
            wf = _load(skill)
            if wf is None:
                continue
            p = lowres.plan(wf)
            if not p["on"]:
                continue
            for v in p["canvas"]:
                self.assertEqual(v % 8, 0, "%s 画布 %r 不是 8 的倍数" % (skill, v))

    def test_no_upscale_chain_is_left_alone(self):
        # moss 系没有放大链：降了成品就真变小了，所以不降
        wf = _load("moss-qunyou")
        if wf is None:
            self.skipTest("skills/moss-qunyou 不在")
        p = lowres.plan(wf)
        self.assertFalse(p["on"])

    def test_unknown_workflow_is_left_alone(self):
        for wf in ({}, {"1": {"class_type": "KSampler", "inputs": {}}},
                   {"1": {"class_type": "EmptyLatentImage",
                          "inputs": {"width": "x", "height": 1}}}):
            p = lowres.plan(wf)
            self.assertFalse(p["on"])

    def test_apply_only_touches_canvas_and_latent(self):
        wf = _load("silver")
        if wf is None:
            self.skipTest("skills/silver/workflow.json 不在")
        orig = copy.deepcopy(wf)
        p = lowres.apply(wf)
        self.assertTrue(p["on"], p.get("why"))
        for nid, node in orig.items():
            now = wf[nid]
            if node.get("class_type") == "LatentUpscaleBy":
                self.assertNotEqual(now["inputs"]["scale_by"],
                                    node["inputs"]["scale_by"])
                # 只动 scale_by，别的字段原样
                for k, v in node["inputs"].items():
                    if k != "scale_by":
                        self.assertEqual(now["inputs"][k], v, nid + "." + k)
                continue
            if node.get("class_type") == "EmptyLatentImage":
                self.assertEqual((now["inputs"]["width"],
                                  now["inputs"]["height"]), p["canvas"])
                continue
            self.assertEqual(now, node, "节点 %s 不该被改" % nid)

    def test_apply_is_idempotent_safe_on_refusal(self):
        wf = {"1": {"class_type": "KSampler", "inputs": {}}}
        snap = copy.deepcopy(wf)
        p = lowres.apply(wf)
        self.assertFalse(p["on"])
        self.assertEqual(wf, snap)


if __name__ == "__main__":
    unittest.main()
