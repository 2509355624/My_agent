/* 参考图：输入栏上方那条缩略图、消息里的参考卡、上传、以及画廊的「画同款」。
   从 chat.js 拆出来的——那边原来是角色、上传、重放全挤在一起。

   口径（用户 2026-11-23 定的）：这里没有图生图。相册上传是一张「参考」，
   「画同款」是把那张图读成一段描述再生成——所以两者都只往输入栏挂缩略图，
   真正的生成仍由 chat.js 按用户这句话去打 /api/gen/submit。

   **识图时机（2026-11-23 晚用户第二次纠正）**：上传只是「选了一张图」，
   不许立刻去读；等用户点了发送，`ensureDescribed()` 才把这一轮的几张图
   读成描述，跟用户那句话一起拼给大模型。理由是用户上传完往往还要想一秒
   才写需求，立刻读出来的描述会先一步填进输入框，把用户想说的话顶掉；
   而且那时白白烧一次视觉模型的钱。 */
(function (global) {
  "use strict";

  var S = { refs: [] };
  var el = {};

  function $(id) { return document.getElementById(id); }

  function isPrivateRef(id) {
    return typeof id === "string" && /^[A-Za-z0-9_-]{24,128}$/.test(id);
  }

  function imageUrl(item) {
    if (item.url) return item.url;
    return isPrivateRef(item.ref_id || item.id)
      ? "/api/gal/ref/" + encodeURIComponent(item.ref_id || item.id)
      : "/api/gal/img/" + item.id + "?s=t";
  }

  function describeUrl(id) {
    if (isPrivateRef(id)) {
      return "/api/gal/ref/describe?ref_id=" + encodeURIComponent(id);
    }
    return "/api/gal/describe?id=" + encodeURIComponent(id);
  }

  /* 参考卡一律挂在根消息区（跟 chat.js 的 box() 同一条规矩，理由见那边的
     注释：挂在子页里会被退页动画一起回收） */
  function box() {
    return document.getElementById("chat-scroll");
  }

  // ── 参考卡 ────────────────────────────────────────────

  /* 参考图挂在用户气泡后面，形状跟豆包一样：一条缩略图带，右上角能删。
     它只是「这一轮要带着的图」，真正发送时把 id 一起交给后端。 */
  function localError(anchor, text) {
    var host = anchor && anchor.parentNode;
    if (!host) return;
    var old = host.querySelector(".ref-error");
    if (old) old.remove();
    var msg = document.createElement("p");
    msg.className = "ref-error";
    msg.textContent = text;
    host.appendChild(msg);
  }

  function paintRef(item) {
    global.WSChat.hideEmpty();
    var wrap = document.createElement("div");
    wrap.className = "refs";
    wrap.dataset.ref = item.id || "";
    /* `data-draft`：这张是「还没发出去的草稿」，发送后由 commit() 删掉。
       存档重放出来的参考卡（archive.js 的 restoreRef）不带这个标记——那是
       历史里那一轮的图，不该被发送动作顺手删掉。 */
    wrap.dataset.draft = "1";

    var img = document.createElement("img");
    img.alt = "参考图";
    img.src = imageUrl(item);
    img.addEventListener("click", function () {
      if (isPrivateRef(item.ref_id || item.id)) return;
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
    /* 用户又选了一次同一张（相册重传、又点了一次画同款）：把之前恢复出来
       的「不算数」标记摘掉——他这次是真的要带上这张了。 */
    var had = S.refs.filter(function (r) { return r.id === item.id; })[0];
    if (had) { delete had.resumed; syncHint(); return; }
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
     又带着同一张，用户会莫名其妙收到一张套着上一张图的图。

     顺手把存档里那几条 `k:"ref"` 记录删掉（2026-11-24 晚）：它们是「还没
     发出去的那张」的回执，发完了就没有意义。以前只从 S.refs 里摘掉、存档
     不动，于是每次刷新 restore 都会从存档尾巴把它们认回来——用户看到的
     就是「我什么都没选，引用图栏自己冒出来了」。

     **还要把消息区那张大缩略图删掉**（2026-11-25 用户报的 bug：「发送之后
     的图片还会留在我这个候选栏里面」）。`paintRef()` 把缩略图挂进了
     `#chat-scroll`（`.refs`，带 `data-ref`），那是「还没发出去的草稿」的
     样子；只清 `S.refs` 不清 DOM，气泡后面就永远杵着一张删不掉的图——
     而它对应的引用图栏（`#ref-bar`）已经空了，看着就像没发出去。

     已经拼进用户气泡的那份由 `addUser(text, refs)` 画的 `.bubble-refs`
     负责，它没有 `data-ref`，不会误删。 */
  function commit() {
    global.sessionStorage.removeItem("ws_same_pending");
    global.sessionStorage.removeItem("ws_same_result");
    var ids = S.refs.map(function (r) { return r.id; });
    S.refs = [];
    /* 一次全查出来再按 id 挑：参考图通常只有一两张，但这里别为每张各扫一遍 DOM。 */
    var want = {};
    ids.forEach(function (id) { want[String(id)] = 1; });
    if (ids.length) {
      var host = box();
      Array.prototype.forEach.call(
        host ? host.querySelectorAll("[data-ref]") : [], function (n) {
          if (n.dataset.draft && want[String(n.dataset.ref)]) n.remove();
        });
    }
    if (ids.length && global.WSChat) global.WSChat.dropRefRecords(ids);    syncHint();
  }

  /* 「画同款」：把画廊这张读成一段描述再生成（用户 2026-11-23 定的口径——
     这里没有图生图）。调用方已经退回了聊天页，这里只负责挂参考图、
     切回图片生成、把光标放进输入框，等用户再补一句要求。 */
  function useSame(work, itemId) {
    global.sessionStorage.removeItem("ws_same_result");
    if (global.WSCreate) global.WSCreate.setMode("img", true);
    addRef({ wid: (work && work.id) || 0, id: itemId || 0,
             url: itemId ? ("/api/gal/img/" + itemId + "?s=t") : "" });
    syncHint();
    var say = document.getElementById("say");
    if (say) {
      say.placeholder = "说说这张怎么改，照着它重画";
      say.focus();
    }
    // 立刻把这张读成一段描述填进输入框：用户点「画同款」要的是「照着它」，
    // 光挂一张缩略图什么都不会发生。读出来就填，读不出来就让用户自己写一句
    // ——两种情况都给明确反馈，不留「点了没反应」的空档。
    if (itemId) {
      describeInto(itemId, say).then(function (text) {
        /* 读成功 → 给这张图打上「已读」标记。发送时（chat.js 的 send）
           看到这个标记就直接拿去出图，不再让后端识一次——用户 2026-11-24
           确认的口径：「不再识，用输入栏那段」。 */
        if (!text) return;
        S.refs.forEach(function (r) {
          if (r.id === itemId) { r.pre = true; r.text = text; }
        });
      });
    }
    global.dispatchEvent(new Event("resize"));
  }

  /* 读图反推 → 填进输入框。已有内容就不覆盖（那是用户自己写的）。
     只给「画同款」用：那是用户明确点了一下「照着这张画」，填进去才叫反馈。
     相册上传不走这里（见文件头的识图时机说明）。 */
  function describeInto(itemId, say) {
    if (!say) return Promise.resolve("");
    global.sessionStorage.setItem("ws_same_pending", JSON.stringify({ id: itemId }));
    /* 「正在识图…」只放在输入框自己的 placeholder 里（用户 2026-11-25：
       挂一行字在输入栏上面会把它压得又扁又窄）。placeholder 不新增节点、
       不改宽度，输入栏跟平时一模一样，识图完恢复原提示。 */
    say.placeholder = "正在识图中…";
    function finish() {
      say.placeholder = "说说这张怎么改，照着它重画";
    }
    return global.WSApi.get(describeUrl(itemId))
      .then(function (d) {
        finish();
        global.sessionStorage.removeItem("ws_same_pending");
        if (!d || !d.ok) {
          localError(say.parentNode, (d && d.error) || "这张没读出来，自己写一句就行");
          return "";
        }
        global.sessionStorage.setItem("ws_same_result", JSON.stringify({ id: itemId, text: d.text || "" }));
        if (say.value.trim()) return d.text || "";   // 用户已经在写了，别顶掉
        say.value = d.text || "";
        say.dispatchEvent(new Event("input"));
        say.focus();
        return d.text || "";
      })
      .catch(function () {
        finish();
        localError(say.parentNode, "读图没连上，回来后会重新识图，也可以自己写一句");
        return "";
      });
  }

  /* 只读、不填输入框：发送那一刻用。成功才记进 item.text，失败的返回空串
     （后端也没有描述时由 submit 那边说「没读到，按描述画」）。 */
  function describeOnly(item) {
    return global.WSApi.get(describeUrl(item.id))
      .then(function (d) {
        if (d && d.ok && d.text) { item.text = d.text; return d.text; }
        return "";
      })
      .catch(function () { return ""; });
  }

  /* 发送时把这一轮还没读过的参考图读出来，返回带描述的数组。
     全部在本地读完再发，不是一个一个等——几张图并行，用户只等最慢的那张。
     读不出来不算失败：图仍然带着走，让后端按「只有图、没描述」处理。 */
  function ensureDescribed(list) {
    var items = (list || S.refs).slice();
    var todo = items.filter(function (r) { return r.id && !r.text; });
    if (!todo.length) return Promise.resolve(items);
    var tag = global.WSChat.busyNote(
      todo.length > 1 ? "正在看 " + todo.length + " 张参考图…" : "正在看图…");
    return Promise.all(todo.map(describeOnly)).then(function () {
      if (tag) tag.remove();
      return items;
    });
  }

  /* 撤掉一张：输入栏的缩略图、消息里那张参考卡、存档里那条记录一起删 */
  function drop(id) {
    global.sessionStorage.removeItem("ws_same_pending");
    global.sessionStorage.removeItem("ws_same_result");
    S.refs = S.refs.filter(function (o) { return o.id !== id; });
    global.WSChat.dropRef(id);
    syncHint();
  }

  /* 刷新时把存档里最后那条参考图认回来。**只记着，不显示。**

     以前这里会把那张挂回输入栏，等于「刷新一下，上一条消息用过的参考图
     又出现在待发送里」——用户 2026-11-24 报的就是这个。存档里那条 `ref`
     发送成功时就该用掉了，只是一直没人删它，于是每次都从存档尾巴上被
     认回来。现在恢复出来的带 `resumed`，syncHint 不画它。 */
  function rehydrate(m) {
    if (!m || !m.id) return;
    if (S.refs.some(function (r) { return r.id === m.id; })) return;
    S.refs.push({ wid: m.wid, id: m.id, url: m.url || "", resumed: true });
    if (!el.bar) el.bar = $("ref-bar");
    syncHint();
  }

  /* 输入框上方的小尾巴：显示这次带了哪些图、当前是什么模式。

     只画**真的在等发送**的那几张：恢复回来的（`resumed`）不算，用户碰过
     它们（再点一次、重新选图）才算自己的。 */
  function syncHint() {
    if (!el.bar) return;
    el.bar.innerHTML = "";
    var comic = global.WSCreate && global.WSCreate.mode() === "comic";
    var live = S.refs.filter(function (r) { return !r.resumed; });
    el.bar.hidden = !live.length;
    if (!live.length) return;
    live.forEach(function (r) {
      var wrap = document.createElement("div");
      wrap.className = "refs mini";
      var img = document.createElement("img");
      img.alt = "参考图";
      img.src = imageUrl(r);
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
    var tip = global.WSChat.busyNote("正在传参考图…");
    var errorHost = el.bar && el.bar.parentNode;
    shrink(file).then(function (blob) {
      var fd = new FormData();
      fd.append("file", blob, (file.name || "ref").replace(/\.[^.]+$/, "") + ".webp");
      /* `use=temp`（2026-11-24 用户定）：这张图只为「识图反推」活一下，
         不该进「我的作品」。服务端据此打上 use='temp'，所有作品列表都
         过滤掉它，24 小时后连文件带缓存一起清。
         以前不传这个字段，于是手机相册里的照片全被当成作品集攒在「我的」
         里——用户看到的是「我的作品里全是我拍的照片」。 */
      fd.append("use", "temp");
      return fetch("/api/gal/ref", {
        method: "POST", body: fd, credentials: "same-origin"
      }).then(function (r) { return r.json().catch(function () { return {}; }); });
    }).then(function (d) {
      if (tip) tip.remove();
      if (!d || !d.ok) {
        localError(errorHost, "参考图没传上去：" + ((d && d.error) || "网络断了"));
        return;
      }
      // 私有 reference 不创建 work/item，前端只保存 ref_id 和私有 URL。
      if (d.ref_id) {
        addRef({ id: d.ref_id, ref_id: d.ref_id, url: d.url || imageUrl(d) });
        syncHint();
        return;
      }
      localError(errorHost, "参考图传上去了，但服务端没有返回 ref_id");
    }).catch(function () {
      if (tip) tip.remove();
      localError(errorHost, "参考图没传上去，网络断了一下");
    });
  }

  // ── 引用已出好的图（2026-11-27） ──────────────────────

  /* 正在上传的张数：`send()` 见到 >0 就先别发——不然「图还没挂上就点了
     发送」会悄悄退化成一张纯文生图，用户只会觉得「我明明引用了它」。 */
  var pending = 0;

  function toDataUrl(blob) {
    return new Promise(function (resolve, reject) {
      if (!global.FileReader) { reject(new Error("no FileReader")); return; }
      var fr = new global.FileReader();
      fr.onload = function () { resolve(String(fr.result || "")); };
      fr.onerror = function () { reject(fr.error || new Error("read fail")); };
      fr.readAsDataURL(blob);
    });
  }

  /* 卡片上的「引用」：把这张已经出好的图挂成这一轮的参考图。

     用户 2026-11-27 定：**不往输入框填原提示词**——输入栏很窄，他要的是
     「我接着说一句」，不是去编辑一长串 tags。点一下就把键盘叫起来、挂上
     这张图，用户直接打「换成正面视角看向镜头」再发送。

     挂 `cr_` 临时引用（`/api/gen/chat-upload`）而不是画廊私有 ref：工作台
     ask 路径的 `_web_images()` 只认 `cr_`，别的形态会被丢掉，等于没带图。

     `pre: true` = 「前端别再识一次图」：`send()` 见到 pre 直接交给后端，
     由 `_revise` 读一次、自己判要不要垫图。相册上传那条要前端读一遍、
     后端再读一遍（用户 2026-11-27 说多余），引用这条不再重复。 */
  function quoteCard(card) {
    var say = $("say");
    // 焦点必须在点击的同步栈里给；等异步回来再 focus，键盘不会弹。
    if (say) { try { say.focus(); } catch (e) { /* 忽略 */ } }
    var file = card && (card.file || (card.rec && card.rec.file));
    if (!file) {
      if (global.WSChat) global.WSChat.sysNote("这张还没出来，等它画完再引用");
      return;
    }
    var src = global.WSGen.img(file, (global.WSShot && global.WSShot.FIT) || "1m");
    var tip = global.WSChat ? global.WSChat.busyNote("正在引用这张图…") : null;
    pending++;
    global.fetch(src, { credentials: "same-origin" })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.blob();
      })
      .then(function (b) {
        // 只认真图：48B 的 JSON 错误体 / 空响应都别当图（同 shot.js）
        if (global.WSShot && global.WSShot.usable) {
          return global.WSShot.usable(b).then(function (ok) {
            if (!ok) throw new Error("拿回来的不是图");
            return b;
          });
        }
        return b;
      })
      .then(shrink)
      .then(toDataUrl)
      .then(function (dataUrl) { return global.WSGen.chatUpload(dataUrl); })
      .then(function (d) {
        if (tip) tip.remove();
        if (!d || !d.ok || !d.ref) {
          if (global.WSChat) {
            global.WSChat.sysNote("引用没成功：" + ((d && d.error) || "网络断了"));
          }
          return;
        }
        addRef({ id: d.ref, ref_id: d.ref, url: src, pre: true });
        syncHint();
        if (say) { try { say.focus(); } catch (e2) { /* 忽略 */ } }
      })
      ["catch"](function () {
        if (tip) tip.remove();
        if (global.WSChat) global.WSChat.sysNote("引用没成功，检查网络后再点一次");
      })
      .then(function () { pending--; });
  }


  function init() {
    el.bar = $("ref-bar");
    syncHint();
    try {
      var pending = JSON.parse(global.sessionStorage.getItem("ws_same_pending") || "null");
      var result = JSON.parse(global.sessionStorage.getItem("ws_same_result") || "null");
      if (pending && pending.id) useSame(null, pending.id);
      else if (result && result.text && !$("say").value.trim()) {
        addRef({ id: result.id, pre: true, text: result.text });
        $("say").value = result.text;
        $("say").dispatchEvent(new Event("input"));
        syncHint();
      }
    } catch (e) {
      global.sessionStorage.removeItem("ws_same_pending");
      global.sessionStorage.removeItem("ws_same_result");
    }
  }

  global.WSRef = {
    init: init,
    add: addRef,
    clear: clearRefs,
    commit: function () { commit(); },
    list: function () { return S.refs.slice(); },
    useSame: useSame,
    upload: upload,
    quote: quoteCard,
    /* 还有引用图在路上：`send()` 拿它挡一下，别让用户发出没带图的请求。 */
    busy: function () { return pending > 0; },
    ensureDescribed: ensureDescribed,
    syncHint: syncHint,
    rehydrate: rehydrate
  };
})(window);
