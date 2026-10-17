import json

SRC = ["skills/qwen-charsheet/workflow.json",
       "skills/qwen-charsheet/workflow_i2i.json"]

# ③ 肖像 + 三视图（纯人物、无文字、横屏）
NEW_PROMPT = (
    "CANVAS & STYLE: wide 16:9 landscape, off-white paper, thin black border. "
    "Extract and strictly preserve the exact visual style, rendering technique, "
    "line quality, shading method, and color palette from the reference image. "
    "The sheet must match the reference's aesthetic completely. "
    "No text, labels, titles, or watermarks anywhere.\n\n"
    "LEFT PORTRAIT (x:0-45%): A large, detailed close-up portrait of the "
    "character's face and upper body, head turned slightly, calm expression. "
    "Fill the whole panel - this is the main focal point. Strictly preserve the "
    "exact character design, facial features, hair, clothing details, and visual "
    "effects from the reference image.\n\n"
    "RIGHT SECTION (x:45-100%): Three full-body views side by side on a single "
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

for path in SRC:
    raw = open(path, encoding="utf-8").read()
    wf = json.loads(raw.replace("__SEED__", PLACE))
    wf["7"]["inputs"]["width"] = 2048
    wf["7"]["inputs"]["height"] = 1152
    wf["9"]["inputs"]["steps"] = 15
    wf["8"]["inputs"]["prompt"] = NEW_PROMPT + "\n\n__MULTI_PROMPTS__"
    out = json.dumps(wf, indent=2, ensure_ascii=False).replace(PLACE, "__SEED__")
    open(path, "w", encoding="utf-8").write(out)
    print("wrote", path, "|", wf["7"]["inputs"]["width"], "x",
          wf["7"]["inputs"]["height"], "| steps", wf["9"]["inputs"]["steps"])
