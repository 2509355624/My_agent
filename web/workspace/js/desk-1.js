/* 桌面右栏：历史作品（M2 · 2026-11-28）
   ─────────────────────────────────────────────────────────
   用户原话：「工作台的桌面形态改成主列 + 右侧栏……（右栏）当成历史作品
   去上下滑动，点击那图片能够放大去查看，也可以在它自己的界面内切换」。

   数据不新开接口：右栏就是聊天流里那些结果图的**镜像**，顺序反过来
   （最新在最上面）。这样两件事都省了：
     · 不用跟 archive 的存档格式打交道；
     · 出图、重画、删卡本来就改 DOM，一个 MutationObserver 全跟得上。

   缩略图走 `?w=240`（服务端白名单里最小的一档，见 routes.py 的
   `_IMG_THUMB_W`）。工作台铺的图是 1MB 那一档，几十张下来就是几个 MB；
   右栏格子只有 150px 宽，240 的 JPEG 一张二十几 KB 就够看。点开大图仍
   走查看器那套 1MB 档，两边不互相拖累。

   点一张 → `WSViewer.open(这一栏的文件列表, 第几张)`：查看器里还能左右
   切，切的是同一套顺序，不会出现「右栏点第 3 张，大图里是第 7 张」。
*/
(function (global) {
  "use strict";

  var MIN = 1180;              // 必须和 desk-1.css 的断点同一个数
  var side = null;
  var grid = null;
  var countEl = null;
  var emptyEl = null;
  var sig = "";                // 当前这一组文件的指纹，没变就不重建
  var raf = 0;

  function collectFiles() {
    var out = [];
    var list = document.querySelectorAll(".shot");
    for (var i = 0; i < list.length; i++) {
      if (list[i].dataset.file) out.push(list[i].dataset.file);
    }
    return out;
  }

  function thumbUrl(file) {
    var u = "/api/gen/img?f=" + encodeURIComponent(file) + "&w=240";
    var k = global.WSGen && global.WSGen.sign ? global.WSGen.sign() : "";
    if (k) u += "&k=" + encodeURIComponent(k);
    return u;
  }

  function build() {
    var app = document.querySelector(".app-chat");
    if (!app) return false;

    side = document.createElement("aside");
    side.id = "desk-side";
    side.className = "desk-side";

    var head = document.createElement("div");
    head.className = "desk-head";
    var label = document.createElement("b");
    label.textContent = "历史作品";
    countEl = document.createElement("span");
    countEl.className = "desk-count";
    head.appendChild(label);
    head.appendChild(countEl);

    grid = document.createElement("div");
    grid.className = "desk-grid";

    emptyEl = document.createElement("p");
    emptyEl.className = "desk-empty";
    emptyEl.textContent = "在这边说一句就开始画，出好的图会出现在这里";

    side.appendChild(head);
    side.appendChild(grid);
    side.appendChild(emptyEl);
    app.appendChild(side);

    /* 点一张开大图：传的是「这一栏的顺序」，跟点到的位置严格对应。
       查看器自己认字符串数组（file 列表），不用再从 DOM 里找节点。 */
    grid.addEventListener("click", function (e) {
      var t = e.target;
      var tile = t && t.closest ? t.closest(".desk-tile") : null;
      if (!tile || !global.WSViewer) return;
      var files = collectFiles().reverse();
      var i = files.indexOf(tile.dataset.file);
      if (i < 0) i = 0;
      global.WSViewer.open(files, i);
    });

    return true;
  }

  function el(tag, cls) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    return node;
  }

  function render(files) {
    grid.innerHTML = "";
    countEl.textContent = files.length ? String(files.length) : "";
    emptyEl.hidden = !!files.length;

    /* 两列各自纵向排，交替分配。为什么不用 CSS grid 或 columns：
       · grid 里图片撑不开行高（`aspect-ratio` 对 grid item 不成立，行高
         会压到 30px、格子互相叠起来）；
       · columns 在固定高度容器里会横向溢出（43 张排到 6800px 宽）。
       两列 flex 没有这些问题，也是画廊那种瀑布观感。 */
    var cols = [el("div", "desk-col"), el("div", "desk-col")];

    for (var i = 0; i < files.length; i++) {
      var tile = el("button", "desk-tile");
      tile.type = "button";
      tile.dataset.file = files[i];
      tile.setAttribute("aria-label", "查看第 " + (i + 1) + " 张");
      var im = el("img");
      im.loading = "lazy";
      im.decoding = "async";
      im.alt = "";
      im.src = thumbUrl(files[i]);
      tile.appendChild(im);
      cols[i % 2].appendChild(tile);
    }

    grid.appendChild(cols[0]);
    grid.appendChild(cols[1]);
  }

  function refresh() {
    if (!side) return;
    var files = collectFiles().reverse();      // 最新在上
    var s = files.join("\n");
    if (s === sig) return;                     // 没变就一个节点都不动
    sig = s;
    render(files);
  }

  function schedule() {
    if (raf) return;
    raf = global.requestAnimationFrame(function () {
      raf = 0;
      refresh();
    });
  }

  function init() {
    if (!build()) return;

    var scroll = document.getElementById("chat-scroll");
    if (scroll && global.MutationObserver) {
      /* 只看子节点增减（卡片、图片进出就是这一类），不去盯属性：
         图换 src 由卡片自己负责，右栏只关心「有哪些图」。 */
      new global.MutationObserver(schedule).observe(scroll, {
        childList: true,
        subtree: true
      });
    }

    refresh();
  }

  global.WSDesk = { refresh: refresh, min: MIN };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})(window);
