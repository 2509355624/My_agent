/* 出图那一张图本身的三件事：铺上去、点开放大、存到本机。
   从 chat.js 拆出来的（2026-11-24）——那边已经一千多行，这三件事只跟
   「一张已经出好的图」有关，跟排队 / 存档 / 重画都没关系。

   和服务端的约定一个字没改：
     · 铺的是**≤1MB 那一档**（`/api/gen/img?fit=`），不是原图；
     · 点开只是把这一张放大，**不再去拉原图**；
     · 下载先问本地缓存（显示时顺手存过），没有才走网络。 */
(function (global) {
  "use strict";

  /* 聊天区铺的图：长边从大往小试、取第一张不超过 1MB 的那一档 WebP（和服务端
     `fit` 对齐，见 image/routes.py 的 `_IMG_FIT_W`）。

     2026-11-24 用户改的口径：「现在只有 102k 还是太小了……直接压到 1m 即可，
     不管什么图片，只要是工作台的，服务端压缩到 1m 再传过来，点击图片不需要
     重新传 10m 的原图，我们默认就只给 1m 即可」——所以这里**就是最终那一份**：
     列表、点开、下载全部用它，不存在「点一下再拉原图」这回事了。 */
  var FIT = "1m";

  /* 缓存 key 和请求地址是两样东西（2026-11-24）：地址上带站点签名 `k=`，
     签名每次会话都可能换（服务重启就换），所以**key 里不带它**，否则重启
     一次本地缓存全成孤儿。`keyOf` 只做一件事：把 `&k=...` 摘掉。 */
  function keyOf(url) {
    return String(url || "").replace(/[&?]k=[^&]*/g, "");
  }

  /* 把一张图的字节拉下来存进本地缓存（按 key 判断存过没有）。
     用 `no-cors`：图本身有站点签名（`k=`），不需要 cookie；写进 IndexedDB
     只看能不能拿到字节，不看能不能读它。**失败一律不管**——缓存是优化，
     页面上的 <img> 自己照样会去请求。
     2026-11-25：存之前验一下文件头。`no-cors` 拿到的是 opaque 响应，签名
     过期时服务端回的是 401/404 的 JSON，存进去就是一段 48 字节的错误体，
     下载按钮一「命中缓存」就把它当图存盘了。 */
  function pull(key, url) {
    if (!key || !global.WSImg) return;
    url = url || key;
    global.WSImg.has(key).then(function (yes) {
      if (yes) return undefined;
      return global.fetch(url, { credentials: "omit", mode: "no-cors" })
        .then(function (r) { return r.blob(); })
        .then(function (b) {
          return usable(b).then(function (ok) {
            return ok ? global.WSImg.put(key, b) : undefined;
          });
        });
    })["catch"](function () { /* 存不进去就算了 */ });
  }

  /* 存一个 Blob 到本机（下载流程的最后一步）。 */
  function saveBlob(blob, name) {
    var url = global.URL.createObjectURL(blob);
    var a = document.createElement("a");
    a.href = url;
    a.download = name;
    a.rel = "noopener";
    document.body.appendChild(a);
    a.click();
    a.remove();
    /* 立刻 revoke 会让部分浏览器来不及存盘，给一个宽限期 */
    global.setTimeout(function () { global.URL.revokeObjectURL(url); }, 60000);
  }

  /* ── 存量图时一个「看起来像图」的响应 ────────────────
     2026-11-25 用户实测：小米浏览器点下载，存下来的是 **48B**，长按图片
     保存却正常。48B 正好是一个 JSON 错误体（`{"ok":false,"error":"…"}`）。

     为什么会这样：这个接口要站点签名 `k=`，签名过期 / 换域名之后，
     服务端回的是 401/404 的 **JSON**；`fetch` 对它照样 `r.ok` 为假时会被
     拦下，但**缓存那条路不拦**——缓存里如果是之前某次 `no-cors` 存进去的
     空/错误响应（`pull()` 用 `no-cors`，opaque 响应读不出内容），
     `blob.size` 可能不是 0，于是「命中缓存」就把它当图存了。

     所以下载前必须真的看一眼字节：WebP/PNG/JPEG 都有固定的文件头，
     不是图就**不许存**，直接走网络重拿。 */
  function looksLikeImage(blob) {
    if (!blob || !blob.size) return Promise.resolve(false);
    if (blob.type && blob.type.indexOf("image/") === 0) return Promise.resolve(true);
    // 类型未知（opaque / 空 type）：读前 12 字节认文件头
    return blob.slice(0, 12).arrayBuffer().then(function (buf) {
      var b = new Uint8Array(buf);
      if (b.length < 4) return false;
      // RIFF....WEBP
      if (b[0] === 0x52 && b[1] === 0x49 && b[2] === 0x46 && b[3] === 0x46) return true;
      // PNG
      if (b[0] === 0x89 && b[1] === 0x50 && b[2] === 0x4e && b[3] === 0x47) return true;
      // JPEG
      if (b[0] === 0xff && b[1] === 0xd8 && b[2] === 0xff) return true;
      // GIF
      if (b[0] === 0x47 && b[1] === 0x49 && b[2] === 0x46) return true;
      return false;
    })["catch"](function () { return false; });
  }

  /* 能直接用的图才返回：太小（错误体）、或者根本不是图片格式，都算没命中。 */
  function usable(blob) {
    if (!blob || !blob.size) return Promise.resolve(false);
    if (blob.size < 200) return Promise.resolve(false);   // 48B 那种 JSON 错误体
    return looksLikeImage(blob);
  }

  /* 点一下把这张**放大到原尺寸看**（2026-11-24 用户改口径后：不再拉原图）。
     原地放大而不是开一个查看器：少一套 DOM，也不会多出「关了查看器消息
     位置跳走」这类毛病。再点一下收回去。 */
  function toggleFull(img) {
    var on = img.classList.toggle("shot-full");
    if (!on) return;
    // 铺开后这张比气泡宽得多，横向要能滑到底看右边，不然等于只看到左半张
    var b = (global.WSChat && global.WSChat.box) ? global.WSChat.box() : null;
    if (!b) return;
    global.requestAnimationFrame(function () {
      var r = img.getBoundingClientRect();
      var br = b.getBoundingClientRect();
      if (r.left < br.left || r.right > br.right) {
        b.scrollLeft += (r.left - br.left) - 12;
      }
    });
  }

  /* 下载这张图（2026-11-24 用户：「点下载的时候会有延迟，得等个七八秒钟才会
     显示」）。

     以前两步都在错的地方：
       ① `<a download href>` 是导航式下载，**不查 HTTP 缓存**，等于把图重下
          一遍（2026-11-24 上午改成 fetch，但拉的是**原图**——一张 8MB，缓存
          里本来就没有，点了当然要等七八秒）；
       ② 现在下载的是**列表上那张 1MB 的压缩档**，它的字节早就躺在本地缓存
          里（显示时顺手存过），点下去是零网络。

     顺序：本地缓存 → fetch（HTTP 缓存 / 服务端）→ 老办法兜底。 */
  function download(card, button) {
    var file = card.file || (card.rec && card.rec.file) || "";
    if (!file) {
      if (global.WSChat) global.WSChat.sysNote("这张还没出来，等它画完再点下载");
      return;
    }
    var name = String(file).split("/").pop() || "image.png";
    var src = global.WSGen.img(file, FIT);
    var key = global.WSGen.imgKey(file, FIT);   // 缓存认这个（不带签名）

    /* 「下载中…」只是个短暂的「点了」反馈，600ms 后自己恢复。
       真正的保存仍走下面原来的缓存 → fetch → 兜底三步，不弹任何面板。 */
    if (button && !button.disabled) {
      var label = button.innerHTML;
      button.disabled = true;
      button.textContent = "下载中…";
      global.setTimeout(function () {
        button.innerHTML = label;
        button.disabled = false;
      }, 600);
    }

    function fromNet() {
      if (typeof global.fetch !== "function") { fail(); return; }
      global.fetch(src, { credentials: "same-origin", cache: "default" })
        .then(function (r) {
          if (!r.ok) throw new Error("HTTP " + r.status);
          return r.blob();
        })
        .then(function (b) {
          return usable(b).then(function (ok) {
            if (!ok) throw new Error("拿回来的不是图");
            saveBlob(b, name);
            if (global.WSImg) global.WSImg.put(key, b);
            done();
          });
        })
        ["catch"](fail);
    }

    /* 失败要说出来。以前这里有个「直接让浏览器去下（`<a download href>`）」
       的兜底，2026-11-25 查出来那正是 48B 文件的来源：导航式下载不查缓存、
       也不带站点签名，服务端回的是 JSON 错误体，存成 .webp 就是 48 字节。
       现在宁可明确说一句「下载不了，长按图片保存」，也不悄悄存个坏文件。 */
    function fail() {
      if (button && !button.disabled) { button.innerHTML = label; button.disabled = false; }
      if (global.WSChat) {
        global.WSChat.sysNote("这张下载不了（取图失败），长按图片选「保存图片」试试");
      }
    }

    function done() {
      if (button && !button.disabled) { button.innerHTML = label; button.disabled = false; }
    }

    if (!global.WSImg) { fromNet(); return; }
    global.WSImg.get(key).then(function (b) {
      return usable(b).then(function (ok) {
        if (ok) { saveBlob(b, name); done(); }
        else fromNet();
      });
    })["catch"](fromNet);
  }

  /* 聊天区里铺的是 **1MB 那一档**（2026-11-24 用户：「直接压到 1m 即可，
     不管什么图片，只要是工作台的，服务端压缩到 1m 再传过来」）。原图常有
     2~8MB，一张卡一张原图，恢复历史十几张就是几十 MB 全从这台机器往上走
     ——家里那条上行扛不住。

     一份到底：点开不再是「换原图地址」，而是把已经下下来的这张放大
     （见 toggleFull），所以这儿不挂原图地址。

     另外它顺手把这张的字节 `pull` 进本地缓存：下载和下次打开都直接拿。 */
  function shotEl(file) {
    var img = document.createElement("img");
    img.alt = "生成结果";
    img.loading = "lazy";
    img.decoding = "async";
    img.src = global.WSGen.img(file, FIT);
    img.dataset.file = file || "";
    img.className = "shot";
    img.addEventListener("click", function () { toggleFull(img); });
    pull(global.WSGen.imgKey(file, FIT), img.src);
    return img;
  }

  global.WSShot = {
    FIT: FIT,
    keyOf: keyOf,
    pull: pull,
    saveBlob: saveBlob,
    usable: usable,
    toggleFull: toggleFull,
    download: download,
    shotEl: shotEl
  };
})(window);
