"""手机端管理页布局体检（只读）。

用 admin.html 里真实的 <style> 搭一个最小页面，按手机宽度渲染后量三件事：
  1. 文档是否比视口宽（整页往右延伸）
  2. 人设卡宽度是否等于视口（而不是只占半屏）
  3. 用量表是否在自己的容器里横滑

    python tools/_probe_admin_mobile.py
"""
import os
import re
import subprocess
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

USAGE_ROWS = [
    ("private_546587874", 93, 385024, 67941, 11496),
    ("vision", 128, 307328, 81591, 16949),
    ("private_1379675234", 76, 198400, 38417, 5942),
]

TABLE = (
    '<table style="width:100%;border-collapse:collapse;font-size:13px;">'
    '<tr style="text-align:left;opacity:.65;">'
    '<th style="padding:4px 8px;">会话 / 类别</th><th>调用</th>'
    '<th>命中 tokens</th><th>未命中 tokens</th><th>输出 tokens</th><th>命中率</th>'
    "</tr>"
    + "".join(
        '<tr style="border-top:1px solid rgba(128,128,128,.25);">'
        '<td style="padding:4px 8px;">%s</td>'
        '<td style="text-align:right;padding:4px 8px;">%d</td>'
        '<td style="text-align:right;padding:4px 8px;">%d</td>'
        '<td style="text-align:right;padding:4px 8px;">%d</td>'
        '<td style="text-align:right;padding:4px 8px;">%d</td>'
        '<td style="text-align:right;padding:4px 8px;">87.4%%</td></tr>' % r
        for r in USAGE_ROWS
    )
    + "</table>"
)


def build(css):
    return """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=5.0">
<style>%s</style></head><body><div class="wrap">
<header><h1>Agent 管理</h1><span class="spacer"></span>
  <a class="back" href="#">漫画台管理 →</a><a class="back" href="#">小说记录 →</a>
  <button class="ghost">人工二审</button><a class="back" href="#">独立界面 ↗</a></header>
<div class="layout">
  <div class="list"><div class="list-item active"><div class="nm">main</div>
    <div class="tag">main · 继承</div></div></div>
  <div class="panel">
    <div class="meta" data-view="basic" class="is-on">工具 5 个 · 技能 38 个</div>
    <div class="card card--wide is-on" data-view="basic" id="personaCard">
      <h2>人设 <span class="file">prompt.md</span></h2>
      <div class="hint">写进 system prompt 的 Role 段。</div>
      <textarea id="prompt"></textarea>
    </div>
    <div class="card is-on" data-view="basic">
      <h2>模型</h2><div class="hint">留空即继承 .env 的全局默认。</div>
      <div class="row"><div class="field"><label>provider</label>
        <select><option>inherit</option></select></div>
        <div class="field"><label>model</label><input id="model"></div></div>
    </div>
    <div class="card card--wide is-on" data-view="basic">
      <h2>用量</h2><div class="sess" id="usageTable">%s</div>
    </div>
    <div class="actions is-on" data-view="basic">
      <button>保存</button><button class="ghost">放弃修改</button></div>
  </div>
</div></div>
<script>
window.addEventListener('load', function () {
  var d = document, de = d.documentElement;
  var card = d.getElementById('personaCard').getBoundingClientRect();
  var box = d.getElementById('usageTable');
  var out = [
    'viewport=' + de.clientWidth,
    'docScrollWidth=' + de.scrollWidth,
    'bodyScrollWidth=' + d.body.scrollWidth,
    'personaCard=' + Math.round(card.width),
    'usageBox=' + box.clientWidth + '/' + box.scrollWidth,
    'tableMin=' + Math.round(d.querySelector('#usageTable table').getBoundingClientRect().width)
  ].join(' | ');
  d.title = out;
  var p = d.createElement('pre');
  p.id = 'report';
  p.textContent = out;
  d.body.appendChild(p);
});
</script>
</body></html>""" % (css, TABLE)


def main():
    html = open(os.path.join(ROOT, "web", "admin.html"), encoding="utf-8").read()
    css = re.search(r"<style>(.*?)</style>", html, re.S).group(1)
    tmp = tempfile.mkdtemp(prefix="admin_mobile_")
    page = os.path.join(tmp, "probe.html")
    with open(page, "w", encoding="utf-8") as fh:
        fh.write(build(css))
    out_dir = os.path.join(ROOT, "state", "_selfcheck")
    os.makedirs(out_dir, exist_ok=True)
    shot = os.path.join(out_dir, "admin_mobile_probe.png")
    cmd = [EDGE, "--headless=new", "--disable-gpu", "--hide-scrollbars",
           "--window-size=500,1700", "--force-device-scale-factor=2",
           "--virtual-time-budget=1500",
           "--screenshot=" + shot, "file:///" + page.replace("\\", "/")]
    subprocess.run(cmd, capture_output=True, timeout=120)
    pdf = subprocess.run([EDGE, "--headless=new", "--disable-gpu",
                          "--window-size=390,900", "--virtual-time-budget=1500",
                          "--dump-dom", "file:///" + page.replace("\\", "/")],
                         capture_output=True, timeout=120)
    dom = pdf.stdout.decode("utf-8", "ignore")
    m = re.search(r'id="report">([^<]+)<', dom)
    print("EXE", EDGE)
    print(m.group(1) if m else "（没拿到测量结果）")
    print("shot:", shot)


if __name__ == "__main__":
    main()
