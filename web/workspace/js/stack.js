/* 页面栈 + 画风抽屉。
   根是聊天页；子页从右侧滑入，X / 左边缘右滑 / 浏览器返回都能退回。
   history 只当一条「退回」记录用，不参与页面内容还原。 */
(function (global) {
  "use strict";

  var SHOW_HEAD = 6;          // 画风太多，抽屉先露这几项，其余点「展开全部」
  var EDGE = 24;              // 触屏：手指起点离左边缘多少像素内才算「边缘右滑」

  var el = {};
  var S = { top: null, depth: 0, views: [], restoring: false };
  var VIEW_KEY = "ws_view_v1";

  function saveView(name, opts) {
    try {
      if (!S.views.length || !name) {
        S.views = [];
        global.sessionStorage.removeItem(VIEW_KEY);
      } else {
        global.sessionStorage.setItem(VIEW_KEY, JSON.stringify({
          views: S.views
        }));
      }
    } catch (e) { /* session storage may be unavailable */ }
  }

  function $(id) { return document.getElementById(id); }

  // ── 页面栈 ────────────────────────────────────────────

  /* 真叠层：作品列表上再压一本漫画，X 一层层退回去（用户 2026-11-23 定） */
  function push(name, opts) {
    /* 同一页已经在栈顶就别再压一层（用户 2026-11-23：一直点「AI 创作」
       会叠出无数层直到卡死）。这里统一拦，不靠每个入口各自记得判断——
       以前「我的」是自己拦的，画廊漏了，连点就叠成一摞。
       真需要同页两层（我的 → 我的 已经不这么走了）时传 force。 */
    if (S.top && S.top.dataset.view === name && !(opts && opts.force)) {
      var sc = S.top.querySelector("[data-scroll]");
      if (sc) sc.scrollTop = 0;
      return S.top;
    }
    var view = global.WSViews.build(name, opts);

    el.stack.appendChild(view);
    // 下一帧再加 .in，否则插入与过渡撞在同一帧里，滑入动画不触发
    global.requestAnimationFrame(function () { view.classList.add("in"); });
    S.top = view;
    S.depth += 1;
    S.views.push({ name: name, opts: opts || {} });
    if (!S.restoring) saveView(name, opts);
    bindView(view);
    /* 一条子页 = 一条历史记录，state.ws 必须等于它当时的层数。
       以前这里有登录页残留的历史（登录后前端 pushState 跳过来），层数从 1 起算，
       但栈深从 0 起算，popstate 里 want(1) ≥ depth(1) 就一层都不退——
       用户看到的是「点返回没反应、再点直接跳回聊天页」（2026-11-23 实测）。 */
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
    if (S.views.length) S.views[S.views.length - 1] = { name: name, opts: opts || {} };
    else S.views.push({ name: name, opts: opts || {} });
    saveView(name, opts);
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
    S.views.pop();
    saveView(S.top && S.top.dataset.view, {});
    view.classList.remove("in");

    // 子页把输入栏借走了（AI 创作页要接着出图），退回时先还回聊天页
    if (view.dataset && view.dataset.dock) global.WSViews.undock();

    var done = function () {
      if (view.parentNode) view.parentNode.removeChild(view);
    };
    /* 只认这一页自己的位移过渡。以前不带判据：页里任何一个子节点先结束
       一个过渡（图片淡入、按钮变色、状态行闪现……），`{once:true}` 就把
       这条监听烧掉了，退场动画才播到一半、页面已经被摘——用户看到的是
       「点返回闪一下」。属性名也一起卡住，别让子节点的 transform 顶替。 */
    view.addEventListener("transitionend", function (e) {
      if (e.target === view && e.propertyName === "transform") done();
    });
    setTimeout(done, 420);              // 兜底：过渡被打断也要摘掉
  }

  /* 用户要退回：优先让浏览器走 history，popstate 里统一出栈 */
  function back() {
    if (!S.top) return;
    /* 平时 pushState 与 depth 一一对应，history.state.ws 一定大于 0（打底那条是
       {ws:0}），所以「state 里没有 ws」不代表栈是空的——那是别处留下的历史记录，
       走 history.back() 会退到一个根本没有记录过页面栈的位置，层数就对不上了。
       只有真的没有子页时才需要就地摘掉（兜底）。 */
    if (Number((global.history.state || {}).ws) > 0) {
      global.history.back();
    } else {
      closeNow();
    }
  }

  /* 「刚有人替我把 history 退了一条，你现在只需要把栈收一层」。

     场景：画廊查看器压了 `{gv:1}`，关它时调了 `history.back()`。那个调用
     是异步的，紧接着再走 back() 读 `history.state.ws` 读到的还是**退之前**
     的值，于是又 back 一次 —— 用户点一下「画同款」，AI 创作页被连退两层
     一起关掉。

     这里不走 history（已经退过了），直接出栈；popstate 稍后到达时 want
     会比 depth 小，`while` 里没有可退的，是空转，不会多退。 */
  function backToChat() {
    closeNow();
  }

  function bindView(view) {
    view.addEventListener("click", function (e) {
      if (e.target.closest("[data-close]")) { back(); return; }
      // 右上角人形进「我的」也要真叠一层：这样 ✕ 退回的是刚才那一页，
      // 不直接跳回聊天页（跟漫画页同一条规则，用户 2026-11-23 定）
      /* 已经在「我的」里就别再压一层（push 里已统一拦同类页，这里不用重复判） */
      if (e.target.closest("[data-me]")) {
        push("me");
        return;
      }
    });
    bindSwipe(view);
  }

  /* 左边缘右滑退回：竖向滑动交给页面自己滚动，不抢。

     **这里以前在页面中间的任意位置右滑也会退页**（2026-11-23 用户第三次报：
     「我在页面中间往右滑，它就自动退出了」）。原因是收尾判据 `dx > 90 &&
     dy < 90` 是松手那一刻的累计位移：先往右下斜着划、再往回拉一下，dy 就
     被拉小，一个纯横向的戳就退页了；而横向的标签条、缩略图横滑本身也会
     产生几十像素的横移，凑够 90 就退。两点一起看，屏幕中间随便一划就退出。

     现在的规则：
     1. **起点必须在屏幕左边缘**（EDGE 内）。中间起手一律不接管——微信、
        Safari 的返回手势也是这么定的，页面中间是内容的。
     2. 起手后**第一次有明显位移**就把方向钉死（`h`）：竖向多就彻底不管；
       横向多才进入拖动，且要求横向位移明显大于竖向（1.2 倍）。
     3. 松手判据用**起始阈值 + 距离**：位移过半才走，或者快速一甩（时间
        短、距离够）也算。绝不能一点回拉就触发。
     拖动过程中手指移出左边缘也不算，只看位移。 */
  function bindSwipe(view) {
    var sx = 0, sy = 0, dx = 0, dy = 0, active = false, decided = false;
    var t0 = 0;

    view.addEventListener("touchstart", function (e) {
      // 多指（双指缩放）不算手势
      if (e.touches.length !== 1) { active = false; return; }
      var t = e.touches[0];
      // 起手点必须在左边缘；中间起手直接不管，避免把横向滚动当成退页
      if (t.clientX > EDGE) { active = false; return; }
      sx = t.clientX; sy = t.clientY; dx = 0; dy = 0;
      active = true; decided = false; t0 = Date.now();
    }, { passive: true });

    view.addEventListener("touchmove", function (e) {
      if (!active) return;
      var t = e.touches[0];
      var mx = t.clientX - sx, my = t.clientY - sy;
      if (!decided) {
        if (Math.abs(mx) < 10 && Math.abs(my) < 10) return;
        // 第一次判定：竖向为主（或向左划）就彻底放手，这一趟不再管
        if (Math.abs(my) >= Math.abs(mx) || mx < -8) { active = false; return; }
        decided = true;
      }
      dx = Math.max(0, mx);
      dy = Math.max(0, my);
      if (dx < 6) return;                 // 抖动不算滑动，别让子页跳一下
      view.classList.add("drag");
      view.style.transform = "translateX(" + dx + "px)";
    }, { passive: true });

    view.addEventListener("touchcancel", function () {
      active = false; decided = false;
      dx = 0; dy = 0;
      view.classList.remove("drag");
      view.style.transform = "";
    }, { passive: true });

    view.addEventListener("touchend", function () {
      if (!active) return;
      active = false;
      var quick = Date.now() - t0 < 380;
      // 位移过半（多半）或者「快速一甩」才退；dy 必须明显小，斜划不算
      var far = dx >= view.clientWidth * 0.5;
      var flick = quick && dx > 70;
      var go = decided && (far || flick) && dy < dx * 0.8;
      dx = 0; dy = 0; decided = false;
      view.classList.remove("drag");
      view.style.transform = "";
      if (go) back();
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
    // 拖动中可能留着 inline transform，不清掉的话它比 CSS 的 translateY(100%)
    // 还优先，抽屉就停在半路不下去了。
    el.sheet.querySelector(".sheet-body").style.transform = "";
    setTimeout(function () { el.sheet.hidden = true; }, 280);
  }

  /* 抽屉下拉关闭（2026-11-27）：小横条原来只是道装饰，用户照着它往下一拨，
     实际滚的是列表，滚到顶还会把整页拽下去（列表那条滚动链已在 CSS 里断掉）。
     现在把手（横条 + 标题行）归这套手势管：竖向往下拖时整个抽屉跟手走，
     松手过阈值或快甩就关，否则弹回。列表区不接管，照旧自己滚。
     判据沿用 bindSwipe 的思路：多指不管、方向一次钉死、横向/往上不算。 */
  function bindSheetDrag() {
    var body = el.sheet.querySelector(".sheet-body");
    var handles = el.sheet.querySelectorAll(".sheet-grip, .sheet-head");
    var d = null;

    function end() {
      if (!d) return;
      var cur = d;
      d = null;
      if (!cur.on) return;
      body.classList.remove("drag");
      body.style.transform = "";
      var quick = Date.now() - cur.t0 < 400;
      if (cur.dy > body.clientHeight * 0.28 || (quick && cur.dy > 60)) {
        closeStyles();
      }
    }

    Array.prototype.forEach.call(handles, function (h) {
      h.addEventListener("touchstart", function (e) {
        if (e.touches.length !== 1) { d = null; return; }
        var t = e.touches[0];
        d = { x: t.clientX, y: t.clientY, dy: 0, on: false, t0: Date.now() };
      }, { passive: true });

      h.addEventListener("touchmove", function (e) {
        if (!d || e.touches.length !== 1) return;
        var t = e.touches[0];
        var dx = t.clientX - d.x, dy = t.clientY - d.y;
        if (!d.on) {
          if (Math.abs(dy) < 8 || Math.abs(dx) > Math.abs(dy)) return;
          if (dy < 0) { d = null; return; }   // 往上拉不接管，留给列表滚
          d.on = true;
          body.classList.add("drag");
        }
        if (e.cancelable) e.preventDefault();  // 别让页面跟着一起动
        d.dy = dy;
        body.style.transform = "translateY(" + dy + "px)";
      }, { passive: false });

      h.addEventListener("touchend", end);
      h.addEventListener("touchcancel", end);
    });
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

  /* 一张画风卡：抽屉和全屏画风页共用同一份构造（封面/选中/简介都只写一处）。
     44/49 个渠道有封面，没图的藏 img 露占位，跟跑图台 coverHtml 一个做法。 */
  function styleCard(c, cur, onPick) {
    var b = document.createElement("button");
    b.type = "button";
    b.className = "style-card" + (c.id === cur ? " on" : "");

    var th = document.createElement("div");
    th.className = "s-thumb";
    var img = document.createElement("img");
    img.loading = "lazy";
    img.decoding = "async";
    img.alt = labelOf(c);
    img.src = "/api/runbench/cover/" + encodeURIComponent(c.id);
    img.onerror = function () { img.style.display = "none"; };
    var ph = document.createElement("div");
    ph.className = "s-ph";
    ph.textContent = labelOf(c);
    th.appendChild(ph);
    th.appendChild(img);
    b.appendChild(th);

    var tick = document.createElement("span");
    tick.className = "s-tick";
    tick.textContent = "✓";
    b.appendChild(tick);

    var meta = document.createElement("div");
    meta.className = "s-meta";
    var nm = document.createElement("div");
    nm.className = "s-name";
    nm.textContent = labelOf(c);
    meta.appendChild(nm);
    var brief = briefOf(c);
    if (brief) {
      var sub = document.createElement("div");
      sub.className = "s-sub";
      sub.textContent = brief;
      meta.appendChild(sub);
    }
    b.appendChild(meta);

    b.addEventListener("click", onPick);
    return b;
  }

  /* 设为默认这件事两处一样，区别只在选完怎么收尾：抽屉里原地重绘并关掉，
     全屏页退回聊天页。派发 ws:skill 让顶部 chip 跟着变。 */
  function pickStyle(cid) {
    global.WSGen.setDefaultSkill(cid);
    global.dispatchEvent(new global.CustomEvent("ws:skill",
      { detail: { id: cid } }));
  }

  function renderStyles() {
    global.WSGen.channels().then(function (d) {
      var list = (d && d.ok && d.channels) || [];
      var cur = global.WSGen.defaultSkill() || ((list[0] || {}).id || "");

      el.list.innerHTML = "";
      /* 抽屉只留 6 张快速切换。49 张全塞进 66vh 必然压成扁条——全部放到
         独立的全屏子页（用户 2026-11-27 拍板）。 */
      list.slice(0, SHOW_HEAD).forEach(function (c) {
        el.list.appendChild(styleCard(c, cur, function () {
          pickStyle(c.id);
          renderStyles();
          closeStyles();
        }));
      });

      el.more.hidden = list.length <= SHOW_HEAD;
      el.more.textContent = "▾ 查看全部 " + list.length + " 个画风 →";
    });
  }

  /* 全屏画风页（2026-11-27）：点「查看全部」关抽屉滑到这里，49 张卡铺满整屏
     （不再被 66vh 挤扁），点一张设为默认并自动退回聊天页。复用现成子页栈，
     ✕ / 左滑 / 浏览器返回都能退，不新造一套页面机制。 */
  function mountStyles(body) {
    global.WSGen.channels().then(function (d) {
      var list = (d && d.ok && d.channels) || [];
      var cur = global.WSGen.defaultSkill() || ((list[0] || {}).id || "");
      var grid = document.createElement("div");
      grid.className = "style-grid";
      list.forEach(function (c) {
        grid.appendChild(styleCard(c, cur, function () {
          pickStyle(c.id);
          back();
        }));
      });
      body.appendChild(grid);
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
    bindSheetDrag();

    /* 查看全部：关抽屉、滑入全屏画风页，不再在 66vh 抽屉里塞 49 张扁卡。
       closeStyles 有 280ms 的 hidden 延时，子页滑入正好盖住它；退回时抽屉
       已经 hidden，不会冒出来。 */
    el.more.addEventListener("click", function () {
      closeStyles();
      push("styles");
    });

    /* 聊天页自己也要占一条历史记录（2026-11-26 用户：「AI 创作按返回整个大退」）。
       以前只 replaceState 打底、聊天页和它前面那条记录共用一格，于是「聊天页」
       在历史里根本不存在：在聊天页按一次返回，浏览器直接退到进站之前那一页，
       整个网页就没了。手机上从微信/扫码点进来，前面那条多半还是空的初始页，
       看起来就是「一下子退出去了」。

       现在聊天页单独占一条（`{ ws: 0, chat: 1 }`），前面留一条替身（`{ ws: 0 }`）。
       在聊天页又按返回时退到替身上，位置还是 1（history.length 不变），一眼认
       得出来，就把聊天页那条再推回去——人留在原地，多余的这次退被浏览器自己
       吃掉。第一次退完之后替身没了（再退位置就是 0，人就出站），所以这种「原地
       不动」只兜住一两次，反复按还是能走，不会把返回键彻底焊死。 */
    global.history.replaceState({ ws: 0 }, "");
    global.history.pushState({ ws: 0, chat: 1 }, "");
    /* 刷新后恢复子页栈和必要参数，不恢复查看器、抽屉等瞬时状态。 */
    try {
      var saved = JSON.parse(global.sessionStorage.getItem(VIEW_KEY) || "null");
      var views = saved && Array.isArray(saved.views) ? saved.views : [];
      S.restoring = true;
      views.forEach(function (v) {
        if (v && v.name && global.WSViews.titles.hasOwnProperty(v.name)) {
          push(v.name, v.opts || {});
        }
      });
      S.restoring = false;
      if (!S.views.length) saveView(null);
    } catch (e) {
      S.restoring = false;
      saveView(null);
    }
    /* 打底这条是「聊天页」。从登录页进来时历史里可能还压着更早的记录，
       state 里没有 ws——那种也算层数 0，否则层数永远比实际多。

       2026-11-26 补（用户报「在 AI 创作里按手机返回，整个网页直接大退」）：
       退到层数 0 之后再按一次，脚下这条就是**进站之前那一页**，退过去人就出站了。
       所以先看这一退是不是退到了替身上：是就立刻把聊天页那条推回去，位置不动、
       页面不卸载，看起来就是「按了但没走」。 */
    global.addEventListener("popstate", function () {
      var want = Number((global.history.state || {}).ws) || 0;
      // 栈深按 history 里的记录走：两层时点一次返回只退一层。
      if (want === 0) {
        var st = global.history.state || {};
        // 位置还是 1 = 替身还在下面，这一退只是踩到它，人是安全的
        if (!st.chat && global.history.length > 1) {
          global.history.pushState({ ws: 0, chat: 1 }, "");
        }
      }
      /* 只收到 `want` 就停。2026-11-26 之前这里有个「want 不比层数小就不动」的
         旧保护，防的是「连按两下、第二下读到退之前的状态、于是多退一层」——
         现在没有靠 state 推层数的入口了，那个保护只会让到了聊天页之后**漏一次
         返回**（怎么按都不动，像按键坏了）。退页动画没播完时 handler 是同步接完
         的，多余的 popstate 进来时层数已经对上了，这里自然是空转。 */
      while (S.depth > want && S.top) closeNow();
    });
  }

  /* 全屏画风页挂在现成子页壳上，跟 gallery/comic 一个注册法。 */
  global.WSViews.register("styles", { mount: mountStyles });

  /* top()：当前真正在眼前的子页（没有就是 null）。聊天页靠它决定消息该
     落在哪——退页动画还没播完时，那一页虽然还在 DOM 里、也还带着 .in，
     但已经不在栈上了，不能往里面写东西。 */
  global.WSStack = {
    init: init,
    push: push,
    replace: replace,
    back: back,
    /* 别人已经替我把浏览器历史退了一条时用（看 gallery.js 的 sameAs） */
    backToChat: backToChat,
    top: function () { return S.top; }
  };
})(window);
