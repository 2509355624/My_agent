# -*- coding: utf-8 -*-
"""生成 5175 群友版漫画工作台。

    web/comic_studio_friend.html = web/comic_studio.html + 本文件的补丁

**不要手改生成出来的那个 HTML**。站主版是成品、是唯一事实源；群友版只做
「隐藏站主专属控件 + 反馈搬到按钮旁边 + 配额/画幅/QQ 发送」这几件事，
之前自己另写一份精简页把台词、标签、气泡、完整提示词、改画面词、红色补正
全丢了，这个错不能再犯。

改群友版 → 改本文件 → 重跑：

    D:/AI/confyui_env/Scripts/python.exe tools/comic_friend_patch.py

每一处替换都断言命中且唯一，母本一改结构就会立刻报错，不会悄悄生成残页。
"""
import io
import os

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(BASE, "web", "comic_studio.html")
DST = os.path.join(BASE, "web", "comic_studio_friend.html")


def rep(s, old, new, tag):
    n = s.count(old)
    if n != 1:
        raise SystemExit("[%s] 母本里命中 %d 次（应为 1 次），母本结构变了，先看一眼再改" % (tag, n))
    return s.replace(old, new, 1)


CSS = r"""
  /* ── 群友版补丁：母本 UI 一律保留，这里只加就地反馈 / 双 Tab / 耗时 / 发送卡 ── */
  .hidden-by-friend { display: none !important; }
  .inline-status { display: none; margin-top: 9px; padding: 9px 11px;
    border-radius: var(--radius-sm); background: var(--bg-input);
    border: 1px solid var(--border); font-size: 12px; line-height: 1.65;
    color: var(--info); word-break: break-word; }
  .inline-status.on { display: block; }
  .inline-status.err { color: var(--error); border-color: var(--error); }
  .inline-status.ok  { color: var(--success); border-color: var(--success); }
  .friend-note { color: var(--text-dim); font-size: 12px; line-height: 1.7; }
  .friend-note b { color: var(--text); font-weight: 500; }
  .mode-tabs { display: flex; gap: 10px; margin-bottom: 12px; }
  .mode-tab { flex: 1; display: flex; flex-direction: column; gap: 3px;
    padding: 10px 14px; border: 1px solid var(--border); border-radius: var(--radius-sm);
    background: var(--bg-input); cursor: pointer; }
  .mode-tab input { display: none; }
  .mode-tab span { font-size: 14px; }
  .mode-tab small { color: var(--text-dim); font-size: 11px; }
  .mode-tab.on, .mode-tab:has(input:checked) { border-color: var(--accent); background: rgba(124,92,255,.12); }
  .mode-tab.on span, .mode-tab:has(input:checked) span { color: var(--accent); }
  .slot .cost { margin-left: 8px; font-size: 11px; color: var(--warn); white-space: nowrap; }
  .chip { display: inline-block; font-size: 11px; padding: 3px 9px; margin-right: 6px;
    border: 1px solid var(--border); border-radius: 20px; color: var(--text-dim); }
  .pack { font-size: 12px; color: var(--text-dim); padding: 5px 0;
    border-bottom: 1px solid var(--border-soft); line-height: 1.6; }
  .pack b { color: var(--text); font-weight: 500; }
  .pack .ok { color: var(--success); }
  .pack .bad { color: var(--error); }
  .ratio-btn.on { border-color: var(--accent); color: var(--accent); }
  #briefRows input, #briefRows textarea { width: 100%; box-sizing: border-box; }
  #briefRows textarea { min-height: 68px; }
"""

TOPBAR = r"""  <div class="topbar">
    <h1>🎬 漫画工作台</h1>
    <span class="spacer"></span>
    <span id="quotaTop"></span>
  </div>

  <div class="card" id="friendNote">
    <div class="friend-note">
      <b>怎么用：</b>顶上选「跑漫画」或「跑写真」，两个是分开的界面。跑漫画就是
      写三行设定 → 生成剧本 → 确认开跑 → 给格子加台词气泡。<br>
      <b>关于次数：</b>写剧本 / 修订走站主的公用中转站，<b>中转站正常就免费随便用</b>；
      中转站偶尔出问题时才扣次数（找站长要次数，用站主的额度跑）。换种子重跑、
      改画面词、加气泡这些只吃显卡，不扣次数。<br>
      <b>发送规则：</b>底图 / 写真跑完<b>先在 QQ 通知你</b>，去网页逐张检查；
      有瑕疵可以换种子重跑。确认后漫画点「<b>一键加气泡</b>」、写真点
      「<b>一键发送 zip</b>」，高清包才发到 QQ，<b>正常排队</b>。<br>
      <b>包的名字：</b>「作品名 无气泡」是底图，「作品名 有气泡」是加完台词的成品；
      网页里只有缩略图。
    </div>
  </div>
"""

MODE_TABS = r"""    <div class="mode-tabs" id="modeTabs">
      <label class="mode-tab on"><input type="radio" name="mode" value="script" checked onchange="switchMode()">
        <span>📖 跑漫画</span><small>写剧本 → 逐格出图 → 加台词气泡</small></label>
      <label class="mode-tab"><input type="radio" name="mode" value="portrait" onchange="switchMode()">
        <span>📷 跑写真</span><small>单人写真集，一次多张，无视角无剧情</small></label>
    </div>
"""

BRIEF_ROWS = r"""      <div id="briefRows" style="flex:1;min-width:220px">
        <input id="bfView" placeholder="① 视角：谁的视角？（例：哥哥 / 我 / 旁观者）" style="margin-bottom:6px">
        <input id="bfHero" placeholder="② 主角是谁？（例：妹妹，长直黑发、校服）" style="margin-bottom:6px">
        <textarea id="bfPlot" rows="2" placeholder="③ 剧情是什么？（例：下雨天兄妹俩在公交站躲雨……）"></textarea>
      </div>
      <textarea id="brief" style="display:none"></textarea>
"""

