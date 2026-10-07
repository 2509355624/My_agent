"""
图片识别：把图变成文字，交给没有视觉能力的模型。

为什么需要这一层：火山那条线（v4flash / v4-pro）是纯文本模型。把 base64 图
直接喂进去不会报错，而是**整条请求挂死**——实测 ReadTimeout 卡满 180 秒才断。
模型把那一长串 base64 当普通文本读，token 涨到几万，服务端一直不返回。所以
带图请求必须先在这里过一道：图 → 文字 → 正常进 agent 循环。

本模块只做三件事：下载 / 压缩 → 转 data URL → 调识图 provider 要一段文字。
不落盘、不进 history、不碰 agent 循环的任何状态。

识图用哪家**可以运行时切**（2026-10-04）：管理页「识图模型」下拉写进
agents/<id>/settings.json 的 vision_model（"provider:model"），本模块的
active_choice() 每次调用都重读（load_settings 自带 mtime 缓存，代价可忽略），
所以切换**热生效、不用重启**。没配过就退回 .env 的 VISION_PROVIDER/VISION_MODEL
——老配置继续有效。

⚠️ **审核不跟着切**（用户 2026-10-04 拍板）：app/image_audit.py 走
audit_choice()，永远用 .env 配的那个（云端）。理由是审核是 fail-closed 且超时
只有 30 秒，本地小模型实测要几十秒到几分钟（2026-10-04 实测跑 4 分半没返回），
跟着切会把每张图都拦下来、根本发不出去。
"""

import base64
import io
import logging
import os
import re
import time

import requests
# 直接引 urllib3 的 Timeout：requests 的 timeout 参数写数字时只是「单个 socket
# 操作」的上限，要「整次请求的总上限」只能用这个对象（requests 会原样透传，
# 见 requests/adapters.py 的 HTTPAdapter.send）。
from urllib3.util import Timeout as _UrllibTimeout

from app.config import (OLLAMA_VISION_KEEP_ALIVE, OLLAMA_VISION_NUM_GPU,
                        OLLAMA_BASE_URL, PROVIDERS, QQ_AGENT_ID,
                        VISION_MAX_EDGE, VISION_MODEL, VISION_PROVIDER,
                        VISION_TIMEOUT)

log = logging.getLogger("vision")

# 识图**专用**降级链（2026-10-07 用户拍板「写死在识图专用链」）。
#
# 和主对话的 LLM_FALLBACK_CHAIN 是**两份独立配置**，不能合并：
# 主对话链是按「文本能力 + 成本」排的（volc → mimo → deepseek），而识图要的是
# 「能不能读图」——纯文本的 volc 排第一会让每次识图都先撞一次挂死（见文件顶部
# 那条 base64 挂死说明）。所以这里只放确认能读图的云端家：豆包多模态 Seed →
# 小米 MiMo（全模态）→ DeepSeek 官方（deepseek-flash 可读图）。
#
# 只在**走管理页/.env 那条路**（describe 没显式传 provider）时生效；显式传
# provider 的调用（生图审核 image_audit、管理页试读）**不参与**——审核是
# fail-closed 且要固定用 .env 那份，不能被降级链悄悄换成别家。
VISION_FALLBACK_CHAIN = ("doubao", "mimo", "deepseek")


def _chain_candidates(primary_pid, primary_model):
    """识图要依次尝试的 (provider, model) 列表——选中的那家排第一。

    第一项就是管理页/.env 选的（可能是 ollama/llama 本地），后面接
    VISION_FALLBACK_CHAIN 里去掉重复的云端家。model 传空串 = 用那家的默认
    模型（describe 会自己回落 cfg["model"]）。

    为什么允许「本地失败 → 掉云端」：本地模型跑内存、速度慢且可能没 pull，
    失败就整轮看不到图；掉云端至少能把图读出来。用户要的是「降级链」，不是
    「本地专用锁」。
    """
    out = [(primary_pid, primary_model)]
    for pid in VISION_FALLBACK_CHAIN:
        if pid == primary_pid:
            continue                     # 选中的那家已经在第一项，别重复试
        if pid not in PROVIDERS:
            continue                     # 配置里没这家（被删了）就跳过
        out.append((pid, ""))
    return out


