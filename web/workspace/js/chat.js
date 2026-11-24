/* 聊天页：用户气泡 + 参考卡 + AI 结果卡。
   生图走 /api/gen/*，轮询有上限，停了能重试；消息进 localStorage，刷新接得回来。
   看图反推提示词走「图片生成」模式（用户 2026-11-23 的口径：画同款是把这张
   读成一段描述再生成，这里不做像素级的图片二次生成）。 */
(function (global) {
  "use strict";

  /* 气泡折叠、轮询间隔、超时两条线都不在这份文件里了（2026-11-24 拆的）：
        · 折叠在 card.js（`WSCard.COLLAPSE_AT`），气泡就是那边画的；
        · 轮询 / 超时在 gen_flow.js（`SETTLE_MS` 排队 8 分钟、`PAINT_MS`
          真在画 25 分钟）；
        · 铺图的档位在 shot.js（`WSShot.FIT = "1m"`）。
     这里只留状态和编排，值一律不再抄第二份。 */

  var el = {};
  var S = {
    skill: "", channels: [], pending: {}, timer: null, fails: 0,
    msgs: [],      // 存档：每条消息一条记录，见 archive.js
    /* 存档记录 → 它的卡片 DOM（2026-11-24）。用户连点「重画」时要认出
       「上一条还是同一份请求」，没有这张表就只能靠提示词去 DOM 里翻。 */
    cards: {}
  };

  function $(id) { return document.getElementById(id); }

  /* 消息区一律是根那一个（#chat-scroll）。
     AI 创作页早先借走过输入栏、也自带过一块 .gchat 收消息，那是为「在浏览页
     里直接对话」准备的；现在口径变了——创作回聊天页做，子页只负责浏览。
     所以「发送气泡」「挂参考卡」都写回根，子页那份历史消息区不再收新东西。

     ⚠️ 别再用「哪个 .view-push 里带 [data-chat-scroll]」来挑：退页动画播完
     之前那一页还在 DOM 里（连 .in 都还挂着，closeNow 先摘 .in、节点要等
     transitionend 才移除），这时候写进去的消息会跟着节点一起被回收——
     「画同款」的参考卡就是这么凭空消失的（2026-11-23 实测）。 */
  function box() {
    return el.scroll;
  }

  function scrollBottom() {
    var b = box();
    if (b) b.scrollTop = b.scrollHeight;
  }

  function hideEmpty() {
    if (el.empty) el.empty.hidden = true;
  }

  // ── 存档 ──────────────────────────────────────────────
  // 形状由 archive.js 重放时按原样读回来，所以这里只负责「加一条、存一次」。

  function log(m) {
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

  /* 一批参考图发出去之后，把存档里对应的 `k:"ref"` 记录一并清掉。

     这类记录的本意是「这张还没发，刷新后要挂回输入栏」，所以只要它留在
     存档尾巴上，下一次 restore 就会把它认回来——用户 2026-11-24 报的
     「每次刷新引用图栏自己冒出来」就是这个。发送成功 = 它已经兑现了，
     记录该走。 */
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

  /* 一条会自己消失的提示（「正在传参考图…」「正在看图…」这类）。
     跟 sysNote 分开：sysNote 是对话记录的一部分，要进存档；这种过场提示
     不该进存档，刷新后也不该再出现一条「正在看图…」。返回的句柄用来提前
     收掉（传完了、看完了），到点也会自己走。 */
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

  /* 用户气泡 / 提示 / 卡片壳 / 按钮全在 card.js（2026-11-24 拆的）。
     那边只管长相，往哪写、写完要不要落盘由这里决定，所以先把它接上我们
     自己的「塞进根消息区 + 滚到底」。 */
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

  function addUser(text, refs) {
    paintUser(text, refs);
    log({ k: "user", t: text, refs: briefRefs(refs) });
  }

  function shell(cls) { return global.WSCard.shell(cls); }
  function spawn(card) { return global.WSCard.spawn(card); }
  function flash(card) { return global.WSCard.flash(card); }
  function buildActs(card, retryLabel) {
    return global.WSCard.buildActs(card, retryLabel, {
      again: function (c) { submitJob(c.prompt, c.skill, c.refs || []); },
      download: function (c) { downloadImage(c); }
    });
  }

  /* 一张新的出图卡。**先只有一行「排队中…」**：没有占位块、没有 3:4 的底，
     真的要开始画了才 `spawn()`（见 gen_flow.js 里 tick 的 running 分支）。 */
  function newCard(prompt, skill, rec) {
    var c = shell();
    c.say("排队中…");
    return { node: c.node, body: c.body, line: c.line, ph: null,
             note: c.note, stop: c.stop, acts: c.acts, fill: c.fill,
             /* 失败后重排要复用它：restart() 会把这一行字改回「排队中…」 */
             say: c.say,
             prompt: prompt, skill: skill, job: "", rec: rec };
  }

  /* 铺图 / 放大 / 下载都搬到 shot.js 了（2026-11-24）：这边已经一千多行，
     那三件事只跟「一张已经出好的图」有关，跟排队、存档、重画都无关。
     FIT 还是那个 1MB 档，值只在 shot.js 里写一次，这里转发一下给老调用点。 */
  var FIT = global.WSShot.FIT;
  function shotEl(file) { return global.WSShot.shotEl(file); }
  function downloadImage(card) { return global.WSShot.download(card); }

  function showImage(card, job) {
    var img = shotEl(job.file);
    img.addEventListener("error", function () {
      card.note.hidden = false;
      card.note.className = "note err";
      card.note.textContent = "图不在了，重画一张";
    });
    if (card.ph) {
      card.ph.parentNode.replaceChild(img, card.ph);
    } else {
      card.body.appendChild(img);
    }
    card.ph = null;
    card.img = img;                 // 留个引用：下载/预览都靠它
    if (card.fill) card.fill();     // 有图了：空壳变回真卡片
    card.note.hidden = true;
    card.stop && (card.stop.hidden = true);
    card.file = job.file || "";
    card.since = 0;
    card.noted = "";
    buildActs(card);
    if (card.rec) {
      card.rec.file = job.file;
      card.rec.note = "";
      card.rec.err = false;
      /* 先在服务端存档里把这条从「排队中」改成「已出图」，再清掉 job：
         只清本地不写服务端的话，换设备登录读回来的还是「排队中…」
         （用户 2026-11-24 看到的那张假卡就是这样留下的）。 */
      global.WSStore.patch(card.rec, { file: job.file, note: "", err: false,
                                       job: "" });
      card.rec.job = "";
      if (global.WSStore) global.WSStore.save({ msgs: S.msgs });
    }
    scrollBottom();
  }

  /* 失败 / 超时。`stuck` 决定给什么按钮口径：卡住的那张给「重试」
     （按同一份提示词重排），普通失败给「重画」（换种子）。 */
  function fail(card, msg, stuck) {
    disarmStop(card);
    card.since = 0;
    card.noted = "";
    card.note.hidden = false;
    card.note.className = "note err";
    card.note.textContent = msg || "这张没出来，点重画再来一次";
    /* 失败不再留一张 3:4 的大空壳在那里（用户 2026-11-24：「网络报错……都是
       使用一个大卡片显示的……每次滑动都是一大堆的大卡片」）。还没出图就把
       那块底纹撤掉、壳退回「一行字」的 idle 态；真出过图的那张
       （`card.img`）留着图不动，`.is-idle` 本来就摘掉了。 */
    if (card.ph) {
      card.ph.remove();
      card.ph = null;
      if (card.node) card.node.classList.add("is-idle");
    }
    buildActs(card, stuck ? "重试" : "");
    if (card.rec) {
      card.rec.note = card.note.textContent;
      card.rec.err = true;
      /* 先在服务端存档里把这条从「排队中」改成失败态，再清掉 job：
         只清本地不写服务端的话，换设备登录读回来的还是「排队中…」
         （用户 2026-11-24 看到的那张假卡就是这样留下的）。 */
      global.WSStore.patch(card.rec, { job: card.rec.job || "",
                                       note: card.note.textContent, err: true });
      card.rec.job = "";
      if (global.WSStore) global.WSStore.save({ msgs: S.msgs });
    }
    scrollBottom();
  }

  // ── 生图 ──────────────────────────────────────────────

  /* 提交 / 轮询 / 判超时 / 终止全在 gen_flow.js（2026-11-24 拆的）：
     那边只做「发请求、看状态」，画卡片的活儿由 card.js 干，这里只负责
     把三样东西交给它——存档数组、落盘函数、和上面那几个渲染函数。 */
  function submitJob(prompt, skill, refs, described) {
    return global.WSGenFlow.submit(prompt, skill, refs, described);
  }
  function armStop(card) { return global.WSGenFlow.armStop(card); }
  function disarmStop(card) { return global.WSGenFlow.disarmStop(card); }
  function startPoll() { return global.WSGenFlow.startPoll(); }
  function bindFlow() {
    global.WSGenFlow.bind({
      state: function () { return S; },
      save: function (rec) {
        if (rec) log(rec);
        else if (global.WSStore) global.WSStore.save({ msgs: S.msgs });
      },
      patch: function (rec, fields) {
        if (global.WSStore) global.WSStore.patch(rec, fields);
      },
      newCard: function (prompt, skill, rec) {
        var c = newCard(prompt, skill, rec);
        S.cards[rec] = c;
        return c;
      },
      spawn: spawn,
      showImage: showImage,
      fail: fail,
      sysNote: sysNote
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

  /* 输入栏被挪进子页（视图构建时移的）那一刻还没有尺寸，上面量到的是 0。
     这里补量一次，并替子页盯着：宿主一出现就补，别停在 0。 */
  function fitWhenSized() {
    if (!el.composer) return;
    if (el.composer.offsetHeight) { fitComposer(); return; }
    global.requestAnimationFrame(function () {
      if (el.composer.offsetHeight) fitComposer();
    });
  }

  function syncSend() {
    el.send.disabled = !el.say.value.trim() || el.say.disabled;
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
    var text = el.say.value.trim();
    if (!text) return;
    var refs = global.WSRef.list();
    addUser(text, refs);
    el.say.value = "";
    grow();
    syncSend();

    /* 在子页里发的话，发完就该回聊天页（2026-11-24 用户：「我在 AI 创作界面，
       上传图片，然后说需求，然后点击发送的时候，没有自动回到初始的聊天界面」）。

       为什么要退而不是原地等：气泡和出图卡片全都写进根消息区（见 box() 上
       面的注释），子页盖在上面时用户看到的是「点了发送、什么都没发生」。
       退页动画 0.28s，正好盖住识图 + 排队的起步，回来就是聊天页在跑。

       只退一层：AI 创作是从聊天页推进来的，退一层就回到根。漫画单本页
       （book）不借输入栏，走不到这儿。 */
    if (global.WSStack && global.WSStack.top()) {
      var top = global.WSStack.top();
      // 只退「借了输入栏」的页；别的子页里如果以后也能发，别顺手把人退出去
      if (top.dataset && top.dataset.dock) global.WSStack.back();
    }

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
      submitJob(text, S.skill, pre.map(function (r) { return r.id; }), true);
      return;
    }

    global.WSRef.ensureDescribed(refs).then(function (read) {
      global.WSRef.commit();
      submitJob(text, S.skill, read.map(function (r) { return r.id; }));
    });
  }

  /* 复制走 card.js 那一份（按钮就在那边，两边共用同一段剪贴板逻辑） */
  function copyText(text) { return global.WSCard.copyText(text); }

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

    global.addEventListener("resize", fitWhenSized);

    el.send.addEventListener("click", send);
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
      if (!el.menu.hidden && !el.menu.contains(e.target) && e.target !== el.plus) {
        el.menu.hidden = true;
      }
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
    /* 先取图片签名再重放存档：`WSGen.img()` 拼出来的地址要带 `k=`，本地缓存
       那条路才走得通（拿不到也不拦，页面照旧按 URL 显示图）。 */
    global.WSGen.imgsign().then(function () {
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
    /* 参考图拆到 ref.js 了，这里留个转发：旧的调用点还在的话不会炸 */
    syncHint: function () { if (global.WSRef) global.WSRef.syncHint(); },
    paintUser: paintUser,
    paintNote: paintNote,
    /* 重放存档时复用同一张图（同一条地址 → 同一个缓存 key，重放和历史不会
       各下一遍）。见 archive.js 的 replay()。 */
    shotEl: shotEl,
    buildActs: buildActs,
    /* 重画收口用（2026-11-24）：存档重放时把「记录 → 卡片」交给这张表，
       用户再点重画才能认得出上一条是同一份请求。 */
    cards: S.cards,
    spawn: spawn,
    flash: flash,
    box: box,
    hideEmpty: hideEmpty,
    scrollBottom: scrollBottom,
    pending: S.pending,
    msgs: function () { return S.msgs; },
    startPoll: startPoll,
    /* 重放存档时用（见 archive.js）：把一条「其实已经没在跑」的卡片就地
       判成失败并给「重试」，以及给还活着的那条挂上终止/计时。 */
    failCard: fail,
    armStop: armStop
  };
})(window);
