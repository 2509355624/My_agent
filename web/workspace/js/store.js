/* 聊天页存档：刷新后要把「说过什么、出了什么图、哪些还在跑」原样接回来。
   只存聊天页这一层，不存画风抽屉开合、不存查看器——那些是一次性的动作。

   存哪儿（2026-11-24 用户问：「会持久化保存吗？我换个浏览器登录还能看到吗」）
   ---------------------------------------------------------------------------
   **现在存两份**：localStorage 是缓存，服务端才是正本。

   · 以前只存 localStorage。它按「浏览器 + 域名」隔离——换手机、换浏览器、
     换隧道域名（Cloudflare 快擦除每次重连都换）就是一片空白。用户要的是
     「我换个浏览器登录，之前的记录还在」。
   · 所以加了 `/api/chat/archive`：登录态下的 QQ 号就是档位，**所有设备共用
     同一份**。进页面先读服务端，本地那份只当「先铺出来别白屏」的缓存，
     拉回服务端的结果后整体替换。
   · 每加一条消息只把**新增的那一段**（`from` 起）推上去，不在每次轮询都
     把整份记录重新传一遍——出图时每 2 秒改一次「正在生成…」，传整份会把
     家用上行吃掉。
   · 服务端不可用（断网、5174 没起）时一切照旧走本地，不因为同步失败就让
     页面没法用。 */
