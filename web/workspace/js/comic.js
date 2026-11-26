/* 我的漫画作品：作品列表 + 翻开一本看页。
   接口全部同源复用 /api/comic/*，本模块只管「看结果」和单页操作；
   剧本、补正、批量生图仍在聊天页与后端，不在这里重复实现。
   用户跑漫画不看剧本只看结果，所以列表和单本都以图为主，文字只留一行。 */
(function (global) {
  "use strict";

  var POLL_MS = 2500;
  var MAX_FAIL = 4;

  var L = { busy: false, tasks: [] };
  var B = { task: "", title: "", panels: [], idx: 0, timer: 0,
            fails: 0, running: "", node: null, strip: null, track: null };
  var el = {};

  function $(id) { return document.getElementById(id); }

  function imgUrl(task, rel) {
    return "/api/comic/img?task=" + encodeURIComponent(task) +
           "&f=" + encodeURIComponent(rel);
  }

  /* 图被重画后文件指纹会变；把 rev 挂到 URL 上，浏览器才不会拿旧缓存 */
  function withRev(url, rev) {
    return url + "&v=" + encodeURIComponent(rev || "");
  }

  function shortTime(s) {
    var m = /^\d{4}-(\d{2}-\d{2})[ T](\d{2}:\d{2})/.exec(s || "");
    return m ? m[1] + " " + m[2] : (s || "");
  }

  function toast(text, bad) {
    var t = document.createElement("div");
    t.className = "wstoast" + (bad ? " bad" : "");
    t.textContent = text;
    document.body.appendChild(t);
    global.requestAnimationFrame(function () { t.classList.add("on"); });
    setTimeout(function () {
      t.classList.remove("on");
      setTimeout(function () { t.remove(); }, 240);
    }, 1800);
  }

  // ── 列表 ──────────────────────────────────────────────

  /* 封面：先找带气泡的成品，没有就退底图；两个都不在就是空壳 */
  function cover(task) {
    var box = document.createElement("div");
    box.className = "cpic";

    var img = document.createElement("img");
    img.loading = "lazy";
    img.decoding = "async";
    img.alt = "";
    /* 图加载完把封面框的比例改成这张图自己的比例：框和图同比例时
       contain 就是「正好铺满」，不会上下留黑边，人物也不会被切。
       竖向漫画多是 2:3（1024×1536 / 512×768），老图也有 832×1216 的。 */
    img.addEventListener("load", function () {
      if (img.naturalWidth && img.naturalHeight) {
        box.style.setProperty("--cpic-ar",
          img.naturalWidth + " / " + img.naturalHeight);
      }
    });
    img.src = imgUrl(task, "final/01_final.png");
    img.addEventListener("error", function () {
      if (img.dataset.step === "raw") {
        img.remove();
        box.classList.add("nopic");
        var s = document.createElement("span");
        s.textContent = "还没图";
        box.appendChild(s);
        return;
      }
      img.dataset.step = "raw";
      img.src = imgUrl(task, "raw/01_raw.png");
    });
    box.appendChild(img);
    return box;
  }

  function taskCard(t) {
    var b = document.createElement("button");
    b.type = "button";
    b.className = "cwork";

    var total = t.panels || 0;
    var done = Math.min(t.done || 0, total);
    var pct = total ? Math.round(done * 100 / total) : 0;

    var info = document.createElement("div");
    info.className = "cinfo";

    var name = document.createElement("div");
    name.className = "cname";
    name.textContent = t.title || t.task;

    var meta = document.createElement("div");
    meta.className = "cmeta";
    /* 有图就算「已画」：`rendered`（底图出来了、气泡还没加）也是画出东西了。
       后端 done 已按这个口径数，这条只是老接口 / 缓存里的数兜一层。 */
    var bits = [total ? "已画 " + done + " / " + total + " 页" : "还没出页"];
    if (t.updated) bits.push(shortTime(t.updated));
    meta.textContent = bits.join(" · ");

    var bar = document.createElement("div");
    bar.className = "cbar";
    var fill = document.createElement("i");
    fill.style.width = pct + "%";
    bar.appendChild(fill);

    info.appendChild(name);
    info.appendChild(meta);
    info.appendChild(bar);

    b.appendChild(cover(t.task));
    b.appendChild(info);
    b.addEventListener("click", function () {
      // 压一层：单本页的 ✕ 退回作品列表，再点一次才回聊天页
      global.WSStack.push("book", { task: t.task, title: t.title || t.task });
    });
    return b;
  }

  function listNote(text, retry) {
    var p = document.createElement("p");
    p.className = "cnote";
    p.textContent = text;
    if (retry) {
      var r = document.createElement("button");
      r.type = "button";
      r.className = "cbtn ghost";
      r.textContent = "重试";
      r.addEventListener("click", loadList);
      p.appendChild(r);
    }
    el.list.appendChild(p);
  }

  function loadList() {
    if (L.busy) return;
    L.busy = true;
    el.list.innerHTML = "";
    for (var i = 0; i < 2; i++) {
      var sk = document.createElement("div");
      sk.className = "cwork csk";
      el.list.appendChild(sk);
    }

    global.WSApi.get("/api/comic/tasks").then(function (d) {
      L.busy = false;
      el.list.innerHTML = "";
      if (!d || !d.ok) {
        listNote((d && d.error) || "作品列表没打开，稍后再试", true);
        return;
      }
      L.tasks = d.tasks || [];
      L.tasks.forEach(function (t) { el.list.appendChild(taskCard(t)); });
      if (!L.tasks.length) {
        listNote("还没有漫画作品。回聊天页说一句剧情，我跑完就出现在这里");
      }
    });
  }

  function mountList(body) {
    body.classList.add("cbody");
    el.list = document.createElement("div");
    el.list.className = "clist";

    // 图在本机、不保证长期保留，这条必须压在列表最上面，且是红的
    var warn = document.createElement("p");
    warn.className = "cwarn";
    warn.textContent = "原图只存在这台机器上，不长期保存——跑完记得下载。";

    body.appendChild(warn);
    body.appendChild(el.list);
    loadList();
  }

  // ── 单本 ──────────────────────────────────────────────

  function bookTop(mid, opts) {
    mid.textContent = (opts && opts.title) || "漫画作品";
  }

  function alive() {
    return !!(B.node && document.body.contains(B.node));
  }

  function current() { return B.panels[B.idx] || null; }

  function pageLabel() {
    if (!B.panels.length) return "";
    return (B.idx + 1) + " / " + B.panels.length;
  }

  /* 让一格「整张图都看得见」：按面板的实际高度算 contain，再交给 CSS。
     box 高 = 轨道高 − 顶部浮层高（--vtop-h）− 这一格自己的上下内边距。
     窄高比 < img 的宽高比时按宽铺满、高留白；反之按高铺满、两边留白。
     宽高比还读不到（图没解码完 / 取失败）就不设，退回 CSS 的 contain。
     2026-11-26 用户：「打开之后没有办法完整显示整个图片」——以前靠
     `height:100%` + grid 居中，靠的是盒子的高度链，任何一环没撑起来图就
     会溢出被裁；现在直接给像素尺寸，所见即所算。 */
  function fitImage(slideEl, img) {
    var w = img.naturalWidth;
    var h = img.naturalHeight;
    if (!w || !h) return;
    var box = slideEl.clientHeight -
              parseFloat(getComputedStyle(slideEl).paddingTop || 0) -
              parseFloat(getComputedStyle(slideEl).paddingBottom || 0);
    var availW = slideEl.clientWidth;
    if (box <= 0 || availW <= 0) return;
    var k = Math.min(availW / w, box / h);
    img.style.width = Math.round(w * k) + "px";
    img.style.height = Math.round(h * k) + "px";
  }

  function fitAll() {
    Array.prototype.forEach.call(B.track.children, function (d) {
      var img = d.querySelector("img");
      if (img) fitImage(d, img);
    });
  }

  function slide(p) {
    var d = document.createElement("div");
    d.className = "cslide";
    var src = p.final_url || p.scene_url;

    if (src) {
      var img = document.createElement("img");
      img.alt = "第 " + p.no + " 页";
      img.decoding = "async";
      img.addEventListener("load", function () { fitImage(d, img); });
      img.src = withRev(src, p.rev);
      /* 缓存命中时 load 可能在挂上监听之前就发生了，再来一发兜底 */
      if (img.complete && img.naturalWidth) fitImage(d, img);
      d.appendChild(img);
      return d;
    }

    var ph = document.createElement("div");
    ph.className = "cwait" + (p.state === "error" ? " bad" : "");
    if (p.state === "error") {
      ph.textContent = p.error || "这页没画出来";
    } else if (p.state === "running") {
      ph.textContent = "正在画…";
    } else if (B.running) {
      /* 整本有线程在跑（比如刚点了「重画这页」），但这一格不动：
         它在队列里等，不是在画。说清楚是哪件事，别让每一格都喊「正在画」。 */
      ph.textContent = "排队中，画好就出来";
    } else {
      /* 没在画、也没出图：不冒充「正在画」。
         2026-11-26 用户报「我只是重画这一页，滑到后面全是正在画」——
         原来判断是 `p.state === "running" || B.running`，只要整本有一个
         后台线程在跑，每一格都顶着「正在画…」。现在只认这一格自己的状态：
         `running` 是这一格正在画，`pending` 就是还没画到。 */
      ph.textContent = "还没画到这页";
    }
    d.appendChild(ph);
    return d;
  }

  function thumb(p, i) {
    var b = document.createElement("button");
    b.type = "button";
    b.className = "cthumb" + (i === B.idx ? " on" : "") +
                  (p.state === "error" ? " bad" : "");
    var src = p.final_url || p.scene_url;
    if (src) {
      var img = document.createElement("img");
      img.loading = "lazy";
      img.alt = "第 " + p.no + " 页";
      img.src = withRev(src, p.rev);
      b.appendChild(img);
    } else {
      var no = document.createElement("span");
      no.textContent = p.no;
      b.appendChild(no);
    }
    b.addEventListener("click", function () { goTo(i); });
    return b;
  }

  function renderPanels() {
    B.track.innerHTML = "";
    B.strip.innerHTML = "";
    B.panels.forEach(function (p, i) {
      B.track.appendChild(slide(p));
      B.strip.appendChild(thumb(p, i));
    });
    goTo(Math.min(B.idx, B.panels.length - 1));
    // 刚插进 DOM 时宽高可能还没算出来，下一帧再对一次位置、再量一次图
    global.requestAnimationFrame(function () {
      goTo(Math.min(B.idx, B.panels.length - 1));
      fitAll();
    });
  }

  /* 横滑翻页：只改滚动位置，不重建节点，图片不会闪 */
  function goTo(i) {
    if (!B.panels.length) {
      el.pageno.textContent = "";
      return;
    }
    B.idx = Math.max(0, Math.min(i, B.panels.length - 1));
    B.track.scrollLeft = B.track.clientWidth * B.idx;
    el.pageno.textContent = pageLabel();
    syncStrip();
    syncActs();
  }

  function syncStrip() {
    Array.prototype.forEach.call(B.strip.children, function (n, i) {
      n.classList.toggle("on", i === B.idx);
    });
    var cur = B.strip.children[B.idx];
    if (!cur) return;
    B.strip.scrollLeft = cur.offsetLeft -
      (B.strip.clientWidth - cur.offsetWidth) / 2;
  }

  function syncActs() {
    var p = current();
    var canBubble = !!(p && (p.state === "rendered" || p.state === "done"));
    var hasPic = !!(p && (p.final_url || p.scene_url));
    el.acts.forEach(function (b) {
      var k = b.dataset.act;
      if (k === "bubble") b.disabled = !canBubble || !!B.running;
      if (k === "redo") b.disabled = !p || !!B.running;
      if (k === "one") b.disabled = !hasPic;
    });
    el.all.disabled = !B.panels.length;
  }

  function sameShape(list) {
    if (!B.panels.length || B.panels.length !== list.length) return false;
    for (var i = 0; i < list.length; i++) {
      if (B.panels[i].rev !== list[i].rev) return false;
      if (B.panels[i].state !== list[i].state) return false;
    }
    return true;
  }

  /* 状态里哪些变化值得重铺整本：图和格状态。`sameShape` 返回真就只刷按钮，
     返回假才重建——所以「正在画 → 排队中 → 出图」每一步都会重铺那一屏，
     不会停在上一句话上。 */
  function apply(d) {
    B.running = d.running || "";
    B.title = d.title || B.title;
    if (!sameShape(d.panels || [])) {
      B.panels = d.panels || [];
      renderPanels();
      return;
    }
    B.panels = d.panels || [];
    syncActs();
  }

  function loadBook(quiet) {
    if (!B.task) return;
    if (!quiet) el.pageno.textContent = "打开中…";

    global.WSApi.get("/api/comic/status?task=" +
                     encodeURIComponent(B.task)).then(function (d) {
      if (!alive()) return;
      if (!d || !d.ok) {
        B.fails += 1;
        if (!quiet) el.pageno.textContent = (d && d.error) || "这本打不开";
        if (B.fails >= MAX_FAIL) stopPoll();
        return;
      }
      B.fails = 0;
      apply(d);
      if (B.running) startPoll(); else stopPoll();
    });
  }

  function startPoll() {
    if (B.timer) return;
    B.timer = setInterval(function () {
      if (!alive()) { stopPoll(); return; }
      loadBook(true);
    }, POLL_MS);
  }

  function stopPoll() {
    if (B.timer) { clearInterval(B.timer); B.timer = 0; }
  }

  function post(url, data, okText) {
    global.WSApi.post(url, data).then(function (d) {
      if (!d || !d.ok) {
        toast((d && d.error) || "没排上队，稍后再试", true);
        return;
      }
      toast(okText);
      loadBook(true);
      startPoll();
    });
  }

  function redo() {
    var p = current();
    if (!p) return;
    post("/api/comic/rerender", { task: B.task, no: p.no },
         "第 " + p.no + " 页重画中，出来就盖掉这页");
  }

  function bubble() {
    var p = current();
    if (!p) return;
    post("/api/comic/bubble", { task: B.task, no: p.no },
         "第 " + p.no + " 页加气泡中");
  }

  function downloadOne() {
    var p = current();
    if (!p) return;
    var src = p.final_url || p.scene_url;
    if (!src) return;
    var a = document.createElement("a");
    a.href = withRev(src, p.rev);
    a.download = B.task + "_" + p.no + ".png";
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  function downloadAll() {
    var a = document.createElement("a");
    a.href = "/api/comic/download?task=" + encodeURIComponent(B.task);
    a.download = B.task + "_final.zip";
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  var ACTS = [
    { act: "redo", text: "重画这页", fn: redo },
    { act: "bubble", text: "加气泡", fn: bubble },
    { act: "one", text: "下载这页", fn: downloadOne },
    { act: "all", text: "下载全部", fn: downloadAll }
  ];

  function mountBook(body, opts) {
    opts = opts || {};
    B.task = opts.task || "";
    B.title = opts.title || "";
    B.panels = [];
    B.idx = 0;
    B.fails = 0;
    B.running = "";
    B.node = body;

    body.classList.add("cbook");

    B.track = document.createElement("div");
    B.track.className = "ctrack";
    B.track.addEventListener("scroll", function () {
      var w = B.track.clientWidth || 1;
      var i = Math.round(B.track.scrollLeft / w);
      if (i === B.idx) return;
      B.idx = Math.max(0, Math.min(i, B.panels.length - 1));
      el.pageno.textContent = pageLabel();
      syncStrip();
      syncActs();
    });

    var stage = document.createElement("div");
    stage.className = "cstage";
    stage.appendChild(B.track);

    el.pageno = document.createElement("div");
    el.pageno.className = "cpageno";

    B.strip = document.createElement("div");
    B.strip.className = "cstrip";

    var acts = document.createElement("div");
    acts.className = "cacts";
    el.acts = [];
    ACTS.forEach(function (a) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "cbtn";
      b.dataset.act = a.act;
      b.textContent = a.text;
      b.addEventListener("click", a.fn);
      acts.appendChild(b);
      if (a.act !== "all") el.acts.push(b);
    });
    el.all = acts.lastChild;

    body.appendChild(stage);
    body.appendChild(el.pageno);
    body.appendChild(B.strip);
    body.appendChild(acts);

    /* 手机地址栏收起/展开、横竖屏切换都会改变画面区高度，图得跟着重量。
       节流到下一帧，免得拖动窗口时每像素算一次。 */
    var refit = 0;
    function onResize() {
      if (!alive()) { global.removeEventListener("resize", onResize); return; }
      if (refit) return;
      refit = global.requestAnimationFrame(function () { refit = 0; fitAll(); });
    }
    global.addEventListener("resize", onResize);

    loadBook(false);
  }

  global.WSViews.register("comic", { mount: mountList });
  global.WSViews.register("book", { mount: mountBook, top: bookTop });

  global.WSComic = {
    reload: loadList,
    stop: stopPoll
  };
})(window);
