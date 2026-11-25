/* 创作面板：输入栏「＋」弹出的两个平级入口「图片生成 / 漫画生成」。
   图片生成 = 回到默认模式；漫画生成 = 接管发送：一句话剧情 → 编剧写剧本
   → 自动开画 → 卡片里画好一页出一页。

   用户跑漫画不看剧本只看结果（2026-11-23 原话），所以剧本好了直接开跑，
   中间不让人再点一次。接口全部同源复用 /api/comic/*，不改后端。

   格数不在这里定：5174 管理页的「一本几格」是站主配的（state/comic_friend.json
   的 max_panels），/api/comic/script 不传 panels 时会按账号上限收口。前端再写死
   一个数就会出现「管理页改了、这里不动」的两套真相。 */
(function (global) {
  "use strict";

  var SCRIPT_MS = 1500;   // 编剧是后台 agent，跑几分钟很正常，慢点问
  var DRAW_MS = 2500;
  var MAX_FAIL = 5;
  var IDLE_MAX = 4;       // 没在跑、也没进展，连续这么多次就收手

  /* 模式要记住（2026-11-25 用户：「我退出之后进不去了，没办法再重新进入到漫画台」）。
     原来只活在内存里：点一下 ✕ 或刷新一下页面就变回「图片生成」，再说话就是在
     生图，用户找不到回去的路。跟画风 / 渠道一样存 localStorage，下次进来还是它。 */
  var LS_MODE = "ws_create_mode";
  var S = { mode: "img", started: [], books: [] };
  var el = {};

  function $(id) { return document.getElementById(id); }

  // ── 模式 ──────────────────────────────────────────────

  function mode() { return S.mode; }

  /* 「这一本开过没有」：重放存档时用它决定那颗按钮打不打「打开整本」。
     打开整本 = 压一层 `book` 子页，而子页是押在聊天页上面的——聊天页还没
     起来就压，等于在空白页上盖一张，✕ 退回去什么都没有。 */
  function used(task) {
    if (!task) return false;
    for (var i = 0; i < S.started.length; i++) {
      if (S.started[i] === task) return true;
    }
    return false;
  }

  function savedMode() {
    try { return global.localStorage.getItem(LS_MODE) === "comic" ? "comic" : "img"; }
    catch (e) { return "img"; }
  }

  function setMode(m, quiet) {
    S.mode = m === "comic" ? "comic" : "img";
    try { global.localStorage.setItem(LS_MODE, S.mode); } catch (e) { /* 存不下就只活在这次 */ }
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
    if (global.WSRef) global.WSRef.syncHint();
    // 模式标记会改变输入栏高度，让聊天页重新算底部留白
    global.dispatchEvent(new Event("resize"));
    // 初始化时不要抢焦点，否则手机上开页就弹键盘
    if (!quiet) el.say.focus();
  }

  // ── 跑漫画 ────────────────────────────────────────────

  /* refs：参考图 id。漫画这边后端没有「拿参考图当角色基准」的口子，
     图会随剧情一起交给编剧，让它写清「照着这张的样子」——所以这里只把
     数量报给用户，不假装已经把图画进去了。 */
  function runComic(brief, refs) {
    var rec = { k: "comic", brief: brief, task: "", title: "",
                job: "", note: "编剧写剧本中…", err: false, cells: [],
                done: false, refs: refs || [] };
    var card = global.WSChat.card("card-comic");
    var run = { card: card, task: "", title: "", timer: 0, fails: 0,
                idle: 0, grid: null, rec: rec, done: false };

    run.grid = document.createElement("div");
    run.grid.className = "cgrid";
    card.body.appendChild(run.grid);

    card.say(rec.note);
    if (refs && refs.length) {
      var tip = document.createElement("p");
      tip.className = "ref-note";
      tip.textContent = "带着 " + refs.length + " 张参考图，编剧会照着写";
      card.body.appendChild(tip);
    }
    push(run);

    global.WSApi.post("/api/comic/script", { brief: brief }).then(function (d) {
      if (!d || !d.ok) return giveUp(run, (d && d.error) || "编剧没排上队");
      rec.job = d.job || "";
      run.job = rec.job;
      push(run);
      pollScript(run, d.job);
    });
    return run;
  }

  /* 存档里那条漫画记录由 chat.js 统一写盘，这里只改字段再叫它存一次 */
  function push(run) {
    if (global.WSChat && global.WSChat.touch) global.WSChat.touch(run.rec);
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
    disarmStop(run);
    run.card.say(msg || "这一步没走通，再发一次试试", true);
    run.rec.note = msg || "这一步没走通";
    run.rec.err = true;
    run.rec.job = "";
    push(run);
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
          run.rec.note = run.card.note.textContent;
          push(run);
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
    run.rec.task = task;
    if (S.started.indexOf(task) < 0) S.started.push(task);
    run.card.say("剧本好了，正在开画…");
    run.rec.note = "剧本好了，正在开画…";
    push(run);

    global.WSApi.post("/api/comic/start", { task: task }).then(function (d) {
      if (!d || !d.ok) return giveUp(run, (d && d.error) || "没开起来");
      acts(run, false);
      armStop(run);
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
        run.rec.title = run.title;
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
        run.rec.note = run.card.note.textContent;
        push(run);
        later(run, function () { pollDraw(run); }, DRAW_MS);
      });
  }

  /* 缩略格按 9:16 完整显示，不裁边；图没出来就留一个空格子 */
  function drawGrid(run, panels) {
    run.rec.cells = [];
    panels.forEach(function (p, i) {
      var cell = run.grid.children[i];
      if (!cell) {
        cell = document.createElement("div");
        cell.className = "ccell";
        cell.addEventListener("click", function () { openBook(run); });
        run.grid.appendChild(cell);
      }
      cell.classList.toggle("bad", p.state === "error");
      if (!hasPic(p)) {
        run.rec.cells.push("");
        return;
      }
      var url = (p.final_url || p.scene_url) + "&v=" + encodeURIComponent(p.rev || "");
      run.rec.cells.push(url);
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
    disarmStop(run);
    run.card.say(ps.length + " 页都画好了，点一格翻整本");
    run.rec.note = run.card.note.textContent;
    run.rec.done = true;
    run.rec.err = false;
    push(run);
    acts(run, true);
  }

  function finishBad(run, msg) {
    stop(run);
    disarmStop(run);
    run.card.say(msg || "画到中途停了，去「我的漫画作品」看哪页没出来", true);
    run.rec.note = run.card.note.textContent;
    run.rec.done = true;
    run.rec.err = true;
    push(run);
    acts(run, true);
  }

  // ── 刷新后接回来 ──────────────────────────────────────

  /* 存档里那条漫画还没画完：把格子先按存下来的图铺好，再接着轮询。
     剧本阶段断掉的（没有 task）只能等用户重发——编剧任务号在服务端内存里，
     进程重启就没了，硬接只会一直转。 */
  function resume(rec, card) {
    if (!rec || !rec.task) {
      card.note.textContent = rec.note || "服务重启过，这一话要重发一次";
      card.note.hidden = false;
      card.note.className = "note err";
      return;
    }
    var run = { card: card, task: rec.task, title: rec.title || "",
                timer: 0, fails: 0, idle: 0, grid: null, rec: rec };
    run.grid = document.createElement("div");
    run.grid.className = "cgrid";
    card.body.appendChild(run.grid);
    if (rec.note) card.say(rec.note, rec.err);
    acts(run, !!rec.done && !rec.err);
    if (!rec.done) armStop(run);
    later(run, function () { pollDraw(run); }, 600);
  }

  /* 重放存档：把这一话已经出来的格子直接画回去（不重新请求接口，
     真正缺的那几页交给 resume 的轮询补） */
  function repaint(shell, rec) {
    if (!rec.cells || !rec.cells.length) return;
    var grid = document.createElement("div");
    grid.className = "cgrid";
    rec.cells.forEach(function (url, i) {
      var cell = document.createElement("div");
      cell.className = "ccell";
      if (url) {
        var img = document.createElement("img");
        img.alt = "第 " + (i + 1) + " 页";
        img.src = url;
        cell.appendChild(img);
        cell.dataset.src = url;
      }
      grid.appendChild(cell);
    });
    shell.body.appendChild(grid);
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

  /* 「终止这一本」（2026-11-23 用户：「漫画生成这一块也是这样子」）：
     服务端 `/api/comic/abort` 会把正在画的那格定向停掉、排队中的整批摘掉、
     逐格循环退出，已经出好的格子一张都不动。 */
  function stopBook(run) {
    if (!run.task) return;
    var b = run.card.stop;
    if (b) { b.disabled = true; b.textContent = "停止中…"; }
    global.WSApi.post("/api/comic/abort", { task: run.task })
      .then(function (d) {
        if (b) { b.hidden = true; b.disabled = false; b.textContent = "终止"; }
        stop(run);
        run.card.say((d && d.ok)
          ? "已终止，已经画好的格子都留着" : "终止没发出去，再点一次");
        run.rec.note = run.card.note.textContent;
        run.rec.done = true;
        run.rec.err = true;
        push(run);
        acts(run, true);
      })["catch"](function () {
        if (b) { b.hidden = false; b.disabled = false; b.textContent = "终止"; }
      });
  }

  /* 画的过程中露出来，画完 / 失败就收起来 */
  function armStop(run) {
    var b = run.card.stop;
    if (!b) return;
    b.hidden = false;
    b.disabled = false;
    b.textContent = "终止";
    b.onclick = function () { stopBook(run); };
  }

  function disarmStop(run) {
    var b = run.card.stop;
    if (b) { b.hidden = true; b.onclick = null; }
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

  /* 「打开整本」不再直接推子页（2026-11-25 用户：「我点击之后它应该是显示一个
     卡片的才对，像之前那个跳出来那个图片卡片一样，点卡片之后打开整本」）。

     为什么原来是直接推：`book` 子页是压在聊天页上面的，聊天页里那条漫画卡
     刚好被盖住——用户点完只看到一页图，退出来才发现「我点过的东西没了」。
     现在分两步：先在聊天记录里落下**一张书卡**（跟出图卡同一种壳、同一套
     按钮），点书卡才进整本。书卡进存档，刷新 / 换设备也还在。

     已经贴过就不再贴：同一本书点两次「打开整本」不该攒出两张卡。 */
  function openBook(run) {
    if (!run.task) return;
    var task = run.task;
    if (S.books.indexOf(task) >= 0) { enterBook(task, run.title); return; }
    var rec = { k: "book", task: task, title: run.title || "",
                cover: (run.rec.cells || []).filter(Boolean).slice(-1)[0] || "" };
    global.WSChat.log(rec);
    drawBook(rec);
    global.WSChat.scrollBottom();
  }

  /* 铺一张书卡。**只按记录铺**——openBook 和存档重放共用这一条，
     这样刷新前后长得一模一样（重放走 archive.js → restoreBook）。 */
  function drawBook(rec) {
    if (!rec || !rec.task) return;
    if (S.books.indexOf(rec.task) < 0) S.books.push(rec.task);
    var task = rec.task;
    var card = global.WSChat.card("card-book");
    card.node.classList.add("is-book");
    card.say((rec.title || task) + " · 点一下看整本");
    card.fill();
    card.body.appendChild(bookCover(rec.cover ? [rec.cover] : [], task));
    var box = card.acts;
    box.hidden = false;
    box.appendChild(btn("read", "看整本", function () { enterBook(task, rec.title); }));
    card.node.addEventListener("click", function (e) {
      // 卡面上的图 / 标题都能点进去，只有按钮区自己按自己的走
      if (e.target.closest(".acts")) return;
      enterBook(task, rec.title);
    });
    return card;
  }

  function restoreBook(rec) { drawBook(rec); }

  /* 书卡上的封面：用这一本最后一页（画完的那页最有信息量），没有就留空框。
     只显示一张，不铺九宫格——书卡的作用是「入口」，不是又一份结果。 */
  function bookCover(cells, task) {
    var cover = document.createElement("div");
    cover.className = "bcover";
    var src = "";
    for (var i = cells.length - 1; i >= 0; i -= 1) {
      if (cells[i]) { src = cells[i]; break; }
    }
    if (src) {
      var img = document.createElement("img");
      img.alt = "封面";
      img.decoding = "async";
      img.src = src;
      cover.appendChild(img);
    } else {
      cover.classList.add("empty");
      cover.textContent = "还没出页";
    }
    cover.dataset.task = task;
    return cover;
  }

  function enterBook(task, title) {
    if (!task) return;
    global.WSStack.push("book", { task: task, title: title || task });
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
    // 没选过就从头开始；选过漫画就还原（见上面 LS_MODE 的说明）
    S.mode = savedMode();
    setMode(S.mode, true);
  }

  global.WSCreate = {
    init: init,
    mode: mode,
    setMode: setMode,
    used: used,
    runComic: runComic,
    resume: resume,
    repaint: repaint,
    restoreBook: restoreBook
  };
})(window);
