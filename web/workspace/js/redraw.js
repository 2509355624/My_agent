/* 重画 = 在列表最底部新开一张卡（2026-11-25 用户实测的口径）。
   两张情况同一个落点：
     · 已经出过图的卡 → 用**实际跑这张用的英文 tags** 换种子再来一张，
       旧气泡、旧卡、旧图都留着不动；
     · 失败 / 已终止的卡 → 用当初那句话重新提交，并把旧卡上的失败提示收掉。

   为什么不再原地重试（2026-11-25 用户报的 bug）：原地重试时新卡建在旧卡的
   位置上，用户看到的是「需求在上、准备中在中间、图在最下面」，会以为这张
   图根本没开始画。重画就该出现在最底下，跟新发一条一样显眼。

   这里只做编排，不碰具体状态：需要什么全由 chat.js 通过 bind 注入。 */
(function (global) {
  "use strict";

  var H = {
    clearFail: null,   // (userNode) → 撤掉用户气泡下那行失败提示
    msgs: null,        // () → 存档数组
    save: null,        // () → 落盘
    discard: null,     // (rec) → 把旧的那张图记录从存档里摘掉（它要重画了）
    submit: null       // (text, skill, refs, described, user, defer, final)
  };

  function bind(hooks) {
    Object.keys(hooks || {}).forEach(function (k) {
      if (k in H) H[k] = hooks[k];
    });
  }

  function run(card) {
    if (!card || card.redrawing) return;   // 一次重画只走一遍
    card.redrawing = true;
    var rec = card.rec || {};
    var done = !!(rec.file || card.file);
    /* 出过图 → 用真正跑出这张的 tags；没出过图 → 用用户当初那句话。
       `ask` 只在建记录时写一次，是不变的「这一次请求」。 */
    var text = done ? (card.prompt || rec.prompt || "")
                    : (rec.ask || card.prompt || rec.prompt || "");
    var refs = (card.refs || rec.refs || []).map(function (r) {
      return (typeof r === "object" && r) ? r : { id: r };
    });
    var described = !!(card.described || rec.described);
    var skill = card.skill || rec.skill || "";

    if (!text || !H.submit) { card.redrawing = false; return; }

    if (card.userNode) H.clearFail(card.userNode);
    /* 旧记录从存档里摘掉：新卡自己的状态要独立落盘，不然刷新后同一张图
       会出现两条（一条旧状态、一条新状态）。旧卡节点留在页面上不动。 */
    if (done && H.discard && card.rec) H.discard(card.rec);
    card.redrawing = false;
    /* 气泡不重画（传 null）：新卡占一条新的用户消息挂在最底部，跟新发一条
       完全一样——用户不会以为它还在上面那格旧的里面。 */
    H.submit(text, skill, done ? [] : refs, done ? true : described,
             null, false, done);
  }

  /* 没排上队（限流）那张：整张撤掉，连用户气泡一起，提示由调用方闪一次。 */
  function drop(card) {
    if (!card) return;
    if (card.node && card.node.parentNode) card.node.parentNode.removeChild(card.node);
    if (card.userNode && card.userNode.parentNode) {
      card.userNode.parentNode.removeChild(card.userNode);
    }
    var msgs = H.msgs();
    if (card.userNode) {
      var text = card.userNode._wsUserText;
      for (var i = msgs.length - 1; i >= 0; i--) {
        if (msgs[i].k === "user" &&
            (msgs[i].id === card.userNode._wsUserId || msgs[i].t === text)) {
          msgs.splice(i, 1);
          break;
        }
      }
    }
    if (card.rec) {
      var at = msgs.indexOf(card.rec);
      if (at >= 0) msgs.splice(at, 1);
    }
    H.save();
  }

  global.WSRedraw = { bind: bind, run: run, drop: drop };
})(window);
