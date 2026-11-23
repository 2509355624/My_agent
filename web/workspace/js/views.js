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

  function register(name, spec) {
    CUSTOM[name] = spec || {};
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
    return view;
  }

  global.WSViews = { build: build, register: register, titles: TITLES };
})(window);