def active_choice(agent_id=None):
    """当前生效的识图 (provider, model)。管理页没配过 = 走 .env 那两个常量。

    每次调用都重读 settings（agent_store.load_settings 自带 mtime 缓存），
    所以在管理页切完立刻生效，不用重启进程。**故意不在 import 时定格**——
    定格了就等于又变成要改 .env + 重启的老路。

    返回的 model 允许是空串：意思是「该 provider 的默认模型」（如 deepseek
    不指定 model 时用它自己的 deepseek-flash）。
    """
    try:
        from app import agents as agent_store
        pid, model = agent_store.vision_choice(agent_id or QQ_AGENT_ID)
    except Exception:                    # noqa: BLE001
        # 配置层坏了不该让识图跟着挂——退回 .env，行为与改动前完全一致。
        log.warning("读识图选择失败，改用 .env 的配置", exc_info=True)
        return (VISION_PROVIDER, VISION_MODEL)
    if not pid:
        return (VISION_PROVIDER, VISION_MODEL)
    return (pid, model)


def audit_choice():
    """生图审核用的 (provider, model) —— **永远走 .env，不跟着界面切**。

    用户 2026-10-04 拍板。判据是审核的失败方向：它是 fail-closed 且超时只有
    30 秒（IMAGE_AUDIT_TIMEOUT），本地小模型实测要几十秒到几分钟 ⇒ 跟着切
    会把每张图都判成"识图失败"直接拦下来，图片根本发不出去。
    对话那条链路（active_choice）慢一点只是这一轮慢，失败还能退化成纯文本。
    """
    return (VISION_PROVIDER, VISION_MODEL)

# 下载图片与调识图接口都不走本机系统代理：本机常驻 Clash 类工具会把代理写进
# 注册表，代理进程一换端口或被杀，这两个出网口就全挂——图生图取图失败、识图
# 失败都是它引起的（2026-09-26 实撞：ProxyError 指向没人监听的 65532）。
# 与 qq_api / comfy_src / image_out / model_catalog 同款。
_session = requests.Session()
_session.trust_env = False

# ─── 总超时闸门 ──────────────────────────────────────
# requests 的 timeout 传数字时，那是**单个 socket 操作**的上限，不是整次请求的
# 上限：建连可以烧满一次，读又能再烧满一次。实测最坏一次识图吃掉 240 秒
# （120 建连 + 120 读），把整条会话线堵死——2026-09-29 群聊一轮 298 秒就是这么
# 来的（240 秒卡在识图，后面还有几十秒在等对话模型）。
# urllib3 的 Timeout(total=...) 才是真正的总闸门：读的配额 = total - 已耗时，
# 整次调用封顶在 total 之内（urllib3/connectionpool.py 里 read_timeout 就是
# 这么算出来再 settimeout 到 socket 上的）。
_CONNECT_TIMEOUT = 10


def _deadline(total, connect=_CONNECT_TIMEOUT):
    """把「整次请求的总时长上限」包成 requests 认识、urllib3 会真正执行的超时。

    建连单独给短值：连不上就快点失败，别把总预算耗在建连上。total 比 connect
    还小时取 total，免得构造出「连接超时 > 总超时」这种自相矛盾的配置。
    """
    total = float(total)
    return _UrllibTimeout(total=total, connect=min(connect, total), read=total)


