# -*- coding: utf-8 -*-
"""角色扮演语音合成（2026-11-18）。

只念黄色高亮的那部分：她真正说出口的引号台词，旁白/心理描写不送 TTS。
第一批只接芙宁娜（furina）和千夏（sunna），其余角色一律静默降级。
"""

import os
import re
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, "web", "persona.html")


class DialogueExtractionTest(unittest.TestCase):
    def test_only_quoted_speech_is_kept(self):
        from app.features.persona import voice

        text = (
            "她把杯子放下，抬眼看你。\n"
            "「你回来了？」\n"
            "“今天好热。”\n"
            "她没再说话。\n"
            '"\u4f60\u5148\u8fdb\u53bb\u3002"\n'
        )
        self.assertEqual(
            voice.extract_dialogue(text),
            "你回来了？\n今天好热。\n你先进去。",
        )

    def test_inner_thought_line_is_not_spoken(self):
        from app.features.persona import voice

        text = "她别过脸。\n〔心声〕「其实不想让你走。」\n「路上小心。」"
        self.assertEqual(voice.extract_dialogue(text), "路上小心。")

    def test_narration_only_returns_empty(self):
        from app.features.persona import voice

        self.assertEqual(voice.extract_dialogue("她低头笑了笑，没有开口。"), "")

    def test_card_mapping_is_limited_to_first_batch(self):
        from app.features.persona import voice

        ids = voice.voice_card_ids()
        self.assertIn("furina", ids)
        self.assertIn("sunna", ids)
        self.assertEqual(voice.profile_for_card("rem"), None)
        self.assertTrue(voice.profile_for_card("furina")["profile_id"])
        self.assertTrue(voice.profile_for_card("sunna")["profile_id"])


class TtsRouteTest(unittest.TestCase):
    def _post(self, body):
        from flask import Flask
        from app.features.persona import routes

        app = Flask(__name__)
        with app.test_request_context(json=body):
            return app.make_response(routes.persona_tts())

    def test_unknown_card_returns_404(self):
        resp = self._post({"card_id": "rem", "text": "「你好」"})
        self.assertEqual(resp.status_code, 404)

    def test_no_dialogue_returns_204(self):
        resp = self._post({"card_id": "furina", "text": "她只是看着你。"})
        self.assertEqual(resp.status_code, 204)

    def test_route_sends_extracted_dialogue_to_voicebox(self):
        from app.features.persona import routes
        from app.features.persona import voice

        with mock.patch.object(voice, "synthesize", return_value=b"RIFFfake") as synth:
            resp = self._post({
                "card_id": "furina",
                "text": "她笑了一下。「你终于来了。」",
            })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "audio/wav")
        synth.assert_called_once_with("furina", "你终于来了。")


class PersonaPageVoiceTest(unittest.TestCase):
    def setUp(self):
        with open(PAGE, encoding="utf-8") as f:
            self.html = f.read()

    def test_page_has_speak_helper_and_endpoint(self):
        self.assertIn("async function speakReply(", self.html)
        self.assertIn("/api/persona/tts", self.html)
        self.assertIn("播放语音", self.html)

    def test_voice_button_sits_beside_the_image_buttons(self):
        shot = self.html.index('bShot.textContent = "出图"')
        voice = self.html.index('bVoice.textContent = "播放语音"')
        back = self.html.index('bRb.textContent = "⏪ 回退"')
        self.assertLess(shot, voice)
        self.assertLess(voice, back)

    def test_voice_cards_load_before_history_render(self):
        fn = self.html[self.html.index("async function loadSession("):]
        fn = fn[:fn.index("\n}\n") + 3]
        self.assertIn("await loadVoiceCards()", fn)

    def test_voice_ui_can_be_switched_off_without_removing_code(self):
        """2026-11-18：入口先关，后端和缓存代码保留，改回 true 即可恢复。"""
        self.assertIn("const TTS_UI = false;", self.html)
        self.assertIn("return !!(TTS_UI &&", self.html)
        self.assertIn("if (!TTS_UI)", self.html)

    def test_audio_is_cached_in_the_browser(self):
        self.assertIn('indexedDB.open("persona-tts-cache"', self.html)
        self.assertIn("ttsCacheGet(cacheKey)", self.html)
        self.assertIn("ttsCachePut(cacheKey, blob)", self.html)

    def test_new_assistant_reply_auto_speaks(self):
        fn = self.html[self.html.index("async function deliver("):]
        fn = fn[:fn.index("\n}\n") + 3]
        self.assertIn("speakReply(d.reply", fn)

    def test_voice_failure_does_not_break_chat(self):
        fn = self.html[self.html.index("async function speakReply("):]
        fn = fn[:fn.index("\n}\n") + 3]
        self.assertIn("catch", fn)
        self.assertNotIn("throw", fn)

    def test_audio_stays_unlocked_while_long_synthesis_runs(self):
        """长台词要等约 100 秒，不能让浏览器在等待期间收回播放权限。"""
        self.assertIn("function keepTtsAlive(", self.html)
        fn = self.html[self.html.index("async function speakReply("):]
        fn = fn[:fn.index("\n}\n") + 3]
        self.assertIn("keepTtsAlive()", fn)
        keep = self.html[self.html.index("function keepTtsAlive("):]
        keep = keep[:keep.index("\n}\n") + 3]
        self.assertIn("loop = true", keep)
        self.assertIn("muted = true", keep)


