/* 页面栈 + 画风抽屉。
   根是聊天页；子页从右侧滑入，X / 左边缘右滑 / 浏览器返回都能退回。
   history 只当一条「退回」记录用，不参与页面内容还原。 */
(function (global) {
  "use strict";

  var SHOW_HEAD = 6;          // 画风太多，抽屉先露这几项，其余点「展开全部」

  var el = {};
  var S = { top: null, allStyles: false, depth: 0 };

  function $(id) { return document.getElementById(id); }

  // ── 页面栈 ────────────────────────────────────────────

  /* 真叠层：作品列表上再压一本漫画，X 一层层退回去（用户 2026-11-23 定） */
  function push(name, opts) {
    var view = global.WSViews.build(name, opts);

    el.stack.appendChild(view);
    // 下一帧再加 .in，否则插入与过渡撞在同一帧里，滑入动画不触发
    global.requestAnimationFrame(function () { view.classList.add("in"); });
    S.top = view;
    S.depth += 1;
    bindView(view);
    global.history.pushState({ ws: S.depth }, "");
    return view;
  }

  /* 换内容不换栈：不做退回的一层，当前无调用方（个人页已改成真叠层） */
  function replace(name, opts) {
    if (!S.top) return push(name, opts);
    var old = S.top;
    var next = global.WSViews.build(name, opts);
    next.classList.add("in");
    el.stack.replaceChild(next, old);
    if (old.dataset && old.dataset.dock && !next.dataset.dock) {
      global.WSViews.undock();
    }
    S.top = next;
    bindView(next);
    return next;
  }

  /* 底下还压着别的子页时，摘掉这层要把栈顶还回去——不然节点还在、S.top
     却是 null，下一次 push 会叠出两层。正常路径是一层，这条是兜底。 */
  function underOf(view) {
    var nodes = Array.prototype.slice.call(
      el.stack.querySelectorAll(".view-push"));
    var rest = nodes.filter(function (n) { return n !== view; });
    return rest.length ? rest[rest.length - 1] : null;
  }

  /* 真正的出栈：播完动画再摘节点 */
  function closeNow() {
    var view = S.top;
    if (!view) return;
    S.top = underOf(view);
    S.depth = Math.max(0, S.depth - 1);
    view.classList.remove("in");

    // 子页把输入栏借走了（AI 创作页要接着出图），退回时先还回聊天页
    if (view.dataset && view.dataset.dock) global.WSViews.undock();

    var done = function () {
      if (view.parentNode) view.parentNode.removeChild(view);
    };
    view.addEventListener("transitionend", done, { once: true });
    setTimeout(done, 420);              // 兜底：过渡被打断也要摘掉
  }

  /* 用户要退回：优先让浏览器走 history，popstate 里统一出栈 */
  function back() {
    if (!S.top) return;
    if (global.history.state && global.history.state.ws) {
      global.history.back();
    } else {
      closeNow();
    }
  }

  function bindView(view) {
    view.addEventListener("click", function (e) {
      if (e.target.closest("[data-close]")) { back(); return; }
      // 右上角人形进「我的」也要真叠一层：这样 ✕ 退回的是刚才那一页，
      // 不直接跳回聊天页（跟漫画页同一条规则，用户 2026-11-23 定）
      if (e.target.closest("[data-me]")) { push("me"); return; }
    });
    bindSwipe(view);
  }

  /* 左边缘右滑退回：竖向滑动交给页面自己滚动，不抢 */
  function bindSwipe(view) {
    var sx = 0, sy = 0, dx = 0, active = false;

    view.addEventListener("touchstart", function (e) {
      if (e.touches.length !== 1) return;
      var t = e.touches[0];
      if (t.clientX > 32) return;
      sx = t.clientX; sy = t.clientY; dx = 0; active = true;
    }, { passive: true });

    view.addEventListener("touchmove", function (e) {
      if (!active) return;
      var t = e.touches[0];
      var mx = t.clientX - sx, my = t.clientY - sy;
      if (Math.abs(my) > Math.abs(mx)) { active = false; return; }
      dx = Math.max(0, mx);
      view.classList.add("drag");
      view.style.transform = "translateX(" + dx + "px)";
    }, { passive: true });

    view.addEventListener("touchend", function () {
      if (!active) return;
      active = false;
      view.classList.remove("drag");
      view.style.transform = "";
      if (dx > 90) back();
    });
  }

  // ── 画风抽屉 ──────────────────────────────────────────

  function openStyles() {
    el.sheet.hidden = false;
    global.requestAnimationFrame(function () { el.sheet.classList.add("on"); });
    renderStyles();
  }

  function closeStyles() {
    el.sheet.classList.remove("on");
    setTimeout(function () { el.sheet.hidden = true; }, 280);
  }

  function labelOf(c) {
    return (c.label && c.label !== c.id) ? c.label : c.id;
  }

  /* 渠道的 summary 形如 "silver · 默认生图渠道"，取后半句当一行灰字说明 */
  function briefOf(c) {
    var s = (c.summary || "").trim();
    if (!s) return "";
    var i = s.indexOf("·");
    var rest = (i >= 0 ? s.slice(i + 1) : s).trim();
    if (!rest || rest === c.id) return "";
    return rest.length > 18 ? rest.slice(0, 18) + "…" : rest;
  }

  function renderStyles() {
    global.WSGen.channels().then(function (d) {
      var list = (d && d.ok && d.channels) || [];
      var cur = global.WSGen.defaultSkill() || ((list[0] || {}).id || "");

      el.list.innerHTML = "";
      var show = S.allStyles ? list : list.slice(0, SHOW_HEAD);

      show.forEach(function (c) {
        var b = document.createElement("button");
        b.type = "button";
        b.className = "style-item" + (c.id === cur ? " on" : "");

        var tick = document.createElement("span");
        tick.className = "tick";
        tick.textContent = "✓";

        var name = document.createElement("span");
        name.className = "sname";
        name.textContent = labelOf(c);

        b.appendChild(tick);
        b.appendChild(name);

        var brief = briefOf(c);
        if (brief) {
          var meta = document.createElement("span");
          meta.className = "meta";
          meta.textContent = brief;
          b.appendChild(meta);
        }

        b.addEventListener("click", function () {
          global.WSGen.setDefaultSkill(c.id);
          global.dispatchEvent(new global.CustomEvent("ws:skill",
            { detail: { id: c.id } }));
          renderStyles();
          closeStyles();
        });

        el.list.appendChild(b);
      });

      el.more.hidden = S.allStyles || list.length <= SHOW_HEAD;
      el.more.textContent = "▾ 展开全部 " + list.length + " 个画风";
    });
  }

  // ── 初始化 ────────────────────────────────────────────

  function init() {
    el.stack = $("stack");
    el.sheet = $("style-sheet");
    el.list = $("style-list");
    el.more = $("style-more");

    Array.prototype.forEach.call(document.querySelectorAll(".chip"), function (c) {
      c.addEventListener("click", function () {
        var k = c.dataset.chip;
        if (k === "style") { openStyles(); return; }
        push(k);
      });
    });

    el.sheet.addEventListener("click", function (e) {
      if (e.target.closest("[data-close]")) closeStyles();
    });

    el.more.addEventListener("click", function () {
      S.allStyles = true;
      renderStyles();
    });

    global.history.replaceState({ ws: 0 }, "");
    global.addEventListener("popstate", function () {
      // 栈深按 history 里的记录走：两层时点一次返回只退一层，
      // 不能像以前那样「只要还有 ws 就不动」，否则按了返回没反应
      var want = (global.history.state && global.history.state.ws) || 0;
      while (S.depth > want) closeNow();
    });
  }

  global.WSStack = { init: init, push: push, replace: replace, back: back };
})(window);
