// 管理页前端自检（2026-10-04 加，同日随「批量人设改居中弹窗」重写）。
//
// 为什么要它：web/admin.html 是纯前端，Python 测试碰不到。这版刚写完就靠它
// 抓出两个真 bug —— ① 点「批量人设…」时事件对象被顶到 openBulkModal 的 mode
// 参数位（MouseEvent 是 truthy）；② 面板原本渲染在中间列，点右列按钮根本看不见。
// 这类错 node --check 查不出、肉眼也难看出来。
//
// 跑法（jsdom 装在隔离工作区，不在项目里，所以要显式给 NODE_PATH）：
//   export PATH="/c/Users/gjl/.workbuddy/binaries/node/versions/22.22.2-3:$PATH"
//   NODE_PATH="C:/Users/gjl/.workbuddy/binaries/node/workspace/node_modules" \
//     node tools/web_check.js web/admin.html
//
// 首次准备（只需一次）：
//   cd C:/Users/gjl/.workbuddy/binaries/node/workspace && npm install jsdom
//
// 它用假 fetch 喂一份固定会话清单，真跑 admin.html 的内联脚本，然后点按钮、
// 看 DOM、并检查**真正发出去的请求体**（范围对不对，比只看界面可靠）。
const fs = require('fs');
const { JSDOM } = require('jsdom');

const htmlPath = process.argv[2] || 'web/admin.html';
const html = fs.readFileSync(htmlPath, 'utf8');
const NOW = Date.now();
const D = 60000;

function mkSession(o) {
  return Object.assign({
    messages: 3, mtime: NOW, session: true,
    interject: true, image_gen: true, nai: false,
    image_audit: false, image_send_format: null,
    cooldown_override: null, chance_override: null, gap_override: null,
  }, o);
}

function resp(obj) {
  return Promise.resolve({
    ok: true, status: 200, json: () => Promise.resolve(obj),
  });
}

const detail = {
  id: 'qq', name: 'QQ助手', description: '', prompt: '（人设）',
  prompt_file: 'prompt.md',
  providers: [{ id: 'deepseek', label: 'DeepSeek' }],
  global_provider: 'deepseek', provider: '', model: '',
  context_budget: 30000, global_context_budget: 30000,
  tools: null, skills: null,
  image_gen_on: true, nai_enabled: false,
  session_prompts: {}, session_prompt_agents: {}, session_system_prompts: {},
  interject_cooldown: 30, interject_chance: 0.3, interject_min_gap: 60,
  private_enable: true, private_whitelist: [], private_whitelist_on: true,
  private_image_quota_on: true, private_image_daily_limit: 10,
  private_image_quota_whitelist: [],
  image_send_format: 'auto',
  image_audit_globals: { group: false, private: false },
  image_audit_prompt: '', image_audit_prompt_default: '',
  vision_prompt: '', vision_prompt_default: '',
};

