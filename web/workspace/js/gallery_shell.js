/* 画廊子页的壳：顶栏标签条、内层滚动容器、两列网格、查看器。

   从 `gallery.js` 拆出来（2026-11-24）。那边有 600 行硬线，而这里全是
   「造节点、挂监听」的死代码，和「数据怎么来、状态怎么变」本来就是两件事。
   拆的时间点：加完「网格回缩略图 / 下载走 blob / 标签栏不重建」之后。

   约定：本文件**不碰接口、不改状态**。`el`（节点表）由调用方持有并传进来，
   这里只往里填；点标签 / 滚到底该干什么，由调用方给的回调决定。 */
(function (global) {
  "use strict";

  function mountTop(title, el) {
    title.className = "gtabs";
    title.textContent = "";
    el.tabs = title;
  }

  function mountBody(body, el, v, hooks) {
    var api = hooks || {};
    el.body = body;
    body.classList.add("gbody");

    /* 两层：外层是子页内容区（不滚，见 chat.css 的 .vbody.v-dock），里层才是
       滚动容器。这样底部留白不会跟着滚动跑，输入栏钉在底下不动。 */
    el.scroll = document.createElement("div");
    el.scroll.className = "gscroll";
    el.scroll.setAttribute("data-scroll", "");

    // 从这一页发的话要落在这一页：输入栏被借到这里（见 views.js 的 dock），
    // 聊天页那层消息区被盖住了，写进去等于石沉大海。
    el.chat = document.createElement("div");
    el.chat.className = "gchat";
    el.chat.setAttribute("data-chat-scroll", "");

    el.grid = document.createElement("div");
    el.grid.className = "ggrid";

    el.more = document.createElement("button");
    el.more.type = "button";
    el.more.className = "gmore";
    el.more.textContent = "加载更多";
    el.more.hidden = true;
    el.more.addEventListener("click", function () {
      if (api.load) api.load(false);
    });

    el.scroll.appendChild(el.grid);
    el.scroll.appendChild(el.more);
    el.scroll.appendChild(el.chat);
    body.appendChild(el.scroll);

    if (api.buildViewer) api.buildViewer();
    if (api.load) api.load(true);
  }

  global.WSGalleryShell = { mountTop: mountTop, mountBody: mountBody };
})(window);
