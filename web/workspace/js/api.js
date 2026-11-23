/* 同源 JSON 请求 + 登录接口。
   页面须挂在 5175 下（那里才有 /login/* 与 /me），否则登录会 404。 */
(function (global) {
  "use strict";

  function req(path, opt) {
    return fetch(path, Object.assign({ credentials: "same-origin" }, opt || {}))
      .then(function (r) {
        return r.json().catch(function () {
          return { ok: false, error: "服务器返回看不懂" };
        });
      })
      .catch(function () {
        return { ok: false, error: "网络断了，检查一下再试" };
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
    }
  };
})(window);
