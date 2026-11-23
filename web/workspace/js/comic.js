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

  function slide(p) {
    var d = document.createElement("div");
    d.className = "cslide";
    var src = p.final_url || p.scene_url;

    if (src) {
      var img = document.createElement("img");
      img.alt = "第 " + p.no + " 页";
      img.decoding = "async";
      img.src = withRev(src, p.rev);
      d.appendChild(img);
      return d;
    }

    var ph = document.createElement("div");
    ph.className = "cwait" + (p.state === "error" ? " bad" : "");
    if (p.state === "error") {
      ph.textContent = p.error || "这页没画出来";
    } else if (p.state === "running" || B.running) {
      ph.textContent = "正在画…";
    } else {
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
    // 刚插进 DOM 时宽度可能还没算出来，下一帧再对一次位置
    global.requestAnimationFrame(function () {
      goTo(Math.min(B.idx, B.panels.length - 1));
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
         "第 " + p.no + " 页换种子重画中");
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

    loadBook(false);
  }

  global.WSViews.register("comic", { mount: mountList });
  global.WSViews.register("book", { mount: mountBook, top: bookTop });

  global.WSComic = {
    reload: loadList,
    stop: stopPoll
  };
})(window);
