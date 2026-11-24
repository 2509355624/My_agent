/* 聊天页存档的重放：刷新之后把说过的话、出过的图、没画完的漫画接回来。
   从 chat.js 拆出来的——那边已经六百多行，重放又只是纯 DOM 还原。
   数据存在 store.js，这里只认消息记录的形状。

   为什么不用 chat.js 的渲染函数重放：paintRef / newCard 会顺手再写一次
   存档，重放一遍记录就翻倍。这里只画最后的样子，重画这类按钮再单独接。 */
(function (global) {
  "use strict";

  /* 列表里铺的那一档：和服务端 `/api/gen/img?fit=` 对齐（长边从大往小试、
     取第一张 ≤1MB）。和 chat.js 里的 `FIT` 是同一个值，两边改一处必须一起改。 */
  var FIT = "1m";

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
     没出图的那条在重放完统一接回轮询（生图走 status，漫画走自己的轮询）。

     数据来源（2026-11-24）：先读服务端 `/api/chat/archive`（按登录的 QQ 号
     存，换浏览器/换手机也是同一份），拿不到再退回本地那份。服务端那份是
     正本——以前只认 localStorage，换设备就是一片空白。 */
  function restore() {
    var local = global.WSStore && global.WSStore.load();

    var ready = (global.WSStore && global.WSStore.pull)
      ? global.WSStore.pull()
      : Promise.resolve(null);

    ready.then(function (d) {
      var use = (d && d.msgs && d.msgs.length) ? d.msgs : null;
      // 服务端没有、本地有：说明这台设备上有还没推上去的记录（比如刚断网
      // 生成完的那批）。照常重放本地那份，下一次 save 会把它补推上去。
      if (!use) {
        if (local && local.msgs && local.msgs.length) replay(local.msgs);
        return;
      }
      /* 服务端有：以它为准。本地多出来的那几条（比服务端新）也接着放，
         不然「另一台刚画完、后台还没推上去」的那张会在这台上凭空消失。 */
      var extra = [];
      if (local && local.msgs && local.msgs.length) {
        var known = {};
        use.forEach(function (m) { known[JSON.stringify(m)] = 1; });
        extra = local.msgs.filter(function (m) {
          return !known[JSON.stringify(m)];
        });
        // 只有本地最后几条算「新的」，老的那些多半是本地还没被服务端覆盖
        if (extra.length > 20) extra = [];
      }
      replay(use.concat(extra));
    });
  }

  function replay(list) {
    if (!list || !list.length) return;
    var d = { msgs: list };

    /* 重放消息里的参考卡时顺手把「待发送」那张也认回来：用户没发的那张
       刷新后还得在输入栏上方等着，不然得重新去画廊点一次。只认最后一张
       ——同一条消息里挂着两三个参考图，重新挂一遍反而更乱。 */
    var lastRef = null;
    var lastRefAt = -1;

    var again = [];
    d.msgs.forEach(function (m, i) {
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
        /* 只认**归档末尾**那条：`k:"ref"` 是「还没发出去」的回执，正常情况
           下它只可能挂在最后（发出去之后 commit 就把它删了，见 ref.js）。
           中间还夹着别的消息，说明那几张早用掉了，认回来等于凭空冒出一张。
           所以这里记下位置，循环结束后只有「它就在末尾」才算数。 */
        lastRef = m;
        lastRefAt = i;
      } else if (m.k === "img") {
        var c = restoreShell(m);
        if (m.file) {
          // 恢复历史也铺**1MB 那一档**（2026-11-24）：翻一次记录可能十几张，
          // 每张原图 2~8MB，全从家用上行出就是「打开页面等半天」的由来。
          // `FIT` 和 chat.js 里同一个值；点开只是放大这一张，不再换原图。
          var img = global.WSChat.shotEl(m.file);
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
          /* 没有 file 也没有 job 的 `img` 记录 = 提交过、但没等到结果就
             没了（服务重启 / 页面关太早）。存下来的 note 十有八九是
             「排队中…」，重放时如果照抄就是一张**永远排队的假卡**
             （2026-11-24 用户：「我重新进入网页，又出现了上面的这个
             排队中，我知道上面的那个肯定是假的排队状态」）。
             改成一张明确的「这条没跑完」+ 重试，别再骗人。 */
          global.WSChat.failCard({
            node: c.node, body: c.body, ph: ph, note: c.note,
            stop: c.stop, acts: c.acts,
            prompt: m.prompt || "", skill: m.skill || "", job: "", rec: m,
            refs: m.refs || [], described: !!m.described
          }, "这条没跑完就断了（服务重启 / 页面关太早），点重试再排一次",
             true);
        }
      } else if (m.k === "comic") {
        var cc = restoreShell(m, "card-comic");
        if (global.WSCreate && global.WSCreate.repaint) {
          global.WSCreate.repaint(cc, m);
        }
        if (m.task && !m.done) again.push({ rec: m, card: cc, comic: true });
      }
    });

    /* 只有它真的在归档末尾才算「还没发出去」；夹在中间的是陈年记录。 */
    if (lastRef && lastRefAt === d.msgs.length - 1 && global.WSRef) {
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
