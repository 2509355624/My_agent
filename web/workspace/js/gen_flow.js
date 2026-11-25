/* 生图这一趟的「干活的」部分：提交、轮询、判超时、挂/摘「终止」。
   从 chat.js 拆出来的（2026-11-24，那边已经七百多行）。

   这里不画任何东西——卡片长什么样在 card.js，图长什么样在 shot.js，
   存档怎么落盘由 chat.js 的回调决定。`bind()` 里那几个回调就是全部接口。

   两条硬口径（都是用户提的，改这里前先看清楚）：
     · 同一份提示词、同一个渠道，连着点「重画」不许越点越多张空卡：
       还在跑 → 只闪一下那一行字；已经失败 → 复用那一张重新排。
     · 排队中 / 报错只是一行字，不占 3:4 的大壳（is-idle，见 card.js）。 */
(function (global) {
  "use strict";

  var POLL_MS = 2000;
  var MAX_FAIL = 5;         // 连续失败这么多次就停轮询，别无限打接口
  /* 卡死兜底（2026-11-24 用户：「昨天晚上的图现在还显示排队中，卡死了」）。
     后端已经有超时，但它只管「线程还在跑」的情形；服务重启 / 任务丢掉之后
     存档里那条会永远停在「排队中」。所以前端**自己**也计时：
       · 一直没轮到（排队 / 准备中）超过 SETTLE_MS → 判它卡住了；
       · 真在画的那张按渠道给得更宽，别误杀。
     判超时不是「报错完事」：卡片上给一个「重试」，用户点了就按同一份提示词
     重排一张。 */
  var SETTLE_MS = 8 * 60 * 1000;
  var PAINT_MS = 25 * 60 * 1000;

  /* 由 chat.js 注入的回调（都是必需的，少一个这里就该报错而不是静默死循环） */
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
    dropCard: null    // 撤掉一张没排上队的临时卡（连用户气泡一起）
  };

  function bind(hooks) {
    Object.keys(hooks || {}).forEach(function (k) {
      if (k in H) H[k] = hooks[k];
    });
  }

  function S() { return H.state(); }

  // ── 提交 ──────────────────────────────────────────────

  /* refs：这一轮要带上的参考图 id。生图接口本身是按描述出图，参考图随请求
     一起交给后端（它按渠道能力决定用不用），前端不自己拼提示词。
     `described`（2026-11-24）：这些参考图**已经读成描述写进 prompt 了**
     （「画同款」就是这种）。带上它，后端就不再识一次图——用户确认过的
     口径是「不再识，用输入栏那段」。 */
  function submit(prompt, skill, refs, described, userNode, defer, finalPrompt) {
    if (!skill) {
      H.sysNote("还没拿到渠道列表，去跑图台看一眼再回来");
      return null;
    }
    /* 每条消息独立提交，数量限制由后端按用户执行。 */
    var rec = { k: "img", cls: "", prompt: prompt, ask: prompt, skill: skill,
                id: H.newId ? H.newId("img") : "",
                refs: (refs || []).slice(),
                described: !!described,
                userId: userNode ? (userNode._wsUserId || "") : "",
                userText: userNode ? (userNode._wsUserText || "") : "",
                job: "", file: "", note: "排队中…", err: false };
    var card = H.newCard(prompt, skill, rec);
    card.rec = rec;
    S().cards.set(rec, card);
    // 卡片自己记住参考图：点「重画」要按同一份角色重来，不能悄悄丢图
    card.refs = rec.refs;
    card.described = !!described;
    if (userNode) card.userNode = userNode;
    rec.userNode = userNode || null;
    card.since = Date.now();       // 超时从这里起算，见 tick()
    card.noted = "排队中…";
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
      fresh.userNode = rec.userNode || null;
      fresh.since = Date.now();
      S().cards.set(rec, fresh);
      rec.job = "";
      rec.file = "";
      rec.note = "排队中…";
      rec.err = false;
      fire(fresh, rec, rec.refs, rec.described);
      return;
    }
    if (card.userNode) global.WSCard.clearFailLine(card.userNode);
    card.refs = rec.refs || [];
    card.described = !!rec.described;
    card.since = Date.now();
    card.noted = "排队中…";
    rec.job = "";
    rec.file = "";
    rec.note = "排队中…";
    rec.err = false;
    if (card.img) { card.img.remove(); card.img = null; }
    card.acts.innerHTML = "";
    card.acts.hidden = true;
    card.say("正在提交…");
    global.WSCard.flash(card);
    H.save(rec);
    H.patch(rec, { job: "", file: "", note: "正在提交…", err: false });
    fire(card, rec, rec.refs, rec.described);
  }

  /* 真正把请求发出去（submit / restart 共用）。 */
  function fire(card, rec, refs, described) {
    card.submitting = true;
    card.say("正在提交…");
    var body = { skill: card.skill, prompt: card.prompt };
    if (refs && refs.length) {
      body.refs = refs;
      // 读过了就别让后端再读一遍，也别把同一段描述拼第二遍
      if (described) body.ref_described = true;
    }
    if (rec.finalPrompt) body.prompt_is_final = true;
    global.WSGen.submit(body).then(function (d) {
      card.submitting = false;
      if (!d || !d.ok) {
        /* 限流（还在画 / 人太多）：只是一次性提示，不落失败行、不留卡。
           否则连点几下就挂一摞「上一张还在画」的红字，越积越多。 */
        if (d && d.busy) {
          if (H.dropCard) H.dropCard(card, rec);
          if (H.busyTip) H.busyTip(d.error || "前面还有图在画，等一下再发");
          return;
        }
        H.fail(card, (d && d.error) || "没排上队");
        return;
      }
      card.job = d.job.id;
      rec.job = card.job;
      /* 复制拿的是**实际出这张图用的那串提示词**，不是用户敲的那句话
         （2026-11-24 用户：「点击复制，结果拿到的是我发送给 ai 的原始需求，
         而不是 ai 生成这个图片使用的英文提示词 tags」）。
         服务端在这趟里做完了翻译/画师串/参考图描述合并，`job.prompt` 就是
         最终进模型的那串英文 tags，直接记下来当复制源。 */
      if (d.job.prompt) {
        card.prompt = d.job.prompt;
        rec.prompt = d.job.prompt;
      }
      armStop(card);
      S().pending[card.job] = card;
      card.say("排队中…");
      rec.note = "排队中…";
      H.patch(rec, { job: rec.job, prompt: rec.prompt, note: rec.note, err: false });
      startPoll();
      tick();
    })["catch"](function () {
      card.submitting = false;
      H.fail(card, "提交失败，检查网络后点重试", true);
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
        card.note.className = "note";
        card.note.textContent = "正在停，稍等…";
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
    delete S().pending[card.job];
    H.fail(card, (ph === "running")
      ? "等了 " + mins + " 分钟还在画，可能卡住了。点重试重排一张"
      : "排队等了 " + mins + " 分钟还没轮到，卡住了。点重试重排一张", true);
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

      Object.keys(S0.pending).forEach(function (id) {
        var card = S0.pending[id];
        var job = byId[id];
        // 任务不在列表里 = 进程重启过，这张再也回不来，别让它一直转。
        // （服务端也会把「线程没了」的收成 error，这里只是兜住更早的那一下）
        if (!job) {
          delete S0.pending[id];
          H.fail(card, "服务重启过，这张没了，点重试再排一次", true);
          return;
        }
        if (job.state === "done") {
          delete S0.pending[id];
          disarmStop(card);
          H.showImage(card, job);
        } else if (job.state === "error" || job.state === "cancelled") {
          delete S0.pending[id];
          disarmStop(card);
          if (job.state === "cancelled") {
            card.since = 0;
            card.note.hidden = false;
            card.note.className = "note";
            card.note.textContent = "已终止";
            card.noted = "已终止";
            if (card.rec) {
              card.job = "";
              card.rec.job = "";
              card.rec.note = "已终止";
              card.rec.err = false;
              H.patch(card.rec, { job: "", note: "已终止", err: false });
              H.save(null);
            }
            return;
          }
          // stuck=true 是服务端判的「线程丢了，这张根本没跑」
          H.fail(card, job.error || "这张没出来", !!job.stuck);
        } else {
          // 带上参考图时把它读图的结果说清楚：读到了就说「照着参考图画」，
          // 没读到就说「按描述画」——用户不该猜这张图到底用没用上。
          var refs = job.ref_note || "";
          var tail = refs ? "（" + refs + "）" : "";
          // 说清楚它**现在到底在哪**（2026-11-23）：`phase` 是后端去 ComfyUI
          // 现问的——排队 / 准备中 / 真在画 / 掉队了。原来一律写「正在生成」，
          // ComfyUI 重启后还挂在那儿转圈，用户根本不知道它是不是死了。
          var ph = job.phase || "";
          if (checkTimeout(card, ph)) return;
          var text;
          if (ph === "lost") {
            text = "ComfyUI 那边已经没有这张了（重启过），重新发一次吧";
          } else if (ph === "running") {
            /* 真开始画了才铺那块 3:4 的底纹（2026-11-24）：排队阶段只是
               一行字，别再先占出一个大空壳。 */
            H.spawn(card);
            text = "正在画…" + tail;
          } else if (ph === "queued") {
            var n = d.queue_depth;
            text = (typeof n === "number" && n > 0)
              ? "排队中，前面还有 " + n + " 张" + tail : "排队中…" + tail;
          } else {
            text = "准备中（等显卡空出来）" + tail;
          }
          card.note.hidden = false;
          card.note.className = "note" + (ph === "lost" ? " err" : "");
          card.note.textContent = text;
          card.noted = text;
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
    startPoll: startPoll,
    stopPoll: stopPoll,
    tick: tick,
    SETTLE_MS: SETTLE_MS,
    PAINT_MS: PAINT_MS
  };
})(window);
