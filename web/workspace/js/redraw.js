/* 「重画」= 把这条消息当作一条新消息重新发送（2026-11-25 用户对齐豆包）。
   不是原地复用旧卡：旧的用户气泡、卡片、存档记录全清掉，再用同一句话、
   同一批引用图走一遍正常发送。这样列表里只留最新的一次请求，不会堆卡，
   也没有「上一张还在画」那种连点限流留下的残影。

   这里只做编排，不碰具体状态：需要什么全由 chat.js 通过 bind 注入，
   `run(card)` 是唯一入口。 */
(function (global) {
  "use strict";

  var H = {
    pending: null,     // () → 在途任务表
    cancel: null,      // (job) → 终止一张
    afterCancel: null, // () → 在途变化后刷新轮询
    clearFail: null,   // (userNode) → 撤掉用户气泡下的失败行
    removeUser: null,  // (userNode) → 从列表与索引里摘掉用户气泡
    removeCard: null,  // (cardNode) → 摘掉卡片 DOM
    msgs: null,        // () → 存档数组
    removeRec: null,   // (rec, text) → 摘掉这条卡与其用户消息的存档记录
    save: null,        // () → 落盘存档
    defaultSkill: null,// () → 当前渠道
    send: null         // (text, refs, described, skill) → 走正常发送路径
  };

  function bind(hooks) {
    Object.keys(hooks || {}).forEach(function (k) {
      if (k in H) H[k] = hooks[k];
    });
  }

  function run(card) {
    if (!card) return;
    var rec = card.rec || {};
    var text = rec.ask || rec.prompt || card.prompt || "";
    /* rec.refs 存的是 id 数组，统一成 {id}：识图和缩略图都认对象。 */
    var refs = (card.refs || rec.refs || []).map(function (r) {
      return (typeof r === "object" && r) ? r : { id: r };
    });
    var described = !!(card.described || rec.described);
    var skill = card.skill || rec.skill || H.defaultSkill();

    // 还挂着的旧任务先掐掉，免得它出完图又冒一张
    var pending = H.pending();
    if (card.job && pending[card.job]) {
      if (H.cancel) H.cancel(card.job);
      delete pending[card.job];
      if (H.afterCancel) H.afterCancel();
    }
    // 清 DOM：失败行 → 用户气泡 → 卡片
    if (card.userNode) H.clearFail(card.userNode);
    if (H.removeCard) H.removeCard(card.node);
    if (card.userNode && H.removeUser) H.removeUser(card.userNode);
    // 清存档
    H.removeRec(rec, text);
    H.save();

    if (!text || !H.send) return;
    H.send(text, refs, described, skill);
  }

  /* 限流（还在画 / 人太多）那张没排上队的卡：整张撤掉，提示只闪一次不刷屏。 */
  function drop(card) {
    if (!card) return;
    if (H.removeCard) H.removeCard(card.node);
    if (card.userNode && H.clearFail) H.clearFail(card.userNode);
    if (card.rec) H.removeRec(card.rec, "");
    H.save();
  }

  global.WSRedraw = { bind: bind, run: run, drop: drop };
})(window);