class VoiceboxAsyncTest(unittest.TestCase):
    """长台词生成会超过 90 秒，不能再靠一次 HTTP 读超时硬等。

    2026-11-18 用户反馈：千夏 128 字台词生成约 96–100 秒，旧实现 90 秒
    读超时后返回 503，前端拿不到 WAV，于是重复点击又重复合成。这里锁住
    “提交异步任务 → 轮询 history → 取 audio”的契约。
    """

    def test_synthesize_submits_polls_and_fetches_audio(self):
        from app.features.persona import voice

        submit = mock.Mock(status_code=200)
        submit.content = b'{"id":"g1"}'
        submit.json.return_value = {"id": "g1", "status": "generating"}

        running = mock.Mock(status_code=200)
        running.content = b'{"status":"generating"}'
        running.json.return_value = {"status": "generating"}

        done = mock.Mock(status_code=200)
        done.content = b'{"status":"completed"}'
        done.json.return_value = {"status": "completed"}

        audio = mock.Mock(status_code=200, content=b"RIFFfake")

        def get(url, **kwargs):
            if url.endswith("/history/g1"):
                return running if get.calls == 0 else done
            if url.endswith("/audio/g1"):
                return audio
            raise AssertionError("unexpected GET %s" % url)

        get.calls = 0

        def get_and_count(url, **kwargs):
            result = get(url, **kwargs)
            get.calls += 1
            return result

        with mock.patch.object(voice.requests, "post",
                               return_value=submit) as post, \
                mock.patch.object(voice.requests, "get",
                                  side_effect=get_and_count) as get_mock, \
                mock.patch.object(voice.time, "sleep"):
            self.assertEqual(
                voice.synthesize("furina", "你终于来了。"),
                b"RIFFfake",
            )

        self.assertTrue(post.call_args.args[0].endswith("/generate"))
        self.assertIn("/history/g1", get_mock.call_args_list[0].args[0])
        self.assertIn("/audio/g1", get_mock.call_args_list[-1].args[0])

    def test_synthesize_reports_generation_failure(self):
        from app.features.persona import voice

        submit = mock.Mock(status_code=200)
        submit.content = b'{"id":"g1"}'
        submit.json.return_value = {"id": "g1", "status": "generating"}
        failed = mock.Mock(status_code=200)
        failed.content = b'{"status":"failed","error":"boom"}'
        failed.json.return_value = {"status": "failed", "error": "boom"}

        with mock.patch.object(voice.requests, "post",
                               return_value=submit), \
                mock.patch.object(voice.requests, "get",
                                  return_value=failed):
            with self.assertRaises(voice.VoiceUnavailable):
                voice.synthesize("furina", "你终于来了。")


if __name__ == "__main__":
    unittest.main()
