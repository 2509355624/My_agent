/* 桌面 + 手机共用的图片查看器（M1 · 2026-11-28）
   ─────────────────────────────────────────────────────────
   用户原话：「电脑端点开某一个图片的话，左右有很大的空缺，应该有换页
   按钮……手机端点击放大之后也是这样的效果，其实类似于我的漫画那边」。

   所以两端是同一个层、同一套 DOM，形态只由 `is-mobile` 一个类切换。

   为什么是「横向滚动轨道」而不是「一个 <img> 换 src」：
   第一版是单图换 src + 自己写的 touchstart/touchend 手势，实测手机上
   往右滑会**直接把页面退掉**——因为横滑没有被页面里的某个容器消费，
   浏览器/WebView 就把它当成系统的返回手势。换成 `overflow-x:auto`
   的轨道（跟「我的漫画」的 `ctrack` 一个思路）之后，横滑就是容器自己
   滚动，`overscroll-behavior-x: contain` + `touch-action: pan-x` 再把
   滚动链掐断，返回手势不会被触发，手感也是跟手的惯性滑动。

   图不一次全建：43 张各 1MB，全插进 DOM 会同时解码几十张。只给当前
   前后各两张插 <img>，滚到哪补哪（`ensure` / `prime`）。

   结构四层（样式见 viewer-1.css）：
     wsv-bed    当前这张放大 / 模糊 / 压暗，铺满整屏当衬底，填掉左右空白
     wsv-track  横向滚动轨道，每张一屏，scroll-snap 对齐
     wsv-arrow  左右箭头（桌面；手机隐藏改滑动手势，窄窗口+鼠标时还回来）
     wsv-strip  底部胶片条，当前那张描边高亮、自动滚到中间

   交互：← → 切图、Esc 关闭、点空白关闭、点缩略图跳转；手机横滑翻页。
   图片地址一律走 `WSGen.img(file, WSShot.FIT)` —— 跟聊天流同一份 1MB
   压缩档，浏览器 HTTP 缓存直接命中，不额外再拉原图。
*/
(function (global) {
  "use strict";

  /* 和 viewer-1.css 的 `.is-mobile` 断点必须同一个数 */
  var MOBILE = "(max-width: 899px)";

  var root = null;
  var track = null;
  var slides = [];
  var files = [];
  var at = 0;
  var opened = false;
  var n = {};
  var scrollRaf = 0;
  var bedTimer = 0;

  function isDesktop() {
    return !global.matchMedia(MOBILE).matches;
  }

  function clamp(i) {
    return Math.max(0, Math.min(i, files.length - 1));
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

    track = el("div", "wsv-track");
    n.track = track;
    track.addEventListener("scroll", onScroll, { passive: true });

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

    stage.appendChild(track);
    stage.appendChild(n.prev);
    stage.appendChild(n.next);

    n.strip = el("div", "wsv-strip");

    root.appendChild(n.bed);
    root.appendChild(n.scrim);
    root.appendChild(top);
    root.appendChild(stage);
    root.appendChild(n.strip);

    /* 点空白处关掉：除了主图本身和任何按钮，别的地方都算空白。
       不写死「等于某个容器」——容器多一层少一层就会失效（这里之前就
       因为 stage 的引用没存下来，点空白完全没反应）。 */
    root.addEventListener("click", function (e) {
      if (e.target.closest && e.target.closest("button")) return;
      if (e.target.tagName === "IMG") return;   // 点图本身不关，方便看细节
      if (e.target === track) return;           // 轨道空白也一样
      close();
    });

    document.body.appendChild(root);
  }

  /* 轨道里只建空的一屏一屏，图按需插进去。 */
  function buildSlides() {
    track.innerHTML = "";
    slides = [];
    for (var i = 0; i < files.length; i++) {
      var s = el("div", "wsv-slide");
      s.dataset.idx = String(i);
      track.appendChild(s);
      slides.push(s);
    }
  }

  function ensure(i) {
    if (i < 0 || i >= slides.length) return;
    var s = slides[i];
    if (s.dataset.on === "1") return;
    s.dataset.on = "1";
    s.classList.add("is-loading");

    var img = document.createElement("img");
    img.className = "wsv-img";
    img.alt = "第 " + (i + 1) + " 张：" + nameOf(files[i]);
    img.decoding = "async";
    img.draggable = false;
    img.addEventListener("load", function () {
      s.classList.remove("is-loading");
    });
    /* 图取不到（签名过期 / 文件挪走）时也要把半透明去掉，否则它一直像
       「还在加载」，比直接露出一张破图更让人迷惑。 */
    img.addEventListener("error", function () {
      s.classList.remove("is-loading");
    });
    s.appendChild(img);
    img.src = urlOf(files[i]);
  }

  /* 当前前后各两张：微信里先出的就是眼前这张，再往外滑立刻有货。 */
  function prime(i) {
    for (var k = i - 2; k <= i + 2; k++) ensure(k);
  }

  function bedFor(file) {
    if (!file || !n.bed) return;
    /* 衬底用同一张图：模糊到只剩颜色块，左右空白立刻有内容。
       地址带引号转义——file 里可能有空格或中文，直接拼进 url() 会坏。 */
    var url = urlOf(file).replace(/"/g, "%22");
    n.bed.style.backgroundImage = 'url("' + url + '")';
  }

  /* 滚动时别每帧都换衬底：模糊是一整层合成，跟着滚动换会抖。停手再换。 */
  function bedSoon() {
    if (bedTimer) global.clearTimeout(bedTimer);
    bedTimer = global.setTimeout(function () {
      bedTimer = 0;
      bedFor(files[at]);
    }, 130);
  }

  function syncStrip() {
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

  function syncUI() {
    var file = files[at];
    var label = nameOf(file);
    n.title.textContent = label;
    n.title.title = label;
    n.count.textContent = (at + 1) + " / " + files.length;
    n.prev.disabled = at <= 0;
    n.next.disabled = at >= files.length - 1;
    syncStrip();
    bedSoon();
  }

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
        jump(parseInt(this.dataset.idx, 10) || 0);
      };
      n.strip.appendChild(b);
    }
  }

  function onScroll() {
    if (!track || scrollRaf) return;
    scrollRaf = global.requestAnimationFrame(function () {
      scrollRaf = 0;
      var w = track.clientWidth || 1;
      var i = clamp(Math.round(track.scrollLeft / w));
      if (i === at) return;
      at = i;
      syncUI();
      prime(i);
    });
  }

  /* 跳到某一张：靠容器滚动，而不是重建节点，图不会闪。 */
  function jump(i) {
    var target = clamp(i);
    if (!track) return;
    var left = target * track.clientWidth;
    if (track.scrollTo) {
      var smooth = Math.abs(target - at) === 1;
      track.scrollTo({ left: left, behavior: smooth ? "smooth" : "auto" });
    } else {
      track.scrollLeft = left;
    }
    /* smooth 滚动期间 scroll 事件要过半才更新计数，这里先对齐一次，
       让标题 / 计数立刻跟上手指。 */
    at = target;
    syncUI();
    prime(target);
  }

  function go(step) { jump(at + step); }

  function open(list, start) {
    if (!list || !list.length) return;
    if (!root) build();
    opened = true;

    /* 两个来源都能喂进来：聊天流给的是 `.shot` 节点（读 dataset.file），
       桌面右栏给的是文件路径字符串（它那份列表本来就是从这些节点抽出来的，
       再找节点反而会错位）。 */
    files = list.map(function (node) {
      return typeof node === "string" ? node : node.dataset.file;
    });
    at = clamp(start);

    /* 形态由 JS 判定、挂在类上，CSS 只管长相。
       以前手机端是在 CSS 里 `display:none` 直接藏掉整层——一旦 JS 和
       媒体查询的判断差一点（平板、微信内核、横屏），就会出现「打开了
       但什么都看不见」，用户看到的就是「点了没反应」。 */
    root.classList.toggle("is-mobile", !isDesktop());

    /* 先把层露出来，再建轨道：藏在 `hidden` 里的元素 clientWidth 是 0，
       摆位置和算「当前这张居中」都会错（第一版实测就是没滚动）。 */
    root.hidden = false;
    buildSlides();
    buildStrip();
    prime(at);

    void track.offsetWidth;                 // 让刚插进去的一屏屏先拿到宽度
    track.scrollLeft = at * track.clientWidth;

    syncUI();
    bedFor(files[at]);

    document.documentElement.classList.add("wsv-lock");
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

  /* 捕获阶段拦掉 `.shot` 的点击：`shot.js` 在那张图上绑的是冒泡监听，
     这里 stopPropagation 之后就不会再走 `toggleFull`（那套桌面端不要，
     手机端也统一换成查看器，两边看到的是同一套东西）。 */
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

  /* 手机地址栏收起/展开、横竖屏一切换，每屏宽度就变了，轨道位置要重量；
     否则会出现「明明在第 5 张，屏幕上是第 4 和第 5 张的缝」。 */
  global.addEventListener("resize", function () {
    if (!opened || !track) return;
    track.scrollLeft = at * track.clientWidth;
  });

  global.WSViewer = { open: open, close: close, go: go, jump: jump };
})(window);