// 每个实例一份请求流水，用来断言「真的按选中的范围发出去」。
function boot(sessions, calls) {
  return new JSDOM(html, {
    runScripts: 'dangerously',
    url: 'http://127.0.0.1:5174/admin.html?agent=qq',
    beforeParse(w) {
      w.confirm = () => true;        // jsdom 不实现 confirm，清除模式要用
      w.fetch = (u, opts) => {
        const s = String(u);
        calls.push({ url: s, body: (opts && opts.body) || null });
        if (s.indexOf('/api/agents') >= 0) {
          return resp({ agents: [{ id: 'qq', name: 'QQ助手' }], default: 'qq' });
        }
        if (s.indexOf('/session_prompt_bulk') >= 0) {
          const b = JSON.parse((opts && opts.body) || '{}');
          const n = (b.keys || []).length;
          return resp({ ok: true, applied: n, cleared: n, skipped: [] });
        }
        if (s.indexOf('/sessions') >= 0) {
          return resp({
            sessions, names_ok: true, agent: 'qq', image_gen_on: true,
            nai_enabled: false, session_prompts: {},
            session_prompt_agents: {}, session_system_prompts: {},
          });
        }
        if (s.indexOf('/api/models') >= 0) return resp({ models: [] });
        if (s.indexOf('/api/usage') >= 0) {
          return resp({ dates: [], table: {}, rows: [] });
        }
        if (s.indexOf('/api/agent/') >= 0) return resp(detail);
        return resp({});
      };
    },
  });
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let fail = 0;
function check(name, cond, extra) {
  console.log((cond ? '  PASS  ' : '  FAIL  ') + name
    + (extra ? '   [' + extra + ']' : ''));
  if (!cond) fail++;
}

const FULL = [
  mkSession({ key: 'group_111', kind: 'group', target_id: '111',
              name: '绘画交流群（111）', mtime: NOW }),
  mkSession({ key: 'group_222', kind: 'group', target_id: '222',
              name: '技术群（222）', mtime: NOW - D }),
  mkSession({ key: 'private_333', kind: 'private', target_id: '333',
              name: '好友A（333）', mtime: NOW - 2 * D }),
  mkSession({ key: 'private_444', kind: 'private', target_id: '444',
              name: '好友B（444）', mtime: NOW - 3 * D }),
  mkSession({ key: 'main', kind: 'main', target_id: null, name: '主会话',
              mtime: NOW - 4 * D, session: true }),
];

(async () => {
  // ── 场景一：群 + 私聊都有 ────────────────────────────
  const calls = [];
  const dom = boot(FULL, calls);
  const w = dom.window, doc = w.document;
  const $ = (id) => doc.getElementById(id);
  await sleep(500);

  const cbs = () => Array.from(
    $('sessList').querySelectorAll('input[type=checkbox]'));
  const modal = () => $('bulkModal');
  const radios = () => Array.from(
    modal().querySelectorAll('input[type=radio]'));
  const btns = () => Array.from(modal().querySelectorAll('button'));
  const radioOf = (id) => radios().find((r) => r.value === id);
  const pick = (id) => {
    const r = radioOf(id);
    r.checked = true;
    r.dispatchEvent(new w.Event('change'));
  };

  check('列表渲染出 4 条可勾选会话（主会话不参与）', cbs().length === 4,
        'cb=' + cbs().length);
  check('批量栏可见', $('sessBulkBar').style.display !== 'none',
        $('sessBulkBar').style.display);
  check('批量栏只剩「批量人设…」「清除人设…」两个入口',
        $('selGroups') === null && $('selPrivates') === null
        && $('selAll') === null && $('selInvert') === null
        && $('bulkApply') !== null && $('bulkClear') !== null);
  check('没勾选时两个入口也能点（不再灰掉）',
        $('bulkApply').disabled === false && $('bulkClear').disabled === false);
  check('弹窗初始是关着的', $('bulkMask').style.display === 'none',
        $('bulkMask').style.display);

  // ── 点「批量人设…」→ 居中弹窗 ────────────────────────
  $('bulkApply').click();
  check('点「批量人设…」弹出浮层', $('bulkMask').style.display === 'flex',
        $('bulkMask').style.display);
  check('标题是「批量设置人设」',
        modal().querySelector('h3').textContent === '批量设置人设',
        modal().querySelector('h3').textContent);
  check('三个输入都在（人设覆盖 / 附加词 / 借用）',
        modal().querySelectorAll('textarea').length === 2
        && modal().querySelectorAll('select').length === 1);
  const labels = radios().map((r) => r.value + '=' + r.parentElement.textContent
    + (r.disabled ? '(禁用)' : ''));
  check('范围三选一：全部群聊 / 全部私聊 / 已勾选（0 条禁用）',
        radios().length === 3 && !radioOf('group').disabled
        && !radioOf('private').disabled && radioOf('picked').disabled,
        labels.join(' | '));
  check('默认选中「全部群聊」', radioOf('group').checked === true);
  check('底部按钮写明会改到谁：「应用到全部群聊（2 条）」',
        btns()[1].textContent === '应用到全部群聊（2 条）',
        btns()[1].textContent);

  // 切到「全部私聊」
  pick('private');
  check('切到全部私聊后按钮跟着改', btns()[1].textContent === '应用到全部私聊（2 条）',
        btns()[1].textContent);
  check('选中态只落在一个范围上',
        radiiOn(radios()).join(',') === 'private', radiiOn(radios()).join(','));

  // 填附加词 → 应用 → 检查真发出去的 keys
  const tas = modal().querySelectorAll('textarea');
  tas[1].value = '说话更毒舌';
  calls.length = 0;
  btns()[1].click();
  await sleep(60);
  const put = calls.filter((c) => c.url.indexOf('session_prompt_bulk') >= 0);
  check('点应用真的发了一个 PUT', put.length === 1, 'n=' + put.length);
  const sent = put.length ? JSON.parse(put[0].body) : {};
  check('发出去的范围 = 2 条私聊（不多不少）',
        JSON.stringify((sent.keys || []).slice().sort())
        === JSON.stringify(['private_333', 'private_444']),
        JSON.stringify(sent.keys));
  check('带上了附加词', sent.text === '说话更毒舌', JSON.stringify(sent.text));
  check('应用后弹窗自动关闭', $('bulkMask').style.display === 'none');
  check('状态栏给出结果', /已对 2 条会话应用/.test($('sessStatus').textContent),
        $('sessStatus').textContent);

  // ── 勾选一条 → 多出「已勾选」范围且默认选它 ─────────────
  const cb0 = cbs()[0];
  cb0.checked = true;
  cb0.dispatchEvent(new w.Event('change'));
  check('勾选后批量栏显示条数 + 出现「清空勾选」',
        /已勾选 1 条/.test($('selCount').textContent)
        && $('selNone').style.display !== 'none',
        $('selCount').textContent);
  $('bulkApply').click();
  check('「已勾选」范围可用了',
        radioOf('picked').disabled === false,
        radioOf('picked').parentElement.textContent);
  check('刚勾过就默认按「已勾选」开', radioOf('picked').checked === true);
  check('按钮写明是 1 条', btns()[1].textContent === '应用到已勾选（1 条）',
        btns()[1].textContent);

  // 点遮罩 / Esc 都能关
  $('bulkMask').click();
  check('点遮罩空白处关闭', $('bulkMask').style.display === 'none');
  $('bulkApply').click();
  doc.dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Escape' }));
  check('Esc 也能关', $('bulkMask').style.display === 'none');
  $('bulkApply').click();
  check('取消按钮能关', (() => {
    const idx = btns().findIndex((b) => b.textContent === '取消');
    btns()[idx].click();
    return $('bulkMask').style.display === 'none';
  })());

  // ── 清除人设：弹窗没有输入框，只有范围 ───────────────
  $('bulkClear').click();
  check('标题是「批量清除人设」',
        modal().querySelector('h3').textContent === '批量清除人设',
        modal().querySelector('h3').textContent);
  check('清除模式不显示三个输入框',
        modal().querySelectorAll('textarea').length === 0
        && modal().querySelectorAll('select').length === 0);
  // 此刻还勾着 1 条，默认会落在「已勾选」——这里显式切到全部群聊，
  // 顺带验证「换范围 → 按钮文案和发出去的 keys 都跟着换」。
  pick('group');
  check('清除按钮写明会清谁：「清除全部群聊的人设（2 条）」',
        btns()[1].textContent === '清除全部群聊的人设（2 条）',
        btns()[1].textContent);
  calls.length = 0;
  btns()[1].click();
  await sleep(60);
  const clr = calls.filter((c) => c.url.indexOf('session_prompt_bulk') >= 0);
  const cbody = clr.length ? JSON.parse(clr[0].body) : {};
  check('清除发的是 clear:true 且只带 2 个群 key',
        cbody.clear === true
        && JSON.stringify((cbody.keys || []).slice().sort())
           === JSON.stringify(['group_111', 'group_222']),
        JSON.stringify(cbody));
  dom.window.close();

  // ── 场景二：一条群都没有 ─────────────────────────────
  const calls2 = [];
  const onlyPriv = FULL.filter((s) => s.kind !== 'group');
  const dom2 = boot(onlyPriv, calls2);
  const w2 = dom2.window, $2 = (id) => w2.document.getElementById(id);
  await sleep(500);
  $2('bulkApply').click();
  const r2 = Array.from($2('bulkModal').querySelectorAll('input[type=radio]'));
  const g2 = r2.find((r) => r.value === 'group');
  check('没有群时「全部群聊」禁用、默认落到「全部私聊」',
        g2.disabled === true && r2.find((r) => r.value === 'private').checked,
        r2.map((r) => r.value + (r.disabled ? '(禁用)' : '')).join(','));
  check('没群时批量栏照常可见', $2('sessBulkBar').style.display !== 'none');
  dom2.window.close();

  console.log(fail ? '\nFAILED ' + fail : '\nALL PASS');
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.error('脚本异常：', e); process.exit(2); });

function radiiOn(list) {
  return list.filter((r) => r.checked).map((r) => r.value);
}
