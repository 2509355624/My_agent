/* 「同步发 QQ」开关（2026-11-27）。

   用户原话：「如果用户已经登录的话，那么我们可以加一个按钮，就是默认是
   勾选绿色的，就是同步发 QQ」——他要的是「在网页上画，手机上也能收到」。

   三条口径：
     · 只有**登录用户**看得见（`role === "user"`）。访客没有 QQ，露出来就是
       个点了没反应的死按钮；5174 直连没有 /me 的那一档才当登录用户处理。
     · 默认**勾上**（绿色）。用户的选择记在 localStorage，下次进来照上次。
     · 只是「多发一份」：图在网页上照常出；QQ 那边失败不影响这张图
       （后端 `_sync_to_qq` 只在档位里留一句提示，不判失败）。

   状态由 `gen_flow.js` 提交那一刻读一次（`WSSync.on()`），跟按钮同源，
   不做第二份开关状态。
*/
(function (global) {
  "use strict";

  var KEY = "ws_qq_sync";
  var ON = true;                 // 默认勾上

  function el() { return document.getElementById("tog-sync"); }

  function load() {
    try {
      if (global.localStorage.getItem(KEY) === "0") ON = false;
    } catch (e) { /* 隐私模式下 localStorage 会抛，照默认走 */ }
  }

  function paint() {
    var b = el();
    if (!b) return;
    if (ON) { b.classList.add("on"); } else { b.classList.remove("on"); }
    b.setAttribute("aria-pressed", ON ? "true" : "false");
    b.title = ON ? "出图后同步发一份到你的 QQ（点击关闭）"
                 : "只在这里出图，不发 QQ（点击开启）";
  }

  /* 身份回来才决定露不露：`/me` 之前 `role` 还是默认值，按它显示会先闪
     一下再消失（访客那一档就是这么闪的）。 */
  function apply(st) {
    var b = el();
    if (!b) return;
    b.hidden = !st || !st.loaded || st.role !== "user";
  }

  function toggle() {
    ON = !ON;
    try { global.localStorage.setItem(KEY, ON ? "1" : "0"); } catch (e) {}
    paint();
  }

  function init() {
    load();
    paint();
    var b = el();
    if (b) b.addEventListener("click", toggle);
    /* 两条都走：`load()` 是异步的，谁先到就谁定——已经回来了直接用，
       还没回来就等 `ws:role`（见 visitor.js 的广播）。 */
    var v = global.WSVisitor;
    if (v && v.state) apply(v.state());
    global.addEventListener("ws:role", function (e) {
      apply(e && e.detail);
    });
  }

  global.WSSync = {
    on: function () { return ON; },
    init: init
  };
})(window);
