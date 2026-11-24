/* 图的本地缓存：把**字节**存进浏览器（IndexedDB），换隧道域名也不失效。

## 为什么不能只靠 HTTP 缓存

浏览器缓存是按**完整 URL** 存的。群友拿到的是 Cloudflare 临时隧道地址，
隧道一重连域名就换（`xxxx.trycloudflare.com` → `yyyy.trycloudflare.com`）。
昨天存下的十几张图，域名一变全部作废——这正是用户 2026-11-24 说的
「我每次重新进入这个工作台，图片好像不是从缓存里面拿的」。

所以这里只认「相对路径」，**域名不进 key**：同一个 `f=xxx.png&fit=512k`
不管挂在哪个域名下都是同一份字节，换域名照样命中。

## 为什么不是 localStorage

图片是二进制，一张 512KB 转 base64 变 680KB，localStorage 一共才 5MB，
存五张就满。IndexedDB 能存 Blob，几百 MB 也没问题。

## 容量与淘汰

默认 300MB（用户 2026-11-24 拍的数）。每次写入顺手把总大小算一遍，超了就
按「最久没用」淘汰，一直删到上限的八成——留一点余量，免得每写一张都要淘汰。
读一次会更新 `used` 时间，所以常用的图不会被挤掉。

## 失败怎么办

隐私模式、配额满、老的 iPaste 浏览器不支持 IndexedDB —— 一律**静默降级**：
`get` 返回 null、`put` 什么都不做，页面照旧走 `<img src>` 和 HTTP 缓存。
缓存是优化，不能因为存不进去就让图看不见。
*/
(function (global) {
  "use strict";

  var DB = "wsimg";
  var STORE = "blob";
  var CAP = 300 * 1024 * 1024;      // 300MB
  var LOW = Math.floor(CAP * 0.8);  // 淘汰到这个水位以下

  var dbp = null;      // Promise<IDBDatabase|null>
  var used = null;     // 打开后填的内存副本：path → {size, used}

  function open() {
    if (dbp) return dbp;
    dbp = new Promise(function (resolve) {
      if (!global.indexedDB) { resolve(null); return; }
      var req;
      try { req = global.indexedDB.open(DB, 1); }
      catch (e) { resolve(null); return; }
      req.onupgradeneeded = function () {
        var db = req.result;
        if (!db.objectStoreNames.contains(STORE)) {
          var os = db.createObjectStore(STORE, { keyPath: "path" });
          os.createIndex("used", "used");
        }
      };
      req.onsuccess = function () { resolve(req.result); };
      req.onerror = function () { resolve(null); };
      req.onblocked = function () { resolve(null); };
    });
    return dbp;
  }

  function tx(db, mode) {
    return db.transaction(STORE, mode).objectStore(STORE);
  }

  function asPromise(req) {
    return new Promise(function (resolve, reject) {
      req.onsuccess = function () { resolve(req.result); };
      req.onerror = function () { reject(req.error); };
    });
  }

  /* 第一次用的时候把「已占多少、各条多老」拉进内存。几万条也就几毫秒，
     而且只在第一次 put 时做一次——比每次写入都全表扫便宜得多。 */
  function ensureUsed(db) {
    if (used) return Promise.resolve(used);
    return asPromise(tx(db, "readonly").openCursor()).then(function () {
      return _scan(db);
    });
  }

  function _scan(db) {
    var map = {};
    return new Promise(function (resolve) {
      var req = tx(db, "readonly").openCursor();
      req.onsuccess = function () {
        var c = req.result;
        if (!c) { used = map; resolve(map); return; }
        var v = c.value || {};
        map[v.path] = { size: v.size || 0, used: v.used || 0 };
        c.continue();
      };
      req.onerror = function () { used = map; resolve(map); };
    });
  }

  function total(map) {
    var n = 0;
    Object.keys(map).forEach(function (k) { n += map[k].size || 0; });
    return n;
  }

  /* 超了就删最久没用的。一次删除只留到八成，避免「每张都淘汰一张」。 */
  function evict(db, map) {
    if (total(map) <= CAP) return Promise.resolve();
    var keys = Object.keys(map).sort(function (a, b) {
      return (map[a].used || 0) - (map[b].used || 0);
    });
    var sum = total(map);
    return new Promise(function (resolve) {
      var store = tx(db, "readwrite");
      keys.forEach(function (k) {
        if (sum <= LOW) return;
        sum -= map[k].size || 0;
        delete map[k];
        try { store["delete"](k); } catch (e) { /* 忽略 */ }
      });
      store.transaction.oncomplete = function () { resolve(); };
      store.transaction.onerror = function () { resolve(); };
    });
  }

  /* 读：没存过 / 存过但过期了（同路径换了内容）都返回 null。 */
  function get(path) {
    return open().then(function (db) {
      if (!db || !path) return null;
      return asPromise(tx(db, "readonly").get(path)).then(function (v) {
        if (!v || !v.blob) return null;
        // 顺手记一次「用过」，淘汰时就知道它还热
        var map = used;
        if (map && map[path]) map[path].used = Date.now();
        return v.blob;
      })["catch"](function () { return null; });
    })["catch"](function () { return null; });
  }

  /* 写：存 Blob 本身（不转 base64）。同一路径重复写就是覆盖。 */
  function put(path, blob) {
    if (!path || !blob || !blob.size) return Promise.resolve(false);
    return open().then(function (db) {
      if (!db) return false;
      return ensureUsed(db).then(function (map) {
        var rec = { path: path, blob: blob, size: blob.size, used: Date.now() };
        map[path] = { size: blob.size, used: rec.used };
        return asPromise(tx(db, "readwrite").put(rec))
          .then(function () { return evict(db, map); })
          .then(function () { return true; });
      })["catch"](function () { return false; });
    })["catch"](function () { return false; });
  }

  function has(path) {
    return get(path).then(function (b) { return !!b; });
  }

  /* 给 <img> 用的对象地址：命中就返回 blob URL（用完要还），没命中给空串。
     调用方负责在换图 / 离开页面时 revoke——一张 512KB 的 blob URL 留着
     不放，切几十张就是几十 MB 的内存。 */
  function url(path) {
    return get(path).then(function (b) {
      return b ? global.URL.createObjectURL(b) : "";
    })["catch"](function () { return ""; });
  }

  function clear() {
    used = null;
    return open().then(function (db) {
      if (!db) return false;
      return asPromise(tx(db, "readwrite").clear()).then(function () {
        return true;
      })["catch"](function () { return false; });
    })["catch"](function () { return false; });
  }

  /* 存了多少（画「我的」页的缓存信息用，也可以不管） */
  function size() {
    return open().then(function (db) {
      if (!db) return 0;
      return ensureUsed(db).then(total);
    })["catch"](function () { return 0; });
  }

  global.WSImg = {
    get: get, put: put, has: has, url: url, clear: clear, size: size,
    CAP: CAP
  };
})(window);
