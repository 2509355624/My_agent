/* 聊天页：用户气泡 + 参考卡 + AI 结果卡。
   生图走 /api/gen/*，轮询有上限，停了能重试；消息进 localStorage，刷新接得回来。
   看图反推提示词走「图片生成」模式（用户 2026-11-23 的口径：画同款是把这张
   读成一段描述再生成，这里不做像素级的图片二次生成）。 */
(function (global) {
  "use strict";

  var COLLAPSE_AT = 1000;   // 用户单条超过这个字数就折叠
  var HEAD_KEEP = 300;      // 折叠后先露这么多
  var POLL_MS = 2000;
  var MAX_FAIL = 5;         // 连续失败这么多次就停轮询，别无限打接口
  /* 卡死兜底（2026-11-24 用户：「昨天晚上的图现在还显示排队中，卡死了」）。
     后端已经有超时，但它只管「线程还在跑」的情形；服务重启 / 任务丢掉之后
     存档里那条会永远停在「排队中」。所以前端**自己**也计时：
       · 一直没轮到（排队 / 准备中）超过 SETTLE_MS → 判它卡住了；
       · 真在画的那张按渠道给得更宽，别误杀。
     判超时不是「报错完事」：卡片上给一个「重试」，用户点了就按同一份提示词
     重排一张。 */
  var SETTLE_MS = 8 * 60 * 1000;
  var PAINT_MS = 25 * 60 * 1000;
  /* 聊天区铺的图：长边从大往小试、取第一张不超过 1MB 的那一档 WebP（和服务端
     `fit` 对齐，见 image/routes.py 的 `_IMG_FIT_W`）。

     2026-11-24 用户改的口径：「现在只有 102k 还是太小了……直接压到 1m 即可，
     不管什么图片，只要是工作台的，服务端压缩到 1m 再传过来，点击图片不需要
     重新传 10m 的原图，我们默认就只给 1m 即可」——所以这里**就是最终那一份**：
     列表、点开、下载全部用它，不存在「点一下再拉原图」这回事了。 */
  var FIT = "1m";

  var el = {};
  var S = {
    skill: "", channels: [], pending: {}, timer: null, fails: 0,
    msgs: []       // 存档：每条消息一条记录，见 archive.js
  };

  function $(id) { return document.getElementById(id); }

  /* 消息区一律是根那一个（#chat-scroll）。
     AI 创作页早先借走过输入栏、也自带过一块 .gchat 收消息，那是为「在浏览页
     里直接对话」准备的；现在口径变了——创作回聊天页做，子页只负责浏览。
     所以「发送气泡」「挂参考卡」都写回根，子页那份历史消息区不再收新东西。

     ⚠️ 别再用「哪个 .view-push 里带 [data-chat-scroll]」来挑：退页动画播完
     之前那一页还在 DOM 里（连 .in 都还挂着，closeNow 先摘 .in、节点要等
     transitionend 才移除），这时候写进去的消息会跟着节点一起被回收——
     「画同款」的参考卡就是这么凭空消失的（2026-11-23 实测）。 */
  function box() {
    return el.scroll;
  }

  function scrollBottom() {
    var b = box();
    if (b) b.scrollTop = b.scrollHeight;
  }

  function hideEmpty() {
    if (el.empty) el.empty.hidden = true;
  }

  // ── 存档 ──────────────────────────────────────────────
  // 形状由 archive.js 重放时按原样读回来，所以这里只负责「加一条、存一次」。

  function log(m) {
    S.msgs.push(m);
    if (S.msgs.length > 200) S.msgs = S.msgs.slice(-160);
    if (global.WSStore) global.WSStore.save({ msgs: S.msgs });
    return m;
  }

  /* 漫画卡片的记录由 create.js 改字段、这里统一落盘，免得两边各存一份 */
  function remember(rec) {
    log(rec);
    return rec;
  }

  function touch(rec) {
    if (global.WSStore) global.WSStore.save({ msgs: S.msgs });
    return rec;
  }

  /* 撤掉一张参考图：消息区里那张卡片和存档里那条记录一起删 */
  function dropRef(id) {
    var sc = box();
    Array.prototype.forEach.call(
      sc ? sc.querySelectorAll("[data-ref]") : [], function (n) {
        if (n.dataset.ref === String(id)) n.remove();
      });
    S.msgs = S.msgs.filter(function (m) {
      return !(m.k === "ref" && m.id === id);
    });
    if (global.WSStore) global.WSStore.save({ msgs: S.msgs });
  }

  // ── 消息区 ────────────────────────────────────────────

  function paintNote(text) {
    hideEmpty();
    var p = document.createElement("p");
    p.className = "card note";
    p.style.alignSelf = "center";
    p.style.border = "0";
    p.textContent = text;
    box().appendChild(p);
    scrollBottom();
    return p;
  }

  function sysNote(text) {
    var p = paintNote(text);
    log({ k: "note", t: text });
    return p;
  }

  /* 一条会自己消失的提示（「正在传参考图…」「正在看图…」这类）。
     跟 sysNote 分开：sysNote 是对话记录的一部分，要进存档；这种过场提示
     不该进存档，刷新后也不该再出现一条「正在看图…」。返回的句柄用来提前
     收掉（传完了、看完了），到点也会自己走。 */
  function busyNote(text, ms) {
    var p = paintNote(text);
    var t = setTimeout(function () { if (p.parentNode) p.remove(); }, ms || 20000);
    return {
      remove: function () {
        clearTimeout(t);
        if (p.parentNode) p.remove();
      }
    };
  }

  /* 用户气泡。refs 非空时把这一轮的参考图缩略图排在上面——豆包也是这样：
     发出去的那条消息里能看见自己当时喂的是哪张图，回头翻记录还知道。
     气泡是消息的一部分，所以图地址走同一个 item id，刷新后照样能显示。 */
  function paintUser(text, refs) {
    hideEmpty();
    var b = document.createElement("div");
    b.className = "bubble";
    var list = (refs || []).filter(function (r) { return r && r.id; });
    if (list.length) {
      var strip = document.createElement("div");
      strip.className = "bubble-refs";
      list.forEach(function (r) {
        var im = document.createElement("img");
        im.alt = "参考图";
        im.loading = "lazy";
        im.decoding = "async";
        im.src = r.url || ("/api/gal/img/" + r.id + "?s=t");
        im.addEventListener("click", function () {
          if (global.WSGallery) global.WSGallery.open({ id: r.wid }, r.id);
        });
        strip.appendChild(im);
      });
      b.appendChild(strip);
    }
    var span = document.createElement("span");
    b.appendChild(span);

    if (text.length > COLLAPSE_AT) {
      span.textContent = text.slice(0, HEAD_KEEP) + "…";
      var more = document.createElement("button");
      more.type = "button";
      more.className = "more";
      more.textContent = "展开全部（" + text.length + " 字）";
      more.addEventListener("click", function () {
        span.textContent = text;
        more.remove();
      });
      b.appendChild(more);
    } else {
      span.textContent = text;
    }

    box().appendChild(b);
    scrollBottom();
    return b;
  }

  /* 存档里的 refs 只留 id / wid / url：text 是那一轮读出来的描述，已经在
     提示词里了，留着只是白占 localStorage。 */
  function briefRefs(refs) {
    return (refs || []).map(function (r) {
      return { id: r.id, wid: r.wid, url: r.url || "" };
    });
  }

  function addUser(text, refs) {
    paintUser(text, refs);
    log({ k: "user", t: text, refs: briefRefs(refs) });
  }

  /* 通用卡片壳：图卡和漫画卡共用一套外观（谁用谁往里塞内容） */
  function shell(cls) {
    hideEmpty();
    var node = document.createElement("div");
    node.className = "card" + (cls ? " " + cls : "");

    var body = document.createElement("div");
    body.className = "card-body";

    var note = document.createElement("p");
    note.className = "note";

    var acts = document.createElement("div");
    acts.className = "acts";
    acts.hidden = true;

    /* 「终止」放在 note 旁边（2026-11-23 用户：「这里一直挂着，我都不知道
       有没有在跑图」）。它只在排队 / 出图中出现，出图或失败后自动收起来。 */
    var stop = document.createElement("button");
    stop.type = "button";
    stop.className = "stop-run";
    stop.hidden = true;
    stop.textContent = "终止";

    node.appendChild(body);
    node.appendChild(note);
    node.appendChild(stop);
    node.appendChild(acts);
    box().appendChild(node);
    scrollBottom();

    return {
      node: node,
      body: body,
      note: note,
      stop: stop,
      acts: acts,
      say: function (text, bad) {
        note.hidden = false;
        note.className = "note" + (bad ? " err" : "");
        note.textContent = text;
        scrollBottom();
      },
      show: function () {
        if (!acts.hidden) return;
        acts.hidden = false;
        scrollBottom();
      }
    };
  }

  function newCard(prompt, skill, rec) {
    var c = shell();
    var ph = document.createElement("div");
    ph.className = "ph";
    c.body.appendChild(ph);
    c.say("排队中…");

    return { node: c.node, body: c.body, ph: ph, note: c.note, stop: c.stop,
             acts: c.acts, prompt: prompt, skill: skill, job: "", rec: rec };
  }

  var ICON_COPY =
    '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
    'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">' +
    '<rect x="9" y="9" width="11" height="11" rx="2.4"/>' +
    '<path d="M15 5.6A2.6 2.6 0 0 0 12.4 3H6.6A2.6 2.6 0 0 0 4 5.6v5.8A2.6 2.6 0 0 0 6.6 14"/></svg>';

  var ICON_AGAIN =
    '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
    'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">' +
    '<path d="M20 12a8 8 0 1 1-2.6-5.9"/><path d="M20 4.6V10h-5.4"/></svg>';

  var ICON_DOWN =
    '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
    'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">' +
    '<path d="M12 4.5v10.4"/><path d="M7.8 11.4L12 15.6l4.2-4.2"/>' +
    '<path d="M4.8 18.4v1.1h14.4v-1.1"/></svg>';

  /* 卡片底部三个按钮：复制提示词 | 下载到本机 | 换种子重画。
     它们原来紧贴在一起（都是小圆圈、间距 8px），用户反馈「很容易误触」——
     改成两边是纯文字按钮、中间是图标按钮，中间留空推开，一个管复制
     一个管下载一个管重画，手指不会点串。
     下载（2026-11-24 定稿）：拉「≤512KB」那一档，先问浏览器本地缓存，
     见 downloadImage；不经过 canvas，手机浏览器直接落到「下载」目录。 */
  /* 默认的「重画」文案写死在字面量里：这一条有测试钉着（按钮必须带文字）。 */
  function buildActs(card, retryLabel) {
    if (card.acts.childElementCount && !retryLabel) return;
    if (retryLabel) card.acts.innerHTML = "";
    card.acts.hidden = false;

    var copy = document.createElement("button");
    copy.type = "button";
    copy.className = "act act-text";
    copy.title = "复制提示词";
    copy.setAttribute("aria-label", "复制提示词");
    copy.innerHTML = ICON_COPY + "<span>复制</span>";
    copy.addEventListener("click", function () {
      copyText(card.prompt).then(function (ok) {
        copy.innerHTML = ok ? '<span class="ok">✓ 已复制</span>' : "复制失败";
        setTimeout(function () {
          copy.innerHTML = ICON_COPY + "<span>复制</span>";
        }, 1200);
      });
    });

    var save = document.createElement("button");
    save.type = "button";
    save.className = "act act-icon";
    save.title = "下载到本机";
    save.setAttribute("aria-label", "下载到本机");
    save.innerHTML = ICON_DOWN + "<span>下载</span>";
    save.addEventListener("click", function () {
      downloadImage(card);
    });

    /* 「重画」= 换种子再来一张；卡住的那张换成「重试」，同一个意思但
       用户点下去是「它卡住了、帮我再排一次」，不是「我不满意这张」。 */
    var again = document.createElement("button");
    again.type = "button";
    again.className = "act act-text";
    again.title = retryLabel ? "重新排一次" : "换种子重画";
    again.setAttribute("aria-label", again.title);
    if (retryLabel) again.innerHTML = ICON_AGAIN + "<span>" + retryLabel + "</span>";
    else again.innerHTML = ICON_AGAIN + "<span>重画</span>";
    again.addEventListener("click", function () {
      submitJob(card.prompt, card.skill, card.refs || []);
    });

    card.acts.appendChild(copy);
    card.acts.appendChild(save);
    card.acts.appendChild(again);
  }

  /* 点一下把这张**放大到原尺寸看**（2026-11-24 用户改口径后：不再拉原图）。

     以前这里是把 src 换成不带 fit 的原图（一张 2~8MB，走一次家用上行，等十几
     秒），用户明确否掉了：「点击图片不需要重新传 10m 的原图，我们默认就只给
     1m 即可」。所以现在点开只是把已经下下来的那份铺开——100% 原尺寸、允许
     手指缩放，**零额外请求**。

     原地放大而不是开一个查看器：少一套 DOM，也不会多出「关了查看器消息位置
     跳走」这类毛病。再点一下收回去。 */
  function toggleFull(img) {
    var on = img.classList.toggle("shot-full");
    if (!on) return;
    // 铺开后这张比气泡宽得多，横向要能滑到底看右边，不然等于只看到左半张
    var b = box();
    if (b) {
      global.requestAnimationFrame(function () {
        var r = img.getBoundingClientRect();
        var br = b.getBoundingClientRect();
        if (r.left < br.left || r.right > br.right) {
          b.scrollLeft += (r.left - br.left) - 12;
        }
      });
    }
  }

  /* 缓存 key 和请求地址是两样东西（2026-11-24）：地址上带站点签名 `k=`，
     签名每次会话都可能换（服务重启就换），所以**key 里不带它**，否则重启
     一次本地缓存全成孤儿。`keyOf` 只做一件事：把 `&k=...` 摘掉。 */
  function keyOf(url) {
    return String(url || "").replace(/[&?]k=[^&]*/g, "");
  }

  /* 把一张图的字节拉下来存进本地缓存（按 key 判断存过没有）。
     用 `no-cors`：图本身有站点签名（`k=`），不需要 cookie；写进 IndexedDB
     只看能不能拿到字节，不看能不能读它。**失败一律不管**——缓存是优化，
     页面上的 <img> 自己照样会去请求。 */
  function pull(key, url) {
    if (!key || !global.WSImg) return;
    url = url || key;
    global.WSImg.has(key).then(function (yes) {
      if (yes) return undefined;
      return global.fetch(url, { credentials: "omit", mode: "no-cors" })
        .then(function (r) { return r.blob(); })
        .then(function (b) {
          // no-cors 的响应可能是 opaque（size 0），那就没有可存的东西
          if (b && b.size) return global.WSImg.put(key, b);
          return undefined;
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

  /* 下载这张图（2026-11-24 用户：「点下载的时候会有延迟，得等个七八秒钟才会
     显示」）。

     以前两步都在错的地方：
       ① `<a download href>` 是导航式下载，**不查 HTTP 缓存**，等于把图重下
          一遍（2026-11-24 上午改成 fetch，但拉的是**原图**——一张 8MB，缓存
          里本来就没有，点了当然要等七八秒）；
       ② 现在下载的是**列表上那张 512KB 的压缩档**，和画廊一个量级；它的字节
          早就躺在本地缓存里（显示时顺手存过），点下去是零网络。

     顺序：本地缓存 → fetch（HTTP 缓存 / 服务端）→ 老办法兜底。 */
  function downloadImage(card) {
    var file = card.file || (card.rec && card.rec.file) || "";
    if (!file) {
      sysNote("这张还没出来，等它画完再点下载");
      return;
    }
    var name = String(file).split("/").pop() || "image.png";
    var src = global.WSGen.img(file, FIT);
    var key = global.WSGen.imgKey(file, FIT);   // 缓存认这个（不带签名）

    function fromNet() {
      if (typeof global.fetch !== "function") { fallback(); return; }
      global.fetch(src, { credentials: "same-origin", cache: "default" })
        .then(function (r) {
          if (!r.ok) throw new Error("HTTP " + r.status);
          return r.blob();
        })
        .then(function (b) {
          if (!b || !b.size) throw new Error("空文件");
          saveBlob(b, name);
          if (global.WSImg) global.WSImg.put(key, b);
        })
        ["catch"](fallback);
    }

    function fallback() {
      // 最后一道：直接让浏览器去下。慢，但点了总有反应。
      var a = document.createElement("a");
      a.href = src;
      a.download = name;
      a.rel = "noopener";
      document.body.appendChild(a);
      a.click();
      a.remove();
    }

    if (!global.WSImg) { fromNet(); return; }
    global.WSImg.get(key).then(function (b) {
      if (b && b.size) saveBlob(b, name);
      else fromNet();
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

  function showImage(card, job) {
    if (card.ph) {
      var img = shotEl(job.file);
      img.addEventListener("error", function () {
        card.note.hidden = false;
        card.note.className = "note err";
        card.note.textContent = "图不在了，重画一张";
      });
      card.ph.parentNode.replaceChild(img, card.ph);
      card.ph = null;
      card.img = img;                 // 留个引用：下载/预览都靠它
    }
    card.note.hidden = true;
    card.stop && (card.stop.hidden = true);
    card.file = job.file || "";
    card.since = 0;
    card.noted = "";
    buildActs(card);
    if (card.rec) {
      card.rec.file = job.file;
      card.rec.note = "";
      card.rec.err = false;
      card.rec.job = "";
      if (global.WSStore) global.WSStore.save({ msgs: S.msgs });
    }
    scrollBottom();
  }

  /* 失败 / 超时。`stuck` 决定给什么按钮口径：卡住的那张给「重试」
     （按同一份提示词重排），普通失败给「重画」（换种子）。 */
  function fail(card, msg, stuck) {
    disarmStop(card);
    card.since = 0;
    card.noted = "";
    card.note.hidden = false;
    card.note.className = "note err";
    card.note.textContent = msg || "这张没出来，点重画再来一次";
    if (card.ph) card.ph.style.opacity = ".3";
    buildActs(card, stuck ? "重试" : "");
    if (card.rec) {
      card.rec.note = card.note.textContent;
      card.rec.err = true;
      card.rec.job = "";
      if (global.WSStore) global.WSStore.save({ msgs: S.msgs });
    }
    scrollBottom();
  }

  // ── 生图 ──────────────────────────────────────────────

  /* refs：这一轮要带上的参考图 id。生图接口本身是按描述出图，参考图随请求
     一起交给后端（它按渠道能力决定用不用），前端不自己拼提示词。 */
  /* `described`（2026-11-24）：这些参考图**已经读成描述写进 prompt 了**
     （「画同款」就是这种）。带上它，后端就不再识一次图——用户确认过的
     口径是「不再识，用输入栏那段」。 */
  function submitJob(prompt, skill, refs, described) {
    if (!skill) {
      sysNote("还没拿到渠道列表，去跑图台看一眼再回来");
      return;
    }
    var rec = { k: "img", cls: "", prompt: prompt, skill: skill,
                refs: (refs || []).slice(),
                described: !!described,
                job: "", file: "", note: "排队中…", err: false };
    var card = newCard(prompt, skill, rec);
    card.rec = rec;
    // 卡片自己记住参考图：点「重画」要按同一份角色重来，不能悄悄丢图
    card.refs = rec.refs;
    card.described = !!described;
    card.since = Date.now();       // 超时从这里起算，见 tick()
    card.noted = "排队中…";
    log(rec);
    var body = { skill: skill, prompt: prompt };
    if (refs && refs.length) {
      body.refs = refs;
      // 读过了就别让后端再读一遍，也别把同一段描述拼第二遍
      if (described) body.ref_described = true;
    }
    global.WSGen.submit(body).then(function (d) {
      if (!d || !d.ok) {
        fail(card, (d && d.error) || "没排上队");
        return;
      }
      card.job = d.job.id;
      rec.job = card.job;
      armStop(card);
      S.pending[card.job] = card;
      startPoll();
    });
  }

  /* 挂上「终止」：只有在卡片真的还挂着任务时才露出来（出图 / 失败时
     `disarmStop` 收起来）。点了就把本台的 job 取消——服务端会把排队中那张
     摘掉、正在跑那张定向掐停，并且**不投递**。 */
  function armStop(card) {
    var b = card.stop;
    if (!b) return;
    b.hidden = false;
    b.disabled = false;
    b.textContent = "终止";
    b.onclick = function () {
      if (!card.job) { b.hidden = true; return; }
      b.disabled = true;
      b.textContent = "停止中…";
      global.WSGen.cancel(card.job).then(function (d) {
        if (!d || !d.ok) {
          b.disabled = false;
          b.textContent = "终止";
          if (d && /结束了/.test(d.error || "")) disarmStop(card);
          return;
        }
        card.note.hidden = false;
        card.note.className = "note";
        card.note.textContent = "正在停，稍等…";
      })["catch"](function () {
        b.disabled = false;
        b.textContent = "终止";
      });
    };
  }

  function disarmStop(card) {
    if (card.stop) { card.stop.hidden = true; card.stop.onclick = null; }
    card.job = "";
  }

  function startPoll() {
    if (S.timer) return;
    S.fails = 0;
    S.timer = setInterval(tick, POLL_MS);
  }

  function stopPoll() {
    if (S.timer) { clearInterval(S.timer); S.timer = null; }
  }

  /* 状态接口连续失败时别再打接口，但**不要把卡片判死**：网络抖一下不等于
     这张图没了。把它们的计时按下暂停（`since` 往后推），等下一次状态恢复
     再接着算——否则一次断网就能把半个小时的画白等。 */
  function pauseClock() {
    var now = Date.now();
    Object.keys(S.pending).forEach(function (id) {
      var card = S.pending[id];
      if (card.since) card.since = now - (card.since - now);
    });
  }

  /* 判超时。两条线：
       · 一直没轮到（排队 / 准备中 / 还没进 ComfyUI）超过 SETTLE_MS；
       · 已经在画（running）超过 PAINT_MS。
     判了不是「报错完事」：卡片换成「重试」，用户点一下就按同一份提示词重排。
     这一步也不能把「画得慢」误杀——真在画的那张只看它自己的 PAINT_MS。 */
  function checkTimeout(card, ph) {
    if (!card.since) return false;
    var limit = (ph === "running") ? PAINT_MS : SETTLE_MS;
    if (Date.now() - card.since < limit) return false;
    var mins = Math.round(limit / 60000);
    delete S.pending[card.job];
    fail(card, (ph === "running")
      ? "等了 " + mins + " 分钟还在画，可能卡住了。点重试重排一张"
      : "排队等了 " + mins + " 分钟还没轮到，卡住了。点重试重排一张", true);
    return true;
  }

  function tick() {
    if (!Object.keys(S.pending).length) { stopPoll(); return; }
    global.WSGen.status().then(function (d) {
      if (!d || !d.ok) {
        S.fails += 1;
        if (S.fails >= MAX_FAIL) { stopPoll(); pauseClock(); }
        return;
      }
      S.fails = 0;
      var byId = {};
      (d.jobs || []).forEach(function (j) { byId[j.id] = j; });

      Object.keys(S.pending).forEach(function (id) {
        var card = S.pending[id];
        var job = byId[id];
        // 任务不在列表里 = 进程重启过，这张再也回不来，别让它一直转。
        // （服务端也会把「线程没了」的收成 error，这里只是兜住更早的那一下）
        if (!job) {
          delete S.pending[id];
          fail(card, "服务重启过，这张没了，点重试再排一次", true);
          return;
        }
        if (job.state === "done") {
          delete S.pending[id];
          disarmStop(card);
          showImage(card, job);
        } else if (job.state === "error" || job.state === "cancelled") {
          delete S.pending[id];
          disarmStop(card);
          // stuck=true 是服务端判的「线程丢了，这张根本没跑」
          fail(card, job.error || "这张没出来", !!job.stuck);
        } else {
          // 带上参考图时把它读图的结果说清楚：读到了就说「照着参考图画」，
          // 没读到就说「按描述画」——用户不该猜这张图到底用没用上。
          var refs = job.ref_note || "";
          var tail = refs ? "（" + refs + "）" : "";
          // 说清楚它**现在到底在哪**（2026-11-23）：`phase` 是后端去 ComfyUI
          // 现问的——排队 / 准备中 / 真在画 / 掉队了。原来一律写「正在生成」，
          // ComfyUI 重启后还挂在那儿转圈，用户根本不知道它是不是死了。
          var ph = job.phase || "";
          if (checkTimeout(card, ph)) return;
          var text;
          if (ph === "lost") {
            text = "ComfyUI 那边已经没有这张了（重启过），重新发一次吧";
          } else if (ph === "running") {
            text = "正在画…" + tail;
          } else if (ph === "queued") {
            var n = d.queue_depth;
            text = (typeof n === "number" && n > 0)
              ? "排队中，前面还有 " + n + " 张" + tail : "排队中…" + tail;
          } else {
            text = "准备中（等显卡空出来）" + tail;
          }
          card.note.hidden = false;
          card.note.className = "note" + (ph === "lost" ? " err" : "");
          card.note.textContent = text;
          card.noted = text;
          if (card.rec) card.rec.note = text;
        }
      });

      if (!Object.keys(S.pending).length) {
        stopPoll();
        if (global.WSStore) global.WSStore.save({ msgs: S.msgs });
      }
    });
  }

  // ── 输入栏 ────────────────────────────────────────────

  function grow() {
    el.say.style.height = "auto";
    el.say.style.height = Math.min(el.say.scrollHeight, 132) + "px";
    fitComposer();
  }

  // 输入栏是浮在消息上的玻璃层，消息区底部得留出它的实际高度
  function fitComposer() {
    if (!el.composer) return;
    document.documentElement.style.setProperty(
      "--composer-h", el.composer.offsetHeight + "px");
  }

  /* 输入栏被挪进子页（视图构建时移的）那一刻还没有尺寸，上面量到的是 0。
     这里补量一次，并替子页盯着：宿主一出现就补，别停在 0。 */
  function fitWhenSized() {
    if (!el.composer) return;
    if (el.composer.offsetHeight) { fitComposer(); return; }
    global.requestAnimationFrame(function () {
      if (el.composer.offsetHeight) fitComposer();
    });
  }

  function syncSend() {
    el.send.disabled = !el.say.value.trim() || el.say.disabled;
  }

  /* 发送。参考图的识图挪到这一步（2026-11-23 用户：上传不许立刻实图，
     点发送才实图）：
       1. 先把用户气泡和一起来的参考图铺上去——用户要能看见自己喂了哪张；
       2. 再读这一轮的参考图（`ensureDescribed`，读过的跳过，并行读）；
       3. 读到的描述交给 submit，跟用户这句话一起拼成最终提示词。
     第 2 步的等待有「正在看图…」顶着，且这时输入框已经清空，不会再被
     读出来的描述顶掉。
     第 3 步原先是在点发送的同一帧去取 refIds；现在识图要等，所以不能先
     commit（commit 会清空待发送列表），等识图完再清。 */
  function send() {
    var text = el.say.value.trim();
    if (!text) return;
    var refs = global.WSRef.list();
    addUser(text, refs);
    el.say.value = "";
    grow();
    syncSend();

    /* 在子页里发的话，发完就该回聊天页（2026-11-24 用户：「我在 AI 创作界面，
       上传图片，然后说需求，然后点击发送的时候，没有自动回到初始的聊天界面」）。

       为什么要退而不是原地等：气泡和出图卡片全都写进根消息区（见 box() 上
       面的注释），子页盖在上面时用户看到的是「点了发送、什么都没发生」。
       退页动画 0.28s，正好盖住识图 + 排队的起步，回来就是聊天页在跑。

       只退一层：AI 创作是从聊天页推进来的，退一层就回到根。漫画单本页
       （book）不借输入栏，走不到这儿。 */
    if (global.WSStack && global.WSStack.top()) {
      var top = global.WSStack.top();
      // 只退「借了输入栏」的页；别的子页里如果以后也能发，别顺手把人退出去
      if (top.dataset && top.dataset.dock) global.WSStack.back();
    }

    // 漫画模式：这句话是剧情需求，交给创作面板去跑「编剧 → 分页画」
    // 漫画那条链自己会读参考，不需要在这里等识图
    if (global.WSCreate && global.WSCreate.mode() === "comic") {
      global.WSRef.commit();
      global.WSCreate.runComic(text, refs.map(function (r) { return r.id; }));
      return;
    }

    /* 「画同款」把图读成描述填进输入栏之后，那张参考图就**已经**被读过了
       （`pre` 标记），用户点发送时不该再让它识一次图——否则同一段描述会被
       拼进提示词两遍，还白花一次视觉模型。见 ref.js 的 `same`。 */
    var pre = refs.filter(function (r) { return r && r.pre && r.id; });
    if (pre.length) {
      global.WSRef.commit();
      submitJob(text, S.skill, pre.map(function (r) { return r.id; }), true);
      return;
    }

    global.WSRef.ensureDescribed(refs).then(function (read) {
      global.WSRef.commit();
      submitJob(text, S.skill, read.map(function (r) { return r.id; }));
    });
  }

  function copyText(text) {
    if (global.navigator.clipboard && global.navigator.clipboard.writeText) {
      return global.navigator.clipboard.writeText(text)
        .then(function () { return true; })
        .catch(function () { return fallbackCopy(text); });
    }
    return Promise.resolve(fallbackCopy(text));
  }

  function fallbackCopy(text) {
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    var ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    document.body.removeChild(ta);
    return ok;
  }

  // ── 画风 ──────────────────────────────────────────────

  /* 选画风不弹提示（用户 2026-11-23：选了就是默认了，别每次都跳一条
     「已更换 XX 渠道」）。名字由输入栏上的画风按钮自己显示。 */
  function pickSkill(id) {
    if (!id || id === S.skill) { syncSkillName(); return; }
    S.skill = id;
    global.WSGen.setDefaultSkill(id);
    syncSkillName();
  }

  function syncSkillName() {
    if (!el.skillName) return;
    var c = S.channels.filter(function (x) { return x.id === S.skill; })[0] || {};
    el.skillName.textContent = (c.label && c.label !== c.id) ? c.label : (c.id || "画风");
  }

  function bootChannels() {
    el.say.disabled = true;
    global.WSGen.channels().then(function (d) {
      el.say.disabled = false;
      syncSend();
      if (!d || !d.ok) {
        sysNote("没拿到渠道列表，先去跑图台看看，回来再试");
        return;
      }
      S.channels = d.channels || [];
      var saved = global.WSGen.defaultSkill();
      var hit = S.channels.filter(function (c) { return c.id === saved; })[0];
      S.skill = (hit || S.channels[0] || {}).id || "";
      syncSkillName();
    });
  }

  // ── 初始化 ────────────────────────────────────────────

  function init() {
    el.scroll = $("chat-scroll");
    el.empty = $("chat-empty");
    el.composer = $("composer");
    el.say = $("say");
    el.send = $("btn-send");
    el.plus = $("btn-plus");
    el.menu = $("plus-menu");
    el.cam = $("file-camera");
    el.album = $("file-album");
    el.skillName = $("skill-name");

    global.addEventListener("resize", fitWhenSized);

    el.send.addEventListener("click", send);
    el.say.addEventListener("input", function () { grow(); syncSend(); });
    el.say.addEventListener("keydown", function (e) {
      // 手机回车就是换行；桌面 Enter 发送，Shift+Enter 换行
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        if (!el.send.disabled) send();
      }
    });

    el.plus.addEventListener("click", function () {
      el.menu.hidden = !el.menu.hidden;
    });
    document.addEventListener("click", function (e) {
      if (!el.menu.hidden && !el.menu.contains(e.target) && e.target !== el.plus) {
        el.menu.hidden = true;
      }
    });

    // 两个平级创作入口：只切模式，发什么由下面 send() 决定
    $("pick-img").addEventListener("click", function () {
      el.menu.hidden = true;
      global.WSCreate.setMode("img");
    });
    $("pick-comic").addEventListener("click", function () {
      el.menu.hidden = true;
      global.WSCreate.setMode("comic");
    });
    $("pick-camera").addEventListener("click", function () {
      el.menu.hidden = true;
      el.cam.click();
    });
    $("pick-album").addEventListener("click", function () {
      el.menu.hidden = true;
      el.album.click();
    });
    [el.cam, el.album].forEach(function (input) {
      input.addEventListener("change", function () {
        if (!input.files || !input.files.length) return;
        var f = input.files[0];
        input.value = "";
        global.WSRef.upload(f);
      });
    });

    global.addEventListener("ws:skill", function (e) {
      pickSkill(e.detail && e.detail.id);
    });

    grow();
    syncSend();
    /* 先取图片签名再重放存档：`WSGen.img()` 拼出来的地址要带 `k=`，本地缓存
       那条路才走得通（拿不到也不拦，页面照旧按 URL 显示图）。 */
    global.WSGen.imgsign().then(function () {
      if (global.WSArchive) global.WSArchive.restore();
    }, function () {
      if (global.WSArchive) global.WSArchive.restore();
    });
    global.WSRef.init();
    bootChannels();
  }

  global.WSChat = {
    init: init,
    sysNote: sysNote,
    busyNote: busyNote,      // 过场提示，不进存档、会用完自己消失
    card: shell,
    remember: remember,
    touch: touch,
    log: log,
    dropRef: dropRef,
    /* 参考图拆到 ref.js 了，这里留个转发：旧的调用点还在的话不会炸 */
    syncHint: function () { if (global.WSRef) global.WSRef.syncHint(); },
    paintUser: paintUser,
    paintNote: paintNote,
    /* 重放存档时复用同一张图（同一条地址 → 同一个缓存 key，重放和历史不会
       各下一遍）。见 archive.js 的 replay()。 */
    shotEl: shotEl,
    buildActs: buildActs,
    box: box,
    hideEmpty: hideEmpty,
    scrollBottom: scrollBottom,
    pending: S.pending,
    msgs: function () { return S.msgs; },
    startPoll: startPoll,
    /* 重放存档时用（见 archive.js）：把一条「其实已经没在跑」的卡片就地
       判成失败并给「重试」，以及给还活着的那条挂上终止/计时。 */
    failCard: fail,
    armStop: armStop
  };
})(window);
