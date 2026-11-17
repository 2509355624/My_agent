# -*- coding: utf-8 -*-
"""查几个候选节点的输入/输出签名，为替换缺节点做准备。"""
import json
import urllib.request

WANT = [
    "ImageScale", "ImageScaleBy", "ImageResize+", "ResizeImagesByLongerEdge",
    "ResizeImageMaskNode", "Image Resize (rgthree)",
    "UnetLoaderGGUF", "UnetLoaderGGUFAdvanced",
    "ComfySwitchNode", "QwenImage21Cache", "ModelAttentionBackend",
    "TextEncodeQwenImage21", "SaveImageAdvanced", "ImageStitch",
    "ResolutionSelector", "LoadImage", "PreviewAny", "PreviewImage",
]

with urllib.request.urlopen("http://127.0.0.1:8188/object_info", timeout=25) as r:
    OI = json.loads(r.read().decode("utf-8"))

for name in WANT:
    n = OI.get(name)
    if not n:
        print("%-32s ❌ 没有" % name)
        continue
    req = n.get("input", {}).get("required", {})
    opt = n.get("input", {}).get("optional", {})
    print("%-32s" % name)
    print("    out :", n.get("output_name") or n.get("output"), "->", n.get("output"))
    for k, v in req.items():
        t = v[0] if isinstance(v, list) and v else v
        if isinstance(t, list):
            t = "COMBO(%d)" % len(t)
        extra = v[1] if isinstance(v, list) and len(v) > 1 else {}
        print("    req : %-22s %s %s" % (k, t, extra if isinstance(extra, dict) and extra else ""))
    for k, v in opt.items():
        t = v[0] if isinstance(v, list) and v else v
        if isinstance(t, list):
            t = "COMBO(%d)" % len(t)
        print("    opt : %-22s %s" % (k, t))
    print()
