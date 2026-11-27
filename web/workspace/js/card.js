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

  /* 用户那条消息下面的失败提示：一条为限，点重画就撤掉。
     失败不再往 AI 卡片里堆红色警告 + 重试按钮（反复点会刷屏），
     改成贴着用户蓝色气泡给一行轻提示，用户一看就知道是哪句没画出来。 */
  function failLine(host, msg, onRetry) {
    if (!host || !host.parentNode) return null;
    var line = host._failLine;
    if (!line || line.parentNode !== host.parentNode) {
      line = document.createElement("div");
      line.className = "user-fail";
      var t = document.createElement("span");
      t.className = "user-fail-t";
      var b = document.createElement("button");
      b.type = "button";
      b.className = "user-fail-btn";
      b.textContent = "重画";
      b.addEventListener("click", function () {
        // 连点只认第一下：重画会撤掉这条消息，按钮来不及拿回来之前再点
        // 就会多送一条一样的请求（用户 2026-11-25 实测连点出三条蓝气泡）。
        if (b.disabled) return;
        b.disabled = true;
        clearFailLine(host);
        if (onRetry) onRetry();
      });
      line.appendChild(t);
      line.appendChild(b);
      host.parentNode.insertBefore(line, host.nextSibling);
      host._failLine = line;
    }
    var text = line.querySelector(".user-fail-t");
    if (text) text.textContent = msg || "这张没画出来，点重画再来一次";
    return line;
  }

  function clearFailLine(host) {
    if (!host) return;
    if (host._failLine && host._failLine.parentNode) host._failLine.remove();
    host._failLine = null;
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

    /* 胶囊下面的「可以先离开」小字（2026-11-27 用户拍板）：放在卡片里，
       不占输入栏那点高度。只有排队 / 在跑的胶囊态才露出来，出图 / 失败
       随 setNote 一起关掉。`note._queueHint` 让 setNote 不必去 DOM 里找兄弟。 */
    var hint = document.createElement("div");
    hint.className = "queue-hint";
    hint.textContent = "排上队就可以离开，画好会自动弹出来 · 人少约 1–2 分钟，人多会久一点";
    hint.hidden = true;
    note._queueHint = hint;

    var acts = document.createElement("div");
    acts.className = "acts";
    acts.hidden = true;

    node.appendChild(body);
    node.appendChild(line);
    node.appendChild(hint);
    node.appendChild(acts);
    mount(node);

    function fill() { node.classList.remove("is-idle"); }

    return {
      node: node,
      body: body,
      line: line,
      note: note,
      stop: stop,
      hint: hint,
      acts: acts,
      fill: fill,
      say: function (text, bad, run, queue) {
        setNote(note, text, { bad: bad, run: run, queue: queue });
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

  /* 引用（2026-11-27）：一张图 + 箭头指回输入栏——「拿这张，接着说」。 */
  var ICON_REF =
    '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
    'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">' +
    '<rect x="3.4" y="5" width="11" height="11" rx="2.2"/>' +
    '<path d="M17 8.2L20.4 11.8 17 15.4"/><path d="M20.4 11.8H15"/></svg>';

  /* 排队 / 准备中 / 画图中转的那一小圈（2026-11-25 用户：「正在跑……
     跑好之后会自动弹出来」）。用 CSS 转，禁用动效的系统设置里会被停掉。 */
  var ICON_SPIN =
    '<svg class="spin" width="13" height="13" viewBox="0 0 24 24" fill="none" ' +
    'stroke="currentColor" stroke-width="2.2" stroke-linecap="round">' +
    '<path d="M12 3.2a8.8 8.8 0 1 1-6.2 2.6"/></svg>';

  /* 卡片底部三个按钮：复制提示词 | 引用这张图改 | 换种子重画。
     它们原来紧贴在一起（都是小圆圈、间距 8px），用户反馈「很容易误触」——
     改成两边是纯文字按钮、中间是图标按钮，中间留空推开，一个管复制
     一个管引用一个管重画，手指不会点串。

     2026-11-27 用户拍板：把「下载」换成「引用」。下载本来用处不大（手机上
     长按图片就能存），而引用能接着改图——点一下弹出键盘、挂上这张图，
     用户直接说新需求。

     **复制拿的是实际出这张图用的那串提示词**（服务端翻完 / 拼完参考图之后
     的英文 tags），不是用户敲的那句话——`card.prompt` 由 chat.js 在提交
     成功时换成 `job.prompt`。存档里记的也是它，所以恢复出来的旧卡片一样
     复制得到 tags。

     下载（2026-11-24 定稿）：拉「≤1MB」那一档，先问浏览器本地缓存。

     `hooks.again` 由 chat.js 提供（重画要走「同一份请求只留一张卡」那套
     收口）；`hooks.quote` 同理。两个都不传就不挂监听——存档重放需要
     按钮长得一样但点了不能炸。 */
  /* 状态行（「排队中 / 正在跑 / 已终止」那一行）。
     `run=true` 时前面带一个转圈的图标，告诉用户「它确实在跑，跑好会自动
     弹出来」——2026-11-25 用户：「准备中这个提示改一下，说正在跑就行」。
     `run` 传假（失败 / 已终止 / 出图）就只剩一行字，别让人以为还在动。 */
  function setNote(note, text, opts) {
    var o = opts || {};
    note.hidden = false;
    /* 胶囊态才显示「可以离开」那行小字；出图 / 失败的收尾都不带 queue。 */
    var qh = note._queueHint;
    if (qh) qh.hidden = !o.queue;
    /* 排队胶囊（2026-11-27）。`card-line` 里正好只装了两样东西——状态行和
       「终止」按钮——所以给它加一个 class 就够它们变成同一张胶囊，DOM 不用
       挪。别的时候必须把这个 class 撤掉，否则出图之后卡片上还扣着一个空壳。 */
    var line = note.parentNode;
    if (line && line.classList) {
      if (o.queue) line.classList.add("is-queue");
      else line.classList.remove("is-queue");
    }
    if (o.queue) {
      /* 结构：[圆点][徽章][正文]，「终止」由 card-line 接在后面。
         徽章文字由调用方给（2026-11-27 用户：「外观统一」）：排队说「正在排队」、
         真开画说「正在画了」、已排上等轮到说「正在跑」。外观始终是同一张粉紫
         胶囊，状态往前推进时只有字在变，不会从胶囊掉回一行灰字。 */
      var tag = (typeof o.queue === "string") ? o.queue : "正在排队";
      note.className = "note queue";
      note.innerHTML = '<span class="q-dot"></span>' +
                       '<span class="q-tag"></span>' +
                       '<span class="note-text"></span>';
      var qt = note.querySelector ? note.querySelector(".q-tag") : null;
      var qn = note.querySelector ? note.querySelector(".note-text") : null;
      if (qt) qt.textContent = tag;
      if (qn) qn.textContent = text;
      else note.textContent = tag + " " + text;   // DOM 桩兜底
      note._qtag = tag;
      return note;
    }
    note.className = "note" + (o.bad ? " err" : "") + (o.run ? " running" : "");
    note.innerHTML = (o.run ? ICON_SPIN : "") +
      '<span class="note-text"></span>';
    var span = note.querySelector ? note.querySelector(".note-text") : null;
    if (span) span.textContent = text;
    else note.textContent = text;      // DOM 桩 / 老浏览器兜底
    return note;
  }

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
        if (retry.disabled) return;
        retry.disabled = true;
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

    /* 引用这张图（2026-11-27 取代下载）：挂成这一轮的参考图、弹键盘，
       用户接着说「换成正面视角」就行。具体流程在 ref.js 的 quoteCard。 */
    var quote = document.createElement("button");
    quote.type = "button";
    quote.className = "act act-icon";
    quote.title = "引用这张图改";
    quote.setAttribute("aria-label", "引用这张图改");
    quote.innerHTML = ICON_REF + "<span>引用</span>";
    quote.addEventListener("click", function () {
      if (hooks.quote) hooks.quote(card, quote);
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
      if (again.disabled) return;      // 一次重画只收一次
      again.disabled = true;
      again.blur();                    // 闪完别在按钮上留一圈焦点框
      if (hooks.again) hooks.again(card);
    });

    card.acts.appendChild(copy);
    card.acts.appendChild(quote);
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
    setNote: setNote,
    paintUser: paintUser,
    failLine: failLine,
    clearFailLine: clearFailLine,
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
