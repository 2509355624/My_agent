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
  var SIGN = "";             // 图片地址的站点签名，见 imgsign()

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

  /* 一句话交给大大怪判（2026-11-26）：跟 QQ 私聊同一条转译、同一份 6 轮
     历史。要画就回 `{ok, job}`（跟 submit 同形，轮询/终止都不用改）；判
     「这轮不用画」就回 `{ok, reply, say:true}`，聊天页显示成一条 AI 消息。

    和 submit 的分工：**只有聊天页那句话走这里**。重画、画同款这类「词已经
     定了、就是要这张图」的请求继续走 submit——那时候再让模型判一遍意图
     纯属浪费，还可能把它改成别的画面。 */
  function ask(body) {
    return global.WSApi.post(BASE + "/ask", body);
  }

  /* 聊天页上传的参考图：存服务端内存、两小时过期，不落作品墙。
     回 `{ok, ref:"cr_…"}`，这个 ref 跟画廊的 item id 一样挂在 refs 里带上去。 */
  function chatUpload(image) {
    return global.WSApi.post(BASE + "/chat-upload", { image: image });
  }

  function status() {
    return global.WSApi.get(BASE + "/status");
  }

  /* 按**提交时间**认领一条还在跑的任务（2026-11-25）。

     为什么要它：任务号是 `/submit` 响应回来之后才写进存档的，页面在那一刻
     刷新，存档里那条就只剩「有提示词、没任务号」。以前前端只能判它「断了」，
     可服务端还排着队。记录自己的出生时间（id 里的时间戳）是唯一还在的凭据，
     拿它去问一句，服务端知道答案。`at` 是毫秒。 */
  function adopt(at) {
    return global.WSApi.get(BASE + "/adopt?at=" + encodeURIComponent(at));
  }

  /* 终止一张（2026-11-23 用户：「加个终止按钮」）。和服务端同一个口子：
     排队中的从队列摘掉，正在画的走定向 /interrupt 并且**不会投递**。 */
  function cancel(id) {
    return global.WSApi.post(BASE + "/cancel", { id: id });
  }

  /* 图片地址。不带参数就是原图；带 `fit` 是「长边从大往小试、取第一张不超过
     这个体积」的 WebP（服务端认 `1m`）——工作台从列表、点开到下载都用它。

     **不加 `w=`**：用户要的是「看着可以、能存下来」的那一档，而 `w` 是按宽度
     切的缩略图，1024 的平涂能压到 200KB 以下，看着就糊了。
     **也不带域名**：`WSImg` 是按这条相对路径存字节的，域名一换缓存不该作废
     （见 imgcache.js 的开头）。`k=` 是站点签名，见 imgsign()。 */
  function img(file, fit) {
    var u = BASE + "/img?f=" + encodeURIComponent(file || "");
    if (fit) u += "&fit=" + encodeURIComponent(fit);
    if (SIGN) u += "&k=" + encodeURIComponent(SIGN);
    return u;
  }

  /* 缓存 key：**把签名摘掉**。签名是每次会话从 `/imgsign` 取的，服务重启
     就换一个——带着它当 key，重启一次所有本地缓存都成了孤儿。
     （域名本来就不在 key 里，见 imgcache.js 开头。） */
  function imgKey(file, fit) {
    var u = BASE + "/img?f=" + encodeURIComponent(file || "");
    if (fit) u += "&fit=" + encodeURIComponent(fit);
    return u;
  }

  /* 取一次站点签名。本地缓存要自己 fetch 一次图片字节并落到 IndexedDB，
     那条路是 `no-cors`（带不了 cookie），所以地址上得有这个签名才过得去
     5174 的令牌校验。失败就空着——页面照旧用 URL 直接显示图，只是不缓存。 */
  function imgsign() {
    if (SIGN) return Promise.resolve(SIGN);
    return global.WSApi.get(BASE + "/imgsign").then(function (d) {
      if (d && d.ok && d.token) SIGN = String(d.token);
      return SIGN;
    })["catch"](function () { return ""; });
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
    ask: ask,
    chatUpload: chatUpload,
    status: status,
    adopt: adopt,
    cancel: cancel,
    img: img,
    imgKey: imgKey,
    imgsign: imgsign,
    sign: function () { return SIGN; },
    defaultSkill: defaultSkill,
    setDefaultSkill: setDefaultSkill
  };
})(window);
