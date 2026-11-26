/* 聊天页存档的重放：刷新之后把说过的话、出过的图、没画完的漫画接回来。
   从 chat.js 拆出来的——那边已经六百多行，重放又只是纯 DOM 还原。
   数据存在 store.js，这里只认消息记录的形状。

   为什么不用 chat.js 的渲染函数重放：paintRef / newCard 会顺手再写一次
   存档，重放一遍记录就翻倍。这里只画最后的样子，重画这类按钮再单独接。 */
(function (global) {
  "use strict";

  /* 这个模块**不在加载时碰任何别的模块**（2026-11-26）：以前开头有一句
     `var FIT = global.WSShot.FIT;`（而且从来没被用过），于是它必须排在
     shot.js 之后才能加载。现在 gen_flow 要回来调 `attach`，两边不能再
     互相挑加载顺序——一律等到真正调用那一刻再去拿。 */

  // ── 重放 ──────────────────────────────────────────────

  /* 存档里的卡片壳。还原时不挂任何监听，只把最后的样子画出来——
     正在跑的任务由下面的 restore() 接回轮询，不能复用 newCard / paintRef，
     那些函数会顺手再写一次存档（重放一遍就等于把记录翻倍）。 */
  function restoreShell(m, cls) {
    var node = document.createElement("div");
    /* 默认是 idle 态（一行字、不占位），真挂了图 / 格子之后由调用方
       `fill()` 变回真卡片。见 chat.js 的 shell() 注释。 */
    node.className = "card is-idle" + (cls ? " " + cls : "");
    var body = document.createElement("div");
    body.className = "card-body";
    var line = document.createElement("div");
    line.className = "card-line";
    var note = document.createElement("p");
    /* 失败记录不在这里铺红字：下一轮 failCard 会把轻提示挂到用户消息下。
       直接在壳里写 m.note + err class，刷新瞬间会闪一行红字（用户
       2026-11-25：「红色的警告会刷屏」）。 */
    note.className = "note";
    note.hidden = true;
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
    line.appendChild(note);
    line.appendChild(stop);
    node.appendChild(body);
    node.appendChild(line);
    node.appendChild(acts);
    global.WSChat.box().appendChild(node);

    function fill() { node.classList.remove("is-idle"); }

    /* 登记「记录 → 卡片」：用户在这条上再点重画时，chat.js 才认得出
       上一条是同一份请求（见 findTwin），不会越点越多。 */
    return { node: node, body: body, line: line, note: note, stop: stop,
             acts: acts, fill: fill, rec: m,
             prompt: m.prompt || "", skill: m.skill || "", job: m.job || "",
             refs: m.refs || [], described: !!m.described,
             say: function (text) {
               note.hidden = false;
               note.className = "note";
               note.textContent = text;
             } };
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

     数据来源（2026-11-24，2026-11-25 收紧）：先读服务端 `/api/chat/archive`
     （按登录的 QQ 号存，换浏览器/换手机也是同一份）。**拉到什么就显示什么，
     不再往里面掺本地记录**——那是「点重画却画出另一张图」「明明出图了却说
     没跑完」的根源：补进来的无 id 旧记录按内容认人，一错位图和提示就串了。
     本机还有没推上去的，走 `WSStore.resync()` 补推，不靠掺进历史。
     服务端连不上（断网 / 5174 没起）才退回本地那份。 */
  var restoring = false;
  var restored = false;

  function restore() {
    if (restoring || restored) return;
    restoring = true;
    var local = global.WSStore && global.WSStore.load();

    /* 访客也拉服务端（2026-11-26 用户拍板）：访客有自己的档位
       `guest_<身份段>`，跑完的图由 5174 在任务结束那一刻自己写进去，
       前端在不在都一样。所以这里不再按身份分叉——本地那份只是在服务端
       拉不回来（断网 / 5174 没起）时垫底。 */
    var ready = (global.WSStore && global.WSStore.pull)
      ? global.WSStore.pull()
      : Promise.resolve(null);

    ready.then(function (d) {
      var use = (d && d.msgs && d.msgs.length) ? d.msgs : null;
      // 服务端没有、本地有：说明这台设备上有还没推上去的记录（比如刚断网
      // 生成完的那批）。照常重放本地那份，下一次 save 会把它补推上去。
      if (!use) {
        if (local && local.msgs && local.msgs.length) replay(local.msgs);
        restored = true;
        restoring = false;
        return;
      }
      replay(use);
      restored = true;
      restoring = false;
    }).catch(function () {
      restoring = false;
    });
  }

  function replay(list) {
    if (!list || !list.length) return;
    /* 去重按**内容 + 顺序**：老记录没有 id，同一句话可能真的画过好几次，
       按内容去重会把「你第二次画的同一句话」吞掉。只有相邻重复（同步时
       重复推上去的同一版）才算同一条。 */
    var prevKey = "";
    list = list.filter(function (m) {
      if (!m || !m.k) return false;
      var key;
      try { key = JSON.stringify(m); } catch (e) { return true; }
      if (key === prevKey) return false;
      prevKey = key;
      return true;
    });
    /* 清掉存档里的 `userNode`（2026-11-25）：早期提交时把**气泡 DOM 节点**
       挂进了记录，JSON.stringify 把它摊成
       `{"_wsUserId":…,"_wsUserText":…,"_failLine":null}` 一起推到了服务端。
       那串 `_wsUserText` 就是整段提示词——同一段 tags 因此在同一条记录里
       存两份，每条消息多挂几百字。
       这里就地摘掉：历史不会再被撑大，下一次 save 会把干净的那版推上去。 */
    list.forEach(function (m) { if (m) delete m.userNode; });
    var d = { msgs: list };

    /* 重放消息里的参考卡时顺手把「待发送」那张也认回来：用户没发的那张
       刷新后还得在输入栏上方等着，不然得重新去画廊点一次。只认最后一张
       ——同一条消息里挂着两三个参考图，重新挂一遍反而更乱。 */
    var lastRef = null;
    var lastRefAt = -1;
    var userNodesByText = {};  // 并行生图按记录中的用户原文找回所属气泡

    var again = [];
    d.msgs.forEach(function (m, i) {
      if (!m || !m.k) return;
      global.WSChat.msgs().push(m);

      if (m.k === "user") {
        // 发出去的那条可能带着参考图，重放时一起铺回来（用户 2026-11-23：
        // 「我那条消息应该包含参考图，因为我能够看到之前的参考图是什么」）
        var userNode = global.WSChat.paintUser(m.t, m.refs || []);
        userNode._wsUserText = m.t;
        userNode._wsUserId = m.id || "";
        if (!userNodesByText[m.t]) userNodesByText[m.t] = [];
        userNodesByText[m.t].push(userNode);
      } else if (m.k === "note") {
        global.WSChat.paintNote(m.t);
      } else if (m.k === "say") {
        /* 大大怪回的那句话（2026-11-26）：不进 `log()`，这里已经 push 过了，
           直接用 paint 那半边，否则刷新一次它会变成两条。 */
        var say = document.createElement("p");
        say.className = "say";
        say.textContent = m.t || "";
        global.WSChat.box().appendChild(say);
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
        /* 配气泡（2026-11-24 那套按原话找，这里保留并加了 userId 优先）：
           记录里存着 `userId` / `userText`，先按它们认人；认不到才退回
           「紧挨着的上一条 user」。 */
        var userMatches = userNodesByText[m.userText || m.ask || ""] || [];
        c.userNode = m.userId ? byId(userNodesByText, m.userId) : null;
        if (!c.userNode) {
          c.userNode = userMatches.length ? userMatches[userMatches.length - 1] : null;
        }
        if (!c.userNode && i > 0) {
          for (var ui = i - 1; ui >= 0; ui--) {
            if (d.msgs[ui].k === "user") {
              var fallback = userNodesByText[d.msgs[ui].t] || [];
              c.userNode = fallback[fallback.length - 1] || null;
              break;
            }
          }
        }
        if (global.WSChat.cards) global.WSChat.cards.set(m, c);
        if (m.file) {
          // 恢复历史也铺**1MB 那一档**（2026-11-24）：翻一次记录可能十几张，
          // 每张原图 2~8MB，全从家用上行出就是「打开页面等半天」的由来。
          // `FIT` 和 chat.js 里同一个值；点开只是放大这一张，不再换原图。
          var img = global.WSChat.shotEl(m.file);
          c.body.appendChild(img);
          c.fill();                    // 有图：idle 态变回真卡片
          // 重放时把 prompt / skill 一起交给 buildActs：不带的话「复制」
          // 复制的是空串、「重画」也排不上队（新版多了下载，同样要 file）
          c.img = img;
          c.file = m.file;
          global.WSChat.buildActs(c);
        } else if (m.job && !m.err) {
          /* 有 job 号、没图、没判错：**它可能还在跑**（另一台设备上刚发出
             去的那张，推上来时还没出图）。先别判死，收进 again 里问一次
             服务端，见下面那段——以前这里直接落到 else 判「没跑完就断了」，
             于是两台同时开着时，另一台一刷新就看到「这张断了，点重排」。 */
          again.push({ rec: m, card: c });
        } else if (willCheck(m)) {
          /* 没有 file 也没有 job —— 以前一律判「这条没跑完就断了」。
             但**提交到一半就被截断**的记录也长这样：点了发送、任务已经在
             服务端排上队了，页面在这一瞬间刷新，`rec.job` 还没等到回填。
             用户 2026-11-25 报的正是这个：「任务已经提交到服务端了，刷新
             回来它应该说正在跑才对」。

             所以这里不急着判死，先按提交时间问一次服务端（见下面
             `promise()`）：新鲜的、确实在跑的接回轮询；查不到、或者本来就
             是陈年记录，才说没跑完。 */
          again.push({ rec: m, card: c, orphan: true });
        } else {
          /* 没有 file 也没有 job 的 `img` 记录 = 提交过、但没等到结果就
             没了（服务重启 / 页面关太早）。存下来的 note 十有八九是
             「排队中…」，重放时如果照抄就是一张**永远排队的假卡**
             （2026-11-24 用户：「我重新进入网页，又出现了上面的这个
             排队中，我知道上面的那个肯定是假的排队状态」）。
             改成一行明确的「这条没跑完」+ 重试，别再骗人——而且不再铺
             那块 3:4 的灰底（用户 2026-11-24：「每次滑动都是一大堆的
             大卡片，影响观看」）。 */
          global.WSChat.failCard(c,
             m.err ? m.note : "这条没跑完就断了（服务重启 / 页面关太早），点重试再排一次",
             true);
        }
      } else if (m.k === "comic") {
        var cc = restoreShell(m, "card-comic");
        if (global.WSCreate && global.WSCreate.repaint) {
          global.WSCreate.repaint(cc, m);
        }
        if (m.task && !m.done) again.push({ rec: m, card: cc, comic: true });
      } else if (m.k === "book") {
        /* 书卡（「打开整本」在聊天里落下的那张入口）：重放只铺壳，不压子页。
           点它才进整本——跟当初点「打开整本」是同一条路，见 create.js
           openBook / restoreBook。 */
        if (global.WSCreate && global.WSCreate.restoreBook) {
          global.WSCreate.restoreBook(m);
        }
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
      /* 重放完落到最底并**钉一小会儿**（用户 2026-11-25：「每次进入浏览器，
         默认就是来到最底下」）。图是懒加载的，落位之后高度还会变。 */
      if (global.WSChat.pinBottom) global.WSChat.pinBottom();
      else global.WSChat.scrollBottom();
      return;
    }
    /* `seen` 要拿到两段 then 之间用（认领会往里补一条），所以先声明在外面 */
    var seen = {};
    global.WSGen.status().then(function (st) {
      if (st && st.ok) {
        (st.jobs || []).forEach(function (j) { seen[j.id] = j; });
      }
      /* 「提交到一半」那几条先去按时间认领：认到了就把服务端真实的任务号
         补回记录上，后面跟普通那条走同一条路（出图 / 接回轮询 / 判死）。 */
      var claims = again.map(function (a) {
        if (!a.orphan) return Promise.resolve();
        return promise(a.rec).then(function (job) {
          if (!job) return;
          a.rec.job = job.id;
          a.card.job = job.id;
          seen[job.id] = job;
        });
      });
      return Promise.all(claims).then(function () { return st; });
    }).then(function (st) {
      again.forEach(function (a) {
        if (a.comic) return;                       // 漫画走自己的轮询
        var job = seen[a.rec.job];
        if (job && job.state === "done" && job.file) {
          global.WSChat.showImage(a.card, job);
          return;
        }
        /* 认领过、服务端也确实有它（还在跑）→ 接回轮询。原来那条
           `job.state !== "error"` 的分支要求「job 存在」，认领回来的正好
           落在这里；没认到（服务端没这张）就继续往下判死。 */
        if (!st || !st.ok || (job &&
            job.state !== "error" && job.state !== "cancelled")) {
          // 计时起点用服务端的提交时间：昨天提交的那张一接回来就算超时
          a.card.since = (job && job.ts ? job.ts * 1000 : 0) || Date.now();
          global.WSChat.pending[a.rec.job] = a.card;
          global.WSChat.armStop(a.card);
          return;
        }
        global.WSChat.failCard(a.card,
          (job && job.error) ||
          (a.orphan
            ? "这条没跑完就断了（服务重启 / 页面关太早），点重试再排一次"
            : "这一条已经没在跑了（服务重启过 / 早结束了），点重试再排一次"),
          true);
      });
      /* 服务端说不了话（5174 刚起 / 网络抖）时，上面那段按「不猜」先接回
         轮询；但轮询的第一步就是「任务不在列表里 → 判它没了」，刚接回来的
         那一下必然判死。所以状态接口告假的时候，先别接轮询，也别判死：
         留着「正在跑…」等下一次刷新或下一次提交把它接上——这比给用户一句
         假的「服务重启过，这张没了」诚实得多。 */
      again.forEach(function (a) {
        if (a.comic && global.WSCreate && global.WSCreate.resume) {
          global.WSCreate.resume(a.rec, a.card);
        }
      });
      if (global.WSChat.pinBottom) global.WSChat.pinBottom();
      else global.WSChat.scrollBottom();
      if (Object.keys(global.WSChat.pending).length) global.WSChat.startPoll();
    }, function () {
      // 接口不通：不猜，卡面留在「正在跑…」，不接轮询（原因见上）
      again.forEach(function (a) {
        if (a.comic) {
          if (global.WSCreate && global.WSCreate.resume) {
            global.WSCreate.resume(a.rec, a.card);
          }
          return;
        }
        a.card.since = 0;
        a.card.note.hidden = false;
        a.card.note.className = "note";
        a.card.note.textContent = "正在跑，画好会自动弹出来";
        a.card.noted = "正在跑，画好会自动弹出来";
      });
      if (Object.keys(global.WSChat.pending).length) global.WSChat.startPoll();
    });
    return;
  }


  /* 往回翻更早的历史（「加载更早」按钮）。服务端一次只给尾部一页，这里要
     一整段回来，扣掉页面上已有的（按内容比），剩下的接在**最上面**，并且
     只把新来的这批交给 replay（它会把参考图、轮询都接回去，顺序按传入的
     先后走）。→ Promise<条数> */
  function older() {
    if (!global.WSStore || !global.WSStore.earlier) {
      return Promise.resolve(0);
    }
    var have = {};
    global.WSChat.msgs().forEach(function (m) {
      var k = keyOf(m);
      if (k) have[k] = 1;
    });
    return global.WSStore.earlier().then(function (all) {
      if (!all || !all.length) return 0;
      var add = all.filter(function (m) {
        var k = keyOf(m);
        return k && !have[k];
      });
      if (!add.length) return 0;
      replay(add);
      return add.length;
    });
  }

  function keyOf(m) {
    try { return m ? JSON.stringify(m) : ""; } catch (e) { return ""; }
  }

  /* 「提交到一半被截断」的记录还值不值得去服务端问一次？

     判据只有时间，而且给得很紧（`FRESH_MS`）：提交后这么久都没等到回填
     job 号的，基本就是页面关掉了、或者服务重启把任务弄丢了——那时候再挂
     一句「正在跑」是骗人，用户第二天回来看见一条转了半天的假卡更糟。
     反过来，刚点完发送就刷新的那种，一定落在这个窗口里。

     老记录（连 userId 都没有的 `old_*`）不问：那些是上个月的事，问了也
     只是白打一次接口。 */
  var FRESH_MS = 10 * 60 * 1000;

  function bornAt(m) {
    /* 记录 id 是 `img` + Date.now().toString(36) + `_序号`（见 chat.js 的
       newId），时间戳就藏在里面——这是唯一一个不需要额外存字段的出生时间。

       ⚠️ 不能拿 `/^[a-z]+/` 去剥前缀：base36 的时间戳里本来就有字母
       （1795596642639 → `mwvvuic1s`），那样会把 `mwv` 一起吃掉，解析出来
       只剩两位数。所以只剥**已知的**那几个前缀词，再按长度兜一道。 */
    var id = String((m && m.id) || "");
    var head = id.split("_")[0].replace(/^(img|comic|note|ref|user|u|m)/i, "");
    if (head.length < 8) return 0;            // 太短，不可能是毫秒时间戳
    var t = parseInt(head, 36);
    return isFinite(t) && t > 1e12 ? t : 0;
  }

  function willCheck(m) {
    if (!m || m.k !== "img" || m.file || m.err || m.job) return false;
    var t = bornAt(m);
    return !!t && (Date.now() - t) < FRESH_MS;
  }

  /* 「提交到一半」那条：去服务端按**提交时间**认领一个还在跑的任务。

     不加这个接口就只能靠猜——要么一律判死（用户现在遇到的），要么一律
     说「正在跑」（第二天回来看见假卡）。服务端手里有 `_JOBS` 的完整
     记录（提交时间、所有者、有没有已经出图），让它自己说这条到底还
     在不在，比前端猜准得多。 */
  function promise(rec) {
    var t = bornAt(rec);
    if (!t) return Promise.resolve(null);
    return global.WSGen.adopt(t).then(function (d) {
      return (d && d.ok && d.job) ? d.job : null;
    })["catch"](function () { return null; });
  }

  /* 按 userId 在「原话 → 气泡」那张表里找人。表是按内容分桶的，这里全扫
     一遍：一条消息的气泡只可能有一个，量级是几十个，不值得再建索引。 */
  function byId(table, uid) {
    var hit = null;
    Object.keys(table).some(function (k) {
      var found = (table[k] || []).filter(function (n) {
        return n._wsUserId === uid;
      })[0];
      if (found) hit = found;
      return !!found;
    });
    return hit;
  }

  // ── 接回一条真任务（2026-11-26）────────────────────────
  //
  // 这两段原来在 gen_flow.js，那边贴着 600 行硬线；而且它们本来就是「刷新 /
  // 断线之后把状态接回来」那一类，和 `restore()` / `promise()` 是同一条命。

  var NOTE_RUN = "正在跑，画好会自动弹出来";
  /* 提交没拿到应答时的那句话。措辞刻意不说「失败」：服务端很可能已经收到
     了，只是这一发被系统掐断。 */
  var NOTE_UNKNOWN = "网络断了，正在确认这张有没有排上";

  /* 把一张卡接到一条**服务端确有其事**的任务上：挂牌 → 进 pending → 开始
     轮询。`fire()` 拿到回执走这里，断网后按时间认领回来也走这里——两处要是
     各写一份，早晚一处改了一处没改（「图画好了、卡片还在转圈」就是这么来的）。 */
  function attach(card, rec, job) {
    if (!card || !rec || !job || !job.id) return false;
    card.job = job.id;
    rec.job = job.id;
    rec.err = false;
    rec.note = NOTE_RUN;
    /* 复制拿的是**实际出这张图用的那串提示词**，不是用户敲的那句话
       （2026-11-24 用户：「点击复制，结果拿到的是我发送给 ai 的原始需求，
       而不是 ai 生成这个图片使用的英文提示词 tags」）。服务端在这一趟里做完
       了翻译 / 画师串 / 参考图合并，`job.prompt` 就是最终进模型的那串。 */
    if (job.prompt) {
      card.prompt = job.prompt;
      rec.prompt = job.prompt;
    }
    if (global.WSChat) {
      global.WSChat.pending[card.job] = card;
      global.WSChat.armStop(card);
      global.WSChat.paintNote(card, NOTE_RUN);
      if (card.say) card.say(NOTE_RUN, false, true);
      if (global.WSStore) {
        global.WSStore.patch(rec, { job: rec.job, prompt: rec.prompt,
                                    note: rec.note, err: false });
        global.WSStore.save({ msgs: global.WSChat.msgs() });
      }
    }
    if (global.WSGenFlow && global.WSGenFlow.kick) global.WSGenFlow.kick();
    return true;
  }

  /* 提交那一发**没拿到应答**（断网 / 页面被切走 / 隧道抖）→ 去问一句「这张
     到底排上了没有」。

     为什么不直接判失败：被掐断的那一刻请求很可能已经到了服务端，任务正在
     排队甚至已经在画。写成 `err` 落进档位，用户回来看到的就是「明明出图了
     却说没跑完」，而且再也不会有人去认领它（用户 2026-11-26 实测：图在
     output 里好好的，回来只有一句「网络断了」）。

     做法：标「结果还没确认」（**不打 err**），拿记录出生时间去 `adopt`；认到
     就接回轮询，认不到才说没排上。等几秒是为了让服务端先把它落进队列。 */
  function revive(card, rec) {
    if (!card || !rec) return;
    rec.note = NOTE_UNKNOWN;
    rec.err = false;
    if (global.WSChat) {
      global.WSChat.paintNote(card, NOTE_UNKNOWN);
      if (global.WSStore) {
        global.WSStore.patch(rec, { job: "", note: NOTE_UNKNOWN, err: false });
        global.WSStore.save({ msgs: global.WSChat.msgs() });
      }
    }
    var at = bornAt(rec);
    if (!at) {
      global.WSChat.failCard(card, "提交没成功，检查网络后点重试", true);
      return;
    }
    setTimeout(function () {
      global.WSGen.adopt(at).then(function (d) {
        if (!d || !d.ok || !d.job) {
          global.WSChat.failCard(card, "这张没排上（网络断了），点重试再发一次",
                                 true);
          return;
        }
        attach(card, rec, d.job);
      }, function () {
        /* 连问都问不到（还是断网）：留在「结果还没确认」。**不判死**——
           下次进页面 `willCheck` 还会再认一次，服务端那边已经会自己把
           结果盖回档位。 */
      });
    }, 4000);
  }

  /* `bornAt` 也对外给：拿到应答之前就断掉的那次提交要靠它去认领，
     两处算法必须是同一份。 */
  global.WSArchive = { restore: restore, replay: replay, older: older,
                       bornAt: bornAt, attach: attach, revive: revive };
})(window);
