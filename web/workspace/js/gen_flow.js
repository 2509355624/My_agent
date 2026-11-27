/* 生图这一趟「干活的」部分：提交、轮询、判超时、挂/摘「终止」。
   这里不画任何东西——卡片长相在 card.js，图在 shot.js，存档怎么落由 chat.js
   的回调决定。两条硬口径：同一份词连点「重画」不许越点越多张空卡；排队中 /
   报错只是一行字，不占 3:4 的大壳（is-idle，见 card.js）。 */

(function (global) {
  "use strict";

  var POLL_MS = 2000, NOTE_RUN = "正在跑，画好会自动弹出来";
  var MAX_FAIL = 5;         // 连续失败这么多次就停轮询，别无限打接口
  /* 卡死兜底：服务重启后存档那条会永远停在「排队中」，前端自己计时，
     排队 / 准备中超 SETTLE_MS 判卡住，给「重试」。 */
  var SETTLE_MS = 8 * 60 * 1000;
  /* status 里查不到这张时的宽容期（两台设备开着，一台刷新不该判死） */
  var GHOST_MS = 15 * 1000;
  var PAINT_MS = 25 * 60 * 1000;

  /* chat.js 注入的回调（都必需：少一个这里就该报错，不能静默死循环） */
  var H = {
    state: null,      // () → {pending, timer, fails, msgs, cards}
    save: null,       // () → 落盘存档
    patch: null,      // (rec, fields) → 回写服务端那条
    newCard: null,    // (prompt, skill, rec) → 卡片
    spawn: null,      // (card) → 挂 3:4 占位
    showImage: null,  // (card, job) → 出图
    fail: null,       // (card, msg, stuck) → 失败态
    sysNote: null,
    newId: null,      // (prefix) → 记录稳定 id（存档按它原位更新）
    busyTip: null,    // 限流这类一次性提示：闪一下就消失，不落存档
    dropCard: null,   // 撤掉一张没排上队的临时卡（连用户气泡一起）
    scroll: null,     // () → 滚到底（出图 / 判失败之后）
    say: null,        // (text) → 大大怪说的一句话（见 sayBack）
    redraw: null      // (card) → 重画（失败提示上那颗按钮）
  };

  function bind(hooks) {
    Object.keys(hooks || {}).forEach(function (k) {
      if (k in H) H[k] = hooks[k];
    });
  }

  function S() { return H.state(); }

  /* 写卡片上的状态行（带不带转圈由 `run` 定，见 card.js 的 setNote）。
     `card.noted` 记最后一次写进去的那句，用来避免每 2 秒重置一次动画。 */
  function paintNote(card, text, bad, run, queue) {
    if (!card || !card.note) return;
    /* `queue` 是**徽章文字**（"正在排队" / "正在画了" / "正在跑"），false = 不用
       胶囊。比较要连徽章一起比：正文没变、徽章变了（排队 → 开画）也得重画。 */
    var q = queue === true ? "正在排队" : (queue || "");
    if (card.noted !== text || String(card.notedQ || "") !== q) {
      if (global.WSCard && global.WSCard.setNote) {
        global.WSCard.setNote(card.note, text,
                             { bad: !!bad, run: run !== false, queue: queue });
      } else {
        card.note.hidden = false;
        card.note.className = "note" + (bad ? " err" : "") + (q ? " queue" : "");
        card.note.textContent = text;
      }
      card.noted = text;
      card.notedQ = q;
    }
  }

  /* ── 卡片收尾：出图 / 失败（2026-11-25 从 chat.js 搬过来）

     这两件事本来就是「一张卡走到终点」的收尾，跟轮询/判超时是同一条命；
     放在 chat.js 那边只能靠 `H.showImage` / `H.fail` 两个钩子回跳，绕一圈
     还让那边顶着 600 行上限。搬过来之后 `H` 上那两个位置只留「测试或别处
     想改口径时覆盖」的作用，不注入就用这里默认的实现。 */

  /* 出图：把占位块换成真图，记下 file，挂上三个按钮。 */
  function showImage(card, job) {
    var img = global.WSShot.shotEl(job.file);
    img.addEventListener("error", function () {
      failUserLine(card, "图不在了，点重画再来一次");
    });
    if (card.ph) card.ph.parentNode.replaceChild(img, card.ph);
    else card.body.appendChild(img);
    card.ph = null;
    card.img = img;                 // 留个引用：下载/预览都靠它
    if (card.fill) card.fill();     // 有图了：空壳变回真卡片
    card.note.hidden = true;
    /* 把状态行**彻底**收掉：隐藏之外还要摘掉 `running`（那一类带着转圈
       图标的动画）和转圈图标本身。只 hidden 的话，下一轮 tick 万一又把这张
       卡捞回来，画出来的第一帧还是「图标在转」的样子——用户看到的就是
       「图已经出来了，卡片还在转圈」。 */
    if (global.WSCard && global.WSCard.setNote) {
      global.WSCard.setNote(card.note, "", { run: false });
      card.note.hidden = true;
    }
    if (card.stop) card.stop.hidden = true;
    if (card.userNode && global.WSCard) {
      global.WSCard.clearFailLine(card.userNode);
    }
    card.file = job.file || "";
    card.since = 0;
    card.noted = "";
    buildActs(card);
    if (card.rec) {
      card.rec.file = job.file;
      card.rec.note = "";
      card.rec.err = false;
      /* 先在服务端存档里把这条从「排队中」改成「已出图」，再清掉 job：
         只清本地不写服务端的话，换设备登录读回来的还是「排队中…」
         （用户 2026-11-24 看到的那张假卡就是这样留下的）。 */
      if (H.patch) H.patch(card.rec, { file: job.file, note: "", err: false,
                                       job: "" });
      card.rec.job = "";
      if (H.save) H.save(null);
    }
    if (H.scroll) H.scroll();
  }

  /* 失败 / 超时：卡住给重试，普通失败给重画。 */
  function fail(card, msg, stuck) {
    disarmStop(card);
    card.since = 0;
    card.noted = "";
    /* 失败提示只挂在用户那条消息下面、且只留一条（用户 2026-11-25：
       「红色警告会刷屏……点重画按钮就消失」）。AI 卡片这边不再写红字、
       不再放重试按钮——反复点重画时原来每次都会刷一行红字 + 一颗按钮。 */
    card.note.hidden = true;
    card.note.textContent = "";
    /* 同 showImage：收尾就把转圈动画类一起摘掉，别留一个「停不下来」的容器 */
    if (global.WSCard && global.WSCard.setNote) {
      global.WSCard.setNote(card.note, "", { run: false });
      card.note.hidden = true;
    }
    if (card.acts) { card.acts.innerHTML = ""; card.acts.hidden = true; }
    failUserLine(card, msg || "这张没画出来，点重画再来一次");
    /* 没出图的卡退回空闲态，别留 3:4 的大空壳；出过图的留图不动。 */
    if (card.ph) {
      card.ph.remove();
      card.ph = null;
      if (card.node) card.node.classList.add("is-idle");
    }
    if (card.rec) {
      card.rec.note = msg || "这张没画出来";
      card.rec.err = true;
      if (H.patch) H.patch(card.rec, { job: card.rec.job || "",
                                       note: card.rec.note, err: true });
      card.rec.job = "";
      if (H.save) H.save(null);
    }
    if (H.scroll) H.scroll();
  }

  /* 大大怪回了一句话（2026-11-26 用户拍板：「聊天气泡回一句」）。

     这张图卡没排上任务，留着只会是个空壳——撤掉它，把用户那条气泡留下来
     （他确实说了这句话），下面接一条 AI 消息。存档里把这条 img 记录换成
     `k:"say"`，刷新后还是一问一答的样子，而不是「说了句话、卡片没了」。 */
  function sayBack(card, rec, text) {
    disarmStop(card);
    if (card && card.rec) card.rec.job = "";
    /* ⚠️ 不能用 `H.dropCard`（那是 redraw.drop）：它会把用户那条气泡一起
       删掉。这里用户是**说了话的**，只是这句不用画，气泡得留着——否则
       屏幕上只剩大大怪一句没头没尾的话。 */
    if (card && card.node && card.node.parentNode) {
      card.node.parentNode.removeChild(card.node);
    }
    if (rec) {
      var at = S().msgs.indexOf(rec);
      if (at >= 0) {
        S().msgs.splice(at, 1);
        if (H.save) H.save(null);
      }
      if (S().cards) S().cards["delete"](rec);
    }
    if (H.say) H.say(text);
    if (H.scroll) H.scroll();
  }

  /* 把失败提示贴到这张卡对应的用户消息下。找不到（存档重放还没配对）
     就不画——宁可少一条提示，也不刷屏。点重画时由重排流程撤掉它。 */
  function failUserLine(card, msg) {
    var host = card.userNode;
    if (!host || !global.WSCard) return;
    global.WSCard.failLine(host, msg, function () {
      if (card && H.redraw) H.redraw(card);
    });
  }

  /* 出图后那三个按钮（「重画 / 下载」的落点在 chat.js），这里只转发。 */
  function buildActs(card) {
    if (global.WSChat && global.WSChat.buildActs) global.WSChat.buildActs(card);
  }

  // ── 提交 ──────────────────────────────────────────────

  /* refs：这一轮要带上的参考图 id。生图接口本身是按描述出图，参考图随请求
     一起交给后端（它按渠道能力决定用不用），前端不自己拼提示词。
     `described`（2026-11-24）：这些参考图**已经读成描述写进 prompt 了**
     （「画同款」就是这种）。带上它，后端就不再识一次图——用户确认过的
     口径是「不再识，用输入栏那段」。 */
  /* `route`（2026-11-26）：走哪条接口。
       - 默认 `"submit"`：翻译后直通。「词已经定了、就是要这张图」的请求
         （重画 / 画同款 / 失败重排）走它，不再让模型判一遍意图。
       - `"ask"`：聊天页那句话，交给大大怪判意图——跟 QQ 私聊同一条转译、
         同一份 6 轮历史。它可能回一句「这轮不用画」，那时卡片撤掉、
         把那句显示成一条 AI 消息（用户 2026-11-26 拍板选的那条口径）。 */
  function submit(prompt, skill, refs, described, userNode, defer, finalPrompt,
                  route) {
    if (!skill) {
      H.sysNote("还没拿到渠道列表，去跑图台看一眼再回来");
      return null;
    }
    /* 每条消息独立提交，数量限制由后端按用户执行。 */
    var rec = { k: "img", cls: "", prompt: prompt, ask: prompt, skill: skill,
                id: H.newId ? H.newId("img") : "",
                refs: (refs || []).slice(),
                described: !!described,
                route: route === "ask" ? "ask" : "",
                userId: userNode ? (userNode._wsUserId || "") : "",
                userText: userNode ? (userNode._wsUserText || "") : "",
                job: "", file: "", note: "正在提交…", err: false };
    var card = H.newCard(prompt, skill, rec);
    card.rec = rec; S().cards.set(rec, card);
    // 卡片自己记住参考图：点「重画」要按同一份角色重来，不能悄悄丢图
    card.refs = rec.refs;
    card.described = !!described;
    if (userNode) card.userNode = userNode;
    /* 气泡节点只挂在卡片上，**不进记录**（2026-11-25 修）：记录会被
       JSON.stringify 推到服务端存档，DOM 节点一旦挂进去，存档里就多出一份
       `{"_wsUserId":…,"_wsUserText":…,"_failLine":null}`——那串 `_wsUserText`
       正是整段提示词，用户随手一问「反推」就能把 500 字的 tags 存两遍。
       存档里只有 userId / userText 这两个字符串，见下面几行。 */
    card.since = Date.now();       // 超时从这里起算，见 tick()
    card.noted = "正在提交…";
    rec.userText = userNode ? (userNode._wsUserText || "") : "";
    rec.userId = userNode ? (userNode._wsUserId || "") : "";
    rec.finalPrompt = !!finalPrompt;
    H.save(rec);
    if (defer) { card.submitting = true; return card; }
    fire(card, rec, refs, described);
    return card;     // 只有真新建了卡片，调用方才该留下那条蓝色消息
  }

  function continueSubmit(card, refs, described) {
    if (!card || !card.rec || S().cards.get(card.rec) !== card) return null;
    fire(card, card.rec, refs || card.rec.refs, described);
    return card;
  }

  /* 复用一张已有的卡再排一次（restore 与「失效了就重排」还留着）。
     注意：用户点「重画」不走这里——那条路径一律在列表底部新开一张，
     见 redraw.js 头注释（2026-11-25 用户实测的 bug）。 */
  function restart(card, rec, refs, described) {
    if (refs && refs.length) {
      rec.refs = refs.slice();
      rec.described = !!described;
    }
    if (!card) {
      var fresh = H.newCard(rec.prompt, rec.skill, rec);
      fresh.rec = rec;
      fresh.refs = rec.refs || [];
      fresh.described = !!rec.described;
      /* `rec.userNode` 是**存档里的旧版**（早期代码把 DOM 节点写进了记录，
         见 submit 里的注释），不认它；气泡由调用方按 userId 重新配。 */
      fresh.userNode = null;
      fresh.since = Date.now();
      S().cards.set(rec, fresh);
      rec.job = "";
      rec.file = "";
      rec.note = "正在提交…";
      rec.err = false;
      fire(fresh, rec, rec.refs, rec.described);
      return;
    }
    if (card.userNode) global.WSCard.clearFailLine(card.userNode);
    card.refs = rec.refs || [];
    card.described = !!rec.described;
    card.since = Date.now();
    card.noted = "正在提交…";
    rec.job = "";
    rec.file = "";
    rec.note = "正在提交…";
    rec.err = false;
    if (card.img) { card.img.remove(); card.img = null; }
    card.acts.innerHTML = "";
    card.acts.hidden = true;
    card.say("排上队就告诉你", false, true, "正在提交");   // 等判意图这段也是胶囊
    global.WSCard.flash(card);
    H.save(rec);
    H.patch(rec, { job: "", file: "", note: "正在提交…", err: false });
    fire(card, rec, rec.refs, rec.described);
  }

  /* 真正把请求发出去（submit / restart 共用）。

     `rec.route === "ask"` 时走 `/api/gen/ask`：那是聊天页那句话，交给
     大大怪判意图（跟 QQ 私聊同一条转译、同一份 6 轮历史）。它可能回一句
     「这轮不用画」——那时这张卡不该留（用户没要到图，留个空壳只会碍事），
     把那句话播成一条 AI 消息，并把这条记录标成 `k:"say"` 收进存档。 */
  function fire(card, rec, refs, described) {
    card.submitting = true; card.say("排上队就告诉你", false, true, "正在提交");
    var ask = rec.route === "ask";
    var body = { skill: card.skill, prompt: card.prompt };
    if (refs && refs.length) {
      body.refs = refs;
      // 读过了就别让后端再读一遍，也别把同一段描述拼第二遍
      if (described) body.ref_described = true;
    }
    if (rec.finalPrompt) body.prompt_is_final = true;
    /* 这条记录自己的 id 一起交上去（2026-11-26）：任务结束那一刻服务端要
       靠它把图盖回档位。页面关掉了、轮询那一发没了，图也不会丢。 */
    if (rec.id) body.rec = rec.id;
    // ask 那条路吃的是**用户原话**（模型靠它判意图、判是不是在改上一张），
    // 所以把 `ask` 一起带上；submit 那条路不看它，多带一个字段无副作用。
    if (ask) body.text = rec.ask || card.prompt;
    var sent = ask ? global.WSGen.ask(body) : global.WSGen.submit(body);
    sent.then(function (d) {
      card.submitting = false;
      if (d && d.ok && d.say && d.reply) {
        sayBack(card, rec, d.reply);
        return;
      }
      if (!d || !d.ok) {
        /* 限流（还在画 / 人太多）：只是一次性提示，不落失败行、不留卡。
           否则连点几下就挂一摞「上一张还在画」的红字，越积越多。 */
        if (d && d.busy) {
          if (H.dropCard) H.dropCard(card, rec);
          if (H.busyTip) H.busyTip(d.error || "前面还有图在画，等一下再发");
          return;
        }
        /* 没拿到应答 ≠ 服务端说不行（2026-11-26）。这条必须先分流，否则
           一次断网就把「其实已经排上队、甚至已经出图」的那张写成了失败卡
           ——用户实测：图在 output 里好好的，回来只有一句「网络断了」。 */
        if (d && d.offline) {
          if (global.WSArchive && global.WSArchive.revive) {
            global.WSArchive.revive(card, rec);
          } else {
            fail(card, "提交没成功，检查网络后点重试", true);
          }
          return;
        }
        fail(card, (d && d.error) || "没排上队");
        return;
      }
      if (!d.job || !d.job.id) {
        // 接口说 ok 却没给任务号，也没给一句回话：不能留一张永远转圈的卡。
        fail(card, "没排上队，重发一次试试");
        return;
      }
      /* 访客额度（2026-11-26）：服务端在提交那一刻扣，回执里带 `left`。
         这里只做显示，不做判断——额度真不够时上面那条 `!d.ok` 已经接住了。 */
      if (d.job.visitor && global.WSVisitor) {
        global.WSVisitor.syncLeft(d.job.visitor_left);
      }
      /* 挂牌 → 进 pending → 开始轮询：和「断网后按时间认领回来」是同一段，
         放在 archive.js 的 `attach`（两处各写一份，早晚一处改了一处没改）。 */
      global.WSArchive.attach(card, rec, d.job);
    })["catch"](function () {
      card.submitting = false;
      /* 这一支是**代码自己炸了**（不是网络），照旧判失败——真没排上。 */
      fail(card, "提交失败，检查网络后点重试", true);
    });
  }

  // ── 终止 ──────────────────────────────────────────────

  /* 挂上「终止」：只有在卡片真的还挂着任务时才露出来（出图 / 失败时
     `disarmStop` 收起来）。点了就把本台的 job 取消——服务端会把排队中那张
     摘掉、正在跑那张定向掐停，并且**不投递**。 */
  function armStop(card) {
    var b = card.stop;
    if (!b) return;
    b.hidden = false;
    b.disabled = false;
    b.textContent = "终止";
    b.onclick = function () {
      if (!card.job) { b.hidden = true; return; }
      b.disabled = true;
      b.textContent = "停止中…";
      global.WSGen.cancel(card.job).then(function (d) {
        if (!d || !d.ok) {
          b.disabled = false;
          b.textContent = "终止";
          if (d && /结束了/.test(d.error || "")) disarmStop(card);
          return;
        }
        card.note.hidden = false;
        paintNote(card, "正在停，稍等…");
      })["catch"](function () {
        b.disabled = false;
        b.textContent = "终止";
      });
    };
  }

  function disarmStop(card) {
    if (card.stop) { card.stop.hidden = true; card.stop.onclick = null; }
    card.job = "";
  }

  // ── 轮询 ──────────────────────────────────────────────

  function startPoll() {
    if (S().timer) return;
    S().fails = 0;
    S().timer = setInterval(tick, POLL_MS);
  }

  function stopPoll() {
    if (S().timer) { clearInterval(S().timer); S().timer = null; }
  }

  /* 把一张卡从「还在跑」里摘干净（按卡片认，不按 id 认）。

     为什么不能只 `delete S().pending[id]`：同一张卡可能同时挂在多个
     id 下（认领回来时会换一次号，`restore` 也会按它自己的键塞进去），
     只删一个键，剩下的那个下一次 tick 还会把它捞出来，于是图出来了卡片
     仍在转圈。这里按对象身份清，一次清干净。 */
  function forget(card) {
    var pending = S().pending;
    Object.keys(pending).forEach(function (k) {
      if (pending[k] === card) delete pending[k];
    });
    card.since = 0;
    card.job = "";
  }

  /* 状态接口连续失败时别再打接口，但**不要把卡片判死**：网络抖一下不等于
     这张图没了。把它们的计时按下暂停（`since` 往后推），等下一次状态恢复
     再接着算——否则一次断网就能把半个小时的画白等。 */
  function pauseClock() {
    var now = Date.now(), pending = S().pending;
    Object.keys(pending).forEach(function (id) {
      var card = pending[id];
      if (card.since) card.since = now - (card.since - now);
    });
  }

  /* 判超时。两条线：
       · 一直没轮到（排队 / 准备中 / 还没进 ComfyUI）超过 SETTLE_MS；
       · 已经在画（running）超过 PAINT_MS。
     判了不是「报错完事」：卡片换成「重试」，用户点一下就按同一份提示词重排。
     这一步也不能把「画得慢」误杀——真在画的那张只看它自己的 PAINT_MS。 */
  function checkTimeout(card, ph) {
    if (!card.since) return false;
    var limit = (ph === "running") ? PAINT_MS : SETTLE_MS;
    if (Date.now() - card.since < limit) return false;
    var mins = Math.round(limit / 60000);
    fail(card, (ph === "running")
      ? "等了 " + mins + " 分钟还在画，可能卡住了。点重试重排一张"
      : "排队等了 " + mins + " 分钟还没轮到，卡住了。点重试重排一张", true);
    forget(card);
    return true;
  }

  function tick() {
    var S0 = S();
    if (!Object.keys(S0.pending).length) { stopPoll(); return; }
    global.WSGen.status().then(function (d) {
      if (!d || !d.ok) {
        S0.fails += 1;
        if (S0.fails >= MAX_FAIL) { stopPoll(); pauseClock(); }
        return;
      }
      S0.fails = 0;
      var byId = {};
      (d.jobs || []).forEach(function (j) { byId[j.id] = j; });
      /* 同一张卡可能挂在两个键下（认领换过号 / 存档也塞过一次）。不按卡
         去重的话，这一轮会把它处理两遍：第一遍出图收尾、第二遍又按「还在
         跑」把状态行写回去——用户看到的就是「图出来了，卡片还在转圈」。 */
      var seenCard = [];

      Object.keys(S0.pending).forEach(function (id) {
        var card = S0.pending[id];
        if (seenCard.indexOf(card) >= 0) return;
        seenCard.push(card);
        var job = byId[id];
        // 任务不在列表里 = 进程重启过，这张再也回不来，别让它一直转。
        // 但状态接口刚恢复的第一次不能这么说：进程重启后 /api/gen/status
        // 本来就是空的，而那张图可能还躺在队列里（服务端重启会从账本里
        // 把它接回来）。先给它一个宽容期，过了还没出现才判死。
        if (!job) {
          if (!card.graceUntil) card.graceUntil = Date.now() + GHOST_MS;
          if (Date.now() < card.graceUntil) {
            card.note.hidden = false;
            paintNote(card, "正在跑，画好会自动弹出来");
            return;
          }
          delete S0.pending[id];
          fail(card, "服务重启过，这张没了，点重试再排一次", true);
          forget(card);
          return;
        }
        card.graceUntil = 0;
        if (job.state === "done") {
          disarmStop(card);
          showImage(card, job);
          /* 这一张到此为止：把它的卡片从这个轮询里彻底摘掉。
             ⚠️ 以前这里只 `delete pending[id]`，同一张卡要是还挂在别的
             键下（认领时换过号、`restore` 也按自己的键塞过），下一次 tick
             又把它当「还在跑」重画一遍状态行——用户看到的就是「图已经出来
             了，卡片还在转圈」。`forget()` 按对象身份清，一次清干净。 */
          forget(card);
        } else if (job.state === "error" || job.state === "cancelled") {
          disarmStop(card);
          if (job.state === "cancelled") {
            card.since = 0;
            card.note.hidden = false;
            paintNote(card, "已终止", false, false);   // 停下来了就别再转圈
            if (card.rec) {
              card.rec.job = "";
              card.rec.note = "已终止";
              card.rec.err = false; card.rec.stopped = true;   // 自己按的停
              H.patch(card.rec, { job: "", note: "已终止", err: false, stopped: true });
              H.save(null);
            }
            forget(card);
            return;
          }
          // stuck=true 是服务端判的「线程丢了，这张根本没跑」
          fail(card, job.error || "这张没出来", !!job.stuck);
          forget(card);
        } else {
          // 带上参考图时把它读图的结果说清楚：读到了就说「照着参考图画」，
          // 没读到就说「按描述画」——用户不该猜这张图到底用没用上。
          var refs = job.ref_note || "";
          var tail = refs ? "（" + refs + "）" : "";
          // 说清楚它**现在到底在哪**（2026-11-23）：`phase` 是后端去 ComfyUI
          // 现问的——排队 / 准备中 / 真在画 / 掉队了。`unknown` 是「问不到」，
          // 按「正在跑」显示：探不到 ComfyUI 不等于这张没了（2026-11-26）。
          var ph = job.phase || "";
          if (checkTimeout(card, ph)) return;
          /* 「前面还有几张」由服务端算好给（`ahead`，2026-11-27）：本进程队列
             + 别的进程（QQ / 漫画台）压着的一起算，比前端自己数准。0 或不给 =
             下一个就轮到它，统一说「正在跑」（用户：「前面 0 张就统一正在跑」）。 */
          var ahead = (typeof job.ahead === "number") ? job.ahead : 0;
          /* 外观统一（2026-11-27 用户拍板）：「排队 → 开画」原来是胶囊掉回一行
             灰字，看着像坏了。现在从提交到出图始终是同一张粉紫胶囊，只有徽章
             和正文在变——排队说「前面还有 N 张」，开画说「画好会自动弹出来」。
             真开画才铺 3:4 底纹，排队阶段别先占一个大空壳（2026-11-24）。 */
          var qtag = ahead > 0 ? "正在排队"
                   : ph === "running" ? "正在画了" : "正在跑";
          var text = ph === "lost"
            ? "这一张没在跑了（服务那边已经没有它），重新发一次吧"
            : ahead > 0 ? "前面还有 " + ahead + " 张"
            : "画好会自动弹出来" + tail;
          if (ph === "running") H.spawn(card);
          paintNote(card, text, ph === "lost", ph !== "lost",
                    ph === "lost" ? false : qtag);
          if (card.rec) card.rec.note = text;
        }
      });

      if (!Object.keys(S0.pending).length) {
        stopPoll();
        H.save(null);
      }
    });
  }

  global.addEventListener("pageshow", function () {
    if (H.state && Object.keys(S().pending).length) { startPoll(); tick(); }
    // 手机把标签页挂起过：这期间产生的记录可能没推上去，回来先补一次。
    if (global.WSStore && global.WSStore.resync) global.WSStore.resync();
  });
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden && H.state && Object.keys(S().pending).length) {
      startPoll();
      tick();
    }
    if (!document.hidden && global.WSStore && global.WSStore.resync) {
      global.WSStore.resync();
    }
  });

  global.WSGenFlow = {
    bind: bind,
    submit: submit,
    continueSubmit: continueSubmit,
    restart: restart,
    armStop: armStop,
    disarmStop: disarmStop,
    linkBatch: function (d, t, u) { return global.WSDirect.linkBatch(d, t, u); },
    newCard: function (p, s, r) { return H.newCard(p, s, r); },
    save: function (rec) { H.save(rec); }, fail: fail,
    dropCard: function (c, t) { if (H.dropCard) H.dropCard(c, t); },
    busyTip: function (t) { if (H.busyTip) H.busyTip(t); },
    newId: function (p) { return H.newId ? H.newId(p) : ""; },
    claim: function (card) {          // 直达卡接进轮询（挂终止 + 塞 pending）
      if (!card || !card.job) return;
      armStop(card);
      S().pending[card.job] = card;
      card.say(NOTE_RUN, false, true); paintNote(card, NOTE_RUN);
    },
    kick: function () { startPoll(); tick(); },
    startPoll: startPoll,
    stopPoll: stopPoll,
    tick: tick,
    showImage: showImage,
    SETTLE_MS: SETTLE_MS,
    PAINT_MS: PAINT_MS
  };
})(window);
