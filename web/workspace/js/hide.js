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

  /* 存一张图到手机。**不能再用 `<a download href=图片地址>`**。

     2026-11-24 站长报「下载的图片只有 48b，打开显示损坏，群友号没有这个问题」。
     原因：`<a download>` 是导航式下载——它不查 HTTP 缓存，走的是**顶层导航
     的凭据链**；站长这边图片接口要鉴权，拿回来的是错误响应的 JSON（正好
     48 字节），存成 .webp 自然打不开。群友走网关吃的是常驻 cookie，碰巧没事。

     现在和聊天页同一个套路：先问本地缓存（显示时顺手存过），没有就 fetch
     原始字节，再从 blob 存盘。只有最后一步用 `<a download>`，而那时 href 是
     `blob:`，和登录态一点关系都没有。 */
  function save(o) {
    var opt = o || {};
    var url = opt.url || "";
    var name = opt.name || "image.webp";
    var fail = typeof opt.onFail === "function" ? opt.onFail : function () {};
    if (!url) return;

    function fire(blob) {
      var u = global.URL.createObjectURL(blob);
      var a = document.createElement("a");
      a.href = u;
      a.download = name;
      document.body.appendChild(a);
      a.click();
      a.remove();
      global.setTimeout(function () { global.URL.revokeObjectURL(u); }, 4000);
    }
    function fromNet() {
      global.fetch(url, { credentials: "same-origin" }).then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.blob();
      }).then(function (b) {
        /* 2026-11-25：拿回来的必须真的是图。签名过期 / 换域名时服务端回的是
           401/404 的 JSON，存成 .webp 就是一个 48 字节的坏文件——聊天页那边
           用户已经踩到了（小米浏览器点下载存下 48B）。 */
        return looksLikeImage(b).then(function (ok) {
          if (!ok) throw new Error("不是图片");
          fire(b);
          if (global.WSImg) global.WSImg.put(url, b).catch(function () {});
        });
      })["catch"](fail);
    }
    if (global.WSImg) {
      global.WSImg.get(url).then(function (b) {
        return looksLikeImage(b).then(function (ok) {
          if (ok) { fire(b); return; }
          fromNet();
        });
      })["catch"](fromNet);
      return;
    }
    fromNet();
  }

  /* 文件头认图：WebP / PNG / JPEG / GIF。类型明确是 image/* 就直接过。
     读不出前 12 字节（opaque 响应）时按「不是图」处理——宁可重下一次，
     也不要把一个坏文件存进相册。 */
  function looksLikeImage(blob) {
    if (!blob || !blob.size) return Promise.resolve(false);
    if (blob.size < 200) return Promise.resolve(false);   // 48B 那种 JSON 错误体
    if (blob.type && blob.type.indexOf("image/") === 0) return Promise.resolve(true);
    return blob.slice(0, 12).arrayBuffer().then(function (buf) {
      var b = new Uint8Array(buf);
      if (b.length < 4) return false;
      if (b[0] === 0x52 && b[1] === 0x49 && b[2] === 0x46 && b[3] === 0x46) return true;
      if (b[0] === 0x89 && b[1] === 0x50 && b[2] === 0x4e && b[3] === 0x47) return true;
      if (b[0] === 0xff && b[1] === 0xd8 && b[2] === 0xff) return true;
      if (b[0] === 0x47 && b[1] === 0x49 && b[2] === 0x46) return true;
      return false;
    })["catch"](function () { return false; });
  }

  global.WSHide = {
    makeButton: makeButton,
    sync: sync,
    save: save,
    icon: ICON_EYE,
    iconOff: ICON_EYE_OFF
  };
})(window);
