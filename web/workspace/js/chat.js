/* 聊天页：用户气泡 + 参考卡 + AI 结果卡。
   生图走 /api/gen/*，轮询有上限，停了能重试；消息进 localStorage，刷新接得回来。
   看图反推提示词走「图片生成」模式（用户 2026-11-23 的口径：画同款是把这张
   读成一段描述再生成，这里不做像素级的图片二次生成）。 */
(function (global) {
  "use strict";

  var COLLAPSE_AT = 1000;   // 用户单条超过这个字数就折叠
  var HEAD_KEEP = 300;      // 折叠后先露这么多
  var POLL_MS = 2000;
  var MAX_FAIL = 5;         // 连续失败这么多次就停轮询，别无限打接口

  var el = {};
  var S = {
    skill: "", channels: [], pending: {}, timer: null, fails: 0,
    msgs: []       // 存档：每条消息一条记录，见 archive.js
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

  // ── 消息区 ────────────────────────────────────────────

  function paintNote(text) {
    hideEmpty();
    var p = document.createElement("p");
    p.className = "card note";
    p.style.alignSelf = "center";
    p.style.border = "0";
    p.textContent = text;
    box().appendChild(p);
    scrollBottom();
  }

  function sysNote(text) {
    paintNote(text);
    log({ k: "note", t: text });
  }

  function paintUser(text) {
    hideEmpty();
    var b = document.createElement("div");
    b.className = "bubble";
    var span = document.createElement("span");
    b.appendChild(span);

    if (text.length > COLLAPSE_AT) {
      span.textContent = text.slice(0, HEAD_KEEP) + "…";
      var more = document.createElement("button");
      more.type = "button";
      more.className = "more";
      more.textContent = "展开全部（" + text.length + " 字）";
      more.addEventListener("click", function () {
        span.textContent = text;
        more.remove();
      });
      b.appendChild(more);
    } else {
      span.textContent = text;
    }

    box().appendChild(b);
    scrollBottom();
  }

  function addUser(text) {
    paintUser(text);
    log({ k: "user", t: text });
  }

  /* 通用卡片壳：图卡和漫画卡共用一套外观（谁用谁往里塞内容） */
  function shell(cls) {
    hideEmpty();
    var node = document.createElement("div");
    node.className = "card" + (cls ? " " + cls : "");

    var body = document.createElement("div");
    body.className = "card-body";

    var note = document.createElement("p");
    note.className = "note";

    var acts = document.createElement("div");
    acts.className = "acts";
    acts.hidden = true;

    node.appendChild(body);
    node.appendChild(note);
    node.appendChild(acts);
    box().appendChild(node);
    scrollBottom();

    return {
      node: node,
      body: body,
      note: note,
      acts: acts,
      say: function (text, bad) {
        note.hidden = false;
        note.className = "note" + (bad ? " err" : "");
        note.textContent = text;
        scrollBottom();
      },
      show: function () {
        if (!acts.hidden) return;
        acts.hidden = false;
        scrollBottom();
      }
    };
  }

  function newCard(prompt, skill, rec) {
    var c = shell();
    var ph = document.createElement("div");
    ph.className = "ph";
    c.body.appendChild(ph);
    c.say("排队中…");

    return { node: c.node, body: c.body, ph: ph, note: c.note, acts: c.acts,
             prompt: prompt, skill: skill, job: "", rec: rec };
  }

  var ICON_COPY =
    '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
    'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">' +
    '<rect x="9" y="9" width="11" height="11" rx="2.4"/>' +
    '<path d="M15 5.6A2.6 2.6 0 0 0 12.4 3H6.6A2.6 2.6 0 0 0 4 5.6v5.8A2.6 2.6 0 0 0 6.6 14"/></svg>';

  var ICON_AGAIN =
    '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
    'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">' +
    '<path d="M20 12a8 8 0 1 1-2.6-5.9"/><path d="M20 4.6V10h-5.4"/></svg>';

  /* card 只要能提供 {acts, prompt, skill} 就够了：恢复出来的卡也走这里 */
  function buildActs(card) {
    if (card.acts.childElementCount) return;
    card.acts.hidden = false;

    var copy = document.createElement("button");
    copy.type = "button";
    copy.title = "复制提示词";
    copy.setAttribute("aria-label", "复制提示词");
    copy.innerHTML = ICON_COPY;
    copy.addEventListener("click", function () {
      copyText(card.prompt).then(function (ok) {
        copy.innerHTML = ok ? '<span class="ok">✓</span>' : "!";
        setTimeout(function () { copy.innerHTML = ICON_COPY; }, 1200);
      });
    });

    var again = document.createElement("button");
    again.type = "button";
    again.title = "换种子重画";
    again.setAttribute("aria-label", "重画");
    again.innerHTML = ICON_AGAIN;
    again.addEventListener("click", function () {
      submitJob(card.prompt, card.skill, []);
    });

    card.acts.appendChild(copy);
    card.acts.appendChild(again);
  }

  function showImage(card, job) {
    if (card.ph) {
      var img = document.createElement("img");
      img.alt = "生成结果";
      img.src = global.WSGen.img(job.file);
      img.addEventListener("error", function () {
        card.note.hidden = false;
        card.note.className = "note err";
        card.note.textContent = "图不在了，重画一张";
      });
      card.ph.parentNode.replaceChild(img, card.ph);
      card.ph = null;
    }
    card.note.hidden = true;
    buildActs(card);
    if (card.rec) {
      card.rec.file = job.file;
      card.rec.note = "";
      card.rec.err = false;
      card.rec.job = "";
      if (global.WSStore) global.WSStore.save({ msgs: S.msgs });
    }
    scrollBottom();
  }

  function fail(card, msg) {
    card.note.hidden = false;
    card.note.className = "note err";
    card.note.textContent = msg || "这张没出来，点重画再来一次";
    if (card.ph) card.ph.style.opacity = ".3";
    buildActs(card);
    if (card.rec) {
      card.rec.note = card.note.textContent;
      card.rec.err = true;
      card.rec.job = "";
      if (global.WSStore) global.WSStore.save({ msgs: S.msgs });
    }
    scrollBottom();
  }

  // ── 生图 ──────────────────────────────────────────────

  /* refs：这一轮要带上的参考图 id。生图接口本身是按描述出图，参考图随请求
     一起交给后端（它按渠道能力决定用不用），前端不自己拼提示词。 */
  function submitJob(prompt, skill, refs) {
    if (!skill) {
      sysNote("还没拿到渠道列表，去跑图台看一眼再回来");
      return;
    }
    var rec = { k: "img", cls: "", prompt: prompt, skill: skill,
                job: "", file: "", note: "排队中…", err: false };
    var card = newCard(prompt, skill, rec);
    card.rec = rec;
    log(rec);
    var body = { skill: skill, prompt: prompt };
    if (refs && refs.length) body.refs = refs;
    global.WSGen.submit(body).then(function (d) {
      if (!d || !d.ok) {
        fail(card, (d && d.error) || "没排上队");
        return;
      }
      card.job = d.job.id;
      rec.job = card.job;
      S.pending[card.job] = card;
      startPoll();
    });
  }

  function startPoll() {
    if (S.timer) return;
    S.fails = 0;
    S.timer = setInterval(tick, POLL_MS);
  }

  function stopPoll() {
    if (S.timer) { clearInterval(S.timer); S.timer = null; }
  }

  function markStuck() {
    Object.keys(S.pending).forEach(function (id) {
      var card = S.pending[id];
      fail(card, "状态断了，点重画再来一次");
    });
    S.pending = {};
  }

  function tick() {
    if (!Object.keys(S.pending).length) { stopPoll(); return; }
    global.WSGen.status().then(function (d) {
      if (!d || !d.ok) {
        S.fails += 1;
        if (S.fails >= MAX_FAIL) { stopPoll(); markStuck(); }
        return;
      }
      S.fails = 0;
      var byId = {};
      (d.jobs || []).forEach(function (j) { byId[j.id] = j; });

      Object.keys(S.pending).forEach(function (id) {
        var card = S.pending[id];
        var job = byId[id];
        // 任务不在列表里 = 进程重启过，这张再也回不来，别让它一直转
        if (!job) { delete S.pending[id]; fail(card, "服务重启过，这张没了，点重画"); return; }
        if (job.state === "done") {
          delete S.pending[id];
          showImage(card, job);
        } else if (job.state === "error" || job.state === "cancelled") {
          delete S.pending[id];
          fail(card, job.error || "这张没出来");
        } else {
          var n = d.queue_depth;
          card.note.hidden = false;
          card.note.className = "note";
          card.note.textContent = (typeof n === "number" && n > 0)
            ? "前面还有 " + n + " 人排队"
            : "正在生成…";
          if (card.rec) card.rec.note = card.note.textContent;
        }
      });

      if (!Object.keys(S.pending).length) {
        stopPoll();
        if (global.WSStore) global.WSStore.save({ msgs: S.msgs });
      }
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

  function send() {
    var text = el.say.value.trim();
    if (!text) return;
    addUser(text);
    el.say.value = "";
    grow();
    syncSend();

    var refIds = global.WSRef.list().map(function (r) { return r.id; });
    // 发出去就把它从输入栏移走（消息里那张卡留着，还能点开看）
    global.WSRef.commit();
    // 漫画模式：这句话是剧情需求，交给创作面板去跑「编剧 → 分页画」
    if (global.WSCreate && global.WSCreate.mode() === "comic") {
      global.WSCreate.runComic(text, refIds);
      return;
    }
    submitJob(text, S.skill, refIds);
  }

  function copyText(text) {
    if (global.navigator.clipboard && global.navigator.clipboard.writeText) {
      return global.navigator.clipboard.writeText(text)
        .then(function () { return true; })
        .catch(function () { return fallbackCopy(text); });
    }
    return Promise.resolve(fallbackCopy(text));
  }

  function fallbackCopy(text) {
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    var ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    document.body.removeChild(ta);
    return ok;
  }

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
    if (global.WSArchive) global.WSArchive.restore();
    global.WSRef.init();
    bootChannels();
  }

  global.WSChat = {
    init: init,
    sysNote: sysNote,
    card: shell,
    remember: remember,
    touch: touch,
    log: log,
    dropRef: dropRef,
    /* 参考图拆到 ref.js 了，这里留个转发：旧的调用点还在的话不会炸 */
    syncHint: function () { if (global.WSRef) global.WSRef.syncHint(); },
    paintUser: paintUser,
    paintNote: paintNote,
    buildActs: buildActs,
    box: box,
    hideEmpty: hideEmpty,
    scrollBottom: scrollBottom,
    pending: S.pending,
    msgs: function () { return S.msgs; },
    startPoll: startPoll
  };
})(window);
