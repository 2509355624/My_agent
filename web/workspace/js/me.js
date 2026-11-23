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
    io = new global.IntersectionObserver(function (rows) {
      if (rows[0] && rows[0].isIntersecting && !S.busy) load(false);
    }, { root: el.body, rootMargin: "200px" });
    io.observe(el.more);
  }

  // ── 挂到子页 ──────────────────────────────────────────

  function mountTop(mid) {
    mid.textContent = "我的";
  }

  function mountBody(body) {
    el.body = body;
    body.classList.add("mebody");

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

    body.appendChild(el.tabs);
    body.appendChild(el.grid);
    body.appendChild(el.more);

    S.tab = "works";
    S.page = 1;
    S.more = true;
    S.busy = false;
    S.total = 0;
    syncTabs();
    load(true);
  }

  global.WSViews.register("me", { top: mountTop, mount: mountBody });
})(window);
