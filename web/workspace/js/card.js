/* 聊天页里「一条消息长什么样」：用户气泡、一行提示、AI 卡片的外壳与按钮。
   从 chat.js 拆出来的（2026-11-24，那边已经快一千行）。

   拆的边界刻意放在「纯 DOM 长相」这一层：
     · 这里**不碰**存档（不调 WSStore）、**不发请求**（不调 WSGen）；
     · 页面往哪写、写了之后要不要落盘，由 chat.js 决定，这里只收一个
       `append` 钩子（= 往消息区塞节点并滚到底）。
   所以 archive.js 重放时也能直接借用同一套外观，不用再抄一份。

   2026-11-24 用户第二次报「大卡片」：排队中 / 网络出错这些**没有图的**
   状态不再占一张 340×3:4 的大壳，壳是 `is-idle`（只剩一行字），有图才
   由 `fill()` 变回真卡片。细节见下面 shell() 的注释。 */
(function (global) {
  "use strict";

  var COLLAPSE_AT = 1000;   // 用户单条超过这个字数就折叠
  var HEAD_KEEP = 300;      // 折叠后先露这么多

  /* 由 chat.js 注入：`function (node) { ... }`——把它塞进消息区并滚到底。
     这里不自己找 `#chat-scroll`：子页借走输入栏那段时间，写错地方的消息
     会跟着正在消失的节点一起被回收（见 chat.js 的 box() 注释）。 */
  var mount = function (node) { return node; };
  var settle = function () { };

  /* `scroll` = 往消息区塞节点之后要不要滚到底（chat.js 传 scrollBottom） */
  function attach(fn, scroll) {
    if (typeof fn === "function") mount = fn;
    if (typeof scroll === "function") settle = scroll;
  }

  // ── 一行提示 ──────────────────────────────────────────

  function paintNote(text) {
    var p = document.createElement("p");
    p.className = "card note";
    p.style.alignSelf = "center";
    p.style.border = "0";
    p.textContent = text;
    mount(p);
    settle();
    return p;
  }

  // ── 用户气泡 ──────────────────────────────────────────

  /* refs 非空时把这一轮的参考图缩略图排在上面——豆包也是这样：发出去的
     那条消息里能看见自己当时喂的是哪张图，回头翻记录还知道。
     气泡是消息的一部分，所以图地址走同一个 item id，刷新后照样能显示。 */
  function paintUser(text, refs) {
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

    mount(b);
    settle();
    return b;
  }

  // ── AI 卡片 ───────────────────────────────────────────

  /* 「有没有图」才是这张卡存不存在的前提。图还没出来时，壳是 `is-idle`：
     没有底纹、没有边框、不占 3:4，只剩一行字（chat.css 里 `.card.is-idle`）。
     图一出来 `fill()` 把它变回真卡片，占位块再撑起 3:4。

     状态行 +「终止」放在同一行（`.card-line`）：状态本来就只有一行字，
     终止是个小胶囊，贴在它右边，不再单独占一整行。 */
  function shell(cls) {
    var node = document.createElement("div");
    node.className = "card is-idle" + (cls ? " " + cls : "");

    var body = document.createElement("div");
    body.className = "card-body";

    var line = document.createElement("div");
    line.className = "card-line";

    var note = document.createElement("p");
    note.className = "note";

    /* 「终止」只在排队 / 出图中出现，出图或失败后自动收起来。 */
    var stop = document.createElement("button");
    stop.type = "button";
    stop.className = "stop-run";
    stop.hidden = true;
    stop.textContent = "终止";

    line.appendChild(note);
    line.appendChild(stop);

    var acts = document.createElement("div");
    acts.className = "acts";
    acts.hidden = true;

    node.appendChild(body);
    node.appendChild(line);
    node.appendChild(acts);
    mount(node);

    function fill() { node.classList.remove("is-idle"); }

    return {
      node: node,
      body: body,
      line: line,
      note: note,
      stop: stop,
      acts: acts,
      fill: fill,
      say: function (text, bad) {
        note.hidden = false;
        note.className = "note" + (bad ? " err" : "");
        note.textContent = text;
        settle();
      },
      show: function () {
        if (!acts.hidden) return;
        acts.hidden = false;
      }
    };
  }

  /* 一张卡真的开始有画面了：挂上 3:4 的底纹占位，壳变回真卡片。
     排队 / 等稿那种「还没东西」的阶段不再预先挂一个空占位块。 */
  function spawn(card) {
    if (card.ph) return card.ph;
    var ph = document.createElement("div");
    ph.className = "ph";
    card.body.appendChild(ph);
    card.ph = ph;
    if (card.fill) card.fill();
    return ph;
  }

  /* 把状态行闪一下（2026-11-24 用户：「用户还是不断地点击重画就直接闪烁一下
     那行字，高亮一下即可，然后逐渐消失」）。
     不动文字内容、不新增节点——真正的提示由调用方自己 `say` 写；
     这个只管「让用户看见我又点了一次」。 */
  function flash(card) {
    var n = card && card.note;
    if (!n) return;
    n.hidden = false;
    n.classList.remove("blink");
    // 强制一次重排，连续点才有动画（否则同一个类名加回去不触发）
    void n.offsetWidth;
    n.classList.add("blink");
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

     **复制拿的是实际出这张图用的那串提示词**（服务端翻完 / 拼完参考图之后
     的英文 tags），不是用户敲的那句话——`card.prompt` 由 chat.js 在提交
     成功时换成 `job.prompt`。存档里记的也是它，所以恢复出来的旧卡片一样
     复制得到 tags。

     下载（2026-11-24 定稿）：拉「≤1MB」那一档，先问浏览器本地缓存。

     `hooks.again` 由 chat.js 提供（重画要走「同一份请求只留一张卡」那套
     收口）；`hooks.download` 同理。两个都不传就不挂监听——存档重放需要
     按钮长得一样但点了不能炸。 */
  function buildActs(card, retryLabel, hooks) {
    hooks = hooks || {};
    if (retryLabel) {
      card.acts.innerHTML = "";
      card.acts.hidden = false;
      var retry = document.createElement("button");
      retry.type = "button";
      retry.className = "act act-text";
      retry.title = "重新排一次";
      retry.setAttribute("aria-label", "重新排一次");
      retry.innerHTML = ICON_AGAIN + "<span>" + retryLabel + "</span>";
      retry.addEventListener("click", function () {
        retry.blur();
        if (hooks.again) hooks.again(card);
      });
      card.acts.appendChild(retry);
      return;
    }
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
      if (hooks.download) hooks.download(card, save);
    });

    /* 「重画」= 换种子再来一张；卡住的那张换成「重试」，同一个意思但
       用户点下去是「它卡住了、帮我再排一次」，不是「我不满意这张」。 */
    var again = document.createElement("button");
    again.type = "button";
    again.className = "act act-text";
    again.title = retryLabel ? "重新排一次" : "换种子重画";
    again.setAttribute("aria-label", again.title);
    if (retryLabel) again.innerHTML = ICON_AGAIN + "<span>" + retryLabel + "</span>";
    else again.innerHTML = ICON_AGAIN + "<span>重画</span>";
    again.addEventListener("click", function () {
      again.blur();                    // 闪完别在按钮上留一圈焦点框
      if (hooks.again) hooks.again(card);
    });

    card.acts.appendChild(copy);
    card.acts.appendChild(save);
    card.acts.appendChild(again);
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

  global.WSCard = {
    attach: attach,
    paintNote: paintNote,
    paintUser: paintUser,
    shell: shell,
    spawn: spawn,
    flash: flash,
    buildActs: buildActs,
    copyText: copyText,
    /* 折叠阈值给测试和别处引用（改这里就是改所有气泡的折叠口径） */
    COLLAPSE_AT: COLLAPSE_AT,
    HEAD_KEEP: HEAD_KEEP
  };
})(window);
