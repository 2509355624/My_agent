/* 访客模式：一次问清楚「我是谁」，再把界面收成访客该有的样子。

   2026-11-26 用户拍板的口径：
     · 访客能生图，**每天 5 张**（额度在 5174 扣，这里只显示）
     · 没有「漫画生成」——加号里那块直接摘掉，不是藏起来
     · 「我的漫画作品」点进去是空列表 → 把那个入口隐掉，省得白点
     · 「我的」整块隐藏；万一还是点到了右上角那个人形（2026-11-26 用户：
       「AI 画廊里点右上角头像给我一个空白界面」），这一页不再空白——给一张
       「还没登录」+ 蓝色「登录」按钮，点了就退回站首那页登录
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

    guardViews();
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
    dropMeView();

    document.documentElement.setAttribute("data-role", "visitor");
  }

  /* 「我的」那页是点进来才创建的（stack.js 现建现推），所以光摘一次按钮
     不够——`apply()` 跑完之后再从任何地方点「我的」，那页又会被建出来，
     里面 `/api/gal/mine` 没有 qq → 400「参数不对」，看着像页面坏了。

     2026-11-26 用户第四次报这个入口：**空白界面会让用户以为一直在加载**。
     摘掉按钮治不了本（画廊顶栏那个人形是每个子页都有的），所以不再返回
     空壳，改成一块「未登录 + 登录」：说明白现在是访客，给一个能点的蓝按钮
     回登录页。两边共用同一份，见 `guestPanel()`。 */
  function dropMeView() {
    var meView = document.querySelector('.view[data-view="me"]');
    if (meView && meView.parentNode) meView.parentNode.removeChild(meView);
  }

  function guestPanel() {
    var box = document.createElement("div");
    box.className = "vguest";

    var badge = document.createElement("div");
    badge.className = "vguest-badge";
    badge.textContent = "访";

    var title = document.createElement("p");
    title.className = "vguest-title";
    title.textContent = "还没登录";

    var hint = document.createElement("p");
    hint.className = "vguest-hint";
    hint.textContent = "现在是以访客身份看，作品和收藏都没挂在账号上。";

    var out = document.createElement("button");
    out.type = "button";
    out.className = "vguest-btn";
    out.textContent = "登录";
    out.addEventListener("click", function () {
      out.disabled = true;
      /* 先摘掉访客 cookie 再 reload：不摘的话 5175 看到访客身份会把人直接
         放回工作台，等于点了没反应。退出和进登录页是同一套办法（me.js）。 */
      global.WSApi.post("/logout", {}).then(function () {
        if (global.WSStore) global.WSStore.clear();
        global.location.reload();
      }, function () {
        global.location.reload();
      });
    });

    box.appendChild(badge);
    box.appendChild(title);
    box.appendChild(hint);
    box.appendChild(out);
    return box;
  }

  function guardViews() {
    var views = global.WSViews;
    if (!views || guardViews.done) return;
    guardViews.done = true;
    var real = views.build;
    views.build = function (name, opts) {
      var view = real.apply(views, arguments);
      if (isVisitor() && name === "me") {
        /* 顶栏只有 ✕ / 标题 / 人形，人形在这里点进去会再叠一层同样的页，
           没有意义——访客这时已经能看到「怎么登录」了。 */
        var me = view.querySelector("[data-me]");
        if (me && me.parentNode) me.parentNode.removeChild(me);
        var body = view.querySelector(".vbody");
        if (body) {
          body.innerHTML = "";
          body.appendChild(guestPanel());
        }
      }
      return view;
    };
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
      if (isVisitor()) {
        badge();
        /* 收口之后还有人从「我的」那页点进来（外面那层已经建出来的时候
           `apply` 摘不掉它新建的），所以再扫一次，把 `me` 整页摘掉。 */
        dropMeView();
      }
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
