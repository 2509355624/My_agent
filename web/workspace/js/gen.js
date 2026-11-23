/* 生图接口：渠道清单、提交、轮询状态、取图、默认画风。
   只管发请求和存偏好，不碰 DOM。全部同源相对路径。 */
(function (global) {
  "use strict";

  var SKILL_KEY = "ws_skill";
  var CACHE = null;          // 渠道清单一次会话拿一次就够，画风抽屉与发送共用

  function channels(force) {
    if (CACHE && !force) return Promise.resolve(CACHE);
    return global.WSApi.get("/api/gen/channels").then(function (d) {
      if (d && d.ok) CACHE = d;
      return d;
    });
  }

  function submit(body) {
    return global.WSApi.post("/api/gen/submit", body);
  }

  function status() {
    return global.WSApi.get("/api/gen/status");
  }

  function img(file) {
    return "/api/gen/img?f=" + encodeURIComponent(file || "");
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
    img: img,
    defaultSkill: defaultSkill,
    setDefaultSkill: setDefaultSkill
  };
})(window);
