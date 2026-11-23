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

  /* 用户气泡。refs 非空时把这一轮的参考图缩略图排在上面——豆包也是这样：
     发出去的那条消息里能看见自己当时喂的是哪张图，回头翻记录还知道。
     气泡是消息的一部分，所以图地址走同一个 item id，刷新后照样能显示。 */
  function paintUser(text, refs) {
    hideEmpty();
    var b = document.createElement("div");
    b.className = "bubble";
    var list = (refs || []).filter(function (r) { return r && r.id; });
    if (list.length) {
      var strip = document.createElement("div");
      strip.className = "bubble-refs";
      list.forEach(function (r) {
        var im = document.createElement("img");
        im.alt = "参考图";
        im.loading = "lazy";
        im.decoding = "async";
        im.src = r.url || ("/api/gal/img/" + r.id + "?s=t");
        im.addEventListener("click", function () {
          if (global.WSGallery) global.WSGallery.open({ id: r.wid }, r.id);
        });
        strip.appendChild(im);
      });
      b.appendChild(strip);
    }
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

  var ICON_DOWN =
    '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
    'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">' +
    '<path d="M12 4.5v10.4"/><path d="M7.8 11.4L12 15.6l4.2-4.2"/>' +
    '<path d="M4.8 18.4v1.1h14.4v-1.1"/></svg>';

  /* 卡片底部三个按钮：复制提示词 | 下载到本机 | 换种子重画。
     它们原来紧贴在一起（都是小圆圈、间距 8px），用户反馈「很容易误触」——
     改成两边是纯文字按钮、中间是图标按钮，中间留空推开，一个管复制
     一个管下载一个管重画，手指不会点串。
     下载（2026-11-23 用户要的）：走 `<a download>` 拉原始尺寸那张，
     不经过 canvas，手机浏览器会直接落到「下载」目录。 */
  function buildActs(card) {
    if (card.acts.childElementCount) return;
    card.acts.hidden = false;

    var copy = document.createElement("button");
    copy.type = "button";
    copy.className = "act act-text";
    copy.title = "复制提示词";
    copy.setAttribute("aria-label", "复制提示词");
    copy.innerHTML = ICON_COPY + "<span>复制</span>";
    copy.addEventListener("click", function () {
      copyText(card.prompt).then(function (ok) {
        copy.innerHTML = ok ? '<span class="ok">✓ 已复制</span>' : "复制失败";
        setTimeout(function () {
          copy.innerHTML = ICON_COPY + "<span>复制</span>";
        }, 1200);
      });
    });

    var save = document.createElement("button");
    save.type = "button";
    save.className = "act act-icon";
    save.title = "下载到本机";
    save.setAttribute("aria-label", "下载到本机");
    save.innerHTML = ICON_DOWN + "<span>下载</span>";
    save.addEventListener("click", function () {
      downloadImage(card);
    });

    var again = document.createElement("button");
    again.type = "button";
    again.className = "act act-text";
    again.title = "换种子重画";
    again.setAttribute("aria-label", "换种子重画");
    again.innerHTML = ICON_AGAIN + "<span>重画</span>";
    again.addEventListener("click", function () {
      submitJob(card.prompt, card.skill, card.refs || []);
    });

    card.acts.appendChild(copy);
    card.acts.appendChild(save);
    card.acts.appendChild(again);
  }

  /* 下载这张图。地址还是 /api/gen/img 那条（同源、带登录 cookie），
     `download` 属性让浏览器存成文件而不是打开它；文件名用 img 字段，
     ComfyUI 的原始名带上时间戳，重画几次也不会互相覆盖。
    图还没出来（card.file 为空）时提示一句，不静默失败。 */
  function downloadImage(card) {
    var file = card.file || (card.rec && card.rec.file) || "";
    if (!file) {
      sysNote("这张还没出来，等它画完再点下载");
      return;
    }
    var a = document.createElement("a");
    a.href = global.WSGen.img(file);
    a.download = String(file).split("/").pop() || "image.png";
    a.rel = "noopener";
    document.body.appendChild(a);
    a.click();
    a.remove();
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
    card.file = job.file || "";
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
                refs: (refs || []).slice(),
                job: "", file: "", note: "排队中…", err: false };
    var card = newCard(prompt, skill, rec);
    card.rec = rec;
    // 卡片自己记住参考图：点「重画」要按同一份角色重来，不能悄悄丢图
    card.refs = rec.refs;
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
          // 带上参考图时把它读图的结果说清楚：读到了就说「照着参考图画」，
          // 没读到就说「按描述画」——用户不该猜这张图到底用没用上。
          var refs = job.ref_note || "";
          card.note.textContent = (typeof n === "number" && n > 0)
            ? "前面还有 " + n + " 人排队" + (refs ? "（" + refs + "）" : "")
            : "正在生成…" + (refs ? "　" + refs : "");
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

    // 漫画模式：这句话是剧情需求，交给创作面板去跑「编剧 → 分页画」
    // 漫画那条链自己会读参考，不需要在这里等识图
    if (global.WSCreate && global.WSCreate.mode() === "comic") {
      global.WSRef.commit();
      global.WSCreate.runComic(text, refs.map(function (r) { return r.id; }));
      return;
    }

    global.WSRef.ensureDescribed(refs).then(function (read) {
      global.WSRef.commit();
      submitJob(text, S.skill, read.map(function (r) { return r.id; }));
    });
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
    busyNote: busyNote,      // 过场提示，不进存档、会用完自己消失
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
