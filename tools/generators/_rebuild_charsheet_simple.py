import json

SRC_A = "skills/qwen-charsheet/workflow.json"
SRC_B = "skills/qwen-charsheet/workflow_i2i.json"

# 新的静态提示词：只有「主角色 + 三视图」，纯人物、无文字/色卡
NEW_PROMPT = (
    "CANVAS & STYLE: square 1:1, off-white paper, thin black border. "
    "Extract and strictly preserve the exact visual style, rendering technique, "
    "line quality, shading method, and color palette from the reference image. "
    "The sheet must match the reference's aesthetic completely. "
    "No text, labels, titles, or watermarks anywhere.\n\n"
    "TOP SECTION (y:0-58%): One large full-body main view of the character, "
    "centered, standing pose, head-to-toe, calm expression, facing slightly "
    "toward viewer. Strictly preserve exact character design, clothing, materials, "
    "and visual effects from the reference image. Extract background style from "
    "reference or use a neutral gradient.\n\n"
    "BOTTOM SECTION (y:58-100%): Three full-body views side by side on a single "
    "baseline with soft shadows, Front view, Side view, Back view. Keep the same "
    "scale across all three. Strictly preserve design consistency (silhouette, "
    "outfit, hair, proportions) across every angle.\n\n"
    "CONSTRAINTS: Strictly preserve the reference image's exact visual style, "
    "rendering technique, color palette, character design, proportions, and "
    "details. Extract all visual data from the input image. No style drift, no "
    "redesigns, no extra objects, no missing limbs, no overlapping panels. "
    "Absolutely no text, labels, numbers, or watermarks in any section."
)

PLACE = '"__SEED_PLACEHOLDER__"'

def transform(path):
    raw = open(path, encoding="utf-8").read()
    raw2 = raw.replace("__SEED__", PLACE)
    wf = json.loads(raw2)

    # 画布 1536x1536（简化版，更快 + 每面板更大）
    wf["7"]["inputs"]["width"] = 1536
    wf["7"]["inputs"]["height"] = 1536
    # 步数回到 15（放弃 18 步 + 4x 那条慢路）
    wf["9"]["inputs"]["steps"] = 15
    # 提示词换成 主角色 + 三视图
    wf["8"]["inputs"]["prompt"] = NEW_PROMPT + "\n\n__MULTI_PROMPTS__"
    # 移除 4x 超分链（节点 12/13/14）
    for k in ("12", "13", "14"):
        wf.pop(k, None)
    # SaveImage 直连 VAEDecode（去掉 4x）
    wf["11"]["inputs"]["images"] = ["10", 0]

    out = json.dumps(wf, indent=2, ensure_ascii=False)
    out = out.replace(PLACE, "__SEED__")
    open(path, "w", encoding="utf-8").write(out)
    print("wrote", path, "| nodes:", len(wf),
          "| canvas:", wf["7"]["inputs"]["width"], "x", wf["7"]["inputs"]["height"],
          "| steps:", wf["9"]["inputs"]["steps"])

transform(SRC_A)
transform(SRC_B)