# 提示词：描述画面 + 原样提取文字。实测输出约 340 token，信息密度够用。
# 「原样」和「不要翻译」两句不能省——少了它们，模型会顺手把报错截图里的
# 英文翻成中文，而 agent 后续要靠原文去搜错误码。
#
# 这两份（_PROMPT / _PROMPT_WITH_QUESTION）是**内置默认**，用户可以在管理页
# 整份换掉：settings.json 的 `vision_prompt`（热生效，读写接口见 main.py 的
# /api/agent/<id>/vision_prompt，取值见 agents.vision_prompt）。2026-10-02 用户
# 要求「识图的提示词我要能自己改，老是分析不清楚」。**没设自定义时行为与从前
# 一字不差**；设了也只是换掉读图要求，「用户的需求」那条照样带（见
# build_prompt 的 base 分支）。审核（image_audit）和表情包打标签（stickers）
# 各自的提示词**不受这个开关影响**——它们传的是自己的 prompt。
_PROMPT = (
    "请描述这张图片的内容，并原样提取其中的所有文字。\n"
    "文字部分不要翻译、不要改写、不要总结，保持原有换行与顺序。"
    "如果图片里没有文字，只描述画面即可。"
)

# 带用户问题的版本。识图模型看不见用户说了什么，只按上面那句通用指令读图，
# 下游文本模型拿到的就是一段泛泛的描述——用户问「这报错怎么解决」，它却可能
# 只答了画面里有几个人。把问题带进去，描述才服务于真实需求。
#
# 但「原样提取文字」这条不能因为加了问题就松掉（见 _PROMPT 的注释）。所以
# 是「先全量描述、再补需求重点」，而不是「只回答用户问的那一点」——用户问得
# 窄时，后者会把图里的版本号、错误码一并丢掉。
#
# 第一句是在点明「输出给谁看」：不自报身份，而是说明读者是个看不见图的模型，
# 这样它才会把话说全，而不是用「如图所示」这种对人有效、对模型无意义的措辞。
_PROMPT_WITH_QUESTION = (
    "你在替一个看不见图片的文本模型读图，你的输出是它唯一的眼睛。\n"
    "用户的需求：%s\n\n"
    "请先完整描述画面内容，并原样提取其中的所有文字"
    "（不要翻译、不要改写、不要总结，保持原有换行与顺序）；"
    "再针对上面的需求补充用户关心的细节。"
)

# 用户问题进 prompt 前的长度上限。QQ 群聊传进来的是「昵称：内容」多行拼接，
# 一轮静默窗口合并几条就能上千字，全塞进去只会挤占识图自己的输出预算。
QUESTION_MAX_CHARS = 300

# 自定义提示词里放「用户问题」的占位符（2026-10-02 加，配合管理页的
# vision_prompt）。为什么用花括号而不是 `%s`：用户贴进去的文本里出现 `%`
# 是常态（「100% 还原」「利润率 30%」），走 % 格式化会当场抛
# ValueError: unsupported format character。replace() 对内容完全无要求。
QUESTION_PLACEHOLDER = "{question}"


def default_prompt():
    """内置默认那份**通用读图**提示词，给管理页当初始内容用。

    （审核提示词在 app/image_audit.py，表情包标签在 app/stickers.py，
    那两份各自独立、不归这里管。）
    """
    return _PROMPT


def build_prompt(question="", index=0, total=1, base=None):
    """按用户问题组装识图 prompt。

    base = **自定义的读图要求**（管理页 vision_prompt 存的那份）。传了就不
    碰内置默认，用户问题按两种情况进去：
      - 文本里有 `{question}` → 在原位替换（他想把需求放在开头、或想要
        「先答需求再描述」的顺序，由他自己排）；
      - 没有占位符 → 尾部补一行「用户的需求：…」。**不补不行**：识图模型
        看不见用户说了什么，只按通用指令读图，下游文本模型拿到的就是一段
        泛泛的描述（见 _PROMPT_WITH_QUESTION 的注释）——但也不替用户改写
        他的正文，塞多了他不知道自己到底在让模型读什么。
      没问题时两种情况都只是 base 本身。

    base 不传（= 没设自定义）走内置那两份：
    没问题时退回通用的 _PROMPT——「只发图不打字」是常见用法（"帮我看下
    这个"），不能因此拼出一个空的「用户的需求：」。

    total > 1 时在开头标出这是第几张：一批多图是逐张送进去的，不标的话
    模型会以为手上这张就是全部。（内置那份**没问题时**没有这个头，是历史
    行为，钉在 tests/test_vision.py 里，别顺手"修"成统一的。）
    """
    q = (question or "").strip()
    if len(q) > QUESTION_MAX_CHARS:
        q = q[:QUESTION_MAX_CHARS] + "…"
    head = ""
    if total > 1:
        head = "（这是第 %d 张，共 %d 张）\n" % (index, total)
    if base:
        text = base
        if q:
            if QUESTION_PLACEHOLDER in text:
                text = text.replace(QUESTION_PLACEHOLDER, q)
            else:
                text = text + "\n用户的需求：" + q
        elif QUESTION_PLACEHOLDER in text:
            # 「只发图不打字」是常见用法：占位符不能孤零零留在文案里，
            # 不然模型会看到一个空的「用户的需求：」。替换成空串，正文里
            # 那几个引出它的字（"答："之类）由用户自己认——不去猜着删，
            # 猜错就是在改他写的文案。
            text = text.replace(QUESTION_PLACEHOLDER, "")
        return head + text
    if not q:
        return _PROMPT
    return head + _PROMPT_WITH_QUESTION % q


