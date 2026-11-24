/* 聊天页存档的重放：刷新之后把说过的话、出过的图、没画完的漫画接回来。
   从 chat.js 拆出来的——那边已经六百多行，重放又只是纯 DOM 还原。
   数据存在 store.js，这里只认消息记录的形状。

   为什么不用 chat.js 的渲染函数重放：paintRef / newCard 会顺手再写一次
   存档，重放一遍记录就翻倍。这里只画最后的样子，重画这类按钮再单独接。 */
(function (global) {
  "use strict";

  /* 列表里铺的那一档：和服务端 `/api/gen/img?fit=` 对齐（长边试到 ≤512KB）。
     和 chat.js 里的 `FIT` 是同一个值，两边改一处必须一起改。 */
  var FIT = "512k";

  // ── 重放 ──────────────────────────────────────────────

  /* 存档里的卡片壳。还原时不挂任何监听，只把最后的样子画出来——
     正在跑的任务由下面的 restore() 接回轮询，不能复用 newCard / paintRef，
     那些函数会顺手再写一次存档（重放一遍就等于把记录翻倍）。 */
  function restoreShell(m, cls) {
    var node = document.createElement("div");
    node.className = "card" + (cls ? " " + cls : "");
    var body = document.createElement("div");
    body.className = "card-body";
    var note = document.createElement("p");
    note.className = "note" + (m.err ? " err" : "");
    note.textContent = m.note || "";
    note.hidden = !m.note;
    var acts = document.createElement("div");
    acts.className = "acts";
    acts.hidden = true;
    /* 重放出来的卡也要有「终止」位：上面 push 进 pending 的那些还要接着
       轮询，用户得能掐掉它（原来重放完就只剩一行「排队中…」，掐不掉）。 */
    var stop = document.createElement("button");
    stop.type = "button";
    stop.className = "stop-run";
    stop.hidden = true;
    stop.textContent = "终止";
    node.appendChild(body);
    node.appendChild(note);
    node.appendChild(stop);
    node.appendChild(acts);
    global.WSChat.box().appendChild(node);
    return { node: node, body: body, note: note, stop: stop, acts: acts };
  }

  /* 缓存 key：把地址上的站点签名 `k=` 摘掉（它每次会话都可能换，带着当
     key，服务一重启缓存全成孤儿；域名本来就不在 key 里）。 */
  function keyOf(url) {
    return String(url || "").replace(/[&?]k=[^&]*/g, "");
  }

  /* 把一张图的字节存进本地缓存（存过就跳过）。no-cors：地址上有站点签名，
     不需要 cookie。失败一律不管——缓存是优化，不能影响显示。 */
  function pull(url) {
    if (!url || !global.WSImg) return;
    var key = keyOf(url);
    global.WSImg.has(key).then(function (yes) {
      if (yes) return undefined;
      return global.fetch(url, { credentials: "omit", mode: "no-cors" })
        .then(function (r) { return r.blob(); })
        .then(function (b) {
          if (b && b.size) return global.WSImg.put(key, b);
          return undefined;
        });
    })["catch"](function () { return undefined; });
  }

  function restoreRef(m) {
    global.WSChat.hideEmpty();
    var wrap = document.createElement("div");
    wrap.className = "refs";
    wrap.dataset.ref = m.id || "";
    var img = document.createElement("img");
    img.alt = "参考图";
    img.loading = "lazy";
    img.decoding = "async";
    img.src = m.url || ("/api/gal/img/" + m.id + "?s=t");
    img.addEventListener("click", function () {
      if (global.WSGallery) global.WSGallery.open({ id: m.wid }, m.id);
    });
    wrap.appendChild(img);
    global.WSChat.box().appendChild(wrap);
  }

  /* ── 恢复 ────────────────────────────────────────────
     消息按顺序重放：气泡、参考卡、结果卡（带图）、漫画格。
     没出图的那条在重放完统一接回轮询（生图走 status，漫画走自己的轮询）。 */
  function restore() {
    var d = global.WSStore && global.WSStore.load();
    if (!d || !d.msgs || !d.msgs.length) return;

    /* 重放消息里的参考卡时顺手把「待发送」那张也认回来：用户没发的那张
       刷新后还得在输入栏上方等着，不然得重新去画廊点一次。只认最后一张
       ——同一条消息里挂着两三个参考图，重新挂一遍反而更乱。 */
    var lastRef = null;

    var again = [];
    d.msgs.forEach(function (m) {
      if (!m || !m.k) return;
      global.WSChat.msgs().push(m);

      if (m.k === "user") {
        // 发出去的那条可能带着参考图，重放时一起铺回来（用户 2026-11-23：
        // 「我那条消息应该包含参考图，因为我能够看到之前的参考图是什么」）
        global.WSChat.paintUser(m.t, m.refs || []);
      } else if (m.k === "note") {
        global.WSChat.paintNote(m.t);
      } else if (m.k === "ref") {
        restoreRef(m);
        lastRef = m;
      } else if (m.k === "img") {
        var c = restoreShell(m);
        if (m.file) {
          var img = document.createElement("img");
          img.alt = "生成结果";
          img.loading = "lazy";
          img.decoding = "async";
          // 恢复历史也铺**512KB 那一档**（2026-11-24）：翻一次记录可能十几张，
          // 每张原图 2~8MB，全从家用上行出就是「打开页面等半天」的由来。
          // `FIT` 和 chat.js 里同一个值，下载拿的也是这一张。
          img.src = global.WSGen.img(m.file, FIT);
          img.dataset.full = global.WSGen.img(m.file);
          img.className = "shot";
          img.addEventListener("click", function () {
            if (!img.dataset.full || img.dataset.fullLoaded === "1") return;
            img.dataset.fullLoaded = "1";
            img.classList.add("shot-full");
            img.src = img.dataset.full;
            // 顺手把原图也存进本地缓存：下次点开、换域名再来都不用重拉
            pull(img.dataset.full);
          });
          // 列表这张本身也进本地缓存——下载直接拿它，不再穿一次隧道
          pull(img.src);
          c.body.appendChild(img);
          // 重放时把 prompt / skill 一起交给 buildActs：不带的话「复制」
          // 复制的是空串、「重画」也排不上队（新版多了下载，同样要 file）
          global.WSChat.buildActs({
            acts: c.acts, prompt: m.prompt || "", skill: m.skill || "",
            refs: m.refs || [], file: m.file, rec: m
          });
        } else {
          var ph = document.createElement("div");
          ph.className = "ph";
          ph.style.opacity = ".3";
          c.body.appendChild(ph);
          if (m.job) {
            again.push({ rec: m, card: {
              node: c.node, body: c.body, ph: ph, note: c.note,
              stop: c.stop, acts: c.acts,
              prompt: m.prompt || "", skill: m.skill || "", job: m.job, rec: m,
              refs: m.refs || [], described: !!m.described
            } });
          }
        }
      } else if (m.k === "comic") {
        var cc = restoreShell(m, "card-comic");
        if (global.WSCreate && global.WSCreate.repaint) {
          global.WSCreate.repaint(cc, m);
        }
        if (m.task && !m.done) again.push({ rec: m, card: cc, comic: true });
      }
    });

    if (lastRef && global.WSRef) {
      global.WSRef.rehydrate(lastRef);
    }

    /* 存档里「还在跑」的那几条先放一放，问一次服务端再决定接不接：
       · 状态里还有它（且还是 queued）→ 接回轮询；
       · 状态里没有 / 已经结束 / 接口不通 → 就地判超时，直接给「重试」。
       用户 2026-11-24 报的就是这一步：昨晚那张的服务重启过，存档里那条
       永远停在「排队中」，刷新几次都一样。 */
    if (!again.length) {
      global.WSChat.scrollBottom();
      return;
    }
    global.WSGen.status().then(function (st) {
      var seen = {};
      if (st && st.ok) {
        (st.jobs || []).forEach(function (j) { seen[j.id] = j; });
      }
      again.forEach(function (a) {
        if (a.comic) return;                       // 漫画走自己的轮询
        var job = seen[a.rec.job];
        if (job && job.state === "queued") {
          // 计时起点用服务端的提交时间：昨天提交的那张一接回来就算超时
          a.card.since = (job.ts ? job.ts * 1000 : 0) || Date.now();
          global.WSChat.pending[a.rec.job] = a.card;
          global.WSChat.armStop(a.card);
          return;
        }
        global.WSChat.failCard(a.card,
          (job && job.error) ||
          "这一条已经没在跑了（服务重启过 / 早结束了），点重试再排一次", true);
      });
      again.forEach(function (a) {
        if (a.comic && global.WSCreate && global.WSCreate.resume) {
          global.WSCreate.resume(a.rec, a.card);
        }
      });
      global.WSChat.scrollBottom();
      if (Object.keys(global.WSChat.pending).length) global.WSChat.startPoll();
    }, function () {
      // 接口不通：不猜，接回轮询交给它自己的超时去判
      again.forEach(function (a) {
        if (a.comic) {
          if (global.WSCreate && global.WSCreate.resume) {
            global.WSCreate.resume(a.rec, a.card);
          }
          return;
        }
        a.card.since = Date.now();
        global.WSChat.pending[a.rec.job] = a.card;
        global.WSChat.armStop(a.card);
      });
      if (Object.keys(global.WSChat.pending).length) global.WSChat.startPoll();
    });
    return;
  }


  global.WSArchive = { restore: restore };
})(window);
