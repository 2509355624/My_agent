/* 用 jsdom 把 moss_studio.html 跑一遍：验证渲染、事件绑定、自动保存、出图请求。
 * 视觉验证不了（本机 Edge headless 不出图、CDP 连上不响应），逻辑能过就是能过。
 *
 *   NODE_PATH=<node workspace>/node_modules node tools/check_moss_page.js
 *
 * 依赖 state/_list.json 与 state/_status.json 两份接口快照（test client 抓的）。
 */
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const ROOT = path.dirname(__dirname);
const html = fs.readFileSync(path.join(ROOT, "web/moss_studio.html"), "utf8");
const LIST = JSON.parse(fs.readFileSync(path.join(ROOT, "state/_list.json"), "utf8"));
const STATUS = JSON.parse(fs.readFileSync(path.join(ROOT, "state/_status.json"), "utf8"));

const calls = [];
let saved = null;
const errors = [];

const dom = new JSDOM(html, {
  url: "http://127.0.0.1:5199/moss",
  runScripts: "dangerously",
  pretendToBeVisual: true,
  beforeParse(win) {
    win.fetch = async (p, opt) => {
      const body = opt && opt.body ? JSON.parse(opt.body) : null;
      calls.push({ path: p, body });
      let data = { ok: false, error: "未桩" };
      if (p.indexOf("/api/moss/list") === 0) data = LIST;
      else if (p.indexOf("/api/moss/status") === 0) data = STATUS;
      else if (p.indexOf("/api/moss/save") === 0) {
        saved = body;
        data = JSON.parse(JSON.stringify(STATUS));
        data.issues = [];
        data.pages[0].prompt = "SAVED-PROMPT:" + body.spec.pages[0].panels[0].bubble;
      } else if (p.indexOf("/api/moss/render") === 0) data = { ok: true, pages: [1] };
      return { ok: true, status: 200, json: async () => data };
    };
    win.scrollTo = () => {};
    win.addEventListener("error", (e) => errors.push(String(e.message)));
  },
});
const doc = dom.window.document;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const ok = (cond, msg) => console.log((cond ? "  ✓ " : "  ✗ ") + msg);
let fail = 0;

function check(cond, msg) {
  if (!cond) fail++;
  ok(cond, msg);
}

(async () => {
  await sleep(900);

  console.log("[启动]");
  check(errors.length === 0, "无 JS 错误" + (errors.length ? "：" + errors.join(" | ") : ""));
  check(doc.querySelectorAll("#histsel option").length > 1, "历史任务列表已填充");
  check(doc.getElementById("spec-card").style.display !== "none", "剧本卡已显示");

  console.log("[渲染]");
  const block = doc.querySelector(".page-block");
  check(!!block, "生成了页块");
  check(doc.querySelectorAll(".panel").length === 4, "四格编辑器（实际 " +
    doc.querySelectorAll(".panel").length + "）");
  const badge = doc.querySelector('[data-role="badge"]');
  check(badge && badge.textContent === "已完成", "徽标=已完成（" + (badge || {}).textContent + "）");
  check(/seed 20261501/.test(doc.querySelector('[data-role="seed"]').textContent),
    "显示 seed 与耗时：" + doc.querySelector('[data-role="seed"]').textContent);
  const img = doc.querySelector('.canvas img');
  check(img && img.style.display !== "none" && img.getAttribute("src").indexOf("/api/moss/img") === 0,
    "图片已挂上：" + (img ? img.getAttribute("src").slice(0, 48) : "无"));
  const pt = doc.querySelector('[data-role="prompt"]');
  check(pt && pt.value.length > 500, "提示词预览已填（" + (pt ? pt.value.length : 0) + " 字符）");
  check(doc.getElementById("issues").textContent.indexOf("体检通过") >= 0, "体检通过提示");

  console.log("[编辑 → 自动保存]");
  const bub = doc.querySelector('input[data-field="bubble"]');
  bub.value = "喵";
  bub.dispatchEvent(new dom.window.Event("input", { bubbles: true }));
  await sleep(1600);
  check(saved && saved.spec.pages[0].panels[0].bubble === "喵", "改动进了 save 请求");
  check(saved && saved.task === "20261021_2327_打盹的柴犬", "save 带了 task");
  check(pt.value.indexOf("SAVED-PROMPT:喵") === 0, "保存后提示词预览跟着更新");

  console.log("[换版式 = 换格数]");
  const lay = doc.querySelector('select[data-field="layout"]');
  lay.value = "3t";
  lay.dispatchEvent(new dom.window.Event("input", { bubbles: true }));
  await sleep(600);
  check(doc.querySelectorAll(".panel").length === 3, "2x2 → 3t 后变 3 格（实际 " +
    doc.querySelectorAll(".panel").length + "）");
  // 切版式会整页重建，旧 select 的引用已经不在 DOM 里了 → 重新取
  const lay2 = doc.querySelector('select[data-field="layout"]');
  lay2.value = "2x2";
  lay2.dispatchEvent(new dom.window.Event("input", { bubbles: true }));
  await sleep(900);
  check(doc.querySelectorAll(".panel").length === 4, "3t → 2x2 后变回 4 格");
  check(!!doc.querySelector('.canvas img').getAttribute("src"), "重建后图片重新挂上");

  console.log("[出图]");
  const before = calls.length;
  doc.querySelector('button[data-act="draw"]')
    .dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true }));
  await sleep(800);
  const render = calls.slice(before).filter((c) => c.path.indexOf("/api/moss/render") === 0);
  check(render.length === 1, "点了出图 → 发了一次 render");
  check(render[0] && render[0].body.pages[0] === 1, "render 指定了第 1 页");
  check(!render[0].body.reroll, "普通出图不带 reroll");

  console.log("[换 seed]");
  const before2 = calls.length;
  doc.querySelector('button[data-act="reroll"]')
    .dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true }));
  await sleep(800);
  const re = calls.slice(before2).filter((c) => c.path.indexOf("/api/moss/render") === 0);
  check(re.length === 1 && re[0].body.reroll === true, "换 seed 带 reroll=true");

  console.log("\n" + (fail ? "✗ " + fail + " 项失败" : "✓ 全部通过"));
  process.exit(fail ? 1 : 0);
})();
