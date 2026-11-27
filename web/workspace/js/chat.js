/* 聊天页：用户气泡 + 参考卡 + AI 结果卡。
   生图走 /api/gen/*，轮询有上限，停了能重试；消息进 localStorage，刷新接得回来。
   看图反推提示词走「图片生成」模式（用户 2026-11-23 的口径：画同款是把这张
   读成一段描述再生成，这里不做像素级的图片二次生成）。 */
(function (global) {
  "use strict";

  /* 折叠在 card.js、轮询/超时在 gen_flow.js、图片档位在 shot.js。
     这里只留状态和编排，那些值一律不再抄第二份。 */

  var el = {};
  var S = {
    skill: "", channels: [], pending: {}, timer: null, fails: 0,
    msgs: [],      // 存档：每条消息一条记录，见 archive.js
    /* 存档记录 → 卡片节点，用于识别重复请求。 */
    cards: new Map()
  };

  function $(id) { return document.getElementById(id); }

  /* 消息统一写到根 #chat-scroll。退页动画期间不要往子页写消息。 */
  function box() {
    return el.scroll;
  }

  /* 滚动本身（落到底 / 进页面钉在底部）拆到 scroll.js 了：这块只跟一个
     容器打交道，跟消息内容无关，放这边白白占行数。这里只留一个短名字，
     免得每个调用点都写 `global.WSScroll.bottom()`。 */
  function scrollBottom() {
    if (global.WSScroll) global.WSScroll.bottom();
  }

  function hideEmpty() {
    if (el.empty) el.empty.hidden = true;
  }

  // ── 存档 ──────────────────────────────────────────────
  // 形状由 archive.js 重放时按原样读回来，所以这里只负责「加一条、存一次」。

  /* 记录 id：一张卡从「排队中」走到「已出图」还是同一条记录，靠它认人。
     以前服务端按整条 JSON 去重，状态一变就被当成新记录再追加一遍——240 条
     里 165 条是卡，顺序全乱了（2026-11-25 用户实测）。 */
  var seq = 0;
  function newId(prefix) {
    seq += 1;
    return (prefix || "m") + Date.now().toString(36) + "_" + seq;
  }

  function log(m) {
    if (m && !m.id) m.id = newId(m.k === "img" ? "img" : "m");
    S.msgs.push(m);
    if (S.msgs.length > 200) S.msgs = S.msgs.slice(-160);
    if (global.WSStore) global.WSStore.save({ msgs: S.msgs });
    return m;
  }

  /* 漫画卡片的记录由 create.js 改字段、这里统一落盘，免得两边各存一份 */
  function remember(rec) {
    log(rec);
    return rec;
  }

  function touch(rec) {
    if (global.WSStore) global.WSStore.save({ msgs: S.msgs });
    return rec;
  }

  /* 撤掉一张参考图：消息区里那张卡片和存档里那条记录一起删 */
  function dropRef(id) {
    var sc = box();
    Array.prototype.forEach.call(
      sc ? sc.querySelectorAll("[data-ref]") : [], function (n) {
        if (n.dataset.ref === String(id)) n.remove();
      });
    S.msgs = S.msgs.filter(function (m) {
      return !(m.k === "ref" && m.id === id);
    });
    if (global.WSStore) global.WSStore.save({ msgs: S.msgs });
  }

  /* 参考图发出去后清掉存档里的 `k:"ref"`：这类记录的本意是「还没发，
     刷新后挂回输入栏」，留着就会被 restore 认回来（2026-11-24 实测）。 */
  function dropRefRecords(ids) {
    if (!ids || !ids.length) return;
    var want = {};
    ids.forEach(function (i) { want[String(i)] = 1; });
    S.msgs = S.msgs.filter(function (m) {
      return !(m.k === "ref" && want[String(m.id)]);
    });
    if (global.WSStore) global.WSStore.save({ msgs: S.msgs });
  }

  // ── 消息区 ────────────────────────────────────────────

  function paintNote(text) {
    var p = global.WSCard.paintNote(text);
    scrollBottom();
    return p;
  }
  function sysNote(text) {
    var p = paintNote(text);
    log({ k: "note", t: text });
    return p;
  }
  /* 大大怪说的一句话（2026-11-26：工作台接上 QQ 那套 AI 之后，模型判
     「这轮不用画」会回话，比如「这句没说要画什么」）。跟提示行分开：
     贴左、能换行、跟正文一样大，因为它就是一次对话，不是系统通知。 */
  function sayText(text) {
    var p = document.createElement("p");
    p.className = "say";
    p.textContent = text || "";
    box().appendChild(p);
    hideEmpty();
    scrollBottom();
    log({ k: "say", t: text || "" });
    return p;
  }

  function busyNote(text, ms) {
    var p = paintNote(text);
    var t = setTimeout(function () { if (p.parentNode) p.remove(); }, ms || 20000);
    return {
      remove: function () {
        clearTimeout(t);
        if (p.parentNode) p.remove();
      }
    };
  }

  /* 气泡长相在 card.js；往哪写由这里定（根消息区 + 滚到底）。 */
  function paintUser(text, refs) {
    var b = global.WSCard.paintUser(text, refs);
    scrollBottom();
    return b;
  }

  /* 存档里的 refs 只留 id / wid / url：text 是那一轮读出来的描述，已经在
     提示词里了，留着只是白占 localStorage。 */
  function briefRefs(refs) {
    return (refs || []).map(function (r) {
      return { id: r.id, wid: r.wid, url: r.url || "" };
    });
  }

  /* 失败行按气泡配对。 */
  var userNodes = [];

  function addUser(text, refs) {
    var node = paintUser(text, refs);
    node._wsUserId = newId("u");
    node._wsUserText = text;
    userNodes.push(node);
    log({ k: "user", id: node._wsUserId, t: text, refs: briefRefs(refs) });
    return node;
  }

  function dropUser(node, text) {
    if (node && node.parentNode) node.parentNode.removeChild(node);
    userNodes = userNodes.filter(function (n) { return n !== node; });
    if (text) {
      for (var i = S.msgs.length - 1; i >= 0; i--) {
        if (S.msgs[i].k === "user" && S.msgs[i].t === text) {
          S.msgs.splice(i, 1);
          break;
        }
      }
    }
    if (global.WSStore) global.WSStore.save({ msgs: S.msgs });
  }

  function shell(cls) { return global.WSCard.shell(cls); }
  function spawn(card) { return global.WSCard.spawn(card); }
  function flash(card) { return global.WSCard.flash(card); }
  function buildActs(card, retryLabel) {
    return global.WSCard.buildActs(card, retryLabel, {
      again: function (c) { redraw(c); },
      quote: function (c) { global.WSRef.quote(c); }   // 2026-11-27 取代下载
    });
  }

  /* 新出图卡：先只有一行「排队中…」，真要开画才 spawn()（见 gen_flow.js）。 */
  function newCard(prompt, skill, rec) {
    var c = shell();
    c.say("排队中…");
    var user = null;
    return { node: c.node, body: c.body, line: c.line, ph: null,
             note: c.note, stop: c.stop, acts: c.acts, fill: c.fill,
             /* 失败后重排要复用它：restart() 会把这一行字改回「排队中…」 */
             say: c.say,
             prompt: prompt, skill: skill, job: "", rec: rec,
             userNode: user };
  }

  /* 铺图 / 放大在 shot.js，FIT（1MB 档）那里只写一次，这里转发。
     下载能力仍在 shot.js；卡片上那个位置 2026-11-27 换成了「引用」。 */
  var FIT = global.WSShot.FIT;
  function shotEl(file) { return global.WSShot.shotEl(file); }

  /* 出图 / 失败是「一张卡走到终点」的收尾，跟轮询同一件事，在 gen_flow.js。 */
  function showImage(card, job) {
    return global.WSGenFlow.showImage(card, job);
  }

  function fail(card, msg, stuck) {
    return global.WSGenFlow.fail(card, msg, stuck);
  }

  // ── 生图 ──────────────────────────────────────────────

  /* 提交 / 轮询 / 判超时 / 终止全在 gen_flow.js，两边靠 bindFlow 的钩子对接。 */
  function submitJob(prompt, skill, refs, described, userNode, defer, finalPrompt,
                      route) {
    return global.WSGenFlow.submit(prompt, skill, refs, described, userNode,
                                   defer, finalPrompt, route);
  }
  function armStop(card) { return global.WSGenFlow.armStop(card); }
  function disarmStop(card) { return global.WSGenFlow.disarmStop(card); }
  function startPoll() { return global.WSGenFlow.startPoll(); }
  /* 把重画依赖交给 redraw.js（识图 → 提交，和手动发送同一套）。 */
  function bindRedraw() {
    global.WSRedraw.bind({
      clearFail: function (node) { global.WSCard.clearFailLine(node); },
      msgs: function () { return S.msgs; },
      save: function () {
        if (global.WSStore) global.WSStore.save({ msgs: S.msgs });
      },
      /* 旧的那张图记录从存档里摘掉：新卡要自己记状态，留着就是两条。 */
      discard: function (rec) {
        var at = S.msgs.indexOf(rec);
        if (at >= 0) S.msgs.splice(at, 1);
        S.cards.delete(rec);
      },
      /* 重画一律走**当前选的默认画风**（用户 2026-11-25：换成极光之后点
         重画就该用极光，不能沿用这张当初的渠道）。 */
      currentSkill: function () { return S.skill; },
      /* `user` 恒为 null（重画不重画气泡）：新卡贴在列表最底下，
         跟新发一条一样显眼。旧卡和旧气泡都留在原位不动。 */
      submit: function (text, skill, refs, described, user, _defer, finalPrompt) {
        user = user || addUser(text, refs);
        var ids = refs.map(function (r) { return r.id || r; });
        var submitted = submitJob(text, skill, ids, !!described, user,
                                  false, !!finalPrompt);
        if (!submitted) { dropUser(user, text); return null; }
        return submitted;
      }
    });
  }

  function bindFlow() {
    global.WSGenFlow.bind({
      state: function () { return S; },
      save: function (rec) {
        if (rec && S.msgs.indexOf(rec) < 0) log(rec);
        else if (global.WSStore) global.WSStore.save({ msgs: S.msgs });
      },
      patch: function (rec, fields) {
        if (global.WSStore) global.WSStore.patch(rec, fields);
      },
      newCard: function (prompt, skill, rec) {
        var c = newCard(prompt, skill, rec);
        S.cards.set(rec, c);
        return c;
      },
      spawn: spawn,
      sysNote: sysNote,
      say: sayText,
      newId: newId,
      busyTip: function (text) { busyNote(text, 3000); },
      dropCard: function (card) { global.WSRedraw.drop(card); },
      /* 出图 / 判失败之后滚到底，以及失败提示上那颗按钮点了往哪儿走
         （出图/失败本身现在住在 gen_flow.js，见那边的 showImage / fail）。 */
      scroll: scrollBottom,
      redraw: redraw
    });
  }

  // ── 输入栏 ────────────────────────────────────────────

  function grow() {
    el.say.style.height = "auto";
    el.say.style.height = Math.min(el.say.scrollHeight, 132) + "px";
    fitComposer();
  }

  // 输入栏是浮在消息上的玻璃层，消息区底部得留出它的实际高度
  function fitComposer() {
    if (!el.composer) return;
    document.documentElement.style.setProperty(
      "--composer-h", el.composer.offsetHeight + "px");
  }

  /* 输入栏被挪进子页那一刻还没尺寸（量到的是 0）：这里补量一次。 */
  function fitWhenSized() {
    if (!el.composer) return;
    if (el.composer.offsetHeight) { fitComposer(); return; }
    global.requestAnimationFrame(function () {
      if (el.composer.offsetHeight) fitComposer();
    });
  }

  function syncSend() {
    var off = !el.say.value.trim() || el.say.disabled;
    el.send.disabled = off;
    if (el.direct) el.direct.disabled = off;
  }

  /* 在子页里发完话要退回聊天页（2026-11-24 用户：「我在 AI 创作界面，上传
     图片，然后说需求，然后点击发送的时候，没有自动回到初始的聊天界面」）。

     为什么要退而不是原地等：气泡和出图卡片全都写进根消息区（见 box() 上面
     的注释），子页盖在上面时用户看到的是「点了发送、什么都没发生」。退页
     动画 0.28s，正好盖住识图 + 排队的起步，回来就是聊天页在跑。

     只退一层：AI 创作是从聊天页推进来的，退一层就回到根。漫画单本页
     （book）不借输入栏，走不到这儿。两个发送按钮（AI 发送 / 直达）共用。 */
  function leaveDock() {
    if (global.WSStack && global.WSStack.top()) {
      var top = global.WSStack.top();
      // 只退「借了输入栏」的页；别的子页里如果以后也能发，别顺手把人退出去
      if (top.dataset && top.dataset.dock) global.WSStack.back();
    }
  }

  /* 发送。参考图的识图挪到这一步（2026-11-23 用户：上传不许立刻实图，
     点发送才实图）：
       1. 先把用户气泡和一起来的参考图铺上去——用户要能看见自己喂了哪张；
       2. 再读这一轮的参考图（`ensureDescribed`，读过的跳过，并行读）；
       3. 读到的描述交给 submit，跟用户这句话一起拼成最终提示词。
     第 2 步的等待有「正在看图…」顶着，且这时输入框已经清空，不会再被
     读出来的描述顶掉。
     第 3 步原先是在点发送的同一帧去取 refIds；现在识图要等，所以不能先
     commit（commit 会清空待发送列表），等识图完再清。 */
  function send() {
    // 引用图还在上传就别发：refs 里还没它，发出去会静默变成纯文生图（2026-11-27）
    if (global.WSRef.busy && global.WSRef.busy()) { sysNote("引用图还在准备，稍等一秒"); return; }
    var text = el.say.value.trim();
    if (!text) return;
    var refs = global.WSRef.list();
    var user = addUser(text, refs);
    el.say.value = "";
    grow();
    syncSend();
    leaveDock();

    // 漫画模式：这句话是剧情需求，交给创作面板去跑「编剧 → 分页画」
    // 漫画那条链自己会读参考，不需要在这里等识图
    if (global.WSCreate && global.WSCreate.mode() === "comic") {
      global.WSRef.commit();
      global.WSCreate.runComic(text, refs.map(function (r) { return r.id; }));
      return;
    }

    /* 「画同款」把图读成描述填进输入栏之后，那张参考图就**已经**被读过了
       （`pre` 标记），用户点发送时不该再让它识一次图——否则同一段描述会被
       拼进提示词两遍，还白花一次视觉模型。见 ref.js 的 `same`。 */
    var pre = refs.filter(function (r) { return r && r.pre && r.id; });
    if (pre.length) {
      global.WSRef.commit();
      var preCard = submitJob(text, S.skill,
                              pre.map(function (r) { return r.id; }), true, user,
                              false, false, "ask");
      if (!preCard) dropUser(user, text);
      return;
    }

    global.WSRef.ensureDescribed(refs).then(function (read) {
      global.WSRef.commit();
      var newCard = submitJob(text, S.skill,
                              read.map(function (r) { return r.id; }), false, user,
                              false, false, "ask");
      if (!newCard) dropUser(user, text);
    });
  }

  /* 「直达」：粘进来的词**原样**用，不经过大大怪，点一下只跑一张。
     （2026-11-27 用户拍板：直达就是「把这一句直接注入工作流」，5 张是
     早先按粉丝群反馈自己加的连排，不是直达的本意。）仍走 `/api/gen/batch`
     建任务：它和 submit 共用一套排队/记账，只是能一次建 N 条；这里只传 1。
     参考图照旧读成描述拼进去：原样 = 不改写用户的词，不是不看用户喂的图。 */
  var DIRECT_N = 1;

  function sendDirect() {
    if (global.WSRef.busy && global.WSRef.busy()) { sysNote("引用图还在准备，稍等一秒"); return; }
    var text = el.say.value.trim();
    if (!text) return;
    var refs = global.WSRef.list();
    var user = addUser(text, refs);
    el.say.value = "";
    grow();
    syncSend();
    leaveDock();

    if (global.WSCreate && global.WSCreate.mode() === "comic") {
      // 漫画页上按了直达：漫画另有入口，不该在这里插一张单图。
      // 退回去当一条普通漫画需求，并说一句，免得他以为按错了。
      sysNote("漫画模式不连发，已按一次生成提交");
      global.WSRef.commit();
      global.WSCreate.runComic(text, refs.map(function (r) { return r.id; }));
      return;
    }

    var pre = refs.filter(function (r) { return r && r.pre && r.id; });
    if (pre.length) {
      global.WSRef.commit();
      fireDirect(text, pre.map(function (r) { return r.id; }), true, user);
      return;
    }
    global.WSRef.ensureDescribed(refs).then(function (read) {
      global.WSRef.commit();
      fireDirect(text, read.map(function (r) { return r.id; }), false, user);
    });
  }

  function fireDirect(text, refs, described, user) {
    var body = { prompt: text, skill: S.skill, count: DIRECT_N,
                 refs: refs, ref_described: !!described };
    // 直达也认同步开关（2026-11-27）
    if (global.WSSync && global.WSSync.on()) body.qq_sync = true;
    global.WSGen.direct(body).then(function (d) {
      global.WSGenFlow.linkBatch(d, text, user);
    }, function () {
      dropUser(user, text);
      sysNote("直达没发出去，检查网络后再点一次");
    });
  }

  /* 重画在 redraw.js（bindRedraw() 在 init 里接上），这里只转发。 */
  function redraw(card) { return global.WSRedraw.run(card); }
  function copyText(text) { return global.WSCard.copyText(text); }   // card.js

  // ── 画风 ──────────────────────────────────────────────

  /* 选画风不弹提示（用户 2026-11-23：选了就是默认了，别每次都跳一条
     「已更换 XX 渠道」）。名字由输入栏上的画风按钮自己显示。 */
  function pickSkill(id) {
    if (!id || id === S.skill) { syncSkillName(); return; }
    S.skill = id;
    global.WSGen.setDefaultSkill(id);
    syncSkillName();
  }

  function syncSkillName() {
    if (!el.skillName) return;
    var c = S.channels.filter(function (x) { return x.id === S.skill; })[0] || {};
    el.skillName.textContent = (c.label && c.label !== c.id) ? c.label : (c.id || "画风");
  }

  function bootChannels() {
    el.say.disabled = true;
    global.WSGen.channels().then(function (d) {
      el.say.disabled = false;
      syncSend();
      if (!d || !d.ok) {
        sysNote("没拿到渠道列表，先去跑图台看看，回来再试");
        return;
      }
      S.channels = d.channels || [];
      var saved = global.WSGen.defaultSkill();
      var hit = S.channels.filter(function (c) { return c.id === saved; })[0];
      S.skill = (hit || S.channels[0] || {}).id || "";
      syncSkillName();
    });
  }

  // ── 初始化 ────────────────────────────────────────────

  function init() {
    el.scroll = $("chat-scroll");
    el.empty = $("chat-empty");
    el.composer = $("composer");
    el.say = $("say");
    el.send = $("btn-send");
    el.direct = $("btn-direct");
    el.plus = $("btn-plus");
    el.menu = $("plus-menu");
    el.cam = $("file-camera");
    el.album = $("file-album");
    el.skillName = $("skill-name");

    /* card.js / gen_flow.js 都不知道消息区在哪儿，也不知道存档怎么落盘：
       这两件事还是这边说了算，启动时一次性交给它们。 */
    global.WSCard.attach(function (node) {
      if (node) box().appendChild(node);
      scrollBottom();
    }, scrollBottom);
    bindFlow();
    bindRedraw();

    global.addEventListener("resize", fitWhenSized);

    el.send.addEventListener("click", send);
    /* 直达：原样跑一张，不经过 AI。和「AI 发送」分开，别在输入框回车时
       误触——两个入口的行为不一样。 */
    if (el.direct) el.direct.addEventListener("click", sendDirect);
    el.say.addEventListener("input", function () { grow(); syncSend(); });
    el.say.addEventListener("keydown", function (e) {
      // 手机回车就是换行；桌面 Enter 发送，Shift+Enter 换行
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        if (!el.send.disabled) send();
      }
    });

    el.plus.addEventListener("click", function () {
      el.menu.hidden = !el.menu.hidden;
    });
    document.addEventListener("click", function (e) {
      var out = !el.menu.contains(e.target) && e.target !== el.plus;
      var direct = el.menu.contains(e.target) && e.target.closest("#btn-direct");
      if (!el.menu.hidden && (out || direct)) el.menu.hidden = true;
    });

    // 两个平级创作入口：只切模式，发什么由下面 send() 决定
    $("pick-img").addEventListener("click", function () {
      el.menu.hidden = true;
      global.WSCreate.setMode("img");
    });
    $("pick-comic").addEventListener("click", function () {
      el.menu.hidden = true;
      global.WSCreate.setMode("comic");
    });
    $("pick-camera").addEventListener("click", function () {
      el.menu.hidden = true;
      el.cam.click();
    });
    $("pick-album").addEventListener("click", function () {
      el.menu.hidden = true;
      el.album.click();
    });
    [el.cam, el.album].forEach(function (input) {
      input.addEventListener("change", function () {
        if (!input.files || !input.files.length) return;
        var f = input.files[0];
        input.value = "";
        global.WSRef.upload(f);
      });
    });

    global.addEventListener("ws:skill", function (e) {
      pickSkill(e.detail && e.detail.id);
    });

    grow();
    syncSend();
    /* 进来先落到底，而且**认账一段时间**：重放是异步的、图是懒加载的，
       只滚一次等于没滚（用户 2026-11-25：「每次进入浏览器，默认就是来到
       最底下」）。用户自己一滑就放手，见 scroll.js 的 attach。 */
    if (global.WSScroll) {
      global.WSScroll.attach(el.scroll);
      global.WSScroll.pin();
    }

    /* 先认身份（访客要换本地存档的键、也不该去拉服务端那份）再取图片签名
       ——`WSGen.img()` 拼出来的地址要带 `k=`，本地缓存那条路才走得通
       （拿不到也不拦，页面照旧按 URL 显示图）。 */
    var who = (global.WSStore && global.WSStore.whoami)
      ? global.WSStore.whoami() : Promise.resolve(null);
    who.then(function () {
      return global.WSGen.imgsign();
    }).then(function () {
      if (global.WSArchive) global.WSArchive.restore();
    }, function () {
      if (global.WSArchive) global.WSArchive.restore();
    });
    global.WSRef.init();
    bootChannels();
  }

  global.WSChat = {
    init: init,
    sysNote: sysNote,
    busyNote: busyNote,      // 过场提示，不进存档、会用完自己消失
    card: shell,
    remember: remember,
    touch: touch,
    log: log,
    dropRef: dropRef,
    dropRefRecords: dropRefRecords,
    syncHint: function () { if (global.WSRef) global.WSRef.syncHint(); },
    paintUser: paintUser,
    paintNote: paintNote,
    say: sayText,
    shotEl: shotEl,     // 重放复用同一张图（同一个缓存 key，不重下）
    buildActs: buildActs,
    cards: S.cards,     // 记录 → 卡片，重画时认得出「上一条」
    spawn: spawn,
    flash: flash,
    box: box,
    hideEmpty: hideEmpty,
    scrollBottom: scrollBottom,
    pinBottom: function (ms) { if (global.WSScroll) global.WSScroll.pin(ms); },
    pending: S.pending,
    msgs: function () { return S.msgs; },
    startPoll: startPoll,
    failCard: fail,     // 重放时把「其实没在跑」的卡判失败
    showImage: showImage,
    armStop: armStop
  };
})(window);
