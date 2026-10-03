// 管理页前端自检（2026-10-04 加）。
//
// 为什么要它：web/admin.html 是纯前端，Python 测试碰不到；「全部群 / 全部私聊」
// 这版刚写完就靠它抓出一个真 bug —— 点「批量人设…」时事件对象被顶到
// openBulkPanel 的 force 参数位上（MouseEvent 是 truthy），面板只会重建、
// 永远收不起来。这类错 node --check 查不出、肉眼也难看出来。
//
// 跑法（jsdom 装在隔离工作区，不在项目里，所以要显式给 NODE_PATH）：
//   export PATH="/c/Users/gjl/.workbuddy/binaries/node/versions/22.22.2-3:$PATH"
//   NODE_PATH="C:/Users/gjl/.workbuddy/binaries/node/workspace/node_modules" \
//     node tools/web_check.js web/admin.html
//
// 首次准备（只需一次）：
//   cd C:/Users/gjl/.workbuddy/binaries/node/workspace && npm install jsdom
//
// 它用假 fetch 喂一份固定会话清单，真跑 admin.html 的内联脚本，然后点按钮看 DOM。
// 改了管理页的会话列表 / 批量人设相关逻辑，就跑一遍。
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

function boot(sessions) {
  return new JSDOM(html, {
    runScripts: 'dangerously',
    url: 'http://127.0.0.1:5174/admin.html?agent=qq',
    beforeParse(w) {
      w.fetch = (u) => {
        const s = String(u);
        if (s.indexOf('/api/agents') >= 0) {
          return resp({ agents: [{ id: 'qq', name: 'QQ助手' }], default: 'qq' });
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
              name: '好友A（333）', mtime: NOW - 2 * D, nai: false }),
  mkSession({ key: 'private_444', kind: 'private', target_id: '444',
              name: '好友B（444）', mtime: NOW - 3 * D, nai: false }),
  mkSession({ key: 'main', kind: 'main', target_id: null, name: '主会话',
              mtime: NOW - 4 * D, session: true }),
];

(async () => {
  // ── 场景一：群 + 私聊都有 ────────────────────────────
  const dom = boot(FULL);
  const w = dom.window, doc = w.document;
  const $ = (id) => doc.getElementById(id);
  await sleep(500);

  const cbs = () => Array.from(
    $('sessList').querySelectorAll('input[type=checkbox]'));
  const checkedKeys = () => cbs().filter((c) => c.checked)
    .map((c) => c.dataset.key).sort();

  check('列表渲染出 4 条可勾选会话（主会话不参与）', cbs().length === 4,
        'cb=' + cbs().length);
  check('批量栏可见（有群/私聊时）',
        $('sessBulkBar').style.display !== 'none', $('sessBulkBar').style.display);

  // 点「全部群」
  $('selGroups').click();
  check('点「全部群」后已选 = 2（只有群）', $('selCount').textContent === '已选 2 条',
        $('selCount').textContent);
  check('勾选框跟着勾上：恰好两个 group_',
        checkedKeys().join(',') === 'group_111,group_222', checkedKeys().join(','));
  check('面板被自动弹开且标题写明是整类',
        /全部群/.test($('sessBulkPanel').textContent)
        && /2 条会话/.test($('sessBulkPanel').textContent),
        $('sessBulkPanel').textContent.slice(0, 60));
  check('面板里有三个输入（人设覆盖/附加词/借用）',
        $('sessBulkPanel').querySelectorAll('textarea').length === 2
        && $('sessBulkPanel').querySelectorAll('select').length === 1,
        'ta=' + $('sessBulkPanel').querySelectorAll('textarea').length
        + ' sel=' + $('sessBulkPanel').querySelectorAll('select').length);

  // 再点「全部私聊」：叠加（群 2 + 私聊 2 = 4）
  $('selPrivates').click();
  check('点「全部私聊」是叠加：已选 4 条', $('selCount').textContent === '已选 4 条',
        $('selCount').textContent);
  check('面板按新选中集重建（标题换成全部私聊）',
        /全部私聊/.test($('sessBulkPanel').textContent),
        $('sessBulkPanel').textContent.slice(0, 40));

  // ── 场景二：搜索状态下点「全部群」应无视搜索 ──────────
  $('sessSearch').value = '技术';
  $('sessSearch').dispatchEvent(new w.Event('input'));
  await sleep(50);
  check('搜索「技术」后列表只剩 1 行', cbs().length === 1, 'cb=' + cbs().length);
  $('selGroups').click();
  check('点「全部群」把搜索框清空了', $('sessSearch').value === '',
        'value=' + JSON.stringify($('sessSearch').value));
  check('且选中的是全部群（不是筛出来的那 1 个）',
        checkedKeys().join(',') === 'group_111,group_222', checkedKeys().join(','));

  // 「批量人设…」按钮的 toggle 语义没被 force 改坏
  $('bulkApply').click();
  check('点「批量人设…」能收起面板（toggle 仍有效）',
        $('sessBulkPanel').textContent === '',
        $('sessBulkPanel').textContent.slice(0, 30));

  // 搜索一个谁都匹配不上的词：批量栏不该跟着消失，否则连「全部群」按钮都没了
  $('sessSearch').value = 'zzzz-no-match';
  $('sessSearch').dispatchEvent(new w.Event('input'));
  await sleep(50);
  check('搜不到任何会话时，批量栏仍在（「全部群」按钮不跟着消失）',
        $('sessBulkBar').style.display !== 'none',
        $('sessBulkBar').style.display);
  dom.window.close();

  // ── 场景三：一条群都没有时 ───────────────────────────
  const onlyPriv = FULL.filter((s) => s.kind !== 'group');
  const dom2 = boot(onlyPriv);
  const w2 = dom2.window, $2 = (id) => w2.document.getElementById(id);
  await sleep(500);
  $2('selGroups').click();
  check('没有群时点「全部群」给出提示、不弹面板',
        /还没有任何群会话/.test($2('sessStatus').textContent)
        && $2('sessBulkPanel').textContent === '',
        $2('sessStatus').textContent);
  check('没有群但仍有私聊：批量栏照常可见（不被搜索/空类连累）',
        $2('sessBulkBar').style.display !== 'none',
        $2('sessBulkBar').style.display);
  dom2.window.close();

  console.log(fail ? '\nFAILED ' + fail : '\nALL PASS');
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.error('脚本异常：', e); process.exit(2); });
