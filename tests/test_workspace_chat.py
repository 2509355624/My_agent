"""新工作台聊天页壳的静态结构守卫。

聊天页在 `web/workspace/chat.html`，纯静态无构建。这里守住用户 2026-11-23 定下的
几条硬约束：无顶栏无底栏、输入栏上方只有三个横滑 chip、2 万字上限、1 千字折叠、
生图走同源 `/api/gen/*`、页面栈能回退。测试只看结构，不跑浏览器。
"""
import os
import shutil
import subprocess
import unittest
from html.parser import HTMLParser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WS = os.path.join(ROOT, "web", "workspace")

CHAT_FILES = ("chat.html", "css/chat.css", "css/sheet.css",
              "js/gen.js", "js/chat.js", "js/stack.js", "js/views.js",
              "js/card.js", "js/gen_flow.js", "js/shot.js", "js/redraw.js")


def _read(rel):
    with open(os.path.join(WS, rel), encoding="utf-8") as f:
        return f.read()


def _read_app(rel):
    with open(os.path.join(ROOT, "app", rel), encoding="utf-8") as f:
        return f.read()


class _Balance(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []

    def handle_starttag(self, tag, attrs):
        if tag in ("div", "section", "main"):
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in ("div", "section", "main") and self.stack:
            self.stack.pop()


class TestWorkspaceChatLayout(unittest.TestCase):

    def test_module_files_exist_and_stay_small(self):
        for rel in CHAT_FILES:
            path = os.path.join(WS, rel)
            self.assertTrue(os.path.isfile(path), "缺少模块文件: " + rel)
            with open(path, encoding="utf-8") as f:
                lines = sum(1 for _ in f)
            self.assertLess(lines, 600, rel + " 超过 600 行，需拆分")

    def test_html_tags_balanced(self):
        p = _Balance()
        p.feed(_read("chat.html"))
        self.assertEqual(p.stack, [], "div/section 标签未配平")

    def test_no_topbar_and_no_bottom_tabs(self):
        """用户明确要求：聊天页不要顶栏，也不要有底部状态栏。"""
        html = _read("chat.html")
        self.assertNotIn("<header", html, "聊天页不应有顶栏")
        self.assertNotIn("data-tab=", html, "聊天页不应有底部 tab")
        self.assertIn('name="viewport"', html)
        self.assertIn("viewport-fit=cover", html, "全面屏要避开刘海")

    def test_three_chips_only(self):
        html = _read("chat.html")
        self.assertEqual(html.count('class="chip"'), 3, "横滑 chip 只应有三个")
        for text in ("AI 创作", "画风", "我的漫画作品"):
            self.assertIn(text, html)


class TestWorkspaceChatComposer(unittest.TestCase):

    def test_input_limits(self):
        html = _read("chat.html")
        self.assertIn('maxlength="20000"', html, "输入上限 2 万字")
        # 2026-11-24 晚：折叠写在 card.js（气泡就是那边画的）
        js = _read("js/card.js")
        self.assertIn("1000", js, "超过 1 千字要折叠")

    def test_plus_menu_has_two_create_entries(self):
        """「＋」里先是两个平级创作入口，相机/相册退到下面一条。"""
        html = _read("chat.html")
        self.assertIn('id="btn-plus"', html)
        self.assertIn("图片生成", html)
        self.assertIn("漫画生成", html)
        self.assertIn('class="pm-grid"', html)
        self.assertIn("相机", html)
        self.assertIn("相册", html)
        # 只收图片，不收文档
        self.assertIn('accept="image/*"', html)

    def test_send_button_starts_disabled(self):
        html = _read("chat.html")
        self.assertRegex(html, r'id="btn-send"[^>]*disabled')


class TestWorkspaceChatGen(unittest.TestCase):

    def test_same_origin_gen_paths(self):
        """渠道/提交/轮询/取图都拼在同一个前缀上，不写死域名。

        gen.js 改成 `BASE + "/channels"` 这种拼法之后，字面量断言要跟着变——
        但「四个接口都在、前缀是 /api/gen」这件事照样得钉住：5175 网关只反代
        这个前缀，换成别的（比如早先写过的 /api/friend/gen）那边直接 404，
        表现就是「没拿到渠道列表」。
        """
        js = _read("js/gen.js")
        self.assertIn('var BASE = "/api/gen";', js, "生图接口前缀必须是 /api/gen")
        for seg in ('"/channels"', '"/submit"', '"/status"', '"/img?f="'):
            self.assertIn(seg, js, "缺少接口: " + seg)
        self.assertNotIn("http://", js, "接口不能写死域名")
        self.assertNotIn("https://", js, "接口不能写死域名")

    def test_bubble_actions_and_queue_text(self):
        # 2026-11-24 晚：按钮文案在 card.js（卡片长什么样那儿画的）
        js = _read("js/card.js") + _read("js/chat.js")
        self.assertIn("复制", js)
        self.assertIn("重画", js)
        self.assertIn("排队", js, "生成中要告诉用户前面还有多少人")

    def test_polling_can_stop(self):
        # 2026-11-24 晚：轮询搬到 gen_flow.js（chat.js 破六百行）
        js = _read("js/gen_flow.js")
        self.assertIn("setInterval", js)
        self.assertIn("clearInterval", js, "轮询必须能停，不能无限打接口")


class TestWorkspaceChatStack(unittest.TestCase):

    def test_stack_push_pop_and_history(self):
        js = _read("js/stack.js")
        for token in ("push", "pop", "popstate", "pushState"):
            self.assertIn(token, js, "页面栈缺少: " + token)

    def test_stack_restores_subviews_after_refresh(self):
        js = _read("js/stack.js")
        self.assertIn('var VIEW_KEY = "ws_view_v1"', js)
        self.assertIn("sessionStorage.getItem(VIEW_KEY)", js)
        self.assertIn("Array.isArray(saved.views)", js)
        self.assertIn("global.WSViews.titles.hasOwnProperty(v.name)", js)

    def test_viewer_uses_gallery_preview_not_chat_fit(self):
        js = _read("js/gallery.js")
        self.assertIn('function imgUrl(it) { return "/api/gal/img/" + it.id + "?s=v"; }', js)

    def test_subview_has_close_and_user_button(self):
        js = _read("js/views.js")
        self.assertIn("关闭", js)
        self.assertIn("aria-label", js)

    def test_styles_use_tokens(self):
        css = _read("css/chat.css")
        self.assertIn("var(--card", css)
        self.assertIn("var(--text", css)
        self.assertIn("safe-area-inset-bottom", css, "输入栏要避开手机安全区")
        self.assertIn("backdrop-filter", css, "玻璃层要真的模糊背景")
        tokens = _read("css/tokens.css")
        self.assertIn("--glass", tokens)
        self.assertIn("--shadow-2", tokens)


@unittest.skipUnless(shutil.which("node"), "需要 Node 执行前端行为回归")
class TestWorkspaceFeedback(unittest.TestCase):
    def test_archive_restore_is_idempotent_and_deduplicates_merged_messages(self):
        script = r'''
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const root = process.argv[1];
let resolvePull, paints = 0, entries = [];
const server = {k:"note",t:"来自服务端"};
const local = {k:"note",t:"本地新增"};
const w = {
  WSShot:{FIT:"fit"},
  WSStore:{load:()=>({msgs:[server,local]}),pull:()=>new Promise(r=>{resolvePull=r;})},
  WSChat:{msgs:()=>entries,paintNote(){paints++;},scrollBottom(){},box(){return {appendChild(){}}},
    hideEmpty(){},paintUser(){},restoreRef(){},cards:new Map(),failCard(){},buildActs(){},shotEl(){},
    pending:{},startPoll(){}}
};
const context = vm.createContext({window:w, document:{createElement:()=>({className:"",appendChild(){},classList:{remove(){}},dataset:{}})},
  Promise, Map, Date, Object});
vm.runInContext(fs.readFileSync(root+"/js/archive.js","utf8"),context);
w.WSArchive.restore();
w.WSArchive.restore();
resolvePull({ok:true,msgs:[server]});
Promise.resolve().then(()=>Promise.resolve()).then(()=>{
  assert.equal(entries.length,2,"本地与服务端合并只保留唯一记录");
  assert.equal(paints,2,"合并后的历史只渲染一次");
  w.WSArchive.restore();
  assert.equal(entries.length,2,"重复调用恢复不能再次追加历史");
}).catch(e=>{console.error(e);process.exitCode=1;});
'''
        result = subprocess.run(
            [shutil.which("node"), "-e", script, WS],
            capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_return_restores_done_image_and_reference_reading(self):
        archive = _read("js/archive.js")
        self.assertIn('job.state === "done" && job.file', archive)
        self.assertIn('global.WSChat.showImage(a.card, job)', archive)
        self.assertIn('note.className = "note"', archive)
        ref = _read("js/ref.js")
        self.assertIn('say.placeholder = "正在识图中…"', ref)
        self.assertNotIn('say.parentNode.appendChild(tag)', ref)
        self.assertIn('if (pending && pending.id) useSame(null, pending.id)', ref)
        self.assertIn('ws_same_result', ref)

    def test_redraw_flow_keeps_old_result_and_uses_final_prompt(self):
        redraw = _read("js/redraw.js")
        self.assertIn("WSRedraw", redraw)
        # 出过图 → 用实际英文 tags，且不再传气泡（null = 底部新建一条）
        self.assertIn("H.submit(text, skill, done ? [] : refs, done ? true : described,", redraw)
        self.assertIn("null, false, done)", redraw)
        self.assertNotIn("H.retry(", redraw, "重画不再原地重试，一律底部新开")
        self.assertNotIn("afterCancel", redraw)
        chat = _read("js/chat.js")
        self.assertIn("submitJob(prompt, skill, refs, described, userNode, defer, finalPrompt)", chat)
        self.assertIn("user = user || addUser(text, refs);", chat)
        self.assertIn("prompt_is_final", _read("js/gen_flow.js"))
        self.assertIn("busyTip: function (text) { busyNote(text, 3000); }", chat)
        self.assertIn("dropCard: function (card) { global.WSRedraw.drop(card); }", chat)
        flow = _read("js/gen_flow.js")
        self.assertNotIn("findTwin", flow)
        self.assertIn('job.state === "cancelled"', flow)
        self.assertIn('card.note.textContent = "已终止"', flow)
        routes = _read_app("features/image/routes.py")
        self.assertIn('"owner": owner', routes)
        self.assertIn("image_jobs.inflight_limit()", routes)
        # 5174 管理页不许再有一张的独立闸：上限只认 jobs 那一套口径
        self.assertNotIn("_MAX_ACTIVE", routes)
        # 存档接口按记录 id 原位更新，不再按整条 JSON 追加
        chat_routes = _read_app("features/chat/routes.py")
        self.assertIn("def _rec_id(", chat_routes)
        self.assertIn("msgs[by_id[rid]] = m", chat_routes)

    def test_redraw_accepts_only_one_click(self):
        script = r'''
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
class El {
  constructor() {
    this.children = []; this.listeners = {}; this.disabled = false;
    this.hidden = false; this.innerHTML = ""; this.textContent = "";
    this.style = {}; this.classList = {add(){}, remove(){}};
  }
  appendChild(n) { this.children.push(n); return n; }
  addEventListener(k, fn) { this.listeners[k] = fn; }
  setAttribute() {} blur() {} remove() {}
  click() { if (this.listeners.click) this.listeners.click(); }
}
const root = process.argv[1];
const w = {addEventListener(){}, setTimeout(){}};
const context = vm.createContext({window:w, document:{addEventListener(){},
  createElement:()=>new El(), body:new El()}, console, Map, Promise, Date,
  setInterval:()=>1, clearInterval(){}, setTimeout:()=>1});
function load(name) { vm.runInContext(fs.readFileSync(root+"/js/"+name,"utf8"),context); }
load("card.js"); load("gen_flow.js"); load("redraw.js");
let again = 0;
const card = {acts:new El(), node:new El(), say(){}, skill:"s"};
w.WSCard.buildActs(card, "", {again(){again++;}, download(){}});
const button = card.acts.children[2];   // 复制 / 下载 / 重画
button.click(); button.click();
assert.equal(again, 1, "同一颗重画按钮连点只收一次");
  let retries = 0, submits = 0, dropped = 0;
  w.WSRedraw.bind({msgs:()=>[],save(){},clearFail(){},retry(){retries++;},
    discard(){dropped++;}, submit(){submits++;}});
  const old = {rec:{ask:"画猫"}, refs:[], skill:"s", node:new El(),
               userNode:new El(), redrawing:false};
  w.WSRedraw.run(old);
  assert.equal(submits, 1, "失败重画也在底部新开一张");
  assert.equal(retries, 0, "失败重画不再原地重试");
  assert.equal(dropped, 0, "没出过图的旧记录不该被摘掉");
  const done = {rec:{file:"cat.webp",prompt:"cat tags"},file:"cat.webp",
    prompt:"cat tags",refs:[],skill:"s",node:new El(),userNode:new El(),redrawing:false};
  w.WSRedraw.run(done);
  assert.equal(submits, 2, "成功卡片重画在底部新增卡片");
  assert.equal(dropped, 1, "出过图的旧记录要让位给新卡的状态");
'''
        result = subprocess.run(
            [shutil.which("node"), "-e", script, WS],
            capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_inflight_limit_restored_to_five(self):
        api = os.path.join(ROOT, "app", "features", "qq", "friend_api.py")
        with open(api, encoding="utf-8") as f:
            py = f.read()
        self.assertIn("PER_INFLIGHT = 5", py)
        # 高峰不再写死在群友页：两处都读 jobs 的同一口径，提示也共用。
        self.assertIn("image_jobs.inflight_limit()", py)
        self.assertIn("image_jobs.inflight_busy_text", py)
        jobs = os.path.join(ROOT, "app", "features", "image", "jobs.py")
        with open(jobs, encoding="utf-8") as f:
            py = f.read()
        self.assertIn("MAX_INFLIGHT = 5", py)
        self.assertIn("(10, 12, \"早高峰\"", py)
        self.assertIn("(22, 24, \"晚高峰\"", py)
        self.assertIn("min(limit, chan.max_inflight)", py)

    def test_download_has_no_panel_and_failure_is_one_line(self):
        hide = _read("js/hide.js")
        self.assertNotIn("savePanel", hide)
        self.assertNotIn("系统保存 / 分享", hide)
        card = _read("js/card.js")
        self.assertIn("function failLine", card)
        self.assertIn("function clearFailLine", card)
        chat = _read("js/chat.js")
        self.assertIn("failUserLine(card, msg", chat)
        gallery_css = _read("css/gallery.css")
        self.assertIn("overflow: hidden;", gallery_css)

    def test_mount_submit_dedup_and_download_feedback(self):
        script = r'''
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
class Element {
  constructor() {
    this.children = []; this.hidden = false; this.disabled = false; this.style = {};
    this.innerHTML = ""; this.textContent = "";
    this.classList = {add(){}, remove(){}};
  }
  appendChild(n) { this.children.push(n); return n; }
  setAttribute() {} removeAttribute() {} click() {} remove() {}
}
const root = process.argv[1];
const timers = [];
const w = {addEventListener(){}, URL: {createObjectURL(){return "blob:test";}, revokeObjectURL(){}},
  setTimeout(fn){timers.push(fn);}, fetch: async()=>({ok:true, blob:async()=>({size:10})})};
const context = vm.createContext({window:w, document:{addEventListener(){}, createElement:()=>new Element(), body:new Element()},
  setInterval:()=>1, clearInterval(){}, console, Map, Promise, Date});
function load(name) { vm.runInContext(fs.readFileSync(root+"/js/"+name,"utf8"),context); }
async function flush() { for(let i=0;i<12;i++) await Promise.resolve(); }
(async()=>{
  load("card.js");
  const mounted=[];
  w.WSCard.attach(n=>mounted.push(n));
  w.WSCard.shell();
  assert.equal(mounted.length,1,"生成卡必须挂入消息区");
  load("gen_flow.js");
  const state={pending:{}, cards:new Map(), msgs:[], timer:null};
  let calls=0, resolvers=[];
  w.WSGen={submit(){calls++; return new Promise(r=>resolvers.push(r));}, status:async()=>({ok:false})};
  w.WSGenFlow.bind({state:()=>state, save:r=>{if(r&&!state.msgs.includes(r))state.msgs.push(r);},
    patch(){}, newCard(prompt,skill,rec){return Object.assign(w.WSCard.shell(),{prompt,skill,rec});},
    fail(c,msg){c.say(msg);}, sysNote(){}});
  w.WSGenFlow.submit("画猫","s");
  const rec=state.msgs[0], card=state.cards.get(rec);
  assert.equal(card.note.textContent,"正在提交…");
  w.WSGenFlow.submit("画猫","s");
  assert.equal(calls,2,"相同提示词的独立消息分别提交");
  resolvers[0]({ok:true,job:{id:"j1",prompt:"cat tags"}}); await flush();
  assert.equal(card.note.textContent,"排队中…");
  assert.equal(state.msgs.length,2,"相同文本的两条请求各有独立记录");
  resolvers[1]({ok:true,job:{id:"j2",prompt:"second tags"}}); await flush();
  assert.equal(Object.keys(state.pending).length,2,"两条任务分别进入轮询");
  delete state.pending.j2; state.msgs[1].file="second.webp";
  const before=state.msgs.length;
  const redrawCard={rec:state.msgs[1],file:"second.webp",prompt:"second tags",skill:"s",
    userNode:null,refs:[],node:new Element(),redrawing:false};
  load("redraw.js");
  w.WSRedraw.bind({msgs:()=>state.msgs,save(){},submit:(p,s,r,d,u,defer,final)=>
    w.WSGenFlow.submit(p,s,r,d,u,defer,final),retry(){}});
  w.WSRedraw.run(redrawCard);
  assert.equal(state.msgs.length,before+1,"成功重画只新增一条图片记录");
  assert.equal(state.msgs[1].file,"second.webp","旧成功结果仍保留");
  assert.equal(state.msgs.at(-1).prompt,"second tags","新卡复用实际英文 tags");
  assert.equal(state.msgs.at(-1).finalPrompt,true,"新卡标记为最终提示词");
  resolvers[2]({ok:false,error:"offline"}); await flush();
  load("hide.js");
  assert.equal(typeof w.WSHide.save,"function");
  assert.equal(w.WSHide.downloadButton,undefined,"下载不应再新增面板/分享入口");
  const panel=context.document.body.children.find(n=>n.children.some(c=>c.textContent==="保存图片"));
  assert.equal(panel,undefined,"下载不应弹出保存面板");
  // 重画：出过图的旧记录让位，新卡在底部新建（含新的用户气泡）
  load("redraw.js");
  const msgs=[{k:"user",t:"画狗"},{k:"img",ask:"画狗",file:"old.webp",prompt:"cat tags"}];
  const oldRec=msgs[1];
  const oldUser=new Element(), oldNode=new Element();
  let sent=null;
  w.WSRedraw.bind({msgs:()=>msgs,save(){},clearFail(){},retry(){},
    discard(rec){const at=msgs.indexOf(rec); if(at>=0) msgs.splice(at,1);},
    submit:(text,skill,refs,described,user,defer,finalPrompt)=>{
      sent={text,skill,refs,described,user,defer,finalPrompt};
    }});
  w.WSRedraw.run({rec:oldRec,refs:[],skill:"s",userNode:oldUser,node:oldNode,
                  prompt:"cat tags",file:"old.webp"});
  assert.equal(msgs.length,1,"旧图记录让位给新卡，旧用户消息仍保留");
  assert.equal(msgs[0].t,"画狗");
  assert.equal(sent.text,"cat tags");
  assert.equal(sent.finalPrompt,true,"重画直接使用最终英文 tags");
  assert.equal(sent.user,null,"气泡不重画：交给上层在底部新建");
  assert.equal(sent.refs.length,0,"出过图的重画不再带参考图");
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        result = subprocess.run(
            [shutil.which("node"), "-e", script, WS],
            capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
