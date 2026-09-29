# -*- coding: utf-8 -*-
"""NapCat 登录：需要扫码时，把二维码直接画在当前这个 cmd 窗口里。

    python napcat_qr.py            先试保存的登录票，不行就把二维码画在这里
    python napcat_qr.py --check    只报状态，不出码
    python napcat_qr.py --relogin  只试票，不出码（票没了返回 2）
    python napcat_qr.py --tries 3 --wait 150

为什么要这么个脚本：napcat.mjs 里那句
    (NAPCAT_QUICK_ACCOUNT || 快速登录列表非空 || 密码env) && (a = true)
    !a && !isLogined && getQRCodePicture()
意味着只要机器上有任何一个号存着登录票（这台机器有 2509355624、3505606177），
NapCat 自己就永远不会去申请二维码，它的窗口里自然也就永远看不到码。
这里绕过那个判断：直接调 WebUI 的 RefreshQRcode 拿链接，自己画。
"""

import argparse
import glob
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

WEBUI_PORT = 6099
TICKET_WAIT_HINT = "下次启动 NapCat 才会写进快速登录列表"

# webui.json 的真实位置：...\resources\app\napcat\config\webui.json
_CFG_RE = re.compile(r'napcat[\\/]config[\\/]', re.I)


def find_webui_json():
    hits = [p for p in glob.glob(r'D:\AI\NapCat\**\webui.json', recursive=True)
            if _CFG_RE.search(p)]
    if not hits:
        return None
    return sorted(hits, key=os.path.getmtime)[-1]


def find_qrcode_png(webui_json):
    # ...\resources\app\napcat\config\webui.json -> ...\resources\app\napcat\cache
    root = os.path.dirname(os.path.dirname(webui_json))
    p = os.path.join(root, 'cache', 'qrcode.png')
    if os.path.exists(p):
        return p
    hits = glob.glob(r'D:\AI\NapCat\**\qrcode.png', recursive=True)
    return sorted(hits, key=os.path.getmtime)[-1] if hits else None


class WebUI:
    def __init__(self, token):
        self.base = 'http://127.0.0.1:%d' % WEBUI_PORT
        # 显式空代理：本机 127.0.0.1 被系统代理拦掉过（WinError 10061 的假象）
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.cred = self._login(token)

    def _req(self, path, body=None, method=None):
        data = None
        headers = {'Authorization': 'Bearer ' + self.cred}
        if body is not None:
            data = json.dumps(body).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        req = urllib.request.Request(self.base + path, data=data, headers=headers,
                                     method=method or ('POST' if data is not None else 'GET'))
        with self.opener.open(req, timeout=15) as r:
            return json.loads(r.read().decode('utf-8'))

    def _login(self, token):
        h = hashlib.sha256((token + '.napcat').encode('utf-8')).hexdigest()
        r = self._raw_login(h)
        # 雷注意：这套 WebUI 的返回包是 {code, data, message}，没有 success 字段，
        # code == 0 才是成功（拿 Credential 判空比判字段名靠谱）。
        d = r.get('data') or {}
        cred = d.get('Credential') or r.get('Credential')
        if not cred:
            raise SystemExit('[FAIL] WebUI 登录被拒（token 不对？）：%s' % r.get('message'))
        return cred

    def _raw_login(self, h):
        req = urllib.request.Request(
            self.base + '/api/auth/login',
            data=json.dumps({'hash': h}).encode('utf-8'),
            headers={'Content-Type': 'application/json'}, method='POST')
        with self.opener.open(req, timeout=15) as r:
            return json.loads(r.read().decode('utf-8'))

    def status(self):
        return self._req('/api/QQLogin/CheckLoginStatus', {})['data']

    def tickets(self):
        d = self._req('/api/QQLogin/GetQuickLoginListNew')
        return d.get('data') or []

    def refresh_qr(self):
        return self._req('/api/QQLogin/RefreshQRcode', {})

    def set_quick(self, uin):
        return self._req('/api/QQLogin/SetQuickLogin', {'uin': uin})

    def is_online(self, st):
        return bool(st.get('isLogin')) and bool(st.get('coreReady'))


def draw_qr(url):
    """把链接画成终端二维码：一个模块 = 2 字符宽 1 行高（字素宽高比约 1:2，方块的）。

    不能用 qrcode 自带的 print_ascii —— 它依赖 ▀▄，而 GBK 码页没有这两个字符。
    """
    import qrcode
    q = qrcode.QRCode(border=2)
    q.add_data(url)
    q.make(fit=True)
    matrix = q.get_matrix()
    for row in matrix:
        print(''.join('██' if cell else '  ' for cell in row))
    return len(matrix)


def wait_online(ui, seconds):
    """轮询到上线为止，上线 True，超时 False。"""
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            if ui.is_online(ui.status()):
                return True
        except Exception:
            pass
        time.sleep(2)
    return False