# 压缩后再兜一道的字节上限。1024 长边 + JPEG q85 通常在 200KB 以内，
# 这里只是防 PNG 截图之类压不下来的东西把请求体撑大。
_MAX_BYTES = 4 * 1024 * 1024


# ─── 取图与压缩 ──────────────────────────────────────

def sniff_mime(raw):
    """从文件头判断图片类型（不依赖文件名后缀）。"""
    if raw[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def shrink_image(raw):
    """把图片压到最长边 VISION_MAX_EDGE，返回 (字节, mime)。

    PIL 不可用或图无法解码时原样返回——宁可让请求自己失败，也不要在这里
    抛异常把整轮对话打断（调用方按"看不到这张图"降级）。
    """
    try:
        from PIL import Image
    except ImportError:
        log.warning("PIL 不可用，图片不做压缩")
        return raw, sniff_mime(raw)

    try:
        im = Image.open(io.BytesIO(raw))
        im.load()
    except Exception as exc:
        log.warning("图片无法解码，原样返回：%s", exc)
        return raw, sniff_mime(raw)

    w, h = im.size
    long_edge = max(w, h)
    if long_edge > VISION_MAX_EDGE > 0:
        scale = VISION_MAX_EDGE / float(long_edge)
        im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                       Image.LANCZOS)

    # 带透明通道的图直接存 JPEG 会变黑底（截图、贴纸尤其明显），先铺白底
    if im.mode in ("RGBA", "LA", "P"):
        rgba = im.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.split()[-1])
        im = bg
    elif im.mode != "RGB":
        im = im.convert("RGB")

    out = _encode_jpeg(im, 85)
    if len(out) > _MAX_BYTES:
        out = _encode_jpeg(im, 60)
    return out, "image/jpeg"


def _encode_jpeg(im, quality):
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def to_data_url(raw):
    """原始图片字节 → 可直接放进 messages 的 data URL。"""
    data, mime = shrink_image(raw)
    return "data:%s;base64,%s" % (mime, base64.b64encode(data).decode("ascii"))


def _local_image_path(url):
    """`file://` 指向本机时给出磁盘路径，不是就返回 None。

    机器人**自己发**的图走的就是这条路：image_out.prepare_for_send 把产物拷进
    临时目录、用 file:// 交给协议端（见 qq_api.image_segment）。用户**引用机器人
    自己发的图**时，协议端回传的 url 就是这个 file:// —— requests 不认，会抛
    InvalidSchema（"No connection adapters were found"）。于是「引用刚发的那张
    来改」这条最常用的路一直取不到图（2026-09-30~10-01 日志里 44 次）。

    只认自家产物目录：file:// 能读任意本机文件，而 QQ 段的 url 是外部输入，
    不给它开这个口子。
    """
    s = str(url or "").strip()
    if not s.lower().startswith("file://"):
        return None
    from urllib.parse import unquote, urlparse

    from app.image_out import _out_dir

    path = unquote(urlparse(s).path or "")
    if re.match(r"^/[A-Za-z]:", path):   # Windows 的 file:///C:/x 解析出来是 /C:/x
        path = path[1:]
    if not path:
        return None
    try:
        root = os.path.abspath(_out_dir())
        if os.path.commonpath([root, os.path.abspath(path)]) != root:
            return None
    except (OSError, ValueError):        # 跨盘符 commonpath 会抛 ValueError
        return None
    return path