RENDER_CARD = r"""  <div class="card" id="friendRenderCard">
    <h2>⚙ 画幅 <span style="color:var(--text-dim);font-size:12px;font-weight:400">改动即时生效，只影响之后画的格子</span></h2>
    <div class="row" id="ratios" style="margin-bottom:9px"></div>
    <div class="row">
      <span class="friend-note">或自己填底图尺寸</span>
      <input type="number" id="frW" min="512" max="2048" step="8" style="width:92px">
      <span class="friend-note">×</span>
      <input type="number" id="frH" min="512" max="2048" step="8" style="width:92px">
      <span class="friend-note" id="frMsg">已固定跳过最终 2 倍放大，单张约 4MB。</span>
    </div>
  </div>

"""

SEND_CARD = r"""
  <div class="card" id="sendCard">
    <h2>📤 QQ 发送</h2>
    <div class="friend-note">
      <b>跑完先通知，检查好了才发送。</b><br>
      漫画：检查底图没问题 → 点「一键加气泡」，加完自动把「无气泡」+「有气泡」一起发到 QQ。<br>
      写真：检查没问题 → 点下面「一键发送 zip」。换种子重跑不会自动发。
    </div>
    <div class="row" style="margin-top:10px">
      <button class="btn primary" id="btnSendQQ" onclick="doSendQQ()">📤 一键发送 zip 到 QQ</button>
      <span class="friend-note" id="queueHint"></span>
    </div>
    <div class="inline-status" id="lsSend"></div>
    <div id="packs" style="margin-top:10px"></div>
  </div>
"""