def try_ticket(ui, acct):
    """用保存的登录票静默上线，不用扫码。Returns True on success.

    票只在 GetQuickLoginListNew 里出现时才有效；刚扫完码的那一次列表快照里
    往往还没有，所以这条路失败是常态，不算错。
    """
    if not acct:
        return False
    try:
        uins = [str(r.get('uin') or '') for r in ui.tickets() if isinstance(r, dict)]
    except Exception:
        return False
    if acct not in uins:
        print('  本机没有 %s 的登录票（有票的号：%s）'
              % (acct, ', '.join(x for x in uins if x) or '无'))
        return False
    print('  有票，正在静默登录 %s ...' % acct)
    try:
        ui.set_quick(acct)
    except Exception as e:
        print('  [!] 快速登录请求失败：%s' % e)
        return False
    if wait_online(ui, 40):
        print('[OK] 凭证直接用了，不用扫码')
        return True
    print('  [!] 票在，但这 40 秒没登上')
    return False


def report_ticket(ui, acct):
    try:
        rows = ui.tickets()
    except Exception:
        return
    uins = []
    for r in rows:
        if isinstance(r, dict):
            uins.append(str(r.get('uin') or ''))
    if str(acct) in uins:
        print('[OK] 登录票已保存：%s —— 下次启动免扫码' % acct)
    else:
        print('[i] 该账号的票暂不在列表里（%s）。有票的号：%s'
              % (TICKET_WAIT_HINT, ', '.join(x for x in uins if x) or '无'))


def main():
    ap = argparse.ArgumentParser(description='NapCat 扫码登录（二维码画在本窗口）')
    ap.add_argument('--check', action='store_true', help='只报状态，不出码')
    ap.add_argument('--relogin', action='store_true', help='只试登录票，不扫码')
    ap.add_argument('--no-ticket', dest='no_ticket', action='store_true',
                    help='跳过试票，直接出码')
    ap.add_argument('--tries', type=int, default=3, help='最多换几张码')
    ap.add_argument('--wait', type=int, default=150, help='一张码等多少秒')
    ap.add_argument('--account', default='', help='覆盖 webui.json 里的 autoLoginAccount')
    args = ap.parse_args()

    cfg_path = find_webui_json()
    if not cfg_path:
        print('[FAIL] 找不到 webui.json，NapCat 装在哪？')
        return 1
    cfg = json.load(open(cfg_path, encoding='utf-8'))
    acct = args.account or cfg.get('autoLoginAccount') or ''
    png = find_qrcode_png(cfg_path)

    try:
        ui = WebUI(cfg['token'])
    except Exception as e:
        print('[FAIL] 连不上 NapCat WebUI(端口 %d)：%s' % (WEBUI_PORT, e))
        print('       NapCat 没在跑。先双击 一键启动.bat')
        return 1

    st = ui.status()
    if ui.is_online(st):
        print('[OK] 已登录，不用扫码（账号 %s）' % acct)
        report_ticket(ui, acct)
        return 0
    if args.check:
        print('[i] 未登录：phase=%s  原因=%s' % (st.get('loginPhase'), st.get('loginError') or '无'))
        report_ticket(ui, acct)
        print('    去掉 --check 再跑一次就会在这里出二维码')
        return 2

    if not args.no_ticket:
        if try_ticket(ui, acct):
            report_ticket(ui, acct)
            return 0
        if args.relogin:
            print('[i] 票用不上，需要人工扫码。去掉 --relogin 就会在这里出码。')
            return 2

    print('账号 %s 需要扫码登录。' % acct)
    print('用手机 QQ「扫一扫」对准下面这块（一张码管 %d 秒，窗口太小就拉大或调字号）：'
          % args.wait)
    print()
    for i in range(1, args.tries + 1):
        st = ui.status()
        if ui.is_online(st):
            print('[OK] 已登录')
            report_ticket(ui, acct)
            return 0
        if st.get('qrLoginAccepted'):
            print('  这张码手机上已经扫过了，去手机上点「确认登录」，别再刷新')
        else:
            try:
                r = ui.refresh_qr()
            except Exception as e:
                print('[FAIL] 申请二维码失败：%s' % e)
                time.sleep(5)
                continue
            if (r.get('data') or {}).get('restarting'):
                print('  NapCat 正在重连，等 8 秒再来一张')
                time.sleep(8)
                continue
            url = (r.get('data') or {}).get('qrcodeurl') or ''
            if not url:
                print('[FAIL] NapCat 返回空二维码链接：%s' % json.dumps(r, ensure_ascii=False))
                time.sleep(5)
                continue
            sys.stdout.write('\r' * 80)
            draw_qr(url)
            print()
            print('  （这块扫不出来就看备用图：%s，或浏览器打开 http://127.0.0.1:%d/webui）'
                  % (png or '无', WEBUI_PORT))
        deadline = time.time() + args.wait
        while time.time() < deadline:
            time.sleep(3)
            st = ui.status()
            if ui.is_online(st):
                print()
                print('[OK] 扫码成功，她上线了')
                report_ticket(ui, acct)
                return 0
            if st.get('qrLoginAccepted'):
                sys.stdout.write('  已扫描，等手机确认...\r')
                sys.stdout.flush()
        print()
        print('  第 %d 张码没人扫，超时了' % i)
    print('[FAIL] 换了 %d 张码都没登上去。手机 QQ 是不是没登这个号？' % args.tries)
    return 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('\n已取消')
        sys.exit(130)