def fetch_image(url, timeout=None, max_bytes=None):
    """下载一张网络图片，返回原始字节。失败抛 RuntimeError。

    QQ 的图片段带的是腾讯图床的直链（multimedia.nt.qq.com.cn），实测 GET
    即可拿到 JPEG，不需要额外的鉴权头。

    例外：引用机器人自己发的图时 url 是 file://，那条走本地读盘
    （见 _local_image_path）。
    """
    from app.config import QQ_IMAGE_MAX_BYTES, QQ_IMAGE_TIMEOUT

    limit = QQ_IMAGE_MAX_BYTES if max_bytes is None else max_bytes

    local = _local_image_path(url)
    if local is not None:
        try:
            with open(local, "rb") as f:
                raw = f.read(limit + 1) if limit else f.read()
        except FileNotFoundError:
            raise RuntimeError(
                "图片读不出来：这张图的本地临时副本已经过期清掉了（%s）" % local)
        except OSError as exc:
            raise RuntimeError("图片读不出来（%s）：%s" % (local, exc))
        if limit and len(raw) > limit:
            raise RuntimeError("图片过大（%d 字节，上限 %d）" % (len(raw), limit))
        if not raw:
            raise RuntimeError("图片内容为空")
        return raw

    try:
        resp = _session.get(url, timeout=_deadline(timeout or QQ_IMAGE_TIMEOUT))
    except Exception as exc:
        raise RuntimeError("图片下载失败：%s" % exc)
    if resp.status_code >= 400:
        raise RuntimeError("图片下载失败 HTTP %d" % resp.status_code)

    raw = resp.content
    if limit and len(raw) > limit:
        raise RuntimeError("图片过大（%d 字节，上限 %d）" % (len(raw), limit))
    if not raw:
        raise RuntimeError("图片内容为空")
    return raw


# ─── 识图 ────────────────────────────────────────────

def describe(data_url, timeout=None, prompt=None, provider=None, model=None):
    """调识图 provider 识图，返回文字。失败抛 RuntimeError。

    prompt 不传用默认的「描述画面+原样提取文字」；表情包打标签等场景
    传自己的。

    provider / model **都不传** = 走管理页「识图模型」选的那个（没选过就
    退回 .env 的 VISION_PROVIDER/VISION_MODEL，见 active_choice）。这跟
    2026-10-03 之前不一样：以前是直接读 .env 常量，现在每次调用重读配置，
    所以在界面上切换**热生效、不用重启**。这条路上若首选那家失败，会**依次
    降级**到 VISION_FALLBACK_CHAIN（豆包→mimo→deepseek，2026-10-07 加），
    所以单家额度耗尽/超时不会让整轮图直接看不到。

    显式传 provider = 这一次临时换一家，**完全绕过界面选择、也不走降级链**。
    审核就走这条（audit_choice 固定给 .env 那份），别让它被界面上的选择或
    降级链带跑。

    ⚠️ 显式传了 provider 时，model 留空就用**该 provider 的默认模型**，不回落
    VISION_MODEL——否则指定了云端 provider，却会把读图那个本地模型名套上去。
    """
    if provider is None and model is None:
        primary_pid, primary_model = active_choice()
        # active_choice 真跑时 pid 为空会自动退回 .env 那两个常量；但测试/异常
        # 路径可能直接喂回 (None, "")，这里再兜一道——否则主选家会整个丢掉，
        # 降级链变成「只剩豆包打头」，与「没配就走 .env」的语义不符。
        if not primary_pid:
            primary_pid, primary_model = VISION_PROVIDER, (primary_model or VISION_MODEL)
        # 主选家写错了（配置里没有这个 provider）属于**配置错误**，要立刻报错。
        # 不能塞进降级链跟着往下试——那会把一个拼错的 provider 名悄悄变成
        # 「那就用豆包吧」，用户永远不知道自己的配置根本没生效。
        if primary_pid not in PROVIDERS:
            raise RuntimeError("识图 provider 未配置：%r" % primary_pid)
        candidates = _chain_candidates(primary_pid, primary_model)
        last_exc = None
        for i, (pid, mdl) in enumerate(candidates):
            try:
                return _describe_one(data_url, timeout, prompt, pid, mdl)
            except Exception as exc:                 # noqa: BLE001
                last_exc = exc
                if i + 1 < len(candidates):
                    log.warning("识图 %s 失败，降级到下一家（%s）：%s",
                                pid, candidates[i + 1][0], exc)
        # 全链都挂：把最后那条异常原样抛出，调用方按「识图失败」降级。
        raise last_exc

    # 显式指定了 provider：单发一次，不走降级链（audit_choice 那条路）。
    # provider 为 None 只可能是「只传了 model 没传 provider」（老调用方式），
    # 这时退回 .env 的 provider，保持与改动前一致的语义。
    return _describe_one(data_url, timeout, prompt,
                         provider if provider is not None else VISION_PROVIDER,
                         model)


