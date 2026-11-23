/* 聊天页：用户气泡 + AI 结果卡。生图走 /api/gen/*，轮询有上限，停了能重试。 */
(function (global) {
  "use strict";

  var COLLAPSE_AT = 1000;   // 用户单条超过这个字数就折叠
  var HEAD_KEEP = 300;      // 折叠后先露这么多
  var POLL_MS = 2000;
  var MAX_FAIL = 5;         // 连续失败这么多次就停轮询，别无限打接口

  var el = {};
  var S = { skill: "", channels: [], pending: {}, timer: null, fails: 0 };

  function $(id) { return document.getElementById(id); }

  function scrollBottom() {
    el.scroll.scrollTop = el.scroll.scrollHeight;
  }

  function hideEmpty() {
    if (el.empty) el.empty.hidden = true;
  }

  // ── 消息区 ────────────────────────────────────────────

  function sysNote(text) {
    hideEmpty();
    var p = document.createElement("p");
    p.className = "card note";
    p.style.alignSelf = "center";
    p.style.border = "0";
    p.textContent = text;
    el.scroll.appendChild(p);
    scrollBottom();
  }

  function addUser(text) {
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

    el.scroll.appendChild(b);
    scrollBottom();
  }

  function newCard(prompt, skill) {
    hideEmpty();
    var card = document.createElement("div");
    card.className = "card";

    var ph = document.createElement("div");
    ph.className = "ph";

    var note = document.createElement("p");
    note.className = "note";
    note.textContent = "排队中…";

    var acts = document.createElement("div");
    acts.className = "acts";
    acts.hidden = true;

    card.appendChild(ph);
    card.appendChild(note);
    card.appendChild(acts);
    el.scroll.appendChild(card);
    scrollBottom();

    return { node: card, ph: ph, note: note, acts: acts,
             prompt: prompt, skill: skill, job: "" };
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
    again.addEventListener("click", function () { submitJob(card.prompt, card.skill); });

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
      card.node.replaceChild(img, card.ph);
      card.ph = null;
    }
    card.note.hidden = true;
    buildActs(card);
    scrollBottom();
  }

  function fail(card, msg) {
    card.note.hidden = false;
    card.note.className = "note err";
    card.note.textContent = msg || "这张没出来，点重画再来一次";
    if (card.ph) card.ph.style.opacity = ".3";
    buildActs(card);
    scrollBottom();
  }

  // ── 生图 ──────────────────────────────────────────────

  function submitJob(prompt, skill) {
    if (!skill) {
      sysNote("还没拿到渠道列表，去跑图台看一眼再回来");
      return;
    }
    var card = newCard(prompt, skill);
    global.WSGen.submit({ skill: skill, prompt: prompt }).then(function (d) {
      if (!d || !d.ok) {
        fail(card, (d && d.error) || "没排上队");
        return;
      }
      card.job = d.job.id;
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
      card.note.hidden = false;
      card.note.className = "note err";
      card.note.textContent = "状态断了，点重画再来一次";
      buildActs(card);
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
        if (!job) { delete S.pending[id]; return; }
        if (job.state === "done") {
          delete S.pending[id];
          showImage(card, job);
        } else if (job.state === "error") {
          delete S.pending[id];
          fail(card, job.error || "这张没出来");
        } else {
          var n = d.queue_depth;
          card.note.textContent = (typeof n === "number" && n > 0)
            ? "前面还有 " + n + " 人排队"
            : "正在生成…";
        }
      });

      if (!Object.keys(S.pending).length) stopPoll();
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
    submitJob(text, S.skill);
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

  // ── 画风 / 参考图 ─────────────────────────────────────

  function pickSkill(id) {
    if (!id || id === S.skill) return;
    S.skill = id;
    global.WSGen.setDefaultSkill(id);
    var c = S.channels.filter(function (x) { return x.id === id; })[0];
    if (c) sysNote("画风换成「" + (c.label || c.id) + "」");
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

    global.addEventListener("resize", fitComposer);

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
        input.value = "";
        sysNote("参考图反推要等 P1 接上，现在先直接描述你要的内容");
      });
    });

    global.addEventListener("ws:skill", function (e) {
      pickSkill(e.detail && e.detail.id);
    });

    grow();
    syncSend();
    bootChannels();
  }

  global.WSChat = { init: init, sysNote: sysNote };
})(window);
