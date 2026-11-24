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
  var MAX = 120;          // 只留最近 120 条，再多的老消息不值得占空间
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
      var raw = global.localStorage.getItem(KEY);
      if (!raw) return null;
      var d = JSON.parse(raw);
      if (!d || !Array.isArray(d.msgs)) return null;
      return d;
    } catch (e) {
      return null;
    }
  }

  /* 本地那份（同步返回）。页面首屏先拿它铺，避免等服务端回来白屏一下。 */
  function load() {
    return read();
  }

  var timer = 0;

  /* 写本地盘（节流）。生成中每 2 秒会改一次「正在生成…」，没必要每次都
     序列化。返回真正落盘的那个数组，调用方据此算「服务端还缺哪几条」。 */
  function saveLocal(msgs) {
    if (!alive()) return [];
    var keep = (msgs || []).slice(-MAX);
    try {
      global.localStorage.setItem(KEY, JSON.stringify({ v: 1, msgs: keep }));
    } catch (e) {
      /* 配额满了就丢掉更老的一半再试一次，还不行就放弃 */
      var half = keep.slice(-Math.floor(keep.length / 2));
      try {
        global.localStorage.setItem(KEY, JSON.stringify({ v: 1, msgs: half }));
      } catch (e2) { half = []; }
      return half;
    }
    return keep;
  }

  function save(data) {
    clearTimeout(timer);
    timer = setTimeout(function () {
      var keep = saveLocal((data && data.msgs) || []);
      /* 只推「本地还留着、服务端该有的最后一段」：从尾部往前找到第一条
         和上次同名的记录，往后就是新增。简单粗暴：推最后 SYNC_TAIL 条，
         服务端按内容指纹去重，重复的丢掉。 */
      if (keep.length) push(keep.slice(-SYNC_TAIL));
    }, 300);
  }

  /* 一次最多推这么多条。出图卡片改状态时 `keep` 里会带上已经推过的那些，
     服务端按指纹去重，所以这里"多推几条"是安全的，只是多几 KB。 */
  var SYNC_TAIL = 12;

  function fp(m) {
    try { return JSON.stringify(m); } catch (e) { return ""; }
  }

  /* ── 服务端 ─────────────────────────────────────────── */

  /* 读服务端正本。→ Promise<{ok, msgs} | null>；5174 直连（站主本机）或
     未登录时接口 404/401，返回 null，调用方继续用本地那份。 */
  function pull() {
    return fetch(API, { credentials: "same-origin" })
      .then(function (r) {
        if (!r.ok) return null;
        return r.json().catch(function () { return null; });
      })
      .then(function (d) {
        if (!d || !d.ok || !Array.isArray(d.msgs)) return null;
        // 服务端那份就是正本：顺手写回本地，下次首屏更快
        saveLocal(d.msgs);
        return d;
      })
      .catch(function () { return null; });
  }

  var pushing = false, again = false;

  /* 推增量上去。`msgs` 是本地最后若干条，服务端只收它没有的。 */
  function push(msgs) {
    if (!msgs || !msgs.length) return Promise.resolve(null);
    if (pushing) { again = true; return Promise.resolve(null); }
    pushing = true;
    var body = { msgs: msgs.map(function (m) {
      return { m: m, f: fp(m) };
    }) };
    return fetch(API, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(function (r) { return r.ok ? r.json() : null; })
      .catch(function () { return null; })
      .then(function (d) {
        pushing = false;
        if (again) { again = false; }
        return d;
      });
  }

  /* 退出登录：本地清掉。服务端那份**不删**——它属于这个 QQ 号，换台设备
     登录还得能看见（用户 2026-11-24 的要求）。共享设备上的隔离由服务端
     按登录态分档保证，不靠清本地。 */
  function clear() {
    clearTimeout(timer);
    try { global.localStorage.removeItem(KEY); } catch (e) { /* 忽略 */ }
  }

  global.WSStore = {
    load: load,
    save: save,
    clear: clear,
    pull: pull,
    push: push,
    KEY: KEY
  };
})(window);