def _describe_one(data_url, timeout, prompt, provider, model):
    """对**单独一家**识图 provider 发一次请求，返回文字（成功）或抛异常。

    这是 describe 的实际执行体；降级链只是在外面把候选依次喂进来。

    provider 一定是**具体的 provider id**（调用方已解析好），不会是 None——
    所以 model 留空时用该家的默认模型，不回落 VISION_MODEL（那条回落逻辑
    只对「主选家」有意义，已由 describe/_chain_candidates 处理）。
    """
    pid = (provider or "").lower()
    cfg = PROVIDERS.get(pid)
    if not cfg:
        raise RuntimeError("识图 provider 未配置：%r" % provider)

    model = model or cfg["model"]

    # 识图是**隐形调用**（每张图都要跑一次，请求数远多于对话轮数），但它自己
    # 发 HTTP、不经过 app/llm.py，所以从前完全不产生 `[llm]` 行——排查「哪家
    # 模型在拖时间」时会漏掉这一大块。打同一个前缀，`grep '\[llm\]'` 就能捞全。
    log.info("[llm] %s / %s vision", pid, model)

    started = time.monotonic()
    if pid == "ollama":
        data = _post_ollama(cfg, model, data_url, prompt or _PROMPT, timeout, started)
        _record_usage_ollama(data, pid, model)
        try:
            text = (data["message"]["content"] or "").strip()
        except Exception as exc:
            raise RuntimeError("识图响应无法解析（耗时 %.1fs）：%s"
                               % (time.monotonic() - started, exc))
    else:
        data = _post_openai(cfg, model, data_url, prompt or _PROMPT, timeout, started)
        _record_usage_openai(data, pid, model)
        try:
            text = (data["choices"][0]["message"]["content"] or "").strip()
        except Exception as exc:
            raise RuntimeError("识图响应无法解析（耗时 %.1fs）：%s"
                               % (time.monotonic() - started, exc))

    # 完成日志：从前只有开始那一条，一次识图卡住时日志上只剩个孤零零的起点，
    # 分不清是「卡在识图」还是「卡在后面的对话」（2026-09-29 排查就吃了这个亏：
    # 群聊那轮 298 秒，日志里只有一条 16:49:02 的识图起点，之后全静音）。
    # 故意不带 `[llm]` 前缀——那个前缀的语义是「一行 = 一次模型调用」，
    # 补一行完成日志不该让它变成两行。
    log.info("识图完成 %.1fs（%d 字）", time.monotonic() - started, len(text))
    return text


