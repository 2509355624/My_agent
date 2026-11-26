/* 「我的」：只有作品 / 收藏两个 tab，其它一律不放。
   作品走 /api/gal/mine（作品集），收藏走 /api/gal/favs（单张图）。
   点开都交给画廊查看器，收藏那张直接跳到收藏的那一页。 */
(function (global) {
  "use strict";

  var SIZE = 24;

  var S = { tab: "works", page: 1, more: true, busy: false, total: 0 };
  var el = {};
  var io = null;

  function $(id) { return document.getElementById(id); }

  var EMPTY = {
    works: "还没有作品。回聊天页说一句，画出来就在这儿",
    favs: "还没收藏。在「AI 创作」里点开一张，按星星收藏"
  };

  // ── 卡片 ──────────────────────────────────────────────

  function card(title, meta, url, onClick) {
    var b = document.createElement("button");
    b.type = "button";
    b.className = "mcard";

    var pic = document.createElement("div");
    pic.className = "mpic";
    if (url) {
      var img = document.createElement("img");
      img.loading = "lazy";
      img.decoding = "async";
      img.alt = title;
      img.src = url;
      pic.appendChild(img);
    }
    b.appendChild(pic);

    var name = document.createElement("div");
    name.className = "mname";
    name.textContent = title;
    b.appendChild(name);

    var sub = document.createElement("div");
    sub.className = "mmeta";
    sub.textContent = meta;
    b.appendChild(sub);

    b.addEventListener("click", onClick);
    return b;
  }

  function workCard(w) {
    var url = w.cover ? "/api/gal/img/" + w.cover + "?s=t" : "";
    return card(w.title || "未命名",
                "#" + w.id + (w.n > 1 ? " · " + w.n + " 张" : ""),
                url, function () {
                  global.WSGallery.open({ id: w.id });
                });
  }

  function favCard(it) {
    return card(it.title || "未命名", it.by || "收藏的图",
                "/api/gal/img/" + it.id + "?s=t", function () {
                  // 传 item id，查看器自己定位到第几张（idx 各来源口径不一）
                  global.WSGallery.open({ id: it.work }, it.id);
                });
  }

  function note(text, retry) {
    var p = document.createElement("p");
    p.className = "mempty";
    p.textContent = text;
    if (retry) {
      var r = document.createElement("button");
      r.type = "button";
      r.className = "mretry";
      r.textContent = "重试";
      r.addEventListener("click", function () { load(true); });
      p.appendChild(r);
    }
    el.grid.appendChild(p);
  }

  // ── tab ───────────────────────────────────────────────

  function syncTabs() {
    Array.prototype.forEach.call(el.tabs.children, function (b) {
      b.classList.toggle("on", b.dataset.tab === S.tab);
    });
  }

  function pick(tab) {
    if (tab === S.tab || S.busy) return;
    S.tab = tab;
    syncTabs();
    load(true);
  }

  // ── 列表 ──────────────────────────────────────────────

  function load(reset) {
    if (S.busy) return;
    S.busy = true;

    if (reset) {
      S.page = 1;
      S.more = true;
      S.total = 0;
      el.grid.innerHTML = "";
      el.more.hidden = true;
      if (io) { io.disconnect(); io = null; }
    }

    var path = S.tab === "works" ? "/api/gal/mine" : "/api/gal/favs";
    global.WSApi.get(path + "?page=" + S.page + "&size=" + SIZE)
      .then(function (d) {
        S.busy = false;
        if (!d || !d.ok) {
          note((d && d.error) || "没打开，稍后再试", true);
          return;
        }
        var rows = S.tab === "works" ? (d.works || []) : (d.items || []);
        rows.forEach(function (r) {
          el.grid.appendChild(S.tab === "works" ? workCard(r) : favCard(r));
        });
        S.total += rows.length;
        S.more = !!d.has_more;
        S.page += 1;
        el.more.hidden = !S.more;

        if (!S.total) note(EMPTY[S.tab], false);
        if (S.more) watchMore();
      });
  }

  // 滑到底接着加页，跟画廊一个手感；不另做页码
  function watchMore() {
    if (!global.IntersectionObserver || !el.more) return;
    if (io) io.disconnect();
    io = new global.IntersectionObserver(function (rows) {
      if (rows[0] && rows[0].isIntersecting && !S.busy) load(false);
    }, { root: el.scroll, rootMargin: "200px" });
    io.observe(el.more);
  }

  // ── 账号 ──────────────────────────────────────────────

  /* 顶栏这段是「我是谁 + 怎么换人」。页面进来就显示登录按钮（用户
     2026-11-23：进来之后没有任何入口也没有退出），登录态从 /me 读——
     5175 才有这个接口，站主直连 5174 时它 404，那就直接当站主处理。 */
  function mountAccount() {
    var box = document.createElement("div");
    box.className = "meacc";

    var who = document.createElement("div");
    who.className = "meacc-who";
    var badge = document.createElement("span");
    badge.className = "meacc-badge";
    badge.textContent = "我";
    var name = document.createElement("span");
    name.className = "meacc-name";
    name.textContent = "未登录";
    who.appendChild(badge);
    who.appendChild(name);

    var out = document.createElement("button");
    out.type = "button";
    out.className = "meacc-btn";
    out.textContent = "登录";
    out.addEventListener("click", function () {
      if (out.dataset.act === "logout") {
        out.disabled = true;
        /* 2026-11-26 用户实测：「点退出登录，先显示作品和收藏，然后闪一下又
           变成访客身份」。根因是**两件事抢跑**：`reload()` 和清 cookie 是
           并行的，刷新有可能发生在清 cookie 生效之前，那一瞬服务器看到的
           还是访客 → 把访客页面又发回来。

           现在只做一件事：等 `/logout` 真的回来（cookie 已在响应里清掉），
           再跳干净地址。跳地址而不是 reload，是因为 reload 会从
           sessionStorage 把子页栈恢复回来（又送回「我的」这一页）。
           失败也要跳：留在这一页只会是「点了没反应」。 */
        global.WSApi.post("/logout", {}).then(function () {
          /* 聊天记录是这台设备的，退出登录就该一起清掉——换个人登录
             看到上一个人的对话比「丢了记录」更糟。 */
          if (global.WSStore) global.WSStore.clear();
          global.location.replace("/workspace/");
        })["catch"](function () {
          global.location.replace("/workspace/");
        });
        return;
      }
      /* 去登录用**跳地址**而不是 reload（2026-11-26）：reload 之后子页栈会
         从 sessionStorage 恢复回来，人又被送回「我的」这一页，看起来就是
         「点了登录又弹回来」（见 visitor.js 的 leaveToLogin）。 */
      global.location.replace("/workspace/");
    });

    
    box.appendChild(out);
    // 账号条放在 tab 下面：tab 是「看什么」，账号是「我是谁」，先看东西
    if (el.tabs && el.tabs.parentNode) {
      el.tabs.parentNode.insertBefore(box, el.tabs.nextSibling);
    } else {
      el.scroll.appendChild(box);
    }

    global.WSApi.get("/me").then(function (d) {
      if (!d || !d.ok) {
        // 5174 直连没有 /me：站主本人，不用登录也不给退出
        name.textContent = "站主直连";
        out.hidden = true;
        return;
      }
      var qq = d.qq || "";
      /* 访客模式（2026-11-26）：没 qq 但有 role=visitor。访客这一页本来
         就被 visitor.js 摘掉了，这里留一份兜底显示，免得以后入口又长回来
         却显示成「游客」。
         按钮写「退出访客」而不是「退出登录」：2026-11-26 用户点完问过
         「退出登录，怎么又变成访客了」——那时候按钮就叫「退出登录」，而人
         其实是访客。名不对，行为就会被误判成 bug。 */
      if (d.role === "visitor") {
        var q = d.quota || {};
        badge.textContent = "访";
        name.textContent = "访客 · 今日还剩 " + (q.left || 0) + " 张";
        out.textContent = "退出访客";
        out.dataset.act = "logout";
        return;
      }
      name.textContent = qq ? ("QQ " + qq) : "游客";
      badge.textContent = qq ? qq.slice(-2) : "游";
      if (qq) {
        out.textContent = "退出登录";
        out.dataset.act = "logout";
      }
    });
  }

  // ── 挂到子页 ──────────────────────────────────────────

  /* 顶栏那格的标题（2026-11-26 用户：「头顶那个我的，它不是放在中间的」）。
     位置由 CSS 锚在整栏中线上（sheet.css 的 `.vtop > .vtitle`），这里只管
     文字——建页时它是空的，`mount` 同步跑完就有字了。 */
  function mountTop(mid) {
    mid.textContent = "我的";
  }

  function mountBody(body) {
    el.body = body;
    /* 两层：外层是子页内容区（不滚），里层 .mescroll 才是滚动容器。
       子页内容区本身 overflow-y:auto，一旦它自己滚，任何 padding-bottom 都会
       变成滚动内容跟着走，输入栏就会被拖着上下动（画廊那边同样处理）。
       另存一份滚动位置：从「我的」再进「我的」时（右上角人形会再叠一层）
       回来还能停在刚才那一屏，不会突然掉到顶上。 */
    body.classList.add("mebody");

    el.scroll = document.createElement("div");
    el.scroll.className = "mescroll";
    el.scroll.setAttribute("data-scroll", "");

    el.tabs = document.createElement("div");
    el.tabs.className = "metabs";
    [["works", "作品"], ["favs", "收藏"]].forEach(function (pair) {
      var b = document.createElement("button");
      b.type = "button";
      b.dataset.tab = pair[0];
      b.textContent = pair[1];
      b.addEventListener("click", function () { pick(pair[0]); });
      el.tabs.appendChild(b);
    });

    el.grid = document.createElement("div");
    el.grid.className = "megrid";

    el.more = document.createElement("button");
    el.more.type = "button";
    el.more.className = "memore";
    el.more.textContent = "加载更多";
    el.more.hidden = true;
    el.more.addEventListener("click", function () { load(false); });

    el.scroll.appendChild(el.tabs);
    mountAccount();
    el.scroll.appendChild(el.grid);
    el.scroll.appendChild(el.more);
    body.appendChild(el.scroll);

    S.tab = "works";
    S.page = 1;
    S.more = true;
    S.busy = false;
    S.total = 0;
    syncTabs();
    load(true);

    el.scroll.addEventListener("scroll", function () { POS = el.scroll.scrollTop; });
    global.requestAnimationFrame(function () { el.scroll.scrollTop = POS; });
  }

  /* 上一层的浏览位置：模块级的，换页/退出再进来都能接上 */
  var POS = 0;

  global.WSViews.register("me", { top: mountTop, mount: mountBody });
})(window);