(function (global) {
  "use strict";

  var KEY = "ws_chat_v1";
  /* 访客那一格。2026-11-26 第三轮之后**访客也有服务端档位了**
     （`guest_<身份段>.json`，见 5174 的 `app/features/chat/routes.py`），
     所以这里只是**本机缓存**：先铺出来别白屏、断网时还能看。
     为什么还要按天分段：带身份串的键每续签一次就换一把，重进就看不见自己
     刚画的；点「登录」再点「逛逛」又会续一轮。按天（北京时间）分段之后，
     同一天里进出都是同一格。旧格式（`ws_chat_guest_v1[:sid]`）读的时候并
     一次进来。 */
  var KEY_GUEST = "ws_chat_guest_v1";
  var KEY_GUEST_DAY = "ws_chat_visitor_v2:";
  var KEY_GUEST_FIXED = "ws_chat_guest_v1";
  var migrateDone = false;

  function day() {
    /* 北京时间的日期，跟 5174 记额度那个「零点」是同一个零点 */
    var d = new Date(Date.now() + 8 * 3600 * 1000);
    return d.toISOString().slice(0, 10);
  }
  var MODE = { guest: false, sid: "" };

  /* store.js 在 chat.html 里排在 visitor.js / me.js 之前，拿不到已经
     解析好的身份，所以这里自己去要一次 /me（浏览器会命中 5175 那边
     的短缓存，不额外多走一次转发）。失败就当普通用户，一切照旧。 */
  var ready = null;
  function whoami() {
    if (ready) return ready;
    ready = global.fetch("/me", { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        d = d || {};
        MODE.guest = (d.role === "visitor");
        MODE.sid = MODE.guest ? String(d.session || "") : "";
        return MODE;
      })
      .catch(function () { return MODE; });
    return ready;
  }

  function key() {
    if (!MODE.guest) return KEY;
    return KEY_GUEST_DAY + day();
  }

  /* 本地缓存窗口（条）。**这不是「能看多少历史」**——总长度由服务端的 KEEP
     决定（2000 条）前端按页往回要。120 是「首屏先铺什么」和「离线时还能看到
     什么」的那一段。 */
  var MAX = 120;
  var API = "/api/chat/archive";

  function alive() {
    try {
      global.localStorage.setItem("ws_probe", "1");
      global.localStorage.removeItem("ws_probe");
      return true;
    } catch (e) {
      return false;
    }
  }

  function read() {
    if (!alive()) return null;
    try {
      var raw = global.localStorage.getItem(key());
      if (!raw) return null;
      var d = JSON.parse(raw);
      if (!d || !Array.isArray(d.msgs)) return null;
      return d;
    } catch (e) {
      return null;
    }
  }

  /* 把老访客键里的记录并进今天这一格，然后删掉老键。
   *
   * 2026-11-26 换键的当天，用户手机上「刚画的可莉」还躺在
   * `ws_chat_guest_v1:<身份串>` 里；不搬的话他看到的正是「重进就没了」。
   * 只搬一次（`migrateDone`），并且**只搬带着记录的格子**——空的直接删，
   * 免得配额里堆一堆一天就没用的键。 */
  function migrate() {
    if (migrateDone || !MODE.guest) return;
    migrateDone = true;
    var keep = KEY_GUEST_FIXED;
    var olds = [];
    try {
      for (var i = 0; i < global.localStorage.length; i++) {
        var k = global.localStorage.key(i);
        if (k && (k === keep || k.indexOf(KEY_GUEST + ":") === 0)) olds.push(k);
      }
    } catch (e) { return; }
    var now = key(), msgs = [];
    olds.forEach(function (k) {
      if (k === now) return;
      var d = null;
      try { d = JSON.parse(global.localStorage.getItem(k) || "null"); } catch (e2) { d = null; }
      if (d && Array.isArray(d.msgs)) msgs = msgs.concat(d.msgs);
      try { global.localStorage.removeItem(k); } catch (e3) {}
    });
    if (!msgs.length) return;
    var cur = read();
    var all = merge((cur && cur.msgs) || [], msgs);
    try {
      global.localStorage.setItem(now, JSON.stringify({ v: 2, msgs: all.slice(-MAX) }));
    } catch (e4) { /* 配额满了就算了，下次 save 还会写 */ }
  }

  /* 记录 id。带 id 的记录是「同一条」——卡片从排队中走到已出图，id 不变。 */
  function rid(m) {
    return (m && m.id) ? String(m.id) : "";
  }

  /* 把本地那份并进服务端：按 id 认人。服务端有的按服务端顺序排，
     **本地版本覆盖服务端版本**（本地正握着这次会话里最新的状态），本地独有的
     追加在后面。
     以前是把服务端整份直接写回去，本地刚画完、还没推上去的那条会当场消失；
     更糟的是同一张卡在本地与服务端各存一份不同状态，刷新时又两边都渲染。 */
  function merge(local, server) {
    var out = [], at = {};
    (server || []).forEach(function (m) {
      var d = rid(m);
      if (d) at[d] = out.length;
      out.push(m);
    });
    (local || []).forEach(function (m) {
      var d = rid(m);
      if (d && d in at) {
        out[at[d]] = m;          // 本地比服务端新（正在进行中的状态）
        return;
      }
      if (d) at[d] = out.length;
      out.push(m);
    });
    return out.slice(-MAX);
  }

  /* 本地那份（同步返回）。页面首屏先拿它铺，避免等服务端回来白屏一下。 */
  function load() {
    migrate();
    return read();
  }

  var timer = 0;

  /* 写本地盘（节流）。生成中每 2 秒会改一次「正在生成…」，没必要每次都
     序列化。返回真正落盘的那个数组。
     `pendingIds`：还没成功推上服务端的记录 id，存进同一份 JSON 里。这样
     页面被杀掉再回来，「哪几条欠着」还是知道的——以前只记在内存里，一刷新
     就忘了，那几条永远补不上去（2026-11-25 用户报的跨浏览器不同步）。 */
  function saveLocal(msgs, pendingIds) {
    if (!alive()) return [];
    var keep = (msgs || []).slice(-MAX);
    var bag = pendingIds || readPending();
    var box = { v: 2, msgs: keep, pending: bag };
    try {
      global.localStorage.setItem(key(), JSON.stringify(box));
    } catch (e) {
      /* 配额满了就丢掉更老的一半再试一次，还不行就放弃 */
      var half = keep.slice(-Math.floor(keep.length / 2));
      try {
        global.localStorage.setItem(key(), JSON.stringify({ v: 2, msgs: half, pending: bag }));
      } catch (e2) { half = []; }
      return half;
    }
    return keep;
  }

  /* 还欠着服务端的记录 id（存本地，刷新后还在）。 */
  function readPending() {
    var d = read();
    return (d && d.pending && typeof d.pending === "object") ? d.pending : {};
  }

  function markPending(msgs) {
    if (!msgs || !msgs.length) return;
    var bag = readPending();
    msgs.forEach(function (m) { var d = rid(m); if (d) bag[d] = 1; });
    saveLocal((read() || {}).msgs || [], bag);
  }

  function markSynced(msgs) {
    if (!msgs || !msgs.length) return;
    var bag = readPending(), changed = false;
    msgs.forEach(function (m) {
      var d = rid(m);
      if (d && bag[d]) { delete bag[d]; changed = true; }
    });
    if (changed) saveLocal((read() || {}).msgs || [], bag);
  }

  /* 这条记录上次推上去时的样子（没推过就是空串）。 */
  var pushed = {};

  function fpOf(m) {
    try { return JSON.stringify(m); } catch (e) { return ""; }
  }

  /* 落盘 + 同步。
     口径（2026-11-25 用户实测后定的）：
       · **记录一产生就整条推上去**，不等 300ms 节流、不攒批次——页面关了、
         切后台、手机把标签页杀掉，那条已经在服务端了；
       · 之后状态变了（排队中 → 已出图 → 失败），按 id 原位更新同一条；
       · 每次只推「新增的 + 这次变了的」，靠 `pushed` 里的指纹判断，不再
         傻推尾部固定条数（那既可能漏掉中间新加的，又会把老记录反复推）。 */
  function save(data) {
    clearTimeout(timer);
    var msgs = (data && data.msgs) || [];
    saveLocal(msgs);
    var dirty = [];
    var seen = {};
    msgs.forEach(function (m) {
      var d = rid(m);
      if (!d) { dirty.push(m); return; }      // 老记录没有 id，只能按指纹认
      var f = fpOf(m);
      // 同一份数组里同 id 出现两次（比如迁移时补的 old_*）只推最后那版
      if (seen[d] === f) return;
      seen[d] = f;
      if (f === clean[d]) return;             // 和服务端那份一字不差：不用动
      if (f === pushed[d]) return;            // 这次会话里已经推成功过
      dirty.push(m);
    });
    if (!dirty.length) return;
    /* 访客也有服务端档位了（2026-11-26 用户拍板，见 `whoami` 上面那段注释）：
       `guest_<身份段>.json`，跟 QQ 那份同一套读写、同一条 KEEP。所以这里不再
       按访客早退——原先那句 `if (MODE.guest) return` 等于「页面一关就没人
       写」，而轮询是唯一的写入方，用户大退回来就只能看到一张假卡。 */
    flush(dirty);
  }

  /* 「拉下来的就不算脏」的记号：id → 指纹。和 `pushed` 分开记，因为
     `pushed` 在请求成功返回时才写，中间那几百毫秒里第二次 save 会把整窗
     历史又当成脏数据推一遍。 */
  var clean = {};

  /* 从服务端读到的那份，就是「已经在服务端了」——把它记成干净，否则下一次
     save() 会把刚拉下来的整窗历史当成脏数据全推回去（白白上传、还会把
     服务端顺序再排一遍）。 */
  function forget(m) {
    var d = rid(m);
    if (!d) return;
    var f = fpOf(m);
    clean[d] = f;
    pushed[d] = f;
  }

  var flushing = false, queue = [];

  function flush(list) {
    list.forEach(function (m) {
      var d = rid(m);
      var at = -1;
      if (d) {
        for (var i = 0; i < queue.length; i++) {
          if (rid(queue[i]) === d) { at = i; break; }
        }
      }
      if (at >= 0) queue[at] = m;              // 同一条只留最新那版
      else queue.push(m);
    });
    markPending(queue);
    if (!flushing) send();
  }

  function send() {
    if (!queue.length) return Promise.resolve(null);
    var batch = queue.slice(0, 40);
    queue = queue.slice(batch.length);
    // 发出去的那一刻就记成「已推」：不等响应。否则响应回来之前任何一次
    // save() 都会把这些刚发出去的记录再判一遍脏，一次提交变成两次推送。
    batch.forEach(function (m) {
      var id = rid(m);
      if (id) pushed[id] = fpOf(m);
    });
    flushing = true;
    var body = { msgs: batch.map(function (m) {
      return { m: m, f: fpOf(m) };
    }) };
    return global.fetch(API, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      keepalive: true              // 页面正在关闭时也让它发完
    }).then(function (r) { return r.ok ? r.json() : null; })
      .catch(function () { return null; })
      .then(function (d) {
        flushing = false;
        if (d && d.ok) {
          markSynced(batch);
        } else {
          /* 没推上去：这些 id 要放回 pending，等 resync 补。 */
          markPending(batch);
        }
        if (queue.length) send();
        return d;
      });
  }

  /* 「哪几条欠着」→ 补推。进页面、切回前台时各调一次。
     只补推还在本地窗口里的那些：更老的已经被覆盖掉了，按指纹补推会把
     顺序打乱，不如不补。 */
  function resync() {
    var box = read() || {};
    var msgs = box.msgs || [], bag = readPending(), ids = Object.keys(bag);
    if (!ids.length) return Promise.resolve(null);
    var lost = ids.filter(function (d) {
      return !msgs.some(function (m) { return rid(m) === d; });
    });
    lost.forEach(function (d) { delete bag[d]; });
    var back = msgs.filter(function (m) { return bag[rid(m)]; });
    if (lost.length) saveLocal(msgs, bag);
    if (!back.length) return Promise.resolve(null);
    flush(back);
    return Promise.resolve(true);
  }

  /**

  /* 改服务端存档里的某一条（有 id 认 id，老记录按 `job`）。失败不重试、不提示：
     它只影响「下次换设备读回来时这条显示成什么」，改不动也不该拦用户出图。

     为什么还需要它：常态下靠 `save()` 整条重推就够了，但重推要等页面里那条
     记录真的变了；轮询拿到结果的那一刻，用户可能马上关页面，这里先就地把
     状态落一次，比等他下一次操作更稳。 */
  function patch(rec, fields) {
    var job = String((rec && rec.job) || "").trim();
    var id = rid(rec);
    if (!job && !id) return Promise.resolve(null);
    /* 身份键单独放顶层，要改的字段全部塞进 `m`（服务端优先读 m，见
       `api_archive_patch`）。摊平写会被 fields 里的 `job: ""` 盖掉——那就成了
       「id 和 job 都空」，服务端回 400（2026-11-26 实测 4 次全 400，出图结果
       一条都没写回存档，用户「图有了、记录里没有」就是这么来的）。 */
    var body = { m: fields || {} };
    if (id) body.id = id;
    if (job) body.job = job;
    return global.fetch(API, {
      method: "PATCH",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(function (r) { return r.ok ? r.json() : null; })
      .catch(function () { return null; });
  }

  /* ── 服务端 ─────────────────────────────────────────── */

  /* 读服务端正本。→ Promise<{ok, msgs, total, more} | null>；5174 直连（站主
     本机）或未登录时接口 404/401，返回 null，调用方继续用本地那份。

     **拿到的就是全部该显示的**（服务端已经按「尾部一页」截过），不再往里
     掺本地记录。这一条是 2026-11-25 那几个「点重画却画出另一张图」「明明出图
     了却说没跑完」的直接来源：以前服务端缺记录就把本地整段补进来，补进来的
     是无 id 的旧版记录，按内容认人一错位，图和提示行就串了。
     本机刚产生还没推上去的，靠 `pending` 补推，不靠「并进历史」。 */
  function pull(limit) {
    var url = API + (limit ? "?limit=" + limit : "");
    return global.fetch(url, { credentials: "same-origin" })
      .then(function (r) {
        if (!r.ok) return null;
        return r.json().catch(function () { return null; });
      })
      .then(function (d) {
        if (!d || !d.ok || !Array.isArray(d.msgs)) return null;
        // 服务端这份落本地当缓存；还没推上去的那几条不在里面，但它们已经
        // 记在 `pending` 里，resync 会补推，不会丢。
        d.msgs.forEach(forget);
        saveLocal(d.msgs);
        return d;
      })
      .catch(function () { return null; });
  }

  /* 往回翻更早的历史（「加载更早」）。→ Promise<数组 | null>

     服务端一次默认只给尾部 TAIL 条，要更早的整段就带上更大的 limit（服务端
     封顶 KEEP）。这里不负责拼进页面，由 archive.js 按 id 去重后接在顶上。 */
  function earlier(limit) {
    var n = Math.max(MAX, limit || 0) + MAX;
    return global.fetch(API + "?limit=" + n, { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d || !d.ok || !Array.isArray(d.msgs)) return null;
        d.msgs.forEach(forget);      // 拉回来的就是服务端已有的，别再当脏数据推回去
        return d.msgs;
      })
      .catch(function () { return null; });
  }

  /* 访客档位（2026-11-26 用户拍板）：**和 QQ 号那档同一套**，只是键不同
     （`guest_<身份段>`）。服务端那份是正本，本地这份是缓存——所以下面所有
     网络动作对访客和登录用户一视同仁，不再有「访客不推不拉」的分支。
     访客证 24 小时过期，过期再来是新 id、新档位，互不可见。 */

  /* 退出登录：本地清掉。服务端那份**不删**——它属于这个 QQ 号，换台设备
     登录还得能看见（用户 2026-11-24 的要求）。共享设备上的隔离由服务端
     按登录态分档保证，不靠清本地。 */
  function clear() {
    clearTimeout(timer);
    queue = [];
    pushed = {};
    clean = {};
    try { global.localStorage.removeItem(key()); } catch (e) { /* 忽略 */ }
  }

  /* 让别的模块能问「这次是不是访客」（/me 回来之后才有值）。 */
  function isGuest() { return MODE.guest; }

  global.WSStore = {
    load: load,
    save: save,
    clear: clear,
    pull: pull,
    earlier: earlier,
    resync: resync,
    patch: patch,
    merge: merge,
    isGuest: isGuest,
    whoami: whoami,
    KEY: KEY
  };
})(window);
