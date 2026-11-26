/* 画廊列表的「谁的回包算数」——一次换分类能不能打断正在飞的那次请求。
 *
 * 为什么单独一个文件：`gallery.js` 有 600 行硬线
 * （`tests/test_workspace_gallery.py::test_module_files_exist_and_stay_small`），
 * 而这段逻辑有分支、有注释，塞进去就溢出了。
 *
 * ## 修的是什么
 *
 * 2026-11-26 用户实测：「快速连续点几个标签，会卡住，实际内容跟我选的标签
 * 不一致，漫画那边特别明显」。原文那版 `load()` 头一句是
 *
 *     if (S.busy) return;
 *
 * 它本来挡的是「同一面墙滚到底再翻一页」（翻页不能并发）。可换分类走的是
 * 同一个函数，于是快速连点时除了第一下，后面的点击全被吞掉：
 *
 *   1. `S.grp` 已经改成新分类，标签高亮也跟着换了；
 *   2. 那一发请求**根本没发出去**（被 busy 挡回）；
 *   3. 之前那个旧分类的包回来了，它照自己的 `page` 往网格里塞卡片。
 *
 * → 高亮在 A，内容停在 B。看起来就是「卡住」。
 *
 * ## 现在的口径
 *
 *   · **换分类（reset）永远发得出去**，并且把还没回来的那次作废；
 *   · **翻页（续页）不打断**：换分类和翻页撞在一起时，丢哪边都会缺东西。
 *     翻页请求飞着的时候又点了新分类，翻页那发包回来就作废（序号对不上）。
 *
 * 序号（`seq`）就是「第几次换分类」。发包前拿号，回包时核号，对不上整个丢掉
 * ——连 `busy` 都不碰，因为那是新请求的。
 */
(function (global) {
  "use strict";

  /* 造一台发号机。画廊是单例，所以 `gallery.js` 建一次就够。 */
  function it() {
    var seq = 0;
    var busy = false;

    /* 这次请求归不归我发 → 返回要带着的号；不该发的返回 null。 */
    function claim(reset) {
      if (reset) {
        seq += 1;
        busy = true;
        return seq;
      }
      if (busy) return null;      // 续页不许并发
      busy = true;
      return seq;
    }

    /* 这个回包还是不是「当前」那一次 → true 才许往界面上写。 */
    function owns(n) { return n === seq; }

    /* 让外部（比如「重画完回来刷新」）能把 busy 清掉。 */
    function done() { busy = false; }

    return { claim: claim, owns: owns, done: done };
  }

  /* 滚到底自动加页：`.gmore` 进视野就叫一次 `hit()`。
     老 WebView 没有 IntersectionObserver（安卓 8 及更早），返回 null 就退成
     「点按钮加载」，不报错。传进来的是上一台观察器，先掐掉它——同一时刻只该
     盯一个哨兵，否则翻页会在两个观察器之间来回触发。 */
  function watch(prev, sentinel, root, hit) {
    if (prev) prev.disconnect();
    if (!global.IntersectionObserver || !sentinel) return null;
    var io = new global.IntersectionObserver(function (rows) {
      if (rows[0] && rows[0].isIntersecting) hit();
    }, { root: root, rootMargin: "200px" });
    io.observe(sentinel);
    return io;
  }

  global.WSTake = { it: it, watch: watch };
})(window);
