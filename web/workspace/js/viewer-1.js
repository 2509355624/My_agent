/* 桌面端的图片查看器（M1 · 2026-11-28）
   ─────────────────────────────────────────────────────────
   用户原话：「电脑端点开某一个图片的话，它其实左右有很大的一个空缺，
   然后应该有一个换页的按钮……手机端它是直接完整的显示这个图片的」。

   所以这里只管**桌面**（≥900px）。手机上依旧走 `shot.js` 的
   `toggleFull`（原地放大），一行都不改——两套行为各有各的场景，不要
   硬合成一套，手机端那套是能用的。

   为什么要一个查看器而不是把图放大：
     · 竖图在 2560 屏上放大后左右全是空的，且没法翻；
     · 「这一套有几张、我看到的第几张」在聊天流里根本看不出来。

   结构四层（见 viewer-1.css）：
     `wsv-bed`    当前这张图放大 / 模糊 / 压暗，铺满整屏当衬底，
                  左右空白被同一张图的氛围填掉，不再是一片死白；
     `wsv-img`    主图，等比 contain，≤ 78vh；
     `wsv-arrow`  左右箭头，只有一张时自动隐藏；
     `wsv-strip`  底部胶片条，当前那张描边高亮。

   交互：← → 切图、Esc 关闭、点背景关闭、点缩略图跳转。
   图片地址一律走 `WSGen.img(file, WSShot.FIT)` —— 跟聊天流同一份
   1MB 压缩档，浏览器 HTTP 缓存直接命中，不额外再拉原图。
*/
(function (global) {
  "use strict";

  /* 和 viewer-1.css 的 `@media (max-width: 899px)` 必须同一个数：
     改一边忘另一边，就会出现「DOM 打开了但 CSS 把它藏了」的幽灵层。 */
  var MOBILE = "(max-width: 899px)";

  var root = null;
  var shots = [];        // 当前这一套的元素（按 DOM 顺序）
  var files = [];        // 跟 shots 一一对应的 file
  var at = 0;
  var opened = false;
  var n = {};            // 缓存的节点引用

  function isDesktop() {
    return !global.matchMedia(MOBILE).matches;
  }

  function urlOf(file) {
    return global.WSGen.img(file, global.WSShot.FIT);
  }

  function nameOf(file) {
    var base = String(file || "").split(/[\\/]/).pop();
    /* 出图文件名常带 `Anima_00004_.png` 这种尾缀，去掉扩展名之后再抹掉
       末尾的 `_` / `-`，标题才像回事。 */
    return base.replace(/\.[^.]+$/, "").replace(/[_\-\s]+$/, "") || base || "生成结果";
  }

  function el(tag, cls) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    return node;
  }

  /* 聊天流里现在能点开的图，按屏幕上的顺序排。
     不按「作品」分组：工作台一条消息一张图，用户心里的上一张 / 下一张
     就是眼前这段滚动流里的顺序。 */
  function collect() {
    var out = [];
    var list = document.querySelectorAll(".shot");
    for (var i = 0; i < list.length; i++) {
      if (list[i].dataset.file) out.push(list[i]);
    }
    return out;
  }

  function build() {
    root = el("div");
    root.id = "ws-viewer";
    root.setAttribute("role", "dialog");
    root.setAttribute("aria-modal", "true");
    root.tabIndex = -1;            // 打开时接住焦点，← → 不会跑去输入框里移光标
    root.hidden = true;

    n.bed = el("div", "wsv-bed");
    n.scrim = el("div", "wsv-scrim");

    var top = el("div", "wsv-top");
    n.title = el("div", "wsv-title");
    n.count = el("div", "wsv-count");
    n.close = el("button", "wsv-x");
    n.close.type = "button";
    n.close.textContent = "\u00d7";             // ×
    n.close.setAttribute("aria-label", "关闭");
    n.close.onclick = close;
    top.appendChild(n.title);
    top.appendChild(n.count);
    top.appendChild(n.close);

    var stage = el("div", "wsv-stage");
    n.stage = stage;
    n.prev = el("button", "wsv-arrow wsv-prev");
    n.next = el("button", "wsv-arrow wsv-next");
    n.prev.type = "button";
    n.next.type = "button";
    n.prev.textContent = "\u2039";               // ‹
    n.next.textContent = "\u203a";               // ›
    n.prev.setAttribute("aria-label", "上一张");
    n.next.setAttribute("aria-label", "下一张");
    n.prev.onclick = function (e) { e.stopPropagation(); go(-1); };
    n.next.onclick = function (e) { e.stopPropagation(); go(1); };

    n.img = el("img", "wsv-img");
    n.img.alt = "生成结果";
    n.img.draggable = false;
    n.img.addEventListener("load", function () {
      n.img.classList.remove("is-loading");
    });
    /* 图取不到（签名过期 / 文件挪走）时也要把半透明去掉，否则它一直像
       「还在加载」，比直接露出一张破图更让人迷惑。 */
    n.img.addEventListener("error", function () {
      n.img.classList.remove("is-loading");
    });

    stage.appendChild(n.prev);
    stage.appendChild(n.img);
    stage.appendChild(n.next);

    n.strip = el("div", "wsv-strip");

    root.appendChild(n.bed);
    root.appendChild(n.scrim);
    root.appendChild(top);
    root.appendChild(stage);
    root.appendChild(n.strip);
    /* 手机端左右滑翻页——参照「我的漫画」那边的手感（漫画 ctrack 横滑）。
       这里不做 scroll-snap 的轨道：桌面端是单张 <img> 原地换 src，换成轨道
       要重写整层；手势够用，也不用为两种形态各建一套 DOM。
       横向位移要明显大于纵向才认，否则纵向滑动会被当翻页。 */
    var swipe = { x: 0, y: 0, on: false };

    root.addEventListener("touchstart", function (e) {
      swipe.on = false;
      if (!root.classList.contains("is-mobile")) return;
      /* 胶片条自己会横滑，那上面左右拖是浏览缩略图，不是切页 */
      if (e.target.closest && e.target.closest(".wsv-strip")) return;
      var t = e.touches[0];
      if (!t) return;
      swipe.x = t.clientX;
      swipe.y = t.clientY;
      swipe.on = true;
    }, { passive: true });

    root.addEventListener("touchend", function (e) {
      if (!swipe.on) return;
      swipe.on = false;
      var t = e.changedTouches[0];
      if (!t) return;
      var dx = t.clientX - swipe.x;
      var dy = t.clientY - swipe.y;
      if (Math.abs(dx) > 48 && Math.abs(dx) > Math.abs(dy) * 1.4) {
        go(dx < 0 ? 1 : -1);
      }
    }, { passive: true });

    /* 点空白处关掉：除了主图本身和任何按钮，别的地方都算空白。
       不写死「等于某个容器」——容器多一层少一层就会失效（这里之前就
       因为 stage 的引用没存下来，点空白完全没反应）。 */
    root.addEventListener("click", function (e) {
      if (e.target === n.img) return;
      if (e.target.closest && e.target.closest("button")) return;
      close();
    });
    document.body.appendChild(root);
  }

  /* 胶片条只在打开时建一次：同一套的缩略图，地址跟聊天流完全一致，
     浏览器缓存里已经有了，不会因为开个查看器再下一遍。 */
  function buildStrip() {
    n.strip.innerHTML = "";
    for (var i = 0; i < files.length; i++) {
      var b = el("button", "wsv-thumb");
      b.type = "button";
      b.dataset.idx = String(i);
      b.setAttribute("aria-label", "第 " + (i + 1) + " 张");
      var im = el("img");
      im.src = urlOf(files[i]);
      im.loading = "lazy";
      im.alt = "";
      b.appendChild(im);
      b.onclick = function (e) {
        e.stopPropagation();
        at = parseInt(this.dataset.idx, 10) || 0;
        show();
      };
      n.strip.appendChild(b);
    }
  }

  function show() {
    var file = files[at];
    if (!file) return;
    var url = urlOf(file);

    /* 衬底用同一张图：模糊到只剩颜色块，左右空白立刻有内容。
       地址带引号转义——file 里可能有空格或中文，直接拼进 url() 会坏。 */
    n.bed.style.backgroundImage = 'url("' + url.replace(/"/g, "%22") + '")';
    /* 同一张地址聊天流里已经加载过，正常是缓存秒开；只有刚出图、缩略图
       还没落在缓存里时才会慢半拍，这时候先把主图压淡，别让一块空白顶着。 */
    n.img.classList.add("is-loading");
    n.img.src = url;
    n.title.textContent = nameOf(file);
    n.title.title = nameOf(file);
    n.count.textContent = (at + 1) + " / " + files.length;

    var solo = files.length < 2;
    n.prev.disabled = solo;
    n.next.disabled = solo;

    var thumbs = n.strip.children;
    for (var i = 0; i < thumbs.length; i++) {
      thumbs[i].classList.toggle("on", i === at);
    }
    /* 把当前这张挪到胶片条中间。不用 `scrollIntoView`：它在刚重建的
       条子上（同一 tick、还没布局）经常算出 0，滚不到。直接按 offset
       算目标位置最稳，超出范围浏览器自己会 clamp。 */
    var active = thumbs[at];
    if (active) {
      var want = active.offsetLeft - (n.strip.clientWidth - active.offsetWidth) / 2;
      n.strip.scrollLeft = want > 0 ? want : 0;
    }
  }

  /* 预取左右各一张，按下方向键时不用等下载。 */
  function prefetch() {
    [at - 1, at + 1].forEach(function (i) {
      if (files[i]) {
        var probe = new global.Image();
        probe.src = urlOf(files[i]);
      }
    });
  }

  function open(list, start) {
    if (!list || !list.length) return;
    if (!root) build();
    opened = true;

    shots = list.slice();
    files = shots.map(function (node) { return node.dataset.file; });
    at = Math.max(0, Math.min(start, files.length - 1));

    /* 形态由 JS 判定、挂在类上，CSS 只管长相。
       以前手机端是在 CSS 里 `display:none` 直接藏掉整层——一旦 JS 和
       媒体查询的判断差一点（平板、微信内核、横屏），就会出现「打开了
       但什么都看不见」，用户看到的就是「点了没反应」。 */
    root.classList.toggle("is-mobile", !isDesktop());

    /* 先把层露出来，再建胶片条：藏在 `hidden` 里的元素 clientWidth 是 0，
       算「当前这张居中」时会得到错位置（第一次打开时实测没滚动）。 */
    root.hidden = false;
    buildStrip();
    show();
    prefetch();

    document.documentElement.classList.add("wsv-lock");
    /* 读一次布局，把 `display:none → block` 和「加 .on」挤进不同帧，
       否则透明度过渡在部分浏览器上会直接跳过、变成硬切。 */
    void root.offsetHeight;
    root.focus({ preventScroll: true });
    global.requestAnimationFrame(function () {
      if (opened) root.classList.add("on");
    });
  }

  function close() {
    if (!root || !opened) return;
    opened = false;
    root.classList.remove("on");
    document.documentElement.classList.remove("wsv-lock");
    /* 等淡出播完再藏；180ms 内重新打开就不会被这段定时器藏回去。 */
    global.setTimeout(function () {
      if (!opened) root.hidden = true;
    }, 180);
  }

  function go(step) {
    if (!files.length) return;
    at = (at + step + files.length) % files.length;
    show();
    prefetch();
  }

  /* 捕获阶段拦掉 `.shot` 的点击：`shot.js` 在那张图上绑的是冒泡监听，
     这里 stopPropagation 之后就不会再走 `toggleFull`（那套桌面端不要，
     手机端也统一换成查看器，两边看到的是一样的东西）。 */
  document.addEventListener("click", function (e) {
    var t = e.target;
    if (!t || t.tagName !== "IMG" || !t.classList || !t.classList.contains("shot")) {
      return;
    }
    var list = collect();
    var idx = list.indexOf(t);
    if (idx < 0) return;
    e.preventDefault();
    e.stopPropagation();
    open(list, idx);
  }, true);

  document.addEventListener("keydown", function (e) {
    if (!opened) return;
    if (e.key === "Escape") { close(); return; }
    if (e.key === "ArrowLeft") { e.preventDefault(); go(-1); return; }
    if (e.key === "ArrowRight") { e.preventDefault(); go(1); }
  });

  global.WSViewer = { open: open, close: close, go: go };
})(window);
