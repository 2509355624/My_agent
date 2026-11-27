/* 登录三种走法：QQ 号 → 验证码；字母开头的账号 → 密码（测试用）；访客直进。
   校验同后端，倒计时可清理。 */
(function (global) {
  "use strict";

  var QQ_RE = /^[1-9][0-9]{4,11}$/;
  /* 测试账号（2026-11-27）：字母开头、3~32 位。命中它就切到密码那一步。
     真实 QQ 号全是数字，两者不会撞；后端也只认 `.env` 里配的那一个。 */
  var ACCT_RE = /^[A-Za-z][A-Za-z0-9_.-]{2,31}$/;
  var TIMER = null;
  var el = {};

  /* 子页栈的键（stack.js 里同名）。登录 / 进访客成功时要先把它丢掉：
     不然新页面会从 sessionStorage 把上次那个子页（比如「我的」）恢复出来，
     人一进来先看见一副不该看见的样子，再等 `/me` 回来才换掉——用户
     2026-11-26 报的「会先在我的那个界面闪一下作品和收藏、退出登录」就是它。 */
  var VIEW_KEY = "ws_view_v1";

  function dropViews() {
    try { global.sessionStorage.removeItem(VIEW_KEY); } catch (e) { /* 隐私模式 */ }
  }

  function $(id) { return document.getElementById(id); }

  function say(text, cls) {
    // 三个步骤各有一个消息区，都写一遍，避免提示落到隐藏页。
    [el.msg, el.codeMsg, el.passMsg].forEach(function (node) {
      if (!node) return;
      node.textContent = text || "";
      node.className = "msg" + (cls ? " " + cls : "");
    });
  }

  function showStep(name) {
    el.qqStep.style.display = name === "qq" ? "" : "none";
    el.codeStep.style.display = name === "code" ? "" : "none";
    if (el.passStep) el.passStep.style.display = name === "pass" ? "" : "none";
  }

  function stopTimer() {
    if (TIMER) { clearInterval(TIMER); TIMER = null; }
  }

  function countdown(sec) {
    stopTimer();
    el.send.disabled = true;
    el.send.textContent = sec + " 秒后重发";
    TIMER = setInterval(function () {
      sec -= 1;
      if (sec <= 0) {
        stopTimer();
        el.send.disabled = false;
        el.send.textContent = "获取验证码";
      } else {
        el.send.textContent = sec + " 秒后重发";
      }
    }, 1000);
  }

  function onNext() {
    var v = el.qq.value.trim();
    /* 测试账号先判（2026-11-27）：它不是 QQ 号，先撞 QQ_RE 会被打成「填错了」。
       切到密码页，账号先记在 sessionStorage 里，和 QQ 那条各存各的键。 */
    if (ACCT_RE.test(v)) {
      global.sessionStorage.setItem("ws_acct", v);
      showStep("pass");
      say("", "");
      if (el.passSub) el.passSub.textContent = "账号 " + v;
      if (el.pass) { el.pass.value = ""; el.pass.focus(); }
      return;
    }
    if (!QQ_RE.test(v)) { say("QQ 号看着不对，检查一下", "err"); return; }
    global.sessionStorage.setItem("ws_qq", v);
    showStep("code");
    say("验证码会私聊发到你的 QQ", "");
    el.code.focus();
  }

  function onSend() {
    var qq = global.sessionStorage.getItem("ws_qq") || "";
    if (!QQ_RE.test(qq)) { say("先回去把 QQ 号填好", "err"); showStep("qq"); return; }
    el.send.disabled = true;
    say("正在发…", "");
    global.WSApi.sendCode(qq).then(function (d) {
      if (d.ok) {
        say(d.message || "验证码已发出", "ok");
        countdown(60);
        el.code.focus();
      } else {
        say(d.error || d.message || "发不出去", "err");
        el.send.disabled = false;
      }
    });
  }

  function onLogin() {
    var qq = global.sessionStorage.getItem("ws_qq") || "";
    var code = el.code.value.trim();
    if (!/^\d{6}$/.test(code)) { say("把收到的 6 位数字填上", "err"); return; }
    el.login.disabled = true;
    say("验证中…", "");
    global.WSApi.verify(qq, code).then(function (d) {
      if (d.ok) {
        // 让服务器重判落点；前端直接切状态会停在旧地址，一刷新就打回登录页。
        // 2026-11-26：改成跳地址，顺手把上次的子页栈（`ws_view_v1`）甩掉，
        // 否则登录成功也会被 restoration 送回「我的」那一页。
        stopTimer();
        dropViews();
        global.location.replace("/workspace/chat.html");
      } else {
        say(d.error || "验证码不对", "err");
        el.login.disabled = false;
      }
    });
  }

  function onBack() {
    stopTimer();
    showStep("qq");
    say("", "");
  }

  /* 测试账号登录（2026-11-27）：密码原样送，不 trim——密码里的空格算数。
     成功后的收尾和 QQ 那条完全一样（甩掉子页栈再跳），否则登录成功也会被
     restoration 送回「我的」那一页。 */
  function onPassLogin() {
    var acct = global.sessionStorage.getItem("ws_acct") || "";
    var pwd = el.pass ? el.pass.value : "";
    if (!acct) { showStep("qq"); say("先回去把账号填好", "err"); return; }
    if (!pwd) { say("密码不能空着", "err"); return; }
    el.passLogin.disabled = true;
    say("正在验证…", "");
    global.WSApi.localLogin(acct, pwd).then(function (d) {
      if (d && d.ok) {
        stopTimer();
        dropViews();
        global.location.replace("/workspace/chat.html");
        return;
      }
      say((d && (d.error || d.message)) || "进不去，稍后再试", "err");
      el.passLogin.disabled = false;
    });
  }

  function onPassBack() {
    showStep("qq");
    say("", "");
  }

  function onVisitor() {
    /* 访客模式（2026-11-26）：不收码、不要 QQ，后端签一个 24 小时的身份
       直接下 cookie，然后让服务器重判落点（和登录成功那条同一个做法）。 */
    el.visitor.disabled = true;
    say("正在进访客模式…", "");
    global.WSApi.post("/login/visitor", {}).then(function (d) {
      if (d && d.ok) {
        stopTimer();
        /* 用**跳地址**而不是 reload（2026-11-26）：reload 会让工作台从
           sessionStorage 把上次那个子页栈（比如「我的」）恢复回来，访客一
           进来就落在「还没登录」那页上。换个地址等于从干净首屏开始。 */
        dropViews();
        global.location.replace("/workspace/chat.html");
        return;
      }
      say((d && d.error) || "进不去，稍后再试", "err");
      if (el.visitor) el.visitor.disabled = false;
    });
  }

  /* 手上已经有访客证？把按钮改成「继续以访客身份使用」（2026-11-26）。
     访客点「登录」现在不摘证了（见 visitor.js 的 leaveToLogin），所以到这一页
     他其实还是同一个访客。按钮还写「先以访客身份逛逛」，会让人以为要重新领
     一张——额度看起来就「刷新」了。`/login/visitor` 那边是 `resign()`，沿用
     同一个 id 只续期，点了接着算。 */
  function labelVisitor() {
    if (!el.visitor) return;
    global.WSApi.get("/me").then(function (d) {
      if (!d || d.role !== "visitor") return;
      var left = d.quota ? Number(d.quota.left) : NaN;
      el.visitor.textContent = isFinite(left)
        ? "继续以访客身份使用（今天还剩 " + left + " 张）"
        : "继续以访客身份使用";
    });
  }

  function init() {
    el.qqStep = $("step-qq");
    el.codeStep = $("step-code");
    el.passStep = $("step-pass");
    el.qq = $("qq");
    el.code = $("code");
    el.pass = $("pass");
    el.msg = $("login-msg");
    el.codeMsg = $("code-msg");
    el.passMsg = $("pass-msg");
    el.next = $("btn-next");
    el.send = $("btn-send");
    el.login = $("btn-login");
    el.back = $("btn-back");
    el.passLogin = $("btn-pass-login");
    el.passBack = $("btn-pass-back");
    el.passSub = $("pass-sub");
    el.visitor = $("btn-visitor");

    el.next.addEventListener("click", onNext);
    el.send.addEventListener("click", onSend);
    el.login.addEventListener("click", onLogin);
    el.back.addEventListener("click", onBack);
    if (el.passLogin) el.passLogin.addEventListener("click", onPassLogin);
    if (el.passBack) el.passBack.addEventListener("click", onPassBack);
    if (el.visitor) el.visitor.addEventListener("click", onVisitor);
    el.code.addEventListener("input", function () {
      el.login.disabled = el.code.value.trim().length < 6;
    });
    if (el.pass) {
      el.pass.addEventListener("keydown", function (e) {
        if (e.key === "Enter") { onPassLogin(); }
      });
    }
    el.qq.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { onNext(); }
    });
    el.code.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !el.login.disabled) { onLogin(); }
    });
    labelVisitor();
  }

  global.WSLogin = { init: init };
})(window);
