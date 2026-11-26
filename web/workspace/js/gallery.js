/* 画廊（AI 创作）：文件夹横滑 + 两列缩略图墙 + 单图查看器。
   接口全部同源复用 /api/gal/*；本模块不认识生图与漫画。 */
(function (global) {
  "use strict";

  var SIZE = 24;

  var S = { grp: "", page: 1, more: true, busy: false, works: [], seq: 0 };
  var el = {};
  var io = null;
  var v = { list: [], idx: 0, jump: 0, faved: false, node: null, work: null,
            hist: false, slow: 0 };

  function $id(id) { return document.getElementById(id); }

  function listUrl(page) {
    var q = "?page=" + page + "&size=" + SIZE;
    return "/api/gal/list" + q + (S.grp ? "&grp=" + encodeURIComponent(S.grp) : "");
  }

  // ── 顶栏：文件夹横滑标签 ──────────────────────────────

  /* 标签栏只做**就地增删**（2026-11-23/24 用户两次报「头顶那条滑不动」：
     整条 `innerHTML=""` 重建会把 scrollLeft 清零，手指正按着时就是「卡死」）。
     分类没变就一个节点都不动；变了也只增删差集，DOM 一直在。 */
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
    if (el.tabs.dataset.key === list.join("|")) {
      // 内容没变：只把选中态对齐，永不碰 DOM 结构
      markTabOn();
      return;
    }
    el.tabs.dataset.key = list.join("|");

    /* 就地同步：多出来的按钮 remove，少了的 append，顺序按 list 走。
       等价于重建，但不会把 container 的 scrollLeft 清零。 */
    var have = {};
    Array.prototype.forEach.call(el.tabs.children, function (b) {
      have[b.dataset.grp || ""] = b;
    });
    Object.keys(have).forEach(function (g) {
      if (list.indexOf(g) < 0) el.tabs.removeChild(have[g]);
    });
    list.forEach(function (g, i) {
      var b = have[g];
      if (!b) {
        b = document.createElement("button");
        b.type = "button";
        b.className = "gtab";
        b.dataset.grp = g;
        b.textContent = g || "全部";
        b.addEventListener("click", function () {
          if (g === S.grp) return;
          S.grp = g;
          markTabOn();
          load(true);
        });
      }
      if (el.tabs.children[i] !== b) {
        el.tabs.insertBefore(b, el.tabs.children[i] || null);
      }
    });
    markTabOn();
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
      d.appendChild(document.createElement("div")).className = "gpic";
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
      /* **只要 `s=t`，不要再带 `fit=1m`**（2026-11-24 用户：「我怀疑你把聊天
         界面返回 1m 图片的那个讨论用到了我的画廊里面」——他猜对了）。
         缩略图本身 9KB，加上 fit 就变成 75KB，一屏 24 张多花约 8 倍流量，
         手机端直接卡成幻灯片。网格只要看得清是哪张，要点开看清晰图走查看器。 */
      img.src = "/api/gal/img/" + work.cover + "?s=t";
      pic.appendChild(img);
    }

    /* 这本里有几张被藏了（2026-11-24）：只挂一个小眼睛图标，不写数字。
       不压暗封面——封面本来就是第一张可见的图，压暗会让人以为整本都没了。 */
    if (work.hidden_n > 0) {
      var badge = document.createElement("span");
      badge.className = "gbadge";
      badge.setAttribute("aria-label", "这本里有已隐藏的图");
      badge.innerHTML = ICON_EYE_MINI;
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

  // 空态 / 出错那一行，`retry` 为真时带一个「重试」按钮
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

  /* 新分类能**打断**正在飞的那一次（发号机在 `gallery_load.js`，2026-11-26
     用户实测「快速连点几个标签会卡住、内容和标签对不上」）。 */
  function load(reset) {
    if (!S.claim) S.claim = WSTake.it();
    var seq = S.claim(reset);
    if (seq === null) return;
    if (reset) {
      S.page = 1; S.more = true; S.works = [];
      el.grid.innerHTML = ""; el.more.hidden = true;
      skeleton(6);
    }

    var page = S.page;
    global.WSApi.get(listUrl(page)).then(function (d) {
      if (!S.claim.owns(seq, !reset)) return;   // 已经被更新的一次换分类顶掉
      S.busy = false;
      clearSkeletons();
      if (!d || !d.ok) {
        note((d && d.error) || "图库没打开，稍后再试", true);
        return;
      }
      fillTabs(d.grps || []);

      /* 当前分类没了（目录清空 / 那面墙的图全被隐藏）→ 退回「全部」，
         别让用户停在一面空墙上（2026-11-24）。 */
      if (d.grp_gone && S.grp) {
        S.grp = ""; markTabOn(); load(true); return;
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
    }, function () {
      /* 网络断了：`WSApi` 自己会兜成 `{ok:false, error:"网络断了…"}`，正常
         不会走到这里；真走到（老版本 api.js）就认领回来，别把 S.busy 卡死
         ——卡死之后翻页和换分类全都点不动了。 */
      if (!S.claim.owns(seq)) return;
      S.busy = false;
      clearSkeletons();
      note("图库没打开，稍后再试", true);
    });
  }

  // 滚到底自动加页，不用用户去点（实现在 `gallery_load.js`）
  function watchMore() {
    io = WSTake.watch(io, el.more, el.scroll, function () {
      if (!S.busy) load(false);
    });
  }

  // ── 查看器 ────────────────────────────────────────────

  var ICON_STAR =
    '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
    'stroke-width="1.6" stroke-linejoin="round">' +
    '<path d="M12 3.8l2.6 5.4 5.9.8-4.3 4.1 1 5.9-5.2-2.9-5.2 2.9 1-5.9L3.5 10l5.9-.8z"/></svg>';

  /* 作品集卡片右上角的小眼睛（「这本里有已隐藏的图」）。只有图标、没有数字。 */
  var ICON_EYE_MINI =
    '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" ' +
    'stroke="currentColor" stroke-width="1.9" stroke-linecap="round" ' +
    'stroke-linejoin="round">' +
    '<path d="M2.5 12S6 6.5 12 6.5 21.5 12 21.5 12 18 17.5 12 17.5 2.5 12 2.5 12z"/>' +
    '<circle cx="12" cy="12" r="2.8"/><path d="M4 20 20 4"/></svg>';

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
    box.className = "gviewer"; box.hidden = true;

    var top = document.createElement("div");
    top.className = "gv-top";

    var close = document.createElement("button");
    close.type = "button"; close.className = "gv-btn";
    close.setAttribute("aria-label", "关闭");
    close.textContent = "✕"; close.addEventListener("click", closeViewer);

    var count = document.createElement("span");
    count.className = "gv-count";

    var pad = document.createElement("span"); pad.className = "gv-pad";

    top.appendChild(close);
    top.appendChild(count);
    top.appendChild(pad);

    var stage = document.createElement("div");
    stage.className = "gv-stage";
    var track = stage.appendChild(document.createElement("div"));
    track.className = "gv-track";

    var acts = document.createElement("div");
    acts.className = "gv-acts";

    var fav = document.createElement("button");
    fav.type = "button"; fav.className = "gv-act";
    fav.innerHTML = ICON_STAR + "<span>收藏</span>";
    fav.addEventListener("click", toggleFav);

    var dl = document.createElement("button");
    dl.type = "button"; dl.className = "gv-act";
    dl.innerHTML = ICON_DL + "<span>下载</span>";
    dl.addEventListener("click", download);

    var same = document.createElement("button");
    same.type = "button"; same.className = "gv-act gv-same";
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

  /* 画廊查看器和下载沿用画廊自己的预览档；聊天页面的 `fit=1m` 只属于
     chat/shot.js，不能串到这里。网格仍使用上面的 `s=t` 缩略图。 */
  function imgUrl(it) { return "/api/gal/img/" + it.id + "?s=v"; }

  /* 「很慢」提示的计时器。连开两本时先清上一本，免得旧计时器把标题改回
     「网络有点慢」。回包那一刻也调它，所以只留一个名字。 */
  function linkWa() {
    if (v.slow) { global.clearTimeout(v.slow); v.slow = 0; }
  }

  /* 给查看器压一条历史记录。已经压过就不再压——连开两本会走两次 open，
     第二次再压一条，关的时候要多按一次返回键。 */
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
    /* E 项（2026-11-26 用户：「点进作品集等十几秒什么都没有，只有一个透明
       玻璃」）：大图没回来之前先给一行字，6 秒后补一句「网络有点慢」。 */
    linkWa();
    v.node.count.textContent = "正在打开…";
    v.slow = global.setTimeout(function () {
      if (v.node.box.hidden) return;
      v.node.count.textContent = "网络有点慢，再等等…";
    }, 6000);

    global.WSApi.get("/api/gal/work/" + work.id).then(function (d) {
      linkWa();
      if (!d || !d.ok) {
        v.node.count.textContent = (d && d.error) || "这本打不开";
        return;
      }
      v.list = (d.items || []).filter(function (it) { return !it.hidden; });
      /* **已隐藏的图不进横向轨道**（2026-11-24 用户：「点击已隐藏的作品集，
         直接看到半透明的已隐藏的图片，然后作品集无法左右滑动了……站长账号
         直接看图片被卡死」）。原因不在于半透明，而在于它占了一屏：轨道是
         `scroll-snap x mandatory`，滑到那张既没有内容、又没有下一步，手指
         就在原地打转；同时它还会被当成「当前这张」去下载。
         工作台里站长和群友一样：藏起来的就是不出现，要恢复去管理页。 */
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
      linkWa();
      v.node.count.textContent = v.list.length
        ? (v.idx + 1) + " / " + v.list.length : "";
      global.requestAnimationFrame(function () {
        v.node.track.scrollLeft = v.node.track.clientWidth * v.idx;
      });
      syncFav();
    });
  }

  /* 收起来，**不动历史**。返回键那条路走这里——那时历史已经退回去了，
     再 back 就是连退两层。✕ / 「画同款」走 closeViewer，由它补掉那条。 */
  function hide() {
    if (!v.node) return;
    v.node.box.hidden = true;
    v.node.track.innerHTML = "";
    document.body.classList.remove("g-lock");
  }

  /* 收起查看器。→ true 表示「我顺手把浏览器历史也退了一条」。

     `history.back()` 是异步的：调用完 `history.state.ws` 还是旧值。
     「画同款」是 closeViewer() 紧接着 WSStack.back()，两下一叠就连退两层
     ——AI 创作页被一起关掉。所以把「已经退了一条」返回给调用方。
     没压过 `{gv:1}` 时不 back，返回 false，调用方照原逻辑退页面栈。 */
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

  function download(event) {
    var it = current();
    if (!it) return;
    /* 具体的取字节 + 存盘规矩在 `hide.js`（`WSHide.save`）。挪出去是因为
       `gallery.js` 有 600 行硬线——那边本来就是「查看器底部按钮」的模块，
       下载按钮是底栏四个按钮里的一个。 */
    var btn = event.currentTarget;
    if (btn && !btn.disabled) {
      var label = btn.innerHTML;
      btn.disabled = true;
      btn.textContent = "下载中…";
      global.setTimeout(function () {
        btn.innerHTML = label;
        btn.disabled = false;
      }, 600);
    }
    global.WSHide.save({
      url: imgUrl(it),
      name: "gal-" + it.id + ".webp",
      onFail: function () { note("这张没下下来，检查一下网络再试", false); }
    });
  }

  // ── 挂到子页 ──────────────────────────────────────────

  /* 子页壳（顶栏标签条 / 滚动容器 / 两列网格 / 查看器）在 `gallery_shell.js`，
     「谁的回包算数」在 `gallery_load.js`——本文件只留「数据怎么来、状态怎么
     变」。拆出去是因为本文件有 600 行硬线
     （`tests/test_workspace_gallery.py`），而这两块都是必要逻辑，不该为了
     行数被砍。 */
  function mountTop(title) {
    return global.WSGalleryShell.mountTop(title, el);
  }

  function mountBody(body) {
    return global.WSGalleryShell.mountBody(body, el, v, {
      load: load, buildViewer: buildViewer
    });
  }

  /* 手机返回键 / 浏览器后退落到查看器那一条时，收的是查看器，不是底下的
     子页。`stack.js` 按 `state.ws` 层数退页面，压 `{gv:1}` 时带上了当时的
     ws，所以退到这一条时层数没变，它不会顺手把子页也关掉。 */
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