def _post(url, payload, headers, timeout, started):
    """发一次识图请求。超时用**整次请求的总闸门**（见 _deadline 的注释）。"""
    try:
        resp = _session.post(url, json=payload, headers=headers,
                             timeout=_deadline(timeout or VISION_TIMEOUT))
    except Exception as exc:
        raise RuntimeError("识图请求失败（耗时 %.1fs）：%s"
                           % (time.monotonic() - started, exc))

    if resp.status_code >= 400:
        raise RuntimeError("识图失败 HTTP %d：%s"
                           % (resp.status_code, resp.text[:200]))

    try:
        return resp.json()
    except Exception as exc:
        raise RuntimeError("识图响应无法解析：%s" % exc)


def _post_openai(cfg, model, data_url, prompt, timeout, started):
    """OpenAI 兼容的 /chat/completions（火山 / deepseek / mimo / scnet 都走这条）。"""
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }],
    }
    # 火山系（volc/doubao）显式关思维链：Seed 2.1 系列是深度思考模型，
    # 不关的话识图也会先烧一大段 reasoning——推理 token 按 output 计费，
    # 识图又是请求量最大的一类调用（与 llm._thinking_type 同一笔账，
    # 2026-10-05 实测 disabled 后 completion 22036→1）。deepseek 不带字段
    # 维持现状（那条识图链路已验证过，不赌改动）。
    if cfg is PROVIDERS.get("volc") or cfg is PROVIDERS.get("doubao"):
        payload["thinking"] = {"type": "disabled"}
    headers = {
        "Authorization": "Bearer " + cfg["api_key"],
        "Content-Type": "application/json",
    }
    return _post(url, payload, headers, timeout, started)


def _post_ollama(cfg, model, data_url, prompt, timeout, started):
    """本地 ollama 走**原生 /api/chat**，不走它的 OpenAI 兼容端点。

    两个原因，都不是风格问题：
    1. 兼容端点不接受 `options`，没法指定 `num_gpu` —— 而我们要的正是
       「跑内存、别占显存」（2026-10-03 用户要求，显存留给 ComfyUI 生图）；
    2. 兼容端点在 /v1 下面，而 `OLLAMA_BASE_URL` 是给 /api/chat 用的裸地址
       （`http://127.0.0.1:11434`），直接拼 /chat/completions 实测 404。

    另外 `images` 要**纯 base64**，不能带 `data:image/jpeg;base64,` 前缀。
    """
    b64 = data_url.split(",", 1)[1] if data_url.startswith("data:") else data_url
    url = cfg["base_url"].rstrip("/") + "/api/chat"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt, "images": [b64]}],
        "stream": False,
        "keep_alive": OLLAMA_VISION_KEEP_ALIVE,
        "options": {"num_gpu": OLLAMA_VISION_NUM_GPU},
    }
    return _post(url, payload, None, timeout, started)


def _record_usage(hit, miss, output, pid, model):
    """识图是隐形调用，token 账不进主轮的 [cache] 统计——在这里单独归账，
    挂「vision」类别（跟 LLM 后台对账时，账就齐了）。"""
    if (hit or 0) + (miss or 0) <= 0:
        return
    try:
        from app import usage as usage_stats
        with usage_stats.scope("vision"):
            usage_stats.record(hit or 0, miss or 0, output=output,
                               provider=pid, model=model, kind="vision")
    except Exception:
        pass


def _record_usage_openai(data, pid, model):
    u = data.get("usage") or {}
    hit = u.get("prompt_cache_hit_tokens")
    miss = u.get("prompt_cache_miss_tokens")
    if hit is None:
        details = u.get("prompt_tokens_details") or {}
        hit = details.get("cached_tokens", 0)
        miss = (u.get("prompt_tokens") or 0) - (hit or 0)
    _record_usage(hit or 0, miss or 0, int(u.get("completion_tokens") or 0),
                  pid, model)


def _record_usage_ollama(data, pid, model):
    """ollama 原生口径：prompt_eval_count / eval_count。本地没有缓存计费，
    hit 恒为 0（记账只为了在用量表里看得见「本地跑了多少」）。"""
    _record_usage(0, int(data.get("prompt_eval_count") or 0),
                  int(data.get("eval_count") or 0), pid, model)
