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
    var done = typeof opt.onDone === "function" ? opt.onDone : function () {};
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
      savePanel(blob, u, name);
      done();
    }
    function fromNet() {
      global.fetch(url, { credentials: "same-origin" }).then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.blob();
      }).then(function (b) {
        if (!b || !b.size) throw new Error("empty");
        fire(b);
        if (global.WSImg) global.WSImg.put(opt.key || url, b).catch(function () {});
      })["catch"](fail);
    }
    if (global.WSImg) {
      global.WSImg.get(opt.key || url).then(function (b) {
        if (b && b.size) { fire(b); return; }
        fromNet();
      })["catch"](fromNet);
      return;
    }
    fromNet();
  }

  // 自动下载可能被手机内置浏览器拦截，保留可直接操作的图片与保存入口。
  function savePanel(blob, url, name) {
    var panel = document.createElement("div");
    panel.style.cssText = "position:fixed;inset:0;z-index:10000;background:var(--bg,#fff);color:var(--text,#222);padding:24px;overflow:auto;display:flex;flex-direction:column;align-items:center;gap:16px";
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-label", "保存图片");
    var tip = document.createElement("p");
    tip.textContent = "若没有弹出下载，请点击保存图片；手机也可以长按下方图片保存。";
    var image = document.createElement("img");
    image.src = url;
    image.alt = "长按保存图片";
    image.style.cssText = "max-width:100%;max-height:65vh;object-fit:contain";
    var link = document.createElement("a");
    link.href = url;
    link.download = name;
    link.textContent = "保存图片";
    var close = document.createElement("button");
    close.type = "button";
    close.textContent = "关闭";
    close.onclick = function () {
      panel.remove();
      global.setTimeout(function () { global.URL.revokeObjectURL(url); }, 60000);
    };
    panel.appendChild(tip);
    panel.appendChild(link);
    panel.appendChild(image);
    if (global.navigator && global.navigator.canShare && global.File) {
      var file = new global.File([blob], name, { type: blob.type || "image/webp" });
      if (global.navigator.canShare({ files: [file] })) {
        var share = document.createElement("button");
        share.type = "button";
        share.textContent = "系统保存 / 分享";
        share.onclick = function () {
          global.navigator.share({ files: [file] }).catch(function (e) {
            if (e.name !== "AbortError") tip.textContent = "系统保存未打开，请长按图片保存。";
          });
        };
        panel.appendChild(share);
      }
    }
    panel.appendChild(close);
    document.body.appendChild(panel);
  }

  // 两个下载入口共用按钮反馈；浏览器接管后只能确认已发起下载。
  function downloadButton(btn, options) {
    if (!btn || btn.disabled) return;
    var label = btn.innerHTML;
    btn.disabled = true;
    btn.setAttribute("aria-busy", "true");
    btn.textContent = "下载中…";
    function finish(text) {
      btn.textContent = text;
      btn.removeAttribute("aria-busy");
      global.setTimeout(function () {
        btn.innerHTML = label;
        btn.disabled = false;
      }, 1800);
    }
    save(Object.assign({}, options, {
      onDone: function () { finish("已打开保存图片"); },
      onFail: function () { finish("下载失败，请重试"); }
    }));
  }

  global.WSHide = {
    makeButton: makeButton,
    sync: sync,
    save: save,
    downloadButton: downloadButton,
    icon: ICON_EYE,
    iconOff: ICON_EYE_OFF
  };
})(window);
