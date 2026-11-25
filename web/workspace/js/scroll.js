/* 消息区的滚动：落到底、以及「进来就停在最底下」这段短暂锁定。
   从 chat.js 拆出来的——那边已经贴着 600 行上限，而这块跟消息内容无关，
   只跟一个滚动容器打交道。

   为什么需要「锁定」而不是滚一次就完（2026-11-25 用户：「每一次进入浏览器，
   默认就是来到最底下」）：页面打开时历史是**异步**重放出来的（先取图片
   签名，再拉存档），图又是懒加载的——一张图解码完高度才撑开，滚动条立刻
   被顶上去一截。只滚一次，用户看到的就是「停在一堆图上面」。

   锁定只维持很短一段（默认 1.2s），而且用户自己往上一滑就立刻放手：
   这是「进来默认在底部」，不是「不许往上翻」。 */
(function (global) {
  "use strict";

  var PIN_MS = 1200;

  var el = null;        // 滚动容器（#chat-scroll）
  var pinUntil = 0;     // 截止时间戳；0 = 没锁

  function scrollBottom() {
    if (el) el.scrollTop = el.scrollHeight;
  }

  function paintBottom() {
    scrollBottom();
    /* 两帧后再来一次：重放的地一步 / 图片落位通常发生在这一帧之后。
       有的环境（测试桩、老 WebView）没有 rAF，退回 setTimeout。 */
    if (typeof global.requestAnimationFrame === "function") {
      global.requestAnimationFrame(function () {
        global.requestAnimationFrame(scrollBottom);
      });
    } else {
      setTimeout(scrollBottom, 16);
      setTimeout(scrollBottom, 120);
    }
  }

  /* 落到最底，并在接下来的一小段时间里继续钉住。`ms` 可覆盖。 */
  function pinBottom(ms) {
    pinUntil = Date.now() + (ms || PIN_MS);
    paintBottom();
  }

  function releasePin() { pinUntil = 0; }

  function pinned() { return pinUntil > 0 && Date.now() <= pinUntil; }

  /* 挂在真正的滚动容器上。两条监听：
       · scroll —— 用户自己往上翻超过一屏，就解除锁定（他在看历史，别跟他抢）；
       · load   —— 图解码完高度变了，顺手补一次滚底。`load` 不冒泡，
                   所以必须用 capture 才收得到。 */
  function attach(node) {
    if (!node) return;
    el = node;
    el.addEventListener("scroll", function () {
      if (!pinUntil) return;
      if (Date.now() > pinUntil) { pinUntil = 0; return; }
      /* 判据只看「离底多远」：超过一屏就是存心往上翻。用 clientHeight 而
         不是固定像素，手机和桌面都合适。 */
      if (el.scrollHeight - el.scrollTop - el.clientHeight > el.clientHeight) {
        releasePin();
      }
    }, { passive: true });
    el.addEventListener("load", function (e) {
      var t = e.target;
      if (pinned() && t && t.tagName === "IMG") scrollBottom();
    }, true);
  }

  global.WSScroll = {
    attach: attach,
    bottom: scrollBottom,
    pin: pinBottom,
    release: releasePin,
    pinned: pinned
  };
})(window);
