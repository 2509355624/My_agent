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

  /* 身份回来之前就有人来问「我是访客吗」（`me.js` 的列表在 mount 当刻就会
     问一次）。那时 `role` 还是默认的 `guest`，照直答「不是」就等于放它去
     发 `/api/gal/mine`——用户看到的「闪一下作品/收藏」就是那一发。所以先给
     一个「可能是」：`pending()` 只答「这一页需不需要先按访客处理」，真正
     的身份仍以 `/me` 回来后的 `role` 为准。 */
  function pending() { return !S.loaded; }

  function $(id) { return document.getElementById(id); }

  function isVisitor() { return S.role === "visitor"; }

  /* ── 界面收口 ────────────────────────────────────────
     全部按 id / data-chip 找，不依赖别的模块有没有先跑。

     ⚠️ 身份没回来之前（`loaded` 为假）**一个字都不改**。
     2026-11-26 用户报「访客登录后点头像没反应」，根因就在这里：reload 之后
     `role` 默认是 `guest`，而 `chat.html` 里 `WSStack.init()` 比
     `WSVisitor.load()` 先跑，栈已经把「我的」那页恢复出来了。这时 apply()
     按「非访客」处理、那页也不换内容——用户先
     看到「参数不对 / 重试」，再点人形进的是站主那套（还会报错）。等 /me
     回来（手机上就是几百毫秒，用户那次实测空了 697ms）才收口，屏幕上已经
     是一副错的样子了。 */
  function apply() {
    if (!S.loaded || !isVisitor()) return;

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

    /* 「我的」：右上角人形按钮藏掉，已经建出来的那页换成访客卡。 */
    var meBtns = document.querySelectorAll("[data-me]");
    Array.prototype.forEach.call(meBtns, function (b) { b.hidden = true; });
    paintMeViews();

    document.documentElement.setAttribute("data-role", "visitor");
  }

  /* 「我的」那页是点进来才创建的（stack.js 现建现推），也可能是 reload 之后
     从 sessionStorage 恢复出来的。以前这里一律把整页摘掉，结果是两个坑：

       1. 摘掉的如果是**栈顶那页**，`WSStack.top()` 还指着它、DOM 里却没了
          ——之后点人形、按返回都对不上号（实测 reload 后就是
          `top=me[脱离DOM]`，栈里只剩根视图）。
       2. 用户明明点开了「我的」，屏幕上却什么都没有，像坏了。

     改成**就地换内容**：保留这一页，正文换成 `guestPanel()`。栈、历史、
     返回手势都还成立，用户看到的是「还没登录 + 登录」。
     里面 `/api/gal/mine` 没有 qq → 400「参数不对」，所以必须换掉。 */
  function paintMeViews() {
    var all = document.querySelectorAll('.view[data-view="me"]');
    Array.prototype.forEach.call(all, function (v) {
      var me = v.querySelector("[data-me]");
      /* 顶栏只有 ✕ / 标题 / 人形，人形在这里点进去会再叠一层同样的页，
         没有意义——这时候已经能看到「怎么登录」了。 */
      if (me && me.parentNode) me.parentNode.removeChild(me);
      var body = v.querySelector(".vbody");
      if (!body || body.querySelector(".vguest")) return;
      body.innerHTML = "";
      body.appendChild(guestPanel());
    });
  }

  /* 离开工作台去登录：跳到**登录页自己的地址**（`/workspace/login`）。
   *
   * 2026-11-26 用户实测两次，这条路上踩了两个坑：
   *   ① 落点：`/workspace/` 是**有身份的人回工作台**的地址，访客证还在时它
   *      直接发聊天页，所以「去登录」用同一个地址等于原地画圈。
   *   ② **别摘证、别清存档**：原先这里先 `/logout` 再 `WSStore.clear()`，
   *      于是点「登录」就顺手把额度账本换成了新身份（再点「先逛逛」= 全新
   *      5 张），本地那份记录也当场删了（回到聊天页那张可莉就没了）。用户
   *      原话：「以访客身份点登录，次数可能会被刷新」。
   *
   * 现在「点登录」和「退出访客」是两件事：点登录 = 带着身份去登录页，随时
   * 能按「继续以访客身份使用」回来；退出访客（`me.js`）才是真的清证清本地。
   */
  function leaveToLogin() {
    global.location.replace("/workspace/login");
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
      leaveToLogin();
    });

    box.appendChild(badge);
    box.appendChild(title);
    box.appendChild(hint);
    box.appendChild(out);
    return box;
  }

  /* 包里这一层是为了**以后**从任何地方新建「我的」时直接是访客卡。
     reload 恢复出来的那页不走 build，由 `paintMeViews()` 补。 */
  function guardViews() {
    var views = global.WSViews;
    if (!views || guardViews.done) return;
    guardViews.done = true;
    var real = views.build;
    views.build = function (name, opts) {
      var view = real.apply(views, arguments);
      if (isVisitor() && name === "me") {
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
    if (!global.WSApi) { S.loaded = true; apply(); return Promise.resolve(S); }
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
    pending: pending,        // 身份还没回来（见顶部注释）
    syncLeft: syncLeft,
    apply: apply
  };
})(window);