JS = r"""
/* ═══════════════════════════════════════════════════════════════════════
   群友版补丁（母本：web/comic_studio.html，由 tools/comic_friend_patch.py 注入）
   母本的槽位 / 台词 / 标签 / 气泡 / 完整提示词 / 改画面词 / 红色补正 / 写真
   一行都没动。这里只做：①隐藏站主专属控件 ②反馈搬到按钮正下方
   ③配额 / 画幅 / 每格耗时 / QQ 手动发送。
   ═══════════════════════════════════════════════════════════════════════ */
const FRIEND = {cfg: null, active: 'lsRun', clock: {}, packSig: '',
                sendTimer: null, wasBusy: false, starting: false, readyShown: ''};

function friendEl(id) { return document.getElementById(id); }
function friendInline(id, msg, kind) {
  const el = friendEl(id);
  if (!el) return;
  el.textContent = msg || '';
  el.className = 'inline-status' + (msg ? ' on' : '') + (kind ? ' ' + kind : '');
}
window.friendInline = friendInline;
function friendFocus(id) { FRIEND.active = id; }
function friendSay(msg, isErr) { friendInline(FRIEND.active, msg, isErr ? 'err' : ''); }

// 母本底部浮条保留，同时把同一句反馈写到「刚点的那个按钮」正下方。
const _mToast = toast;
toast = function (msg, isErr) { _mToast(msg, isErr); friendSay(msg, isErr); };

// 母本 setBusy 本来就会把 btnScript/btnStart/... 全置灰，这里只补就地文字。
const _mSetBusy = setBusy;
setBusy = function (b, phase) {
  _mSetBusy(b, phase);
  if (b) friendInline('lsRun', '⏳ 正在跑 · 正常排队 · 跑完会在 QQ 通知你检查');
  else if (FRIEND.wasBusy) friendInline('lsRun', '✅ 跑完了 · QQ 已通知，去网页检查，确认后再发送');
  FRIEND.wasBusy = !!b;
};

// 每个按钮点击时，把反馈落点切到它自己下面那条。
const _mGenScript = genScript;
genScript = async function () {
  if (curMode() === 'portrait') return _mGenScript();
  friendFocus('lsScript'); syncBrief();
  friendInline('lsScript', '⏳ 已提交，编剧正在' + (skipSearch() ? '写剧本' : '查库 / 写剧本') +
    '…（要几分钟，按钮已置灰，别重复点）');
  return _mGenScript();
};
const _mSaveScript = saveScript;
saveScript = async function (andRun) {
  if (!andRun) return _mSaveScript(andRun);
  // 母本要等 /api/comic/start 返回才 setBusy，中间那几秒按钮是亮的，
  // 实测会被当成卡住连点好几次。点下去的瞬间就灰，并吞掉重复提交。
  if (FRIEND.starting) return;
  FRIEND.starting = true;
  friendFocus('lsRun');
  friendInline('lsRun', '⏳ 已提交 · 正常排队 · 跑完会在 QQ 通知你检查');
  const btn = friendEl('btnStart');
  if (btn) btn.disabled = true;
  try {
    return await _mSaveScript(andRun);
  } finally {
    FRIEND.starting = false;
    // 成功时 refresh() 会拿到 running 并继续锁住（wasBusy=true）；
    // 失败时 2 秒轮询会解锁，这里再兜一层，别让人对着灰按钮干等。
    setTimeout(function () {
      const b = friendEl('btnStart');
      if (b && !FRIEND.wasBusy && !RUNNING) b.disabled = false;
    }, 2500);
  }
};
const _mBubbleAll = bubbleAll;
bubbleAll = async function () {
  const btn = friendEl('btnBubbleAll');
  if (btn) btn.disabled = true;
  friendFocus('lsBubble');
  friendInline('lsBubble', '⏳ 已提交，正在给所有待加格加气泡 / 写台词…');
  return _mBubbleAll();
};
const _mGenPortrait = genPortrait;
genPortrait = async function () {
  friendFocus('lsPortrait');
  friendInline('lsPortrait', '⏳ 已提交，提示词员正在写这一批写真…');
  return _mGenPortrait();
};
const _mSwitchMode = switchMode;
switchMode = function () { _mSwitchMode(); friendMode(); };

// 设定拆三行：前端拼回母本原有的「视角:/主角:/剧情:」契约，后端一个字没改。
function syncBrief() {
  const v = (friendEl('bfView').value || '').trim();
  const h = (friendEl('bfHero').value || '').trim();
  const p = (friendEl('bfPlot').value || '').trim();
  friendEl('brief').value = (v ? '视角: ' + v + '\n' : '') +
                            (h ? '主角: ' + h + '\n' : '') +
                            (p ? '剧情: ' + p : '');
}
['bfView', 'bfHero', 'bfPlot'].forEach(function (id) {
  const el = friendEl(id);
  if (el) el.addEventListener('input', syncBrief);
});

// 格数 / 张数由站主按账号给，页面只显示、不给输入（手机端粘贴 30 也没用）。
function friendPanelMax() {
  const q = FRIEND.cfg && FRIEND.cfg.quota;
  const lim = (q && q.limits) || {};
  const portrait = curMode() === 'portrait';
  const n = portrait ? (lim.max_shots || 5) : (lim.max_panels || 10);
  const p = friendEl('panels');
  if (p) p.value = n;
  const hint = friendEl('panelsHint');
  if (hint) hint.textContent = portrait
    ? '一次最多 ' + n + ' 张（站主给的上限，不能自己改）'
    : '每本最多 ' + n + ' 格（站主给的上限，不能自己改）';
}
function friendMode() {
  const p = curMode() === 'portrait';
  const rows = friendEl('briefRows'), b = friendEl('brief');
  if (rows) rows.style.display = p ? 'none' : '';
  if (b) {
    b.style.display = p ? '' : 'none';
    b.rows = 3;                       // 母本默认 220px 高，写真只要一句话
    if (p) b.placeholder = '想拍什么：花田里回眸、天台看夕阳、教室靠窗午睡……（一次多张，每张换场景 / 服装 / 构图）';
    else syncBrief();
  }
  friendPanelMax();
  document.querySelectorAll('.mode-tab').forEach(function (l) {
    const i = l.querySelector('input');
    if (i) l.classList.toggle('on', i.checked);
  });
}

function friendQuota(q) {
  const box = friendEl('quotaTop');
  if (!box || !q) return;
  const names = {script: '剧本', revise: '修订', comic: '漫画', portrait: '写真'};
  const l = q.left || {};
  box.innerHTML = Object.keys(names).map(function (k) {
    return '<span class="chip">' + names[k] + ' 剩 ' + (l[k] || 0) + '</span>';
  }).join('');
}

function friendRatios(list) {
  const box = friendEl('ratios');
  if (!box) return;
  box.innerHTML = list.map(function (p) {
    return '<button class="btn small ratio-btn" data-key="' + esc(p.key) + '" data-w="' +
      p.w + '" data-h="' + p.h + '">' + esc(p.label) + ' ' + p.w + '×' + p.h + '</button>';
  }).join('');
  box.querySelectorAll('button').forEach(function (b) {
    b.onclick = function () {
      friendEl('frW').value = b.dataset.w;
      friendEl('frH').value = b.dataset.h;
      box.querySelectorAll('button').forEach(function (x) { x.classList.toggle('on', x === b); });
      saveFriendSize();
    };
  });
}
async function loadFriendRender() {
  const list = (FRIEND.cfg && FRIEND.cfg.ratio_presets) || [];
  if (!TASK) {
    if (list.length) { friendEl('frW').value = list[0].w; friendEl('frH').value = list[0].h; }
    return;
  }
  try {
    const d = await api('/api/comic/render_opts?task=' + encodeURIComponent(TASK));
    const o = d.opts || {}, w = +o.width || 0, h = +o.height || 0;
    if (w && h) { friendEl('frW').value = w; friendEl('frH').value = h; }
    const hit = list.find(function (p) { return p.w === w && p.h === h; });
    friendEl('ratios').querySelectorAll('button').forEach(function (b) {
      b.classList.toggle('on', !!hit && b.dataset.key === hit.key);
    });
  } catch (e) { /* 老任务没存过参数就用默认值，不打断页面 */ }
}
let _sizeTimer = null;
function saveFriendSize() {
  clearTimeout(_sizeTimer);
  _sizeTimer = setTimeout(async function () {
    const msg = friendEl('frMsg');
    if (!TASK) { if (msg) msg.textContent = '先生成或选一本作品，画幅才会保存'; return; }
    const w = +friendEl('frW').value || 0, h = +friendEl('frH').value || 0;
    if (!w || !h) return;
    try {
      await api('/api/comic/render_opts', {task: TASK, opts: {width: w, height: h}});
      if (msg) msg.textContent = '画幅已生效：' + w + '×' + h + '（已固定跳过最终 2 倍放大）';
    } catch (e) { if (msg) msg.textContent = '画幅没存上：' + e.message; }
  }, 450);
}
friendEl('frW').addEventListener('input', saveFriendSize);
friendEl('frH').addEventListener('input', saveFriendSize);

// 右上角耗时：后端不用改，客户端按状态变化计时（排队 / 待加气泡 / 完成）。
const _mRenderSlot = renderSlot;
renderSlot = function (p) {
  return _mRenderSlot(p).replace('<span class="seed">',
    '<span class="cost" data-no="' + esc(String(p.no)) + '"></span><span class="seed">');
};

function friendPacks(v) {
  const box = friendEl('packs');
  if (!box) return;
  const rows = (v.packages || []).map(function (p) {
    const st = p.state === 'sent' ? '<span class="ok">已发送</span>'
      : p.state === 'failed' ? '<span class="bad">发送失败</span>' : '<span>排队中</span>';
    return '<div class="pack"><b>' + esc(p.name || p.label) + '</b> · ' + st +
      ' · ' + (p.files || 0) + ' 张' +
      (p.error ? '<br><span class="bad">' + esc(p.error) + '</span>' : '') + '</div>';
  }).join('');
  const top = (v.state === 'packing')
    ? '<div class="pack">⏳ 排队打包中</div>'
    : '';
  const sig = rows + '|' + (v.state || '') + '|' + (v.error || '');
  if (sig === FRIEND.packSig) return;
  FRIEND.packSig = sig;
  box.innerHTML = top + (rows ||
    '<div class="pack">跑完会通知 · 检查后点「一键加气泡」或「一键发送 zip」才发。</div>');
}

// 母本 2 秒一轮的 status 已经带 delivery / quota / queue，挂个钩子就够，
// 不再另起轮询打重复请求。
window.friendOnStatus = function (d) {
  const q = friendEl('queueHint');
  if (q) q.textContent = '正常排队 · 前面还有 ' + (d.queue || 0) + ' 张';
  const now = Date.now(), grid = friendEl('grid');
  (d.panels || []).forEach(function (p) {
    const k = String(p.no);
    const c = FRIEND.clock[k] || (FRIEND.clock[k] = {});
    if (!c.start && (p.state === 'pending' || p.scene_url)) c.start = now;
    if ((p.state === 'done' || p.state === 'rendered') && c.start && !c.end) c.end = now;
    const el = grid && grid.querySelector('.cost[data-no="' + k + '"]');
    if (!el) return;
    if (p.error) { el.textContent = '✗ 失败'; el.style.color = 'var(--error)'; }
    else if (p.state === 'pending') {
      el.textContent = c.start ? '⏳ ' + Math.max(0, Math.round((now - c.start) / 1000)) + 's' : '⏳ 排队';
      el.style.color = 'var(--warn)';
    } else if (p.state === 'rendered') {
      el.textContent = '⏳ 待加气泡'; el.style.color = 'var(--info)';
    } else if (p.state === 'done') {
      el.textContent = (c.end && c.start) ? '✓ ' + Math.round((c.end - c.start) / 1000) + 's' : '✓ 完成';
      el.style.color = 'var(--success)';
    }
  });
  if (d.delivery) friendPacks(d.delivery);
  // 底图 / 写真跑完：后端已发过 QQ 文字，网页这边顺手弹一次，最多一条。
  // 用户不会一直挂着网页，所以这只是「正好在看」时的提醒，主通知在 QQ。
  if (d.delivery && d.delivery.state === 'waiting_check') {
    const key = String(d.task || TASK || '');
    if (key && FRIEND.readyShown !== key) {
      FRIEND.readyShown = key;
      const isP = d.delivery.ready_kind === 'portrait';
      const n = d.delivery.ready_count || 0;
      window.alert((n ? n + ' 张' : '') + (isP ? '写真' : '底图') + '已完成\n'
        + '请到网页逐张检查，有瑕疵可以点「换种子重跑」\n'
        + '确认没问题后点「' + (isP ? '一键发送 zip' : '一键加气泡') + '」发到 QQ');
    }
  }
  if (d.quota) friendQuota(d.quota);
};

// 手动发送：按钮就地给阶段反馈，失败 / 60 秒冷却都写在按钮下面，不再只弹顶端。
async function doSendQQ() {
  if (!TASK) { friendInline('lsSend', '先生成或选一本作品', 'err'); return; }
  const btn = friendEl('btnSendQQ');
  if (btn) btn.disabled = true;
  friendInline('lsSend', '⏳ 已提交，打包中');
  try {
    const d = await api('/api/comic/send_qq', {task: TASK});
    friendInline('lsSend', d.hint || '已提交，打包中，正常排队。');
    if (d.delivery) friendPacks(d.delivery);
    friendPollSend();
  } catch (e) {
    friendInline('lsSend', '✗ ' + e.message, 'err');
  } finally {
    setTimeout(function () { if (btn) btn.disabled = false; }, 4000);
  }
}
function friendPollSend() {
  if (FRIEND.sendTimer) clearInterval(FRIEND.sendTimer);
  const t0 = Date.now();
  FRIEND.sendTimer = setInterval(async function () {
    try {
      const d = await api('/api/comic/delivery_status?task=' + encodeURIComponent(TASK));
      friendPacks(d);
      const st = d.state || '';
      const names = (d.packages || []).filter(function (p) { return p.state === 'sent'; })
        .map(function (p) { return p.name || p.label; }).join('、');
      if (st === 'sent') {
        clearInterval(FRIEND.sendTimer); FRIEND.sendTimer = null;
        friendInline('lsSend', '✅ ' + (names || '高清包') + ' 已发送', 'ok');
      } else if (st === 'failed') {
        clearInterval(FRIEND.sendTimer); FRIEND.sendTimer = null;
        friendInline('lsSend', '✗ 发送失败：' + (d.error || '找站主补发'), 'err');
      } else if (d.error) {
        friendInline('lsSend', '✗ ' + d.error, 'err');
      }
      // packing 时不覆盖点击瞬间那句「本次会发哪几个包」，只靠 packs 里那行转圈提示。
    } catch (e) { /* 状态接口偶发失败下一轮再来 */ }
    if (Date.now() - t0 > 180000 && FRIEND.sendTimer) {
      clearInterval(FRIEND.sendTimer); FRIEND.sendTimer = null;
      friendInline('lsSend', '⏳ 正常排队中，发完会在 QQ 通知你。');
    }
  }, 2500);
}

// 群友不该看到站主的编剧模型下拉 / 自定义中转站：这两个接口只在 5174 有，
// 网关也不放行。空 provider/model 由后端收敛到白名单，站主默认即可。
loadProviders = async function () {
  try { FRIEND.cfg = await api('/api/comic/friend_config'); }
  catch (e) { FRIEND.cfg = {ok: true, quota: null, ratio_presets: []}; }
  friendQuota(FRIEND.cfg.quota);
  friendRatios(FRIEND.cfg.ratio_presets || []);
  friendMode();
  loadFriendRender();
};
loadModels = async function () {};

"""


