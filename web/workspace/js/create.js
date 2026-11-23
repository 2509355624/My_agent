/* 创作面板：输入栏「＋」弹出的两个平级入口「图片生成 / 漫画生成」。
   图片生成 = 回到默认模式；漫画生成 = 接管发送：一句话剧情 → 编剧写剧本
   → 自动开画 → 卡片里画好一页出一页。

   用户跑漫画不看剧本只看结果（2026-11-23 原话），所以剧本好了直接开跑，
   中间不让人再点一次。接口全部同源复用 /api/comic/*，不改后端。 */
(function (global) {
  "use strict";

  var PANELS = 10;        // 一话默认 10 页，与漫画台默认值一致
  var SCRIPT_MS = 1500;   // 编剧是后台 agent，跑几分钟很正常，慢点问
  var DRAW_MS = 2500;
  var MAX_FAIL = 5;
  var IDLE_MAX = 4;       // 没在跑、也没进展，连续这么多次就收手

  var S = { mode: "img" };
  var el = {};

  function $(id) { return document.getElementById(id); }

  // ── 模式 ──────────────────────────────────────────────

  function mode() { return S.mode; }

  function setMode(m, quiet) {
    S.mode = m === "comic" ? "comic" : "img";
    if (!el.tag) return;
    var comic = S.mode === "comic";
    el.tag.hidden = !comic;
    el.say.placeholder = comic
      ? "说一句剧情，我写完剧本直接开画"
      : "描述你想创作的内容";
    if (el.hint) {
      el.hint.textContent = comic ? "说一句剧情，我画成漫画" : "说一句话，我帮你出图";
    }
    if (el.img) el.img.classList.toggle("on", !comic);
    if (el.comic) el.comic.classList.toggle("on", comic);
    // 模式标记会改变输入栏高度，让聊天页重新算底部留白
    global.dispatchEvent(new Event("resize"));
    // 初始化时不要抢焦点，否则手机上开页就弹键盘
    if (!quiet) el.say.focus();
  }

  // ── 跑漫画 ────────────────────────────────────────────

  function runComic(brief) {
    var card = global.WSChat.card("card-comic");
    var run = { card: card, task: "", title: "", timer: 0, fails: 0,
                idle: 0, grid: null };

    run.grid = document.createElement("div");
    run.grid.className = "cgrid";
    card.body.appendChild(run.grid);

    card.say("编剧写剧本中…");
    global.WSApi.post("/api/comic/script", { brief: brief, panels: PANELS })
      .then(function (d) {
        if (!d || !d.ok) return giveUp(run, (d && d.error) || "编剧没排上队");
        pollScript(run, d.job);
      });
    return run;
  }

  function later(run, fn, ms) {
    clearTimeout(run.timer);
    run.timer = setTimeout(fn, ms);
  }

  function stop(run) {
    if (run.timer) { clearTimeout(run.timer); run.timer = 0; }
  }

  function giveUp(run, msg) {
    stop(run);
    run.card.say(msg || "这一步没走通，再发一次试试", true);
    if (run.task) acts(run, false);
  }

  function pollScript(run, job) {
    global.WSApi.get("/api/comic/script_status?job=" + encodeURIComponent(job))
      .then(function (d) {
        if (!d || !d.ok) {
          run.fails += 1;
          if (run.fails >= MAX_FAIL) return giveUp(run, (d && d.error) || "编剧状态断了");
          return later(run, function () { pollScript(run, job); }, SCRIPT_MS);
        }
        run.fails = 0;

        if (d.state === "running") {
          var log = d.log || [];
          var last = log.length ? log[log.length - 1] : "";
          run.card.say("编剧写剧本中" + (last ? "（" + last + "）" : "…"));
          return later(run, function () { pollScript(run, job); }, SCRIPT_MS);
        }
        if (d.state === "error") {
          return giveUp(run, "编剧没写成：" + (d.error || "未知原因"));
        }
        start(run, d.task);
      });
  }

  function start(run, task) {
    if (!task) return giveUp(run, "剧本好了但没给出任务号");
    run.task = task;
    run.card.say("剧本好了，正在开画…");

    global.WSApi.post("/api/comic/start", { task: task }).then(function (d) {
      if (!d || !d.ok) return giveUp(run, (d && d.error) || "没开起来");
      acts(run, false);
      later(run, function () { pollDraw(run); }, 1200);
    });
  }

  function hasPic(p) { return !!(p.final_url || p.scene_url); }

  function pollDraw(run) {
    global.WSApi.get("/api/comic/status?task=" + encodeURIComponent(run.task))
      .then(function (d) {
        if (!d || !d.ok) {
          run.fails += 1;
          if (run.fails >= MAX_FAIL) {
            return giveUp(run, "读状态失败，去「我的漫画作品」看结果");
          }
          return later(run, function () { pollDraw(run); }, DRAW_MS);
        }
        run.fails = 0;
        var ps = d.panels || [];
        run.title = d.title || run.title;
        drawGrid(run, ps);

        var done = ps.filter(hasPic).length;
        if (d.job_error) return finishBad(run, "画到一半停了：" + d.job_error);
        if (!d.running && ps.length && done >= ps.length) return finish(run, ps);
        if (!d.running) {
          run.idle += 1;
          if (run.idle >= IDLE_MAX) return finishBad(run, "");
        } else {
          run.idle = 0;
        }

        run.card.say(done
          ? "已经画好 " + done + " / " + ps.length + " 页…"
          : "排队等 GPU，马上开画…");
        later(run, function () { pollDraw(run); }, DRAW_MS);
      });
  }

  /* 缩略格按 9:16 完整显示，不裁边；图没出来就留一个空格子 */
  function drawGrid(run, panels) {
    panels.forEach(function (p, i) {
      var cell = run.grid.children[i];
      if (!cell) {
        cell = document.createElement("div");
        cell.className = "ccell";
        cell.addEventListener("click", function () { openBook(run); });
        run.grid.appendChild(cell);
      }
      cell.classList.toggle("bad", p.state === "error");
      if (!hasPic(p)) return;
      var url = (p.final_url || p.scene_url) + "&v=" + encodeURIComponent(p.rev || "");
      if (cell.dataset.src === url) return;
      var img = cell.querySelector("img");
      if (!img) {
        img = document.createElement("img");
        img.alt = "第 " + p.no + " 页";
        img.decoding = "async";
        cell.appendChild(img);
      }
      img.src = url;
      cell.dataset.src = url;
    });
  }

  function finish(run, ps) {
    stop(run);
    run.card.say(ps.length + " 页都画好了，点一格翻整本");
    acts(run, true);
  }

  function finishBad(run, msg) {
    stop(run);
    run.card.say(msg || "画到中途停了，去「我的漫画作品」看哪页没出来", true);
    acts(run, true);
  }

  // ── 卡片动作 ──────────────────────────────────────────

  function btn(key, text, fn) {
    var b = document.createElement("button");
    b.type = "button";
    b.className = "cact";
    b.dataset.k = key;
    b.textContent = text;
    b.addEventListener("click", fn);
    return b;
  }

  function acts(run, withZip) {
    var box = run.card.acts;
    run.card.show();
    if (!box.querySelector('[data-k="open"]')) {
      box.appendChild(btn("open", "打开整本", function () { openBook(run); }));
    }
    if (withZip && !box.querySelector('[data-k="zip"]')) {
      box.appendChild(btn("zip", "下载全部", function () { downloadAll(run); }));
    }
  }

  function openBook(run) {
    if (!run.task) return;
    global.WSStack.push("book", { task: run.task, title: run.title || run.task });
  }

  function downloadAll(run) {
    var a = document.createElement("a");
    a.href = "/api/comic/download?task=" + encodeURIComponent(run.task);
    a.download = run.task + "_final.zip";
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  // ── 初始化 ────────────────────────────────────────────

  function init() {
    el.tag = $("mode-tag");
    el.img = $("pick-img");
    el.comic = $("pick-comic");
    el.say = $("say");
    el.hint = document.querySelector("#chat-empty p");
    var clear = $("mode-clear");
    if (clear) clear.addEventListener("click", function () { setMode("img"); });
    setMode(S.mode, true);
  }

  global.WSCreate = {
    init: init,
    mode: mode,
    setMode: setMode,
    runComic: runComic
  };
})(window);
