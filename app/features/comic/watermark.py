"""漫画工作台：给成品图叠水印（2026-11-21）。

用户作品被搬运 → 要在**带气泡的成品图**上叠自己的平台水印（如 `dy：233`），
存到**独立目录**，绝不动 raw/ 和 final/ 里的原图。

设计：
- 纯函数 `stamp(src, dst, marks)`：读源图 → 按 marks 叠字 → 原子写目标。
- marks 里坐标是**相对比例** 0~1（前端点哪里就传 0~1），这样一张图定好的位置
  套到所有格子都落在同样的相对位置，不同尺寸的图也不会跑偏。
- 样式固定白字黑描边、字号按图宽缩放——用户只选位置与文字，样式不掺和。
- 目标已存在就覆盖（用户重做水印时不要留旧版本，和需求 1 的覆盖一个口径）。
- Pillow 已在 requirements.txt；中文字体用系统微软雅黑，缺字体时降级到默认。
"""
import os
import tempfile

from PIL import Image, ImageDraw, ImageFont

# 中文字体候选：优先雅黑（细、清晰），再黑体。找不到就用 PIL 默认。
_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
)

# 字号 = 图宽 * 这个比例；再按字数微调，长文字不至于顶出画面。
_SIZE_RATIO = 0.045
_MIN_SIZE = 16
# 描边宽度按比例，最小 1。
_STROKE_RATIO = 0.0035


def _font(size):
    for p in _FONT_CANDIDATES:
        if os.path.isfile(p):
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _clamp(v, lo=0.0, hi=1.0):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return max(lo, min(hi, f)) if f == f else None  # NaN 也挡掉


def _atomic_save(img, dst):
    """先写同目录 .tmp 再 os.replace，避免半张图被页面读到。"""
    directory = os.path.dirname(os.path.abspath(dst)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".wm_", suffix=".png", dir=directory)
    os.close(fd)
    try:
        img.save(tmp, "PNG")
        os.replace(tmp, dst)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def normalize_marks(marks):
    """清洗前端传来的 mark 列表；返回 [(text, rx, ry), ...]。

    只保留有文字、坐标合法的项，最多 8 个——防手抖点一串把画面糊满。
    坐标已在 0~1 内，不在这里换算成像素（像素换算要等知道图宽）。
    """
    out = []
    for m in (marks or []):
        if not isinstance(m, dict):
            continue
        text = str(m.get("text") or "").strip()
        if not text:
            continue
        rx, ry = _clamp(m.get("x")), _clamp(m.get("y"))
        if rx is None or ry is None:
            continue
        out.append((text[:40], rx, ry))
        if len(out) >= 8:
            break
    return out


def stamp(src, dst, marks):
    """把 marks 叠到 src，写到 dst（覆盖），返回实际落地的水印数。

    src 只读、不修改。marks 为空时直接复制一份到 dst（仍走原子写）。
    """
    src = os.path.abspath(src)
    dst = os.path.abspath(dst)
    items = normalize_marks(marks)
    with Image.open(src) as im:
        img = im.convert("RGBA")
    w, h = img.size
    draw = ImageDraw.Draw(img)
    base = max(_MIN_SIZE, int(w * _SIZE_RATIO))
    stroke = max(1, int(w * _STROKE_RATIO))
    for text, rx, ry in items:
        size = max(_MIN_SIZE, int(base * min(1.0, 12.0 / max(1, len(text)))))
        font = _font(size)
        # 以点击点为水印**中心**，这样所见即所得，不会因为文字长度偏到一边。
        box = draw.textbbox((0, 0), text, font=font, stroke_width=stroke)
        tw, th = box[2] - box[0], box[3] - box[1]
        x = int(rx * w - tw / 2.0 - box[0])
        y = int(ry * h - th / 2.0 - box[1])
        x = max(0, min(x, max(0, w - tw)))
        y = max(0, min(y, max(0, h - th)))
        draw.text((x, y), text, font=font, fill=(255, 255, 255, 255),
                  stroke_width=stroke, stroke_fill=(0, 0, 0, 255))
    _atomic_save(img.convert("RGB"), dst)
    return len(items)


def safe_name(text, fallback="wm"):
    """把水印文字变成能进文件名的片段：`dy：233` → `dy233`。

    只留中文/字母/数字/下划线/短横；冒号、空格、斜杠这类全去掉。
    """
    import re
    s = re.sub(r"[^\w\u4e00-\u9fff-]+", "", str(text or "")).strip("-_")
    return (s or fallback)[:24]