def simplify_friend(s):
    """群友页收口：少字、少按钮、手机友好。

    母本仍保留全部功能与 id；这里只把首屏压干净：大段说明点开才看，
    设定区只留三行 + 生成剧本，接上一集 / 视角 / 查库收进「更多设置」。
    """
    s = s.replace(
        'font-family: "SF Mono", "JetBrains Mono", "Consolas", "Microsoft YaHei", monospace;',
        'font-family: -apple-system, "Segoe UI", "Microsoft YaHei", system-ui, sans-serif;',
        1)
    s = s.replace('<h1>🎬 漫画工作台</h1>', '<h1>漫画</h1>', 1)

    old_note = """  <div class="card" id="friendNote">
    <div class="friend-note">
      <b>怎么用：</b>顶上选「跑漫画」或「跑写真」，两个是分开的界面。跑漫画就是
      写三行设定 → 生成剧本 → 确认开跑 → 给格子加台词气泡。<br>
      <b>关于次数：</b>写剧本 / 修订走站主的公用中转站，<b>中转站正常就免费随便用</b>；
      中转站偶尔出问题时才扣次数（找站长要次数，用站主的额度跑）。换种子重跑、
      改画面词、加气泡这些只吃显卡，不扣次数。<br>
      <b>发送规则：</b>底图 / 写真跑完<b>先在 QQ 通知你</b>，去网页逐张检查；
      有瑕疵可以换种子重跑。确认后漫画点「<b>一键加气泡</b>」、写真点
      「<b>一键发送 zip</b>」，高清包才发到 QQ，<b>正常排队</b>。<br>
      <b>包的名字：</b>「作品名 无气泡」是底图，「作品名 有气泡」是加完台词的成品；
      网页里只有缩略图。
    </div>
  </div>"""
    new_note = """  <details class="card note" id="friendNote">
    <summary>怎么用</summary>
    <div class="friend-note">
      <b>怎么用：</b>顶上选「跑漫画」或「跑写真」，两个是分开的界面。跑漫画就是
      写三行设定 → 生成剧本 → 确认开跑 → 给格子加台词气泡。<br>
      <b>关于次数：</b>写剧本 / 修订走站主的公用中转站，<b>中转站正常就免费随便用</b>；
      中转站偶尔出问题时才扣次数（找站长要次数，用站主的额度跑）。换种子重跑、
      改画面词、加气泡这些只吃显卡，不扣次数。<br>
      <b>发送规则：</b>底图 / 写真跑完<b>先在 QQ 通知你</b>，去网页逐张检查；
      有瑕疵可以换种子重跑。确认后漫画点「<b>一键加气泡</b>」、写真点
      「<b>一键发送 zip</b>」，高清包才发到 QQ，<b>正常排队</b>。<br>
      <b>包的名字：</b>「作品名 无气泡」是底图，「作品名 有气泡」是加完台词的成品；
      网页里只有缩略图。
    </div>
  </details>"""
    if old_note not in s:
        raise SystemExit("simplify: friendNote block not found")
    s = s.replace(old_note, new_note, 1)

    s = s.replace('<span>📖 跑漫画</span><small>写剧本 → 逐格出图 → 加台词气泡</small>',
                  '<span>📖 跑漫画</span><small>写设定 → 出图 → 加台词</small>', 1)
    s = s.replace('<span>📷 跑写真</span><small>单人写真集，一次多张，无视角无剧情</small>',
                  '<span>📷 跑写真</span><small>一次多张</small>', 1)

    start = s.find('    <div class="row" style="margin-bottom:10px">\n      <div id="briefRows"')
    end = s.find('    <textarea id="script" style="display:none" spellcheck="false"></textarea>')
    if start < 0 or end < 0 or end < start:
        raise SystemExit("simplify: 设定区没找到")
    block = """    <div class="row" style="margin-bottom:10px">
      <div id="briefRows" style="flex:1;min-width:220px">
        <input id="bfView" placeholder="① 谁的视角？（例：哥哥 / 我）" style="margin-bottom:6px">
        <input id="bfHero" placeholder="② 主角是谁？（例：妹妹，长直黑发、校服）" style="margin-bottom:6px">
        <textarea id="bfPlot" rows="2" placeholder="③ 剧情是什么？（例：下雨天兄妹在公交站躲雨……）"></textarea>
      </div>
      <textarea id="brief" style="display:none"></textarea>
      <input id="heroine" style="display:none;width:220px" placeholder="女主设定：长直黑发、校服、气质文静……（填一次，这批写真每张都自动带上）">
      <input id="character" style="width:130px" placeholder="角色名（可选）">
      <input id="panels" type="hidden" value="10">
      <span id="panelsHint" class="friend-note"></span>
    </div>

    <div class="row" style="margin-bottom:10px">
      <button class="btn primary" id="btnScript" onclick="genScript()">生成剧本</button>
    </div>

    <details class="adv">
      <summary>更多设置</summary>
      <div class="row" style="margin-bottom:10px">
        <span class="hidden-by-friend" style="color:var(--text-dim);font-size:12px">编剧模型</span>
        <select id="prov" class="hidden-by-friend" style="width:140px" onchange="loadModels();syncProvBtn()" title="用哪家模型写剧本"></select>
        <select id="model" class="hidden-by-friend" style="width:200px" onchange="rememberLlm()" title="写剧本用的具体模型（留空 = 该家默认）"></select>
        <button class="btn small hidden-by-friend" id="btnAddProv" onclick="onAddProvClick()"
                title="添加自定义中转站">＋ 中转站</button>
        <span id="prevLabel" style="color:var(--text-dim);font-size:12px;margin-left:6px">接上一集</span>
        <select id="prevTask" style="width:230px;max-width:40vw"
                title="选了就把那一集的气泡和旁白当前情，本集接着往下写">
          <option value="">不接——全新一集</option>
        </select>
        <label id="carryOutfitWrap" style="font-size:12px;white-space:nowrap;color:var(--text-dim)"
               title="同一场戏跨集时，把上一集最后一格的服饰带过来">
          <input type="checkbox" id="carryOutfit"> 沿用服装
        </label>
        <span class="spacer" style="flex:1"></span>
        <label id="povModeWrap" style="font-size:12px;white-space:nowrap;color:var(--text-dim)"
               title="POV：画面里只有女主。自由视角：机位由编剧挑。第三人称：双人同框。">
          <select id="povMode" style="width:auto" onchange="rememberPovMode()">
            <option value="pov">POV 第一人称</option>
            <option value="free">自由视角（自选机位）</option>
            <option value="third">第三人称（双人同框）</option>
          </select>
        </label>
        <label id="skipSearchWrap" style="font-size:12px;white-space:nowrap;color:var(--text-dim)"
               title="勾上就不查标签库，一次写完整剧本">
          <input type="checkbox" id="skipSearch" checked onchange="rememberSkipSearch()"> 跳过查库
        </label>
        <label id="silverModeWrap" style="display:none;font-size:12px;white-space:nowrap;color:var(--text-dim)"
               title="自由视角使用 silver 连续漫画路线">
          <select id="silverMode" style="width:auto" onchange="rememberSilverMode()">
            <option value="no_bubble">连续漫画，无气泡</option>
            <option value="bubble">连续漫画，有气泡</option>
          </select>
        </label>
      </div>
    </details>
"""
    block += '    <div class="inline-status" id="lsScript"></div>\n'
    s = s[:start] + block + s[end:]

    s = s.replace(">🩹 重新补正<", ">重新补正<", 1)
    s = s.replace(">🔁 换渠道重跑<", ">换渠道重跑<", 1)
    s = s.replace(">确认开跑（渲染全部场景图）<", ">开始画<", 1)
    s = s.replace("💬 和修订助手聊剧本", "和修订助手聊剧本", 1)
    s = s.replace(">↩️ 撤销对话改动<", ">撤销改动<", 1)

    css = """
  .note summary, .adv summary { cursor: pointer; color: var(--text-dim);
    font-size: 13px; list-style: none; }
  .note summary::-webkit-details-marker, .adv summary::-webkit-details-marker { display: none; }
  .note summary::after, .adv summary::after { content: " ＋"; color: var(--text-mute); }
  .note[open] summary::after, .adv[open] summary::after { content: " −"; }
  .adv { margin: 0 0 10px; }
  .adv summary { padding: 6px 0; }
  @media (max-width: 640px) {
    .wrap { padding: 12px; }
    .topbar { padding: 4px 0 12px; }
    .note summary, .adv summary { font-size: 14px; }
  }
"""
    s = s.replace("</style>", css + "\n</style>", 1)
    return s


