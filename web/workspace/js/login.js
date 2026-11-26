/* 登录两步流程：QQ 号页 → 验证码页。校验同后端，倒计时可清理。 */
(function (global) {
  "use strict";

  var QQ_RE = /^[1-9][0-9]{4,11}$/;
  var TIMER = null;
  var el = {};

  function $(id) { return document.getElementById(id); }

  function say(text, cls) {
    // 两个步骤各有一个消息区，只写当前可见的那个，避免提示落到隐藏页。
    [el.msg, el.codeMsg].forEach(function (node) {
      node.textContent = text || "";
      node.className = "msg" + (cls ? " " + cls : "");
    });
  }

  function showStep(name) {
    el.qqStep.style.display = name === "qq" ? "" : "none";
    el.codeStep.style.display = name === "code" ? "" : "none";
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
    var qq = el.qq.value.trim();
    if (!QQ_RE.test(qq)) { say("QQ 号看着不对，检查一下", "err"); return; }
    global.sessionStorage.setItem("ws_qq", qq);
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
        global.location.replace("/workspace/chat.html");
        return;
      }
      say((d && d.error) || "进不去，稍后再试", "err");
      if (el.visitor) el.visitor.disabled = false;
    });
  }

  function init() {
    el.qqStep = $("step-qq");
    el.codeStep = $("step-code");
    el.qq = $("qq");
    el.code = $("code");
    el.msg = $("login-msg");
    el.codeMsg = $("code-msg");
    el.next = $("btn-next");
    el.send = $("btn-send");
    el.login = $("btn-login");
    el.back = $("btn-back");
    el.visitor = $("btn-visitor");

    el.next.addEventListener("click", onNext);
    el.send.addEventListener("click", onSend);
    el.login.addEventListener("click", onLogin);
    el.back.addEventListener("click", onBack);
    if (el.visitor) el.visitor.addEventListener("click", onVisitor);
    el.code.addEventListener("input", function () {
      el.login.disabled = el.code.value.trim().length < 6;
    });
    el.qq.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { onNext(); }
    });
    el.code.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !el.login.disabled) { onLogin(); }
    });
  }

  global.WSLogin = { init: init };
})(window);
