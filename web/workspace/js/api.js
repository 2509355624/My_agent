/* 登录接口封装：同源相对路径，带 cookie。
   页面须挂在 5175 下（那里才有 /login/* 与 /me），否则会 404。 */
(function (global) {
  "use strict";

  function post(path, body) {
    return fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify(body || {})
    }).then(function (r) {
      return r.json().catch(function () {
        return { ok: false, error: "服务器返回看不懂" };
      });
    }).catch(function () {
      return { ok: false, error: "网络断了，检查一下再试" };
    });
  }

  global.WSApi = {
    sendCode: function (qq) { return post("/login/send", { qq: qq }); },
    verify: function (qq, code) {
      return post("/login/verify", { qq: qq, code: code });
    }
  };
})(window);
