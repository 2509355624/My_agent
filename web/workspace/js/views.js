/* 子页壳：顶栏（左 X、中标题、右我的）+ 内容位。
   本期只有空壳，内容由后续模块往 [data-host] 里填。 */
(function (global) {
  "use strict";

  var TITLES = {
    gallery: "AI 创作",
    comic: "我的漫画作品",
    me: "我的"
  };

  var PLACEHOLDER = {
    gallery: "画廊还在接，先回聊天页生图",
    comic: "我的漫画作品还在接",
    me: "作品和收藏还在接"
  };

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

  function build(name) {
    var view = document.createElement("section");
    view.className = "view view-push";
    view.dataset.view = name;

    var top = document.createElement("div");
    top.className = "vtop";

    var close = button("data-close", "关闭", "✕");

    var title = document.createElement("div");
    title.className = "vtitle";
    title.textContent = TITLES[name] || "";

    var me = button("data-me", "我的");
    me.innerHTML = ICON_ME;

    top.appendChild(close);
    top.appendChild(title);
    top.appendChild(me);

    var body = document.createElement("div");
    body.className = "vbody";
    body.dataset.host = name;

    var ph = document.createElement("p");
    ph.className = "vph";
    ph.textContent = PLACEHOLDER[name] || "";
    body.appendChild(ph);

    view.appendChild(top);
    view.appendChild(body);
    return view;
  }

  global.WSViews = { build: build, titles: TITLES };
})(window);
