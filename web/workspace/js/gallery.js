/* 画廊（AI 创作）：文件夹横滑 + 两列缩略图墙 + 单图查看器。
   接口全部同源复用 /api/gal/*；本模块不认识生图与漫画。 */
(function (global) {
  "use strict";

  var SIZE = 24;

  var S = { grp: "", page: 1, more: true, busy: false, works: [] };
  var el = {};
  var io = null;
  var v = { list: [], idx: 0, jump: 0, faved: false, node: null, work: null,
            hist: false };

  function $(id) { return document.getElementById(id); }

  function listUrl(page) {
    var q = "?page=" + page + "&size=" + SIZE;
    return "/api/gal/list" + q + (S.grp ? "&grp=" + encodeURIComponent(S.grp) : "");
  }

  // ── 顶栏：文件夹横滑标签 ──────────────────────────────

  /* 重建标签栏时必须把滚动位置还回去。
     2026-11-23 用户第三次报「AI 创作页头顶那条滑动条滑不动」——根因就在这：
     `fillTabs` 每次列表加载完都会 `innerHTML = ""` 整条重建，滚动位置
     随之归零。用户手正按着往右滑的时候正好赶上一次加载回包，标签条就
     「弹回最左边」，手感上就是卡死、白滑。现在记下 scrollLeft，重建完
     立刻写回去（新内容还没排版时写不进就下一帧再补一次）。 */
  function fillTabs(grps) {
    if (!el.tabs) return;
    /* 同名标签只留一个。后端已经按分类收过一遍（2026-11-24 那批重复的
       `new` 是 `SELECT DISTINCT grp, path` 按两列去重造成的），这里再兜
       一层——标签重复时点哪个都跳同一个分类，纯属视觉噪声。 */
    var uniq = [], seen = {};
    (grps || []).forEach(function (g) {
      if (!g || seen[g]) return;
      seen[g] = true;
      uniq.push(g);
    });
    var list = [""].concat(uniq);
    if (el.tabs.dataset.key === list.join("|")) return;
    el.tabs.dataset.key = list.join("|");
    var keep = el.tabs.scrollLeft;
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

    el.tabs.scrollLeft = keep;
    global.requestAnimationFrame(function () {
      if (el.tabs && el.tabs.scrollLeft !== keep) el.tabs.scrollLeft = keep;
      markTabIntoView();
    });
  }

  /* 把选中态跟 S.grp 对齐（不重建节点，所以不会把横向滚动位置弄丢） */
  function markTabOn() {
    if (!el.tabs) return;
    Array.prototype.forEach.call(el.tabs.children, function (x) {
      x.classList.toggle("on", x.textContent === (S.grp || "全部"));
    });
    markTabIntoView();
  }

  /* 选中的那个标签要自己露出来：从深层文件夹跳回来时它可能滚在屏外 */
  function markTabIntoView() {
    if (!el.tabs) return;
    var on = el.tabs.querySelector(".gtab.on");
    if (!on) return;
    var l = on.offsetLeft, r = l + on.offsetWidth;
    var vw = el.tabs.clientWidth;
    if (l < el.tabs.scrollLeft) el.tabs.scrollLeft = l - 8;
    else if (r > el.tabs.scrollLeft + vw) el.tabs.scrollLeft = r - vw + 8;
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
      /* `&fit=1m`（2026-11-24）：画廊网格原来铺 360px 缩略图，在手机上
         两列一格约 180 CSS px，360 正好够；但放大屏 / 平板就显得糊。带上
         fit 可以让服务端挑一档更清楚的（≤1MB），竖图那种又高又窄的封面
         尤其明显。服务端不认就当没传，退回原来的 s=t。 */
      img.src = "/api/gal/img/" + work.cover + "?s=t&fit=1m";
      pic.appendChild(img);
    }

    /* 这本里有几张被藏了：卡片封面右上角挂个「已藏 N」。`hidden_n` 服务端
       只给站主这个数，别人拿到的一律是 0 —— 对他们来说那些图不存在。

       注意**不压暗封面**：封面已经是「第一张可见的图」了（服务端
       `cover_item` 跳过隐藏的），压暗会让人以为整本都没了。 */
    if (work.hidden_n > 0) {
      var badge = document.createElement("span");
      badge.className = "gbadge";
      badge.textContent = "已藏 " + work.hidden_n;
      pic.appendChild(badge);
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

      /* 当前分类没了（目录被清空、或那面墙的图全被隐藏）→ 退回「全部」。
         2026-11-24 用户：「清空 web 的时候这个文件夹目录是空的了，然后这个
         标签就应该隐藏」——标签由 fillTabs 按服务端给的新列表重建，这里负责
         把选中态也收回来，别停在一面空墙上。 */
      if (d.grp_gone && S.grp) {
        S.grp = "";
        markTabOn();
        load(true);
        return;
      }

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
    same.addEventListener("click", sameAs);

    /* 隐藏这一张（只有站主有）。放在「画同款」右边、下载左边——底栏是从
       右往左排的，最右是下载。按钮长相归 hide.js 管，这儿只管它该不该在。 */
    var hide = global.WSHide.makeButton(!!(v.work && v.work.can_hide_item));
    hide.addEventListener("click", toggleHide);

    acts.appendChild(fav);
    acts.appendChild(same);
    acts.appendChild(hide);
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

    v.node = { box: box, count: count, track: track, fav: fav, hide: hide };
  }

  /* 查看器里的大图。2026-11-24 用户：「点击图片不需要重新传 10m 的原图，
     我们默认就只给 1m 即可，下载也是下载 1m 的」——所以查看器、下载、网格
     **用的是同一档**（`fit=1m`），看到哪张就存哪张，不再有「点开等十几秒
     才变清晰」这一步。

     `s=v` 留着：服务端缩不出来时会退到 1600px 预览，总比 404 好。 */
  function imgUrl(it) { return "/api/gal/img/" + it.id + "?s=v&fit=1m"; }

  /* 给查看器压一条历史记录。已经压过（还开着）就不再压——连点两本不同的
     作品集会走两次 open，第二次再压一条，关的时候要多按一次返回键。 */
  function pushViewer() {
    if (v.hist) return;
    v.hist = true;
    global.history.pushState({ gv: 1,
      ws: Number((global.history.state || {}).ws) || 0 }, "");
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
    /* 手机返回键要能关掉这一层（2026-11-24 用户：「打开某个具体画廊的图片
       详情，按下手机的返回键时，没有关闭当前的详情页面，而是底下的界面正常
       回退」）。查看器是个 position:fixed 的覆盖层，不在页面栈里，所以以前
       popstate 只退子页、它赖着不走。

       现在给它单压一条历史记录（`{gv:1}`，不带 ws——页面栈那边只认 ws 这个
       整数，多出来的属性不影响它的层数计算），返回键退到这里，popstate 里
       看见 gv 就把它收起来。`v.hist` 记住「这条是我压的」，避免连开两本时
       压两条。 */
    pushViewer();
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
      /* 作品级的 can_hide_item 只有详情接口给（列表里那份没有），补到
         `v.work` 上——隐藏按钮显示与否全靠它。 */
      if (d.work) {
        v.work.can_hide_item = !!d.work.can_hide_item;
        v.work.can_hide = !!d.work.can_hide;
        v.work.n = d.work.n;
      }
      v.list.forEach(function (it) {
        var slide = document.createElement("div");
        slide.className = "gv-slide";
        var img = document.createElement("img");
        img.alt = work.title;
        img.src = imgUrl(it);
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

  /* 收起来，**不动历史**。返回键那条路走这里——那时浏览器历史已经退回去了，
     再 back 一次就等于连退两层。✕ / 「画同款」走 closeViewer，由它负责把
     多出来的那条补掉。 */
  function hide() {
    if (!v.node) return;
    v.node.box.hidden = true;
    v.node.track.innerHTML = "";
    document.body.classList.remove("g-lock");
  }

  /* 收起查看器。→ true 表示「我顺手把浏览器历史也退了一条」。

     `history.back()` 是**异步**的：调用完 state 不会立刻变，紧跟着再读
     `history.state.ws` 拿到的还是退之前那个值。「画同款」正是 closeViewer()
     之后紧接着 WSStack.back()，两下一叠就成了连退两层——AI 创作页被一起
     关掉。所以把「已经退了一条」这个事实返回给调用方，让它别再退。

     没压过 `{gv:1}`（比如查看器是从「我的」直接开的、历史已被别处清理）
     时不会执行 back，返回 false，调用方照原逻辑退页面栈。 */
  function closeViewer() {
    if (!v.node || v.node.box.hidden) return false;
    hide();
    /* 用户自己关的（✕ / 「画同款」）：那条 `{gv:1}` 用不上了，退回去，
       否则下一次按返回键是一次「什么都没发生」的空退。 */
    if (!v.hist) return false;
    v.hist = false;
    if (Number((global.history.state || {}).gv) !== 1) return false;
    global.history.back();
    return true;
  }

  /* 「画同款」（用户 2026-11-23 定）：不是图生图，是把这张读成一段描述再生成。
     所以要做三件事——关查看器、退回聊天页、把这张挂成参考图。
     以前只把作品名填进输入框就完了，而作品名是「1」「2」这种编号，
     用户看到的就是「弹了内容，什么都没发生」。 */
  function sameAs() {
    var it = current();
    if (!it) return;
    var work = v.work || {};
    /* 关查看器会顺手退掉 `{gv:1}` 那条历史。紧接着调 WSStack.back() 时它
       读到的是**退之前**的 state（back() 异步），于是又退一层，把 AI 创作
       页也关掉。所以先把「还欠一次退页」告诉栈，让它按最终层数收。 */
    var popped = closeViewer();
    if (global.WSStack) {
      if (popped && global.WSStack.backToChat) global.WSStack.backToChat();
      else global.WSStack.back();
    }
    if (global.WSRef) global.WSRef.useSame(work, it.id);
  }

  function current() { return v.list[v.idx] || null; }

  function syncFav() {
    var it = current();
    v.faved = !!(it && it.fav);
    v.node.fav.innerHTML = (v.faved ? ICON_STAR_ON : ICON_STAR) +
      "<span>" + (v.faved ? "已收藏" : "收藏") + "</span>";
    v.node.fav.classList.toggle("on", v.faved);
    syncHide();
  }

  /* 隐藏按钮 / 已藏标记跟着当前这张走。长相和状态都在 hide.js，这边只把
     现场递过去（哪本、第几张、整本列表）。 */
  function syncHide() {
    global.WSHide.sync({
      btn: v.node.hide, track: v.node.track,
      list: v.list, idx: v.idx, work: v.work
    });
  }

  function toggleHide() {
    var it = current();
    if (!it || !v.work || !v.work.can_hide_item) return;
    var on = !it.hidden;
    global.WSApi.post("/api/gal/item/hidden", {
      item_id: it.id, hidden: on
    }).then(function (d) {
      if (!d || !d.ok) {
        note((d && d.error) || "改不动，稍后再试", false);
        return;
      }
      it.hidden = on ? 1 : 0;
      syncHide();
      /* 整本被藏光了就没什么可看的了：告诉用户一声并收起来。这种作品集
         在墙上也不会再出现（服务端把 n 算成可见张数），所以留在这儿只会
         看到一面半透明的墙。 */
      if (d.left === 0) {
        global.WSChat && global.WSChat.sysNote("这本的图全藏起来了，已从墙上下架");
        closeViewer();
        global.WSGallery.reload();
      }
    });
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
    /* 和查看器同一档（`fit=1m`）：用户要的就是「看到哪张存哪张」 */
    a.href = imgUrl(it);
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

  /* 手机返回键 / 浏览器后退落到查看器那一条时，收的是查看器，不是底下的
     子页。`stack.js` 那边按 `state.ws` 层数退页面，压 `{gv:1}` 时把当时的
     ws 一起带上，所以退到这一条时层数没变，它不会顺手把子页也关掉。 */
  global.addEventListener("popstate", function (e) {
    if (!v.node || v.node.box.hidden) return;
    // 退到的这条还带 gv，说明是往前退回了另一个查看器态（几乎不会发生）
    if (e.state && Number(e.state.gv) === 1) return;
    v.hist = false;
    hide();
  });

  global.WSGallery = {
    open: open,
    close: closeViewer,
    reload: function () { S.works = []; load(true); }
  };

  global.WSViews.register("gallery", { top: mountTop, mount: mountBody });
})(window);
