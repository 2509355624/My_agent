/* 生图接口：渠道清单、提交、轮询状态、取图、默认画风。
   只管发请求和存偏好，不碰 DOM。全部同源相对路径。

   ⚠️ 前缀走 `/api/gen/*`：5174（站主直连）本来挂的就是这个前缀，5175（群友
   网关，只反代白名单）为此新加了 `/api/gen/<path>` 反代——读接口放行、
   提交要登录。同一个地址在本机、网关、Cloudflare 隧道下都能走通；
   换成别的前缀两边必有一边 404，表现就是「没拿到渠道列表」。 */
(function (global) {
  "use strict";

  var BASE = "/api/gen";
  var SKILL_KEY = "ws_skill";
  var CACHE = null;          // 渠道清单一次会话拿一次就够，画风抽屉与发送共用

  function channels(force) {
    if (CACHE && !force) return Promise.resolve(CACHE);
    return global.WSApi.get(BASE + "/channels").then(function (d) {
      if (d && d.ok) CACHE = d;
      return d;
    });
  }

  function submit(body) {
    return global.WSApi.post(BASE + "/submit", body);
  }

  function status() {
    return global.WSApi.get(BASE + "/status");
  }

  /* 终止一张（2026-11-23 用户：「加个终止按钮」）。和服务端同一个口子：
     排队中的从队列摘掉，正在画的走定向 /interrupt 并且**不会投递**。 */
  function cancel(id) {
    return global.WSApi.post(BASE + "/cancel", { id: id });
  }

  /* 图片地址。`w` 是缩略图宽度（2026-11-24）：原图常有 2~8MB，聊天区
     和画廊列表里铺十几张就是几十 MB 全走家用上行。带上 w 服务端发
     「长边 w 的 JPEG」，一张 30~70KB；**不带 w 就是原图**，点开大图、
     下载都走那条。服务端只认 240/480/640/900 这几个宽度，其它值当没传。 */
  function img(file, w) {
    var u = BASE + "/img?f=" + encodeURIComponent(file || "");
    if (w) u += "&w=" + encodeURIComponent(w);
    return u;
  }

  function defaultSkill() {
    try { return global.localStorage.getItem(SKILL_KEY) || ""; } catch (e) { return ""; }
  }

  function setDefaultSkill(id) {
    try { global.localStorage.setItem(SKILL_KEY, id || ""); } catch (e) { /* 隐私模式忽略 */ }
  }

  global.WSGen = {
    channels: channels,
    submit: submit,
    status: status,
    cancel: cancel,
    img: img,
    defaultSkill: defaultSkill,
    setDefaultSkill: setDefaultSkill
  };
})(window);
