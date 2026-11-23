/* 画廊（AI 创作）：文件夹横滑 + 两列缩略图墙 + 单图查看器。
   接口全部同源复用 /api/gal/*；本模块不认识生图与漫画。 */
(function (global) {
  "use strict";

  var SIZE = 24;

  var S = { grp: "", page: 1, more: true, busy: false, works: [] };
  var el = {};
  var io = null;
  var v = { list: [], idx: 0, jump: 0, faved: false, node: null, work: null };

  function $(id) { return document.getElementById(id); }

  function listUrl(page) {
    var q = "?page=" + page + "&size=" + SIZE;
    return "/api/gal/list" + q + (S.grp ? "&grp=" + encodeURIComponent(S.grp) : "");
  }

  // ── 顶栏：文件夹横滑标签 ──────────────────────────────

  function fillTabs(grps) {
    if (!el.tabs) return;
    var list = [""].concat(grps || []);
    if (el.tabs.dataset.key === list.join("|")) return;
    el.tabs.dataset.key = list.join("|");
    el.tabs.innerHTML = "";

    list.forEach(function (g) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "gtab" + (g === S.grp ? " on" : "");
      b.textContent = g || "全部";
      b.addEventListener("click", function () {
        if (g === S.grp) return;
        S.grp = g;
        Array.prototype.forEach.call(el.tabs.children, function (x) {
          x.classList.toggle("on", x.textContent === b.textContent);
        });
        load(true);
      });
      el.tabs.appendChild(b);
    });
  }

  // ── 卡片 ──────────────────────────────────────────────

  function skeleton(n) {
    for (var i = 0; i < n; i++) {
      var d = document.createElement("div");
      d.className = "gcard gsk";
      var pic = document.createElement("div");
      pic.className = "gpic";
      d.appendChild(pic);
      el.grid.appendChild(d);
    }
  }

  function clearSkeletons() {
    Array.prototype.slice.call(el.grid.querySelectorAll(".gsk"))
      .forEach(function (n) { n.remove(); });
  }

  function card(work) {
    var b = document.createElement("button");
    b.type = "button";
    b.className = "gcard";

    var pic = document.createElement("div");
    pic.className = "gpic";
    if (work.cover) {
      var img = document.createElement("img");
      img.loading = "lazy";
      img.decoding = "async";
      img.alt = work.title;
      img.src = "/api/gal/img/" + work.cover + "?s=t";
      pic.appendChild(img);
    }

    var name = document.createElement("div");
    name.className = "gname";
    name.textContent = work.title;

    var no = document.createElement("div");
    no.className = "gno";
    no.textContent = "#" + work.id + (work.n > 1 ? " · " + work.n + " 张" : "");

    b.appendChild(pic);
    b.appendChild(name);
    b.appendChild(no);
    b.addEventListener("click", function () { open(work); });
    return b;
  }

  function note(text, retry) {
    var p = document.createElement("p");
    p.className = "gnote";
    p.textContent = text;
    if (retry) {
      var r = document.createElement("button");
      r.type = "button";
      r.className = "gretry";
      r.textContent = "重试";
      r.addEventListener("click", function () { load(true); });
      p.appendChild(r);
    }
    el.grid.appendChild(p);
  }

  // ── 列表 ──────────────────────────────────────────────

  function load(reset) {
    if (S.busy) return;
    S.busy = true;

    if (reset) {
      S.page = 1;
      S.more = true;
      S.works = [];
      el.grid.innerHTML = "";
      el.more.hidden = true;
      skeleton(6);
    }

    var page = S.page;
    global.WSApi.get(listUrl(page)).then(function (d) {
      S.busy = false;
      clearSkeletons();

      if (!d || !d.ok) {
        note((d && d.error) || "图库没打开，稍后再试", true);
        return;
      }
      fillTabs(d.grps || []);

      (d.works || []).forEach(function (w) {
        S.works.push(w);
        el.grid.appendChild(card(w));
      });

      S.more = !!d.has_more;
      S.page = page + 1;
      el.more.hidden = !S.more;

      if (!S.works.length) note("这一格还是空的", false);
      if (S.more) watchMore();
    });
  }

  // 滚到底自动加页，不用用户去点
  function watchMore() {
    if (!global.IntersectionObserver || !el.more) return;
    if (io) io.disconnect();
    io = new global.IntersectionObserver(function (rows) {
      if (rows[0] && rows[0].isIntersecting && !S.busy) load(false);
    }, { root: el.scroll, rootMargin: "200px" });
    io.observe(el.more);
  }

  // ── 查看器 ────────────────────────────────────────────

  var ICON_STAR =
    '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
    'stroke-width="1.6" stroke-linejoin="round">' +
    '<path d="M12 3.8l2.6 5.4 5.9.8-4.3 4.1 1 5.9-5.2-2.9-5.2 2.9 1-5.9L3.5 10l5.9-.8z"/></svg>';

  var ICON_STAR_ON =
    '<svg width="17" height="17" viewBox="0 0 24 24" fill="currentColor" stroke="currentColor" ' +
    'stroke-width="1.6" stroke-linejoin="round">' +
    '<path d="M12 3.8l2.6 5.4 5.9.8-4.3 4.1 1 5.9-5.2-2.9-5.2 2.9 1-5.9L3.5 10l5.9-.8z"/></svg>';

  var ICON_DL =
    '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
    'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">' +
    '<path d="M12 4v10"/><path d="M8 10.6 12 14.6l4-4"/><path d="M5 19h14"/></svg>';

  /* 「画同款」：拿这张的提示词回到聊天页接着出图（用户 2026-11-23 要的） */
  var ICON_SAME =
    '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
    'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">' +
    '<path d="M4.5 9.5h11.2"/><path d="M12.6 6.4 15.7 9.5l-3.1 3.1"/>' +
    '<path d="M19.5 14.5H8.3"/><path d="M11.4 11.4 8.3 14.5l3.1 3.1"/></svg>';

  function buildViewer() {
    var box = document.createElement("div");
    box.className = "gviewer";
    box.hidden = true;

    var top = document.createElement("div");
    top.className = "gv-top";

    var close = document.createElement("button");
    close.type = "button";
    close.className = "gv-btn";
    close.setAttribute("aria-label", "关闭");
    close.textContent = "✕";
    close.addEventListener("click", closeViewer);

    var count = document.createElement("span");
    count.className = "gv-count";

    var pad = document.createElement("span");
    pad.className = "gv-pad";

    top.appendChild(close);
    top.appendChild(count);
    top.appendChild(pad);

    var stage = document.createElement("div");
    stage.className = "gv-stage";
    var track = document.createElement("div");
    track.className = "gv-track";
    stage.appendChild(track);

    var acts = document.createElement("div");
    acts.className = "gv-acts";

    var fav = document.createElement("button");
    fav.type = "button";
    fav.className = "gv-act";
    fav.innerHTML = ICON_STAR + "<span>收藏</span>";
    fav.addEventListener("click", toggleFav);

    var dl = document.createElement("button");
    dl.type = "button";
    dl.className = "gv-act";
    dl.innerHTML = ICON_DL + "<span>下载</span>";
    dl.addEventListener("click", download);

    var same = document.createElement("button");
    same.type = "button";
    same.className = "gv-act gv-same";
    same.innerHTML = ICON_SAME + "<span>画同款</span>";
    same.addEventListener("click", function () {
      if (!global.WSChat || !global.WSChat.useSame) return;
      closeViewer();
      global.WSChat.useSame(v.work);
    });

    acts.appendChild(fav);
    acts.appendChild(same);
    acts.appendChild(dl);

    box.appendChild(top);
    box.appendChild(stage);
    box.appendChild(acts);
    document.body.appendChild(box);

    track.addEventListener("scroll", function () {
      var i = Math.round(track.scrollLeft / Math.max(1, track.clientWidth));
      if (i !== v.idx) {
        v.idx = i;
        count.textContent = (i + 1) + " / " + v.list.length;
        syncFav();
      }
    });

    v.node = { box: box, count: count, track: track, fav: fav };
  }

  /* work：作品集；itemId：可选，从收藏那张直接跳到它在集里的位置 */
  function open(work, itemId) {
    // 「我的」页可以直接 push 出来，不一定先逛过画廊；查看器得能自己建
    if (!v.node) buildViewer();
    /* 同一本正在看就别再开一遍：这张卡片被连点两下时，第二次会把 track 清空
       重灌，图片全白一下（用户说的「卡死」有一半就是这样来的）。 */
    if (!v.node.box.hidden && v.work && v.work.id === work.id && !itemId) return;
    v.work = work;
    v.node.box.hidden = false;
    document.body.classList.add("g-lock");
    v.node.track.innerHTML = "";
    v.node.count.textContent = "";
    v.idx = 0;
    v.jump = itemId || 0;
    v.list = [];
    v.faved = false;

    global.WSApi.get("/api/gal/work/" + work.id).then(function (d) {
      if (!d || !d.ok) {
        v.node.count.textContent = (d && d.error) || "这本打不开";
        return;
      }
      v.list = d.items || [];
      v.list.forEach(function (it) {
        var slide = document.createElement("div");
        slide.className = "gv-slide";
        var img = document.createElement("img");
        img.alt = work.title;
        img.src = "/api/gal/img/" + it.id + "?s=v";
        slide.appendChild(img);
        v.node.track.appendChild(slide);
      });

      // 从「我的 → 收藏」进来时定位到收藏的那张。各来源 idx 口径不一，
      // 按 item id 找位置最稳。
      if (v.jump) {
        for (var i = 0; i < v.list.length; i++) {
          if (v.list[i].id === v.jump) { v.idx = i; break; }
        }
      }
      v.jump = 0;
      v.node.count.textContent = v.list.length
        ? (v.idx + 1) + " / " + v.list.length : "";
      global.requestAnimationFrame(function () {
        v.node.track.scrollLeft = v.node.track.clientWidth * v.idx;
      });
      syncFav();
    });
  }

  function closeViewer() {
    if (!v.node) return;
    v.node.box.hidden = true;
    v.node.track.innerHTML = "";
    document.body.classList.remove("g-lock");
  }

  function current() { return v.list[v.idx] || null; }

  function syncFav() {
    var it = current();
    v.faved = !!(it && it.fav);
    v.node.fav.innerHTML = (v.faved ? ICON_STAR_ON : ICON_STAR) +
      "<span>" + (v.faved ? "已收藏" : "收藏") + "</span>";
    v.node.fav.classList.toggle("on", v.faved);
  }

  function toggleFav() {
    var it = current();
    if (!it) return;
    var on = !v.faved;
    global.WSApi.post("/api/gal/mark", {
      item_id: it.id, kind: "fav", on: on
    }).then(function (d) {
      if (!d || !d.ok) return;
      it.fav = on ? 1 : 0;
      syncFav();
    });
  }

  function download() {
    var it = current();
    if (!it) return;
    var a = document.createElement("a");
    a.href = "/api/gal/img/" + it.id + "?s=v";
    a.download = "gal-" + it.id + ".webp";
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  // ── 挂到子页 ──────────────────────────────────────────

  function mountTop(title) {
    title.className = "gtabs";
    title.textContent = "";
    el.tabs = title;
  }

  function mountBody(body) {
    el.body = body;
    body.classList.add("gbody");

    /* 两层：外层是子页内容区（不滚，见 chat.css 的 .vbody.v-dock），里层才是
       滚动容器。这样底部留白不会跟着滚动跑，输入栏钉在底下不动。 */
    el.scroll = document.createElement("div");
    el.scroll.className = "gscroll";
    el.scroll.setAttribute("data-scroll", "");

    // 从这一页发的话要落在这一页：输入栏被借到这里（见 views.js 的 dock），
    // 聊天页那层消息区被盖住了，写进去等于石沉大海。
    el.chat = document.createElement("div");
    el.chat.className = "gchat";
    el.chat.setAttribute("data-chat-scroll", "");

    el.grid = document.createElement("div");
    el.grid.className = "ggrid";

    el.more = document.createElement("button");
    el.more.type = "button";
    el.more.className = "gmore";
    el.more.textContent = "加载更多";
    el.more.hidden = true;
    el.more.addEventListener("click", function () { load(false); });

    el.scroll.appendChild(el.grid);
    el.scroll.appendChild(el.more);
    el.scroll.appendChild(el.chat);
    body.appendChild(el.scroll);

    buildViewer();
    load(true);
  }

  global.WSGallery = {
    open: open,
    close: closeViewer,
    reload: function () { S.works = []; load(true); }
  };

  global.WSViews.register("gallery", { top: mountTop, mount: mountBody });
})(window);
