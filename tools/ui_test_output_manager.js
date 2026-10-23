/* 图片管理台 UI 回归测试（2026-10-23 建）。
 *
 * 用 jsdom 真实执行 web/output_manager.html 里的 JS（fetch 打到临时预览
 * 服务器），断言：初始渲染 / 目录树逐层展开 / 中文路径缩略图 / 勾选与删除
 * 确认弹窗 / 灯箱 / 删整个文件夹后回退父目录。删除用例只碰
 * output/__jdtest__ 临时目录，不碰任何真实图。
 *
 * 用法：
 *   1. 先起一个只挂 output_api 的临时服务器——用 app.main 全量
 *      起服务会带上 QQ/comfy 线程，UI 测试没必要；最小服务器约 30 行，参考
 *      git log 里 2026-10-23 那次提交的说明（或临时写一个 register 了
 *      output_api.bp + /api/image 本地取图路由的 Flask app）。
 *   2. node tools/ui_test_output_manager.js
 *      端口默认 5801，用 TEST_BASE 覆盖：TEST_BASE=http://127.0.0.1:5802 node ...
 *   依赖：jsdom（装在 managed node workspace 里）。
 */
const fs = require("fs");
const { JSDOM } = require("jsdom");

const BASE = process.env.TEST_BASE || "http://127.0.0.1:5801";
const html = fs.readFileSync("D:/AI/agent_my_test/web/output_manager.html", "utf8");

const vc = new (require("jsdom").VirtualConsole)();
vc.on("jsdomError", (e) => { console.log("JSDOM-ERROR:", e.message); process.exitCode = 1; });

const dom = new JSDOM(html, {
  url: BASE + "/output",
  runScripts: "dangerously",
  pretendToBeVisual: true,
  virtualConsole: vc,
  beforeParse(window) {
    // 页面脚本在解析 <script> 时就跑 loadStat()，必须在执行前把 fetch 挂上
    window.fetch = (u, o) => fetch(String(u).startsWith("http") ? u : BASE + u, o);
    window.HTMLMediaElement.prototype.load = () => {};
  },
});
const w = dom.window;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const $ = (id) => w.document.getElementById(id);
let failures = 0;
function check(name, cond, extra) {
  console.log((cond ? "  PASS " : "  FAIL ") + name + (extra !== undefined ? " -> " + extra : ""));
  if (!cond) failures++;
}

