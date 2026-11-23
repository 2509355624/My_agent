/* 聊天页存档：刷新后要把「说过什么、出了什么图、哪些还在跑」原样接回来。
   只存聊天页这一层，不存画风抽屉开合、不存查看器——那些是一次性的动作。

   为什么落 localStorage 而不是服务端：用户的对话是私密的，服务端没有
   「聊天记录」这张表，为一个前端界面新开一张表还得配清理策略；而这里每条
   消息就是气泡上已有的那点文本 + 图片地址，几十 KB 的量级，localStorage
   的 5MB 完全够，也不会有跨端同步的歧义（换设备看到的就是另一台设备的）。
   出问题（隐私模式、配额满）一律静默降级成「这次不存」，不能因此让页面崩。 */
(function (global) {
  "use strict";

  var KEY = "ws_chat_v1";
  var MAX = 120;          // 只留最近 120 条，再多的老消息不值得占空间

  function alive() {
    try {
      global.localStorage.setItem("ws_probe", "1");
      global.localStorage.removeItem("ws_probe");
      return true;
    } catch (e) {
      return false;
    }
  }

  function load() {
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

  var timer = 0;

  /* 写盘节流：生成中每 2 秒会改一次「正在生成…」，没必要每次都序列化 */
  function save(data) {
    if (!alive()) return;
    clearTimeout(timer);
    timer = setTimeout(function () {
      var msgs = (data.msgs || []).slice(-MAX);
      try {
        global.localStorage.setItem(KEY,
          JSON.stringify({ v: 1, msgs: msgs }));
      } catch (e) {
        /* 配额满了就丢掉更老的一半再试一次，还不行就放弃 */
        try {
          global.localStorage.setItem(KEY, JSON.stringify({
            v: 1, msgs: msgs.slice(-Math.floor(msgs.length / 2)) }));
        } catch (e2) { /* 放弃 */ }
      }
    }, 300);
  }

  function clear() {
    try { global.localStorage.removeItem(KEY); } catch (e) { /* 忽略 */ }
  }

  global.WSStore = { load: load, save: save, clear: clear, KEY: KEY };
})(window);