def main():
    raw = open(SRC, "rb").read()
    crlf = b"\r\n" in raw
    s = raw.decode("utf-8").replace("\r\n", "\n")

    # ① 三格一行（母本 auto-fill minmax(300px) 在宽屏会挤成 4 格）
    s = rep(s,
            "  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 14px; }",
            "  .grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }\n"
            "  @media (max-width: 980px) { .grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }\n"
            "  @media (max-width: 640px) { .grid { grid-template-columns: 1fr; } }\n"
            "  /* 独占通道：站主专属，群友页隐藏（后端对带 qq 的请求本来就不认） */\n"
            "  label:has(> #exclusiveOn) { display: none !important; }",
            "三格一行")
    s = rep(s, "</style>", CSS + "</style>", "补丁 CSS")

    # ② 顶栏：去掉只在 5174 存在的 /comic_admin、/gen、/ 入口
    s = rep(s,
            "  <div class=\"topbar\">\n"
            "    <h1>🎬 漫画工作台</h1>\n"
            "    <span class=\"spacer\"></span>\n"
            "    <a class=\"back\" href=\"/comic_admin\">群友管理 →</a>\n"
            "    <a class=\"back\" href=\"/gen\">🎨 跑图台</a>\n"
            "    <a class=\"back\" href=\"/\">← 回对话</a>\n"
            "  </div>",
            TOPBAR.rstrip("\n"), "顶栏与说明卡")

    # ③ 漫画 / 写真做成顶上两个显式入口（母本 applyMode 已经是换视图）
    s = rep(s,
            "    <div class=\"row\" style=\"margin-bottom:10px\">\n"
            "      <span style=\"color:var(--text-dim);font-size:12px\">模式</span>\n"
            "      <label style=\"font-size:13px;white-space:nowrap\"><input type=\"radio\" name=\"mode\" value=\"script\" checked onchange=\"switchMode()\"> 剧本</label>\n"
            "      <span style=\"color:var(--text-dim);font-size:12px\">POV 视角的多格故事，观众不入画</span>\n"
            "      <label style=\"font-size:13px;white-space:nowrap;margin-left:14px\"><input type=\"radio\" name=\"mode\" value=\"portrait\" onchange=\"switchMode()\"> 生图</label>\n"
            "      <span style=\"color:var(--text-dim);font-size:12px\">单人写真集：只有女主，无视角无剧情，一次多张</span>\n"
            "    </div>",
            MODE_TABS.rstrip("\n"), "双 Tab")

    # ④ 设定拆三行；隐藏的 #brief 仍承载母本契约
    s = rep(s,
            "      <textarea id=\"brief\" rows=\"3\" style=\"flex:1;min-width:220px\" placeholder=\"视角: 哥哥\n"
            "主角: 妹妹\n"
            "剧情: 下雨天兄妹俩在公交站躲雨……（前两行是硬约束：视角=镜头后的「我」，男性→pov、女性→female_pov；主角=画面里唯一入画的人）\"></textarea>",
            BRIEF_ROWS.rstrip("\n"), "设定三行")

    # ⑤ 编剧模型 / 自定义中转站：隐藏（不删，母本 setBusy 等按 id 取元素）
    s = rep(s,
            "      <span style=\"color:var(--text-dim);font-size:12px\">编剧模型</span>",
            "      <span class=\"hidden-by-friend\" style=\"color:var(--text-dim);font-size:12px\">编剧模型</span>",
            "编剧模型 label")
    s = rep(s, "<select id=\"prov\" style=\"width:140px\"",
            "<select id=\"prov\" class=\"hidden-by-friend\" style=\"width:140px\"", "prov")
    s = rep(s, "<select id=\"model\" style=\"width:200px\"",
            "<select id=\"model\" class=\"hidden-by-friend\" style=\"width:200px\"", "model")
    s = rep(s, "<button class=\"btn small\" id=\"btnAddProv\"",
            "<button class=\"btn small hidden-by-friend\" id=\"btnAddProv\"", "自定义中转站")

    # ⑤b 生图渠道行 + 「换渠道重跑」：群友版只认 anima-line 一个渠道
    #（2026-11-11 用户拍板「最快的当默认，其他先隐藏」）。整个行藏掉——
    # sizeHint 是站主渲染卡的预估尺寸提示，群友版有自己那张画幅卡，用不上。
    # 后端 /api/comic/channels 对带 qq 的请求也只返回 anima-line，双保险。
    s = rep(s,
            "    <div class=\"row\" style=\"margin-bottom:10px\">\n"
            "      <span style=\"color:var(--text-dim);font-size:12px\">生图渠道</span>",
            "    <div class=\"row hidden-by-friend\" style=\"margin-bottom:10px\">\n"
            "      <span style=\"color:var(--text-dim);font-size:12px\">生图渠道</span>",
            "生图渠道行")
    s = rep(s, "<button class=\"btn\" id=\"btnRerun\" onclick=\"rerunAll()\"",
            "<button class=\"btn hidden-by-friend\" id=\"btnRerun\" onclick=\"rerunAll()\"",
            "换渠道重跑隐藏")

    # ⑥ 格数：只读文字 + 隐藏值，手机端没法粘贴 30
    s = rep(s,
            "      <input id=\"panels\" type=\"number\" min=\"1\" max=\"60\" value=\"10\" style=\"width:70px\" title=\"剧本格数 / 写真相张数\">",
            "      <input id=\"panels\" type=\"hidden\" value=\"10\">\n"
            "      <span id=\"panelsHint\" class=\"friend-note\"></span>", "格数只读")

    # ⑦ 「只保存」群友用不上（确认开跑本来就会按改过的剧本跑）
    s = rep(s, "<button class=\"btn\" id=\"btnSave\" onclick=\"saveScript(false)\">只保存</button>",
            "<button class=\"btn hidden-by-friend\" id=\"btnSave\" onclick=\"saveScript(false)\">只保存</button>",
            "只保存隐藏")

    # ⑧ 各按钮正下方的就地状态条（2026-11-23 母本加阶段向导后锚点跟着改）
    s = rep(s,
            "      <button class=\"btn primary\" id=\"btnScript\" onclick=\"genScript()\">生成剧本</button>\n"
            "      <span id=\"stage1Hint\" style=\"color:var(--text-dim);font-size:12px\">写好上面的需求，点这里让编剧 AI 出分镜剧本</span>\n"
            "    </div>",
            "      <button class=\"btn primary\" id=\"btnScript\" onclick=\"genScript()\">生成剧本</button>\n"
            "      <span id=\"stage1Hint\" style=\"color:var(--text-dim);font-size:12px\">写好上面的需求，点这里让编剧 AI 出分镜剧本</span>\n"
            "    </div>\n"
            "    <div class=\"inline-status\" id=\"lsScript\"></div>",
            "生成剧本状态条")
    s = rep(s,
            "      <button class=\"btn primary\" id=\"btnStart\" onclick=\"saveScript(true)\">确认开跑（渲染全部场景图）</button>\n"
            "    </div>\n"
            "  </div>",
            "      <button class=\"btn primary\" id=\"btnStart\" onclick=\"saveScript(true)\">确认开跑（渲染全部场景图）</button>\n"
            "    </div>\n"
            "    <div class=\"inline-status\" id=\"lsRun\"></div>\n"
            "  </div>", "确认开跑状态条")
    s = rep(s, "    <div id=\"portraitJob\"></div>",
            "    <div id=\"portraitJob\"></div>\n    <div class=\"inline-status\" id=\"lsPortrait\"></div>",
            "写真状态条")
    s = rep(s,
            "      <button class=\"btn primary\" id=\"btnBubbleAll\" onclick=\"bubbleAll()\">🫧 一键加气泡（全部待加格）</button>\n"
            "      <button class=\"btn\" id=\"btnWatermark\" onclick=\"openWatermark()\"\n"
            "              title=\"在成品图上放水印，产出到独立 watermark/ 目录，不动原图\">💧 一键加水印</button>\n"
            "      <a class=\"btn\" id=\"btnZip\" style=\"text-decoration:none\" href=\"#\" onclick=\"return zipGuard()\">📦 下载成品 zip</a>\n"
            "      <span id=\"progress\" style=\"color:var(--text-dim);font-size:12px\"></span>\n"
            "    </div>",
            "      <button class=\"btn primary\" id=\"btnBubbleAll\" onclick=\"bubbleAll()\">🫧 一键加气泡（全部待加格）</button>\n"
            "      <button class=\"btn hidden-by-friend\" id=\"btnWatermark\" onclick=\"openWatermark()\"\n"
            "              title=\"在成品图上放水印，产出到独立 watermark/ 目录，不动原图\">💧 一键加水印</button>\n"
            "      <button class=\"btn\" id=\"btnSendInline\" onclick=\"doSendQQ()\">📤 重发到 QQ</button>\n"
            "      <a class=\"btn hidden-by-friend\" id=\"btnZip\" style=\"text-decoration:none\" href=\"#\" onclick=\"return zipGuard()\">📦 下载成品 zip</a>\n"
            "      <span id=\"progress\" style=\"color:var(--text-dim);font-size:12px\"></span>\n"
            "    </div>\n"
            "    <div class=\"inline-status\" id=\"lsBubble\"></div>", "加气泡状态条（水印按钮群友隐藏）")

    # ⑨ 渲染参数：母本整卡隐藏（DOM 保留，loadRenderOpts 照常写不报错），
    #    另给一张只有画幅预设 + 自填宽高的卡。
    s = rep(s, "  <div class=\"card\" id=\"renderCard\">",
            "  <div class=\"card hidden-by-friend\" id=\"renderCard\">", "母本渲染卡隐藏")
    s = rep(s, "  <div class=\"card\" id=\"slotCard\">",
            RENDER_CARD + "  <div class=\"card\" id=\"slotCard\">", "简化画幅卡")

    # ⑩ QQ 发送卡：放在槽位卡下面，不放在页面顶端
    s = rep(s,
            "    <div id=\"grid\" class=\"grid\"><div class=\"empty\">还没有任务——先在上面写剧本、确认开跑</div></div>\n"
            "  </div>\n"
            "  </div><!-- /stage-pane 3 -->\n"
            "</div>\n<div id=\"zoom\"",
            "    <div id=\"grid\" class=\"grid\"><div class=\"empty\">还没有任务——先在上面写剧本、确认开跑</div></div>\n"
            "  </div>\n" + SEND_CARD + "\n  </div><!-- /stage-pane 3 -->\n"
            "</div>\n<div id=\"zoom\"", "QQ 发送卡")

    s = rep(s, "<dialog id=\"provDlg\"", "<dialog id=\"provDlg\" class=\"hidden-by-friend\"",
            "自定义中转站弹窗")
    # 水印编辑器：站主防搬运功能，群友版隐藏按钮（上面）与弹窗，JS 保留不报错。
    s = rep(s, "<dialog id=\"wmDlg\"", "<dialog id=\"wmDlg\" class=\"hidden-by-friend\"",
            "水印编辑器弹窗")

    # ⑪ 母本里两处钩子（只加一行，不改逻辑）
    s = rep(s,
            "    document.getElementById('btnScript').textContent = head + last + '）';",
            "    document.getElementById('btnScript').textContent = head + last + '）';\n"
            "    if (window.friendInline) friendInline('lsScript', head + last + '）');",
            "编剧进度就地")
    s = rep(s,
            "  catch (e) { console.warn(e); return; }\n  renderFix(d.fix);",
            "  catch (e) { console.warn(e); return; }\n"
            "  if (window.friendOnStatus) friendOnStatus(d);\n  renderFix(d.fix);",
            "status 钩子")

    s = rep(s,
            "    <details class=\"adv\" id=\"advancedSetup\">\n"
            "      <summary>详细设置 · 渠道 / 编剧模型 / 视角 / 接上一集（一般不用改，已记住上次的选择）</summary>\n"
            "    <div class=\"row hidden-by-friend\" style=\"margin-bottom:10px\">",
            "    <div class=\"row hidden-by-friend\" style=\"margin-bottom:10px\">",
            "群友去掉母本 advancedSetup 开标签（下面 NEW_FORM 自带 details）")
    #    三个 pane 全展开，退回平铺，一屏能看到全部只读内容。
    s = rep(s,
            "  <div class=\"stepbar\" id=\"stepbar\">",
            "  <div class=\"stepbar hidden-by-friend\" id=\"stepbar\" style=\"display:none\">",
            "群友隐藏步骤条")
    s = rep(s,
            "poll();\nloadProviders();\nrestoreSession();\n</script>",
            "document.querySelectorAll('.stage-pane').forEach(function(e){e.style.display='block';});\n"
            "poll();\nloadProviders();\nrestoreSession();\n</script>",
            "群友展开全部 pane")

    # ⑬ 补丁 JS 插在母本初始化三行之前：函数已声明完、初始化还没跑。
    s = rep(s,
            "poll();\nloadProviders();\nrestoreSession();\n</script>",
            JS + "poll();\nloadProviders();\nrestoreSession();\n</script>", "补丁 JS")

    s = simplify_friend(s)

    s = s.replace("<title>漫画工作台</title>",
                  "<title>漫画工作台 · 群友版</title>\n<!-- 由 tools/comic_friend_patch.py 从 comic_studio.html 生成，请勿直接编辑 -->",
                  1)

    out = s  # 2026-11-17：仓库统一 LF，生成页也别再写 CRLF
    io.open(DST, "w", encoding="utf-8", newline="").write(out)
    print("生成 %s（%d 行，母本 %d 行）" %
          (os.path.relpath(DST, BASE), out.count("\n") + 1, raw.count(b"\n") + 1))


if __name__ == "__main__":
    main()