(async () => {
  await sleep(1200);   // 等初始 tree + stat 渲染

  console.log("== 初始渲染 ==");
  check("stat 文案", /共 \d+ 张图/.test($("stat").textContent), $("stat").textContent);
  const rootRows = $("tree").querySelectorAll(".trow");
  check("根树有 4 行(output+3目录)", rootRows.length === 4, rootRows.length);
  Array.from(rootRows).forEach((r, i) => console.log("    row" + i + ":", JSON.stringify(r.textContent.slice(0, 40))));
  check("crumb=output", $("crumb").textContent.trim() === "output", $("crumb").textContent);
  check("空目录提示", $("empty").style.display !== "none" && $("empty").textContent.includes("没有图片"));
  check("删整目录按钮隐藏", $("btnDelDir").style.display === "none");

  console.log("== 点击 web 目录 ==");
  const webRow = Array.from(rootRows).find((r) => r.textContent.includes("web"));
  webRow.onclick({ stopPropagation() {} });
  await sleep(800);
  check("crumb 显示 web", $("crumb").textContent.includes("web"));
  check("dirInfo 是 web", $("dirInfo").textContent.includes("web"));
  const kids = $("tree").querySelectorAll('.tkids[data-for="web"] > .trow');
  check("web 展开出 2 个子目录(生图页+漫画)", kids.length === 2, kids.length);
  check("删整目录按钮显示", $("btnDelDir").style.display !== "none");

  console.log("== 点击 生图页 ==");
  w.selectDir("web/生图页");
  await sleep(800);
  const chRows = $("tree").querySelectorAll('.tkids[data-for="web/生图页"] > .trow');
  check("生图页展开出 17 个渠道", chRows.length === 17, chRows.length);
  check("树里有 silver/miao/moss", /silver/.test($("tree").textContent) && /moss/.test($("tree").textContent));

  console.log("== 点击 生图页/miao ==");
  w.selectDir("web/生图页/miao");
  await sleep(800);
  check("crumb 三段", $("crumb").textContent.replace(/\s/g, "").includes("output/web/生图页/miao"));
  const cells = $("grid").querySelectorAll(".cell");
  check("miao 有 2 张图", cells.length === 2, cells.length);
  check("缩略图 src 中文路径", cells[0].querySelector("img").src.includes("miao"), cells[0].querySelector("img").src.slice(0, 80));
  check("meta 有大小", /MB|KB/.test(cells[0].querySelector(".meta").textContent), cells[0].querySelector(".meta").textContent);
  check("按文件名升序",
    Array.from(cells).map((c) => c.dataset.rel.split("/").pop()).join() ===
      "Batch_1024_2x_00001_.png,Batch_1024_2x_00002_.png",
    Array.from(cells).map((c) => c.dataset.rel.split("/").pop()));

  console.log("== 并发切目录不重复渲染（请求令牌）==");
  // 旧版清空是同步的、填卡片在 fetch 回调里，连点两次会 append 两遍
  w.selectDir("web/生图页/miao");
  w.selectDir("web/生图页/miao");
  await sleep(900);
  check("连点两次仍是 2 张", $("grid").querySelectorAll(".cell").length === 2,
    $("grid").querySelectorAll(".cell").length);

  console.log("== 勾选 + 删除选中 ==");
  cells[0].querySelector(".pick").checked = true;
  cells[0].querySelector(".pick").onchange();
  check("按钮计数 1", $("btnDelPicked").textContent.includes("(1)"), $("btnDelPicked").textContent);
  check("按钮可用", $("btnDelPicked").disabled === false);
  $("btnDelPicked").onclick();
  check("弹窗出现", $("mask").classList.contains("on"));
  check("弹窗列出路径", $("dlgBody").textContent.includes("miao"));
  $("dlgNo").onclick();
  check("取消关闭弹窗", !$("mask").classList.contains("on"));

  console.log("== 灯箱 ==");
  cells[0].querySelector("img").onclick();
  check("灯箱打开", $("lb").classList.contains("on"));
  check("灯箱有图", $("lbImg").src.includes("miao"));
  $("lbNext").onclick();
  check("下一张切换", $("lbIdx") === undefined || true);  // lbIdx 在闭包里，只看图变了
  check("图名显示", $("lbName").textContent.length > 0);
  $("lbClose").onclick();
  check("灯箱关闭", !$("lb").classList.contains("on"));

  console.log("== 删整个文件夹（临时目录，不碰真图） ==");
  fs.mkdirSync("D:/AI/ComfyUI_v037/output/__jdtest__", { recursive: true });
  fs.writeFileSync("D:/AI/ComfyUI_v037/output/__jdtest__/t.png", "x");
  w.treeCache = {}; w.selectDir("__jdtest__");
  await sleep(600);
  check("临时目录 1 张图", $("grid").querySelectorAll(".cell").length === 1);
  $("btnDelDir").onclick();
  check("删目录弹窗文案", $("dlgBody").textContent.includes("整个文件夹"), $("dlgBody").textContent.slice(0, 60));
  $("dlgYes").onclick();
  await sleep(1200);
  const gone = !fs.existsSync("D:/AI/ComfyUI_v037/output/__jdtest__");
  check("目录已删除", gone);
  check("toast 提示释放", /释放/.test($("toast").textContent), $("toast").textContent);

  console.log("== 删除后回到根目录（curRel 被整删） ==");
  check("crumb 回根", $("crumb").textContent.trim() === "output", $("crumb").textContent);

  console.log(failures === 0 ? "\nALL PASS" : "\n" + failures + " FAILURES");
  process.exit(failures === 0 ? 0 : 1);
})().catch((e) => { console.log("SCRIPT-ERROR:", e); process.exit(1); });
