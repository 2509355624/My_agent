/* 单张隐藏：查看器底部那颗「隐藏 / 已隐藏」按钮，以及藏在库里那几张的
   状态同步。

   从 gallery.js 拆出来（那边已经 600 多行，测试卡着 600 的行数线）。
   拆的是**纯 UI 规矩**，不碰任何数据：拿哪本、第几张、能不能藏，全由
   调用方从 `WSGallery.viewer()` 拿到的现场传进来；请求也由调用方发。
   这样这边只管两件事——按钮长什么样、藏完的图长什么样。 */
(function (global) {
  "use strict";

  /* 没藏：一只眼；藏了：同一只眼加一道斜杠 + 实心。一个图标两种状态，
     比换两套图标更容易认出「这是同一个按钮切了状态」。 */
  var ICON_EYE =
    '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
    'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">' +
    '<path d="M2.5 12S6 6.5 12 6.5 21.5 12 21.5 12 18 17.5 12 17.5 2.5 12 2.5 12z"/>' +
    '<circle cx="12" cy="12" r="2.8"/></svg>';

  var ICON_EYE_OFF =
    '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
    'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">' +
    '<path d="M2.5 12S6 6.5 12 6.5 21.5 12 21.5 12 18 17.5 12 17.5 2.5 12 2.5 12z"/>' +
    '<circle cx="12" cy="12" r="2.8"/><path d="M4 20 20 4"/></svg>';

  /* 造按钮。只有站主会出现（`can` 为假时整颗藏起来），所以调用方不用
     再判一次权限。 */
  function makeButton(can) {
    var b = document.createElement("button");
    b.type = "button";
    b.className = "gv-act gv-hide";
    b.hidden = !can;
    paintButton(b, false);
    return b;
  }

  function paintButton(btn, on) {
    if (!btn) return;
    btn.classList.toggle("on", on);
    btn.innerHTML = (on ? ICON_EYE_OFF : ICON_EYE) +
      "<span>" + (on ? "已隐藏" : "隐藏") + "</span>";
  }

  /* 把底部按钮和每一屏大图的状态刷成当前这张的样子。
     `list` 是查看器的整本列表（每项带 `hidden`），`idx` 是当前第几张。
     全部刷而不是只刷当前那张：左右一滑就换一张，只刷一张会留下上一张的
     状态，看着像「藏错了」。 */
  function sync(opts) {
    var o = opts || {};
    var it = o.list && o.list[o.idx];
    var can = !!(o.work && o.work.can_hide_item);
    if (o.btn) {
      o.btn.hidden = !can;
      paintButton(o.btn, !!(it && it.hidden));
    }
    if (!o.track) return;
    Array.prototype.forEach.call(
      o.track.querySelectorAll(".gv-slide"), function (s, i) {
        var d = o.list && o.list[i];
        s.classList.toggle("gv-off", !!(d && d.hidden));
      });
  }

  global.WSHide = {
    makeButton: makeButton,
    sync: sync,
    icon: ICON_EYE,
    iconOff: ICON_EYE_OFF
  };
})(window);
