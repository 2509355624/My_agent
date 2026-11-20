"""角色扮演语音合成（2026-11-18）。

设计边界：
- 只念「黄色高亮」的台词：`persona.html` 的 `fmtDlg` 把引号内文字标成
  `.dlg`，这里用同一套引号规则从原始回复里抽出来，旁白/心理描写不送 TTS。
- 只接明确登记在 `config/voice.json` 的角色卡。未登记的角色一律返回空，
  前端静默跳过——不能因为语音服务没开就把聊天弄挂。
- 语音服务在 3060 上（Voicebox，Qwen3-TTS 0.6B CUDA），本进程只做 HTTP
  转发；`base_url` 可在配置里改，不写死在代码里。
"""
import json
import logging
import os
import re
import time

import requests

log = logging.getLogger("persona.voice")

MAX_SPEECH_CHARS = 5000
_QUOTE_RE = re.compile(r"「([^」]*)」|“([^”]*)”|\"([^\"\n]*)\"")
_CONFIG_PATH = os.path.join(
    os.path.dirname(
        os.path.dirname(
            os.path.dirname(
                os.path.dirname(os.path.abspath(__file__))
            )
        )
    ),
    "config", "voice.json")


class VoiceUnavailable(RuntimeError):
    """语音服务不可用/生成失败。调用方应当降级，不要拦聊天。"""


def _load_config():
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as fh:
            data = json.load(fh) or {}
    except FileNotFoundError:
        log.warning("语音配置不存在：%s", _CONFIG_PATH)
        return {}
    except Exception:                                  # noqa: BLE001
        log.warning("语音配置读取失败：%s", _CONFIG_PATH, exc_info=True)
        return {}
    return data if isinstance(data, dict) else {}


def _profiles():
    cfg = _load_config()
    if not cfg.get("enabled", True):
        return {}
    raw = cfg.get("profiles")
    if not isinstance(raw, dict):
        return {}
    out = {}
    for card_id, item in raw.items():
        if not isinstance(item, dict):
            continue
        pid = str(item.get("profile_id") or "").strip()
        if not pid:
            continue
        out[str(card_id).strip()] = {
            "profile_id": pid,
            "name": str(item.get("name") or card_id),
            "language": str(item.get("language") or "zh"),
        }
    return out


def voice_card_ids():
    """当前启用了语音的角色卡 id，给前端画「语音」按钮。"""
    return tuple(sorted(_profiles().keys()))


def profile_for_card(card_id):
    return _profiles().get(str(card_id or "").strip())


def extract_dialogue(text):
    """只抽引号内的台词，按原文顺序；〔心声〕整行跳过。"""
    src = str(text or "")
    lines = []
    for line in src.splitlines():
        # 心声是给玩家看/给模型读的内心活动，不是她说出口的话。
        if line.lstrip().startswith("〔心声〕"):
            continue
        lines.append(line)
    clean = "\n".join(lines)

    out = []
    for m in _QUOTE_RE.finditer(clean):
        said = (m.group(1) or m.group(2) or m.group(3) or "").strip()
        if said:
            out.append(said)
    joined = "\n".join(out)
    if len(joined) > MAX_SPEECH_CHARS:
        joined = joined[:MAX_SPEECH_CHARS].rstrip()
    return joined


def _voice_error(resp, action):
    """把 Voicebox 的 4xx/5xx 压成一句可读错误，别把整段 HTML 往外抛。"""
    detail = ""
    try:
        data = resp.json()
        if isinstance(data, dict):
            detail = str(data.get("detail") or data.get("error") or "")
    except Exception:                                   # noqa: BLE001
        detail = str(getattr(resp, "text", "") or "")[:120]
    suffix = ("：" + detail) if detail else ""
    return VoiceUnavailable("%s失败（HTTP %s）%s" % (action, resp.status_code, suffix))


def _cancel_generation(base, generation_id):
    """尽力取消排队/生成中的任务，避免玩家重点后旧任务继续占 GPU。"""
    try:
        requests.post("%s/generate/%s/cancel" % (base, generation_id), timeout=5)
    except requests.RequestException:
        pass


