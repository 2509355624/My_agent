/* 「直达」的回执处理：一次请求回 N 条任务，接成 N 张卡。

   从 gen_flow.js 拆出来的（2026-11-26）：那边主体是「一张卡发一次请求」的
   轮询状态机，直达是「一次请求回 N 条」，只共用 pending / 存档 / 终止三样
   （从 `WSGenFlow.claim` 等口子借）。一张需求只留一条用户气泡。 */
(function (global) {
  "use strict";

  /* 把卡接进本台的轮询（pending / 终止 / 存档都在 gen_flow 那一侧）。 */
  function claim(card) {
    if (global.WSGenFlow && global.WSGenFlow.claim) {
      global.WSGenFlow.claim(card);
    }
  }

  function linkBatch(d, text, userNode) {
    var jobs = (d && d.jobs) || [];
    var flow = global.WSGenFlow;
    if (!d || !d.ok || !jobs.length) {
      if (flow && flow.dropCard && userNode) flow.dropCard(userNode, text);
      if (flow && flow.busyTip) {
        flow.busyTip((d && d.error) || "没排上队，等一下再发");
      }
      return 0;
    }
    jobs.forEach(function (j, i) {
      var rec = { k: "img", cls: "", prompt: j.prompt || "",
                  ask: text, skill: j.skill || "",
                  id: flow.newId ? flow.newId("img") : "",
                  refs: [], described: true, route: "direct",
                  userId: userNode ? (userNode._wsUserId || "") : "",
                  userText: text, job: j.id || "", file: "",
                  note: "正在跑，画好会自动弹出来", err: false,
                  batch: { n: jobs.length, at: i + 1 } };
      var card = flow.newCard(rec.prompt || text, rec.skill, rec);
      card.rec = rec;
      card.refs = [];
      card.described = true;
      /* 气泡只给第一张：一条需求五张图，一条气泡才对。后面四张卡靠
         `batch` 那个序号自己认位置（挂同一个节点的话，失败行会在同一条
         气泡下面叠五遍）。 */
      card.userNode = i === 0 ? userNode : null;
      card.since = Date.now();
      flow.save(rec);
      if (!j.id) {
        flow.fail(card, "这张没排上队，等一下再点一次");
        return;
      }
      card.job = j.id;
      claim(card);
    });
    /* 访客额度：batch 回执里每张都带同一个 `visitor_left`，取一条就够。 */
    if (jobs[0].visitor && global.WSVisitor) {
      global.WSVisitor.syncLeft(jobs[0].visitor_left);
    }
    if (d.note && flow.busyTip) flow.busyTip(d.note);
    if ((d.fail || []).length && flow.busyTip) flow.busyTip(d.fail[0]);
    if (flow.kick) flow.kick();
    return jobs.length;
  }

  global.WSDirect = { linkBatch: linkBatch };
})(window);
