/* 画廊查看器的左右箭头 + 键盘换页（桌面）。

   为什么单独一个文件：`gallery.js` 有 600 行硬线（测试
   `tests/test_workspace_gallery.py` 卡着），而查看器的「怎么翻页」不属于
   gallery.js 的状态机——那边只管 list / idx / work，翻页只是把轨道滚到
   第 i 屏，读 DOM 就能算，不用问它。

   和 gallery.js 的关系：只读不写。
   - 不碰 `v.idx`：`gallery.js` 自己的 scroll 监听照样在跑，两边都只看
     `scrollLeft / clientWidth`，算出来的是同一个整数。
   - 换一本时 gallery.js 会 `track.innerHTML = ""` 重灌，这里靠
     MutationObserver 跟着把箭头状态刷一遍。 */

(function (global) {
  "use strict";

  var st = { track: null, prev: null, next: null, mo: null };

  function idxOf() {
    if (!st.track) return 0;
    var w = Math.max(1, st.track.clientWidth);
    return Math.round(st.track.scrollLeft / w);
  }

  function total() { return st.track ? st.track.children.length : 0; }

  function go(i) {
    var n = total();
    if (!n || !st.track || !st.track.clientWidth) return;
    // 越界不滚：`scrollTo` 到 -1 屏会被浏览器自己夹住，但夹的过程有一跳
    i = Math.max(0, Math.min(n - 1, i));
    st.track.scrollTo({ left: st.track.clientWidth * i, behavior: "smooth" });
  }

  /* 第一张不显示「上一张」、最后一张不显示「下一张」——留着也没用，
     点在边界上什么都不发生，不如让它消失（跟画廊页一样的做法）。 */
  function sync() {
    var n = total(), i = idxOf();
    if (st.prev) st.prev.style.visibility = (n > 1 && i > 0) ? "visible" : "hidden";
    if (st.next) st.next.style.visibility = (n > 1 && i < n - 1) ? "visible" : "hidden";
  }

  function makeBtn(dir, label, aria) {
    var b = document.createElement("button");
    b.type = "button";
    b.className = "gv-nav " + dir;
    b.textContent = label;
    b.setAttribute("aria-label", aria);
    b.style.visibility = "hidden";
    b.addEventListener("click", function () {
      go(idxOf() + (dir === "l" ? -1 : 1));
    });
    return b;
  }

  function attach(box) {
    if (st.track) return;
    var stage = box.querySelector(".gv-stage");
    var track = box.querySelector(".gv-track");
    if (!stage || !track) return;

    st.track = track;
    st.prev = makeBtn("l", "‹", "上一张");
    st.next = makeBtn("r", "›", "下一张");
    stage.appendChild(st.prev);
    stage.appendChild(st.next);

    track.addEventListener("scroll", sync, { passive: true });
    /* 换一本 = track 清空 + 逐张 append，中间没有 scroll 事件，箭头会停在
       上一本的状态（比如上一本停在第 3 张，新的一本第 0 张却还显示「上一张」） */
    st.mo = new MutationObserver(sync);
    st.mo.observe(track, { childList: true });
    sync();
  }

  /* 查看器是懒建的：第一次点开作品集时 gallery.js 才 buildViewer()。
     可能已经建好（子页挂载时就 build 过一次），也可能还没——两条都接。 */
  function watch() {
    var box = document.querySelector(".gviewer");
    if (box) { attach(box); return; }
    var mo = new MutationObserver(function () {
      var b = document.querySelector(".gviewer");
      if (!b) return;
      mo.disconnect();
      attach(b);
    });
    mo.observe(document.body, { childList: true });
  }

  /* 键盘 ← → 也顺手接上：桌面上换页最自然的动作就是按方向键。
     只在查看器真的开着的时候管，否则会把聊天页的输入框光标抢走。 */
  document.addEventListener("keydown", function (e) {
    var box = document.querySelector(".gviewer");
    if (!box || box.hidden) return;
    if (e.key === "ArrowLeft") { e.preventDefault(); go(idxOf() - 1); }
    else if (e.key === "ArrowRight") { e.preventDefault(); go(idxOf() + 1); }
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", watch);
  } else {
    watch();
  }

  global.WSViewerNav = { sync: sync };
})(window);