def _wait_generation(base, generation_id, timeout):
    """轮询 history，直到 Voicebox 报告 completed。

    长文本（128 字中文在 3060 上约 96–100 秒）不能再用一次 HTTP 读超时硬等；
    提交后轮询，整体超时由配置控制，失败时尽量取消旧任务。
    """
    deadline = time.monotonic() + timeout
    poll_timeout = min(8, max(2, timeout / 10.0))
    consecutive_errors = 0
    while time.monotonic() < deadline:
        try:
            resp = requests.get(
                "%s/history/%s" % (base, generation_id),
                timeout=poll_timeout)
        except requests.RequestException as exc:
            consecutive_errors += 1
            if consecutive_errors >= 3:
                raise VoiceUnavailable("查询语音任务失败：%s" % exc) from exc
            time.sleep(0.5)
            continue
        consecutive_errors = 0

        if resp.status_code == 404:
            # 任务还没落进 history；继续等。
            time.sleep(0.5)
            continue
        if resp.status_code != 200:
            time.sleep(0.5)
            continue

        try:
            data = resp.json() or {}
        except Exception:                               # noqa: BLE001
            data = {}
        status = str(data.get("status") or "").lower()
        if status == "completed":
            return
        if status in ("failed", "error", "cancelled"):
            _cancel_generation(base, generation_id)
            reason = str(data.get("error") or status)
            raise VoiceUnavailable("语音任务失败：%s" % reason)
        time.sleep(0.5)

    _cancel_generation(base, generation_id)
    raise VoiceUnavailable("语音合成超时（%ss）" % timeout)


def _fetch_audio(base, generation_id, timeout):
    """完成后再取 WAV；刚完成时文件偶尔还没出现在 audio 路由上，短重试。"""
    last_error = None
    for _ in range(3):
        try:
            resp = requests.get(
                "%s/audio/%s" % (base, generation_id),
                timeout=min(30, max(5, timeout)))
        except requests.RequestException as exc:
            last_error = exc
        else:
            if resp.status_code == 200 and resp.content:
                return resp.content
            last_error = "HTTP %s" % resp.status_code
        time.sleep(0.3)
    raise VoiceUnavailable("取语音文件失败：%s" % (last_error or "空响应"))


def synthesize(card_id, speech):
    """把一句/多句台词交给 Voicebox，返回 WAV 字节。

    Voicebox 的 `/generate/stream` 会一直等到整段生成完才返回；长台词在
    3060 上会超过 90 秒，导致 5174 读超时并返回 503。这里改成：
      1) POST /generate 提交任务，拿 generation_id；
      2) GET /history/{id} 轮询状态；
      3) GET /audio/{id} 取最终 WAV。
    这样 HTTP 请求不会卡在单次读超时上，前端也能稳定拿到音频并写缓存。
    """
    profile = profile_for_card(card_id)
    if not profile:
        raise VoiceUnavailable("这个角色没有登记语音")
    text = str(speech or "").strip()
    if not text:
        raise VoiceUnavailable("没有可朗读的台词")

    cfg = _load_config()
    base = str(cfg.get("base_url") or "").strip().rstrip("/")
    if not base:
        raise VoiceUnavailable("没有配置语音服务地址")
    timeout = max(30, min(300, int(cfg.get("timeout_sec") or 240)))
    payload = {
        "profile_id": profile["profile_id"],
        "text": text,
        "language": profile.get("language") or "zh",
        "model_size": str(cfg.get("model_size") or "0.6B"),
        "engine": str(cfg.get("engine") or "qwen"),
        "personality": False,
        "normalize": True,
        "max_chunk_chars": 800,
        "crossfade_ms": 50,
    }

    started = time.monotonic()
    try:
        resp = requests.post(
            base + "/generate", json=payload,
            timeout=min(15, timeout))
    except requests.RequestException as exc:
        raise VoiceUnavailable("语音服务连不上：%s" % exc) from exc
    if resp.status_code != 200:
        raise _voice_error(resp, "提交语音任务")
    try:
        data = resp.json() or {}
    except Exception:                                   # noqa: BLE001
        data = {}
    generation_id = str(data.get("id") or "").strip()
    if not generation_id:
        raise VoiceUnavailable("语音服务没有返回任务 id")

    log.info("[persona] 语音任务已提交 id=%s card=%s chars=%s",
             generation_id, card_id, len(text))
    _wait_generation(base, generation_id, timeout)
    wav = _fetch_audio(base, generation_id, timeout)
    log.info("[persona] 语音任务完成 id=%s card=%s 用时=%.1fs",
             generation_id, card_id, time.monotonic() - started)
    return wav
