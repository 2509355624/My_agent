"""识图配置域：视觉 provider、模型、超时、缩放、提示词上限。

只放通用读图参数；生图审核的阈值在 image.py，两个 file 不互相 import。
"""

import os

# ─── 图片识别（视觉预处理）───────────────────────────
# 给没有视觉能力的模型用的：先把图交给这里识别成文字，再让 agent 接着跑。
# （实测一张图 246 + 340 token，比跑一轮对话还低。）
#
# 2026-10-03 试过把读图改走本机 ollama 省成本，实测**热态 24~42 秒**（冷启 65 秒，
# 见下面 OLLAMA_VISION_* 的注释），而云端 MiMo 只要 1~2 秒 —— 用户当天拍板
# **改回云端**。本地这条路留着当备选，不用改代码：把 .env 的 VISION_PROVIDER
# 改成 `ollama`、VISION_MODEL 填 `qwen3-vl:2b` 即可（app/vision.py 里那条原生
# /api/chat 分支还在，且实测 OCR 正确、英文报错原样保留不翻译）。
#
# 审核（app/image_audit.py）**不做本地档**：它超时只有 30 秒且 fail-closed，
# 本地那个速度会把每张图都拦死。审核永远走这里配的 VISION_PROVIDER。
VISION_PROVIDER = os.getenv("VISION_PROVIDER", "deepseek")
# 空 = 用该 provider 的默认模型
VISION_MODEL = os.getenv("VISION_MODEL", "")

# 本地识图（ollama）跑在 CPU/内存，别跟 ComfyUI 抢显存。为什么不用给 ollama
# 服务设 CUDA_VISIBLE_DEVICES：那要重启 ollama，且会影响**所有** ollama 调用；
# 走原生 /api/chat 的 options 就能按次指定（见 app/vision.py 的 _post_ollama）。
# 实测生效（`ollama ps` 显示 100% CPU、2.0GB 常驻内存、显存零占用）。
OLLAMA_VISION_NUM_GPU = int(os.getenv("OLLAMA_VISION_NUM_GPU", "0"))
# 用完多久卸载模型。2B 模型常驻内存不划算，而且这台机器可用内存常在
# 1.2~2.9GB 之间晃——给个 5 分钟，闲下来就还回去。冷启要重付一次加载成本
# （实测冷 65s / 热 24~42s），但常驻 2GB 更不划算。
OLLAMA_VISION_KEEP_ALIVE = os.getenv("OLLAMA_VISION_KEEP_ALIVE", "5m")

# 识图是单次同步调用，正常几秒返回；超时按失败降级。
# 2026-09-29 从 30 秒放宽到 120 秒：识图走的是 mimo（火山 429 后所有请求都压到它），
# 高峰期实测单张能拖到 30 秒以上——30 秒会把「慢但成功」的调用误判成失败。
# 注意：这是 requests 的「单次 socket 操作」超时（含首字节等待），不是整次请求
# 的总时长上限；服务端若持续滴数据，仍可能超过这个值。
VISION_TIMEOUT = float(os.getenv("VISION_TIMEOUT", "120"))
# 发出去前把图缩到最长边这么多像素。QQ 群里的图有 8MB 级的，直接 base64
# 是 11MB 字符串——既慢又贵，还可能撞服务端请求体上限
VISION_MAX_EDGE = int(os.getenv("VISION_MAX_EDGE", "1024"))

# 自定义**识图（通用读图）提示词**的长度上限（字符），2026-10-02 加。
# 口径与上面那条一致：识图也是**每张图都要重发一遍**的隐形调用，写长了纯烧
# token；但它不像审核那样「丢了输出格式就全拦」，所以长度之外不再做别的校验。
# 内置默认那份约 100 字，2000 够写一版很细的读图要求。
VISION_PROMPT_MAX = int(os.getenv("VISION_PROMPT_MAX", "2000"))


__all__ = [name for name in globals()
           if name.isupper() and not name.startswith("_")]
