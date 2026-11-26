/* 访客模式：一次问清楚「我是谁」，再把界面收成访客该有的样子。

   2026-11-26 用户拍板的口径：
     · 访客能生图，**每天 5 张**（额度在 5174 扣，这里只显示）
     · 没有「漫画生成」——加号里那块直接摘掉，不是藏起来
     · 「我的漫画作品」点进去是空列表 → 把那个入口隐掉，省得白点
     · 「我的」整块隐藏（没有自己的作品，进去是空的）
     · 顶栏/我的页显示「访客 · 今日还剩 N 张」

   为什么不在每个模块里各打一次 `/me`：那会打四五次，而且各模块加载
   顺序不定，谁先谁后不好保证。这里启动时打一次，把结果缓存住并广播
   `ws:role` 事件，谁要谁听。 */
(function (global) {
  "use strict";

  var S = { loaded: false, role: "guest", left: 0, limit: 0, waiting: [] };

  function $(id) { return document.getElementById(id); }

  function isVisitor() { return S.role === "visitor"; }

  /* ── 界面收口 ────────────────────────────────────────
     全部按 id / data-chip 找，不依赖别的模块有没有先跑。 */
  function apply() {
    if (!isVisitor()) return;

    /* 加号里只剩「图片生成」。连整块摘掉而不是 display:none——
       留个空位会在两格网格里撑出一条白边。 */
    var comicBtn = $("pick-comic");
    if (comicBtn && comicBtn.parentNode) {
      comicBtn.parentNode.removeChild(comicBtn);
    }
    var grid = document.querySelector(".pm-grid");
    if (grid) grid.classList.add("pm-solo");

    /* 「我的漫画作品」这个 chip 一并摘掉：点进去是空列表，没有意义。 */
    var chips = document.querySelectorAll('.chip[data-chip="comic"]');
    Array.prototype.forEach.call(chips, function (c) {
      if (c.parentNode) c.parentNode.removeChild(c);
    });

    /* 「我的」：右上角人形按钮 + 已经建出来的那页。 */
    var meBtns = document.querySelectorAll("[data-me]");
    Array.prototype.forEach.call(meBtns, function (b) { b.hidden = true; });
    var meView = document.querySelector('.view[data-view="me"]');
    if (meView && meView.parentNode) meView.parentNode.removeChild(meView);

    document.documentElement.setAttribute("data-role", "visitor");
  }

  /* ── 顶栏那行字 ──────────────────────────────────────
     聊天页根视图没有顶栏（设计如此），所以剩额挂在输入栏上沿一条细提示里
     ——不占版面，看得见。 */
  function badge() {
    var box = $("visitor-bar");
    if (!box) {
      box = document.createElement("div");
      box.id = "visitor-bar";
      box.className = "vbar";
      var composer = $("composer");
      if (composer && composer.parentNode) {
        composer.parentNode.insertBefore(box, composer);
      } else {
        return;
      }
    }
    box.textContent = "访客 · 今日还剩 " + S.left + " 张（每天 " +
      (S.limit || 5) + " 张）";
  }

  function syncLeft(left, limit) {
    if (typeof left === "number") S.left = left;
    if (typeof limit === "number") S.limit = limit;
    if (isVisitor()) badge();
  }

  /* ── 启动 ────────────────────────────────────────────── */
  function load() {
    if (!global.WSApi) { apply(); return Promise.resolve(S); }
    return global.WSApi.get("/me").then(function (d) {
      d = d || {};
      /* 5174 直连没有 /me：那是站主本人，一切照旧。 */
      if (d.ok === false || typeof d.role === "undefined") {
        S.role = "user";
      } else if (d.role) {
        S.role = d.role;
      } else {
        S.role = d.qq ? "user" : "guest";
      }
      if (d.quota) {
        S.left = Number(d.quota.left) || 0;
        S.limit = Number(d.quota.limit) || 0;
      }
      S.loaded = true;
      apply();
      if (isVisitor()) badge();
      try {
        global.dispatchEvent(new CustomEvent("ws:role", { detail: state() }));
      } catch (e) { /* 老浏览器没有 CustomEvent 构造器就算了 */ }
      return S;
    }, function () {
      S.role = "user";
      S.loaded = true;
      return S;
    });
  }

  function state() {
    return { role: S.role, visitor: isVisitor(),
             left: S.left, limit: S.limit, loaded: S.loaded };
  }

  global.WSVisitor = {
    load: load,
    state: state,
    isVisitor: isVisitor,
    syncLeft: syncLeft,
    apply: apply
  };
})(window);
