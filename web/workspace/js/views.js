/* 子页壳：顶栏（左 X、中段、右我的）+ 内容位。
   某个子页要自己接管中段或内容时，向 register() 注册即可；没有注册的
   就是现在这套默认样子（中段标题 + 一行占位）。 */
(function (global) {
  "use strict";

  var TITLES = {
    gallery: "AI 创作",
    comic: "我的漫画作品",
    book: "",
    me: "我的"
  };

  var PLACEHOLDER = {
    gallery: "画廊还在接，先回聊天页生图",
    comic: "我的漫画作品还在接",
    book: "这本打不开",
    me: "作品和收藏还在接"
  };

  // name → { top: fn(中段节点, opts), mount: fn(内容区节点, opts) }
  var CUSTOM = {};

  var ICON_ME =
    '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" ' +
    'stroke="currentColor" stroke-width="1.7" stroke-linecap="round">' +
    '<circle cx="12" cy="8.5" r="3.6"/><path d="M4.8 20c1-3.6 3.8-5.4 7.2-5.4s6.2 1.8 7.2 5.4"/>' +
    "</svg>";

  function button(attr, label, text) {
    var b = document.createElement("button");
    b.type = "button";
    b.setAttribute("aria-label", label);
    b.setAttribute(attr, "");
    if (text) b.textContent = text;
    return b;
  }

  function register(name, spec) { CUSTOM[name] = spec || {}; }

  /* 输入栏在页面上只有一份（聊天页根部）。子页盖上来时它没被摘掉，只是
     被不透明的子页压住了——所以子页里「看不见输入栏」不是没元素，是被盖住。
     这里给「要接着聊 / 接着出图」的子页把输入栏挪进自己的内容区底部，
     退回时再挪回聊天页原位（用户 2026-11-23：进 AI 创作也得能直接发话）。
     输入栏本身是绝对定位，所以宿主必须 position: relative。 */
  var HOME = null;          // { composer, before }：聊天页里的原位置
  var CHATTY = { gallery: true };  // 漫画单本页是全屏读图，不借输入栏

  function dockComposer(host, name) {
    var composer = document.getElementById("composer");
    if (!composer) return;
    if (!HOME) {
      HOME = { composer: composer, before: composer.nextSibling };
    }
    if (!CHATTY[name]) return;
    host.classList.add("v-dock");
    host.appendChild(composer);
    // 输入栏挪了家，高度基准也变了（宿主滚动条、安全区都可能不同），
    // 让聊天页重算一次 --composer-h：子页靠它给底部留白，差几十像素就会
    // 把最后一行压在玻璃下面。
    global.dispatchEvent(new Event("resize"));
  }

  function undockComposer() {
    if (!HOME || !HOME.composer.parentNode) return;
    var home = document.getElementById("chat-scroll");
    if (!home) return;
    /* 已经在原位就别再插一遍：insertBefore 对同一个节点是「先挪后插」，
       索引算错一位就会把输入栏塞到消息区前面去。 */
    var before = HOME.before && HOME.before.parentNode === home.parentNode
      ? HOME.before : home.nextSibling;
    if (HOME.composer.parentNode === home.parentNode &&
        HOME.composer.nextSibling === before) return;
    home.parentNode.insertBefore(HOME.composer, before);
    global.dispatchEvent(new Event("resize"));
  }

  /* 子页里的输入栏发出去的话，气泡得落在用户眼前这一页（聊天页那层的
     消息区被子页盖着，写进去等于石沉大海）。子页自己声明一个消息区就投给它，
     没声明就还是回聊天页。取最后一个：连叠两层时最上面那页才是眼前的。 */
  function scrollOf() {
    var all = document.querySelectorAll(".view-push [data-chat-scroll]");
    if (all.length) return all[all.length - 1];
    return document.getElementById("chat-scroll");
  }

  /* opts：push 时带进来的参数（例如打开哪一本漫画），子页自己决定怎么用 */
  function build(name, opts) {
    var spec = CUSTOM[name] || {};
    var arg = opts || {};

    var view = document.createElement("section");
    view.className = "view view-push";
    view.dataset.view = name;

    var top = document.createElement("div");
    top.className = "vtop";

    var close = button("data-close", "关闭", "✕");

    var mid = document.createElement("div");
    mid.className = "vtitle";
    mid.textContent = TITLES[name] || "";

    var me = button("data-me", "我的");
    me.innerHTML = ICON_ME;

    top.appendChild(close);
    top.appendChild(mid);
    top.appendChild(me);

    var body = document.createElement("div");
    body.className = "vbody";
    body.dataset.host = name;

    if (typeof spec.top === "function") spec.top(mid, arg);

    if (typeof spec.mount === "function") {
      spec.mount(body, arg);

    } else {
      var ph = document.createElement("p");
      ph.className = "vph";
      ph.textContent = PLACEHOLDER[name] || "";
      body.appendChild(ph);
    }

    view.appendChild(top);
    view.appendChild(body);
    dockComposer(body, name);
    /* 输入栏是从聊天页「借」来的，只有借到才需要标记；以前不管借没借都写
       dataset.dock=""，退回时对每一页都调 undock()，而 undock 按 HOME 记忆
       的原位置插回去——连着叠两层（我的 → 我的）就会把输入栏插到错的地方，
       极端情况是插进已经被摘掉的节点所在位置，DOM 结构跟着乱。 */
    if (CHATTY[name]) view.dataset.dock = "1";
    return view;
  }

  global.WSViews = {
    build: build,
    register: register,
    titles: TITLES,
    undock: undockComposer,
    scroll: scrollOf
  };
})(window);
