/* 参考图：输入栏上方那条缩略图、消息里的参考卡、上传、以及画廊的「画同款」。
   从 chat.js 拆出来的——那边原来是角色、上传、重放全挤在一起。

   口径（用户 2026-11-23 定的）：这里没有图生图。相册上传是一张「参考」，
   「画同款」是把那张图读成一段描述再生成——所以两者都只往输入栏挂缩略图，
   真正的生成仍由 chat.js 按用户这句话去打 /api/gen/submit。 */
(function (global) {
  "use strict";

  var S = { refs: [] };
  var el = {};

  function $(id) { return document.getElementById(id); }

  /* 参考卡一律挂在根消息区（跟 chat.js 的 box() 同一条规矩，理由见那边的
     注释：挂在子页里会被退页动画一起回收） */
  function box() {
    return document.getElementById("chat-scroll");
  }

  // ── 参考卡 ────────────────────────────────────────────

  /* 参考图挂在用户气泡后面，形状跟豆包一样：一条缩略图带，右上角能删。
     它只是「这一轮要带着的图」，真正发送时把 id 一起交给后端。 */
  function paintRef(item) {
    global.WSChat.hideEmpty();
    var wrap = document.createElement("div");
    wrap.className = "refs";
    wrap.dataset.ref = item.id || "";

    var img = document.createElement("img");
    img.alt = "参考图";
    img.src = item.url || ("/api/gal/img/" + item.id + "?s=t");
    img.addEventListener("click", function () {
      if (global.WSGallery) global.WSGallery.open({ id: item.wid }, item.id);
    });
    wrap.appendChild(img);

    var x = document.createElement("button");
    x.type = "button";
    x.className = "ref-x";
    x.setAttribute("aria-label", "移除这张参考图");
    x.textContent = "✕";
    x.addEventListener("click", function () {
      wrap.remove();
      drop(item.id);
    });
    wrap.appendChild(x);

    box().appendChild(wrap);
    global.WSChat.scrollBottom();
    return wrap;
  }

  /* 加一张参考图（画廊「画同款」、输入栏＋相册、替换漫画参考都走这里） */
  function addRef(item) {
    if (!item || !item.id) return;
    if (S.refs.some(function (r) { return r.id === item.id; })) return;
    S.refs.push(item);
    paintRef(item);
    global.WSChat.log({ k: "ref", wid: item.wid, id: item.id,
                        url: item.url || "" });
  }

  /* 已经发出去的那张要留在「已发送」里，否则刷新后被 dropRef 一起删掉 */
  function clearRefs() {
    S.refs = [];
    syncHint();
  }

  /* 发送成功才把参考图从「待发送」移走：还压在输入栏里就等于下一句
     又带着同一张，用户会莫名其妙收到一张套着上一张图的图。 */
  function commit() { S.refs = []; syncHint(); }

  /* 「画同款」：把画廊这张读成一段描述再生成（用户 2026-11-23 定的口径——
     这里没有图生图）。调用方已经退回了聊天页，这里只负责挂参考图、
     切回图片生成、把光标放进输入框，等用户再补一句要求。 */
  function useSame(work, itemId) {
    if (global.WSCreate) global.WSCreate.setMode("img", true);
    addRef({ wid: (work && work.id) || 0, id: itemId || 0,
             url: itemId ? ("/api/gal/img/" + itemId + "?s=t") : "" });
    syncHint();
    var say = document.getElementById("say");
    if (say) {
      say.placeholder = "说说这张怎么改，照着它重画";
      say.focus();
    }
    global.dispatchEvent(new Event("resize"));
  }

  /* 撤掉一张：输入栏的缩略图、消息里那张参考卡、存档里那条记录一起删 */
  function drop(id) {
    S.refs = S.refs.filter(function (o) { return o.id !== id; });
    global.WSChat.dropRef(id);
    syncHint();
  }

  /* 刷新后把「还没发出去的那张」挂回输入栏（restore 时调用） */
  function rehydrate(m) {
    if (!m || !m.id) return;
    if (S.refs.some(function (r) { return r.id === m.id; })) return;
    S.refs.push({ wid: m.wid, id: m.id, url: m.url || "" });
    if (!el.bar) el.bar = $("ref-bar");
    syncHint();
  }

  /* 输入框上方的小尾巴：显示这次带了哪些图、当前是什么模式 */
  function syncHint() {
    if (!el.bar) return;
    el.bar.innerHTML = "";
    var comic = global.WSCreate && global.WSCreate.mode() === "comic";
    el.bar.hidden = !S.refs.length;
    if (!S.refs.length) return;
    S.refs.forEach(function (r) {
      var wrap = document.createElement("div");
      wrap.className = "refs mini";
      var img = document.createElement("img");
      img.alt = "参考图";
      img.src = r.url || ("/api/gal/img/" + r.id + "?s=t");
      wrap.appendChild(img);
      var x = document.createElement("button");
      x.type = "button";
      x.className = "ref-x";
      x.setAttribute("aria-label", "移除这张参考图");
      x.textContent = "✕";
      x.addEventListener("click", function () { drop(r.id); });
      wrap.appendChild(x);
      el.bar.appendChild(wrap);
    });
    if (comic) {
      var tag = document.createElement("span");
      tag.className = "ref-mode";
      tag.textContent = "漫画参考";
      el.bar.appendChild(tag);
    }
  }

  /* 漫画卡片的记录由 create.js 改字段、这里统一落盘，免得两边各存一份 */
  function remember(rec) {
    S.msgs.push(rec);
    touch(rec);
  }

  function touch(rec) {
    if (global.WSStore) global.WSStore.save({ msgs: S.msgs });
    return rec;
  }

  // ── 上传参考图 ────────────────────────────────────────

  /* 浏览器里先缩到长边 1600 再传：原图几 MB 顺着上行推过隧道一张要十几秒，
     缩完 150KB 上下，服务端也就不用再编一次（见 gallery.append_upload）。 */
  function shrink(file) {
    return new Promise(function (resolve) {
      var url = URL.createObjectURL(file);
      var im = new Image();
      im.onload = function () {
        var w = im.naturalWidth, h = im.naturalHeight;
        var k = Math.min(1, 1600 / Math.max(w, h));
        var cv = document.createElement("canvas");
        cv.width = Math.round(w * k);
        cv.height = Math.round(h * k);
        cv.getContext("2d").drawImage(im, 0, 0, cv.width, cv.height);
        URL.revokeObjectURL(url);
        cv.toBlob(function (b) { resolve(b || file); }, "image/webp", 0.9);
      };
      im.onerror = function () {
        URL.revokeObjectURL(url);
        resolve(file);
      };
      im.src = url;
    });
  }

  function upload(file) {
    if (!file) return;
    var tip = global.WSChat.sysNote("正在传参考图…");
    shrink(file).then(function (blob) {
      var fd = new FormData();
      fd.append("file", blob, (file.name || "ref").replace(/\.[^.]+$/, "") + ".webp");
      return fetch("/api/gal/upload/one", {
        method: "POST", body: fd, credentials: "same-origin"
      }).then(function (r) { return r.json().catch(function () { return {}; }); });
    }).then(function (d) {
      if (tip && tip.parentNode) tip.remove();
      if (!d || !d.ok) {
        global.WSChat.sysNote("参考图没传上去：" + ((d && d.error) || "网络断了"));
        return;
      }
      // 上传后拿到的是第几张（idx），真正的图 id 得回作品集里取
      return global.WSApi.get("/api/gal/work/" + d.work_id).then(function (w) {
        var items = (w && w.ok && w.items) || [];
        var hit = items.filter(function (it) { return it.idx === d.idx; })[0];
        if (!hit) {
          global.WSChat.sysNote("参考图传上去了，但没找到它，再传一次试试");
          return;
        }
        addRef({ wid: d.work_id, id: hit.id,
                 url: "/api/gal/img/" + hit.id + "?s=t" });
        syncHint();
      });
    }).catch(function () {
      if (tip && tip.parentNode) tip.remove();
      global.WSChat.sysNote("参考图没传上去，网络断了一下");
    });
  }


  function init() {
    el.bar = $("ref-bar");
    syncHint();
  }

  global.WSRef = {
    init: init,
    add: addRef,
    clear: clearRefs,
    commit: commit,
    list: function () { return S.refs.slice(); },
    useSame: useSame,
    upload: upload,
    syncHint: syncHint,
    rehydrate: rehydrate
  };
})(window);
