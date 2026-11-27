/* 同源 JSON 请求 + 登录接口。
   页面须挂在 5175 下（那里才有 /login/* 与 /me），否则登录会 404。 */
(function (global) {
  "use strict";

  function req(path, opt) {
    return fetch(path, Object.assign({ credentials: "same-origin" }, opt || {}))
      .then(function (r) {
        return r.json().catch(function () {
          /* 带上路径和状态码（2026-11-26）：原来只有一句「服务器返回看不懂」，
             服务端日志里一个 5xx 都没有，根本不知道是哪个接口、错在哪一步。 */
          return { ok: false, error: "服务器返回看不懂（" + path + " " + r.status + "）" };
        });
      })
      .catch(function () {
        /* `offline`（2026-11-26）：这一次**根本没拿到应答**（断网、页面正在
           关闭、隧道断了），不是服务端说「不行」。调用方必须分开处理：
           提交被系统掐断时任务其实已经在服务端排上了，把它当成「明确拒绝」
           写进存档，就成了一张永远说不清的失败卡（用户实测：图早就画好了，
           回来只有一句「网络断了」）。 */
        return { ok: false, offline: true, error: "网络断了，检查一下再试" };
      });
  }

  function get(path) { return req(path); }

  function post(path, body) {
    return req(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {})
    });
  }

  global.WSApi = {
    get: get,
    post: post,
    sendCode: function (qq) { return post("/login/send", { qq: qq }); },
    verify: function (qq, code) {
      return post("/login/verify", { qq: qq, code: code });
    },
    /* 测试账号（2026-11-27）：账号 + 密码，不走 QQ。后端只认 .env 里配的那个。 */
    localLogin: function (user, password) {
      return post("/login/local", { user: user, password: password });
    }
  };
})(window);
