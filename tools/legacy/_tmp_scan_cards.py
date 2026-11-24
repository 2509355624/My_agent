import os, json, base64, struct, re

roots = [r'D:\AI\tavern_cards', r'D:\AI\tavern_compare', r'D:\AI\SillyTavern\data\default-user\characters', r'D:\Users\gjl\Downloads']
rows = []

def add(path, name, text):
    if not text:
        return
    cn = len(re.findall(r'[\u4e00-\u9fff]', text))
    if cn < 150:
        return
    rows.append((len(text), cn, path, name, text))

def png_card(path):
    d = open(path, 'rb').read()
    i = 8
    out = {}
    while i + 8 <= len(d):
        n = struct.unpack('>I', d[i:i + 4])[0]
        t = d[i + 4:i + 8].decode('latin1')
        b = d[i + 8:i + 8 + n]
        i += 12 + n
        if t in ('tEXt', 'iTXt'):
            if t == 'tEXt':
                k, _, v = b.partition(b'\x00')
                out[k.decode('latin1')] = v.decode('latin1', 'ignore')
    if 'chara' not in out:
        return None
    try:
        return json.loads(base64.b64decode(out['chara']))
    except Exception:
        return None

for root in roots:
    if not os.path.isdir(root):
        continue
    for dirpath, _, files in os.walk(root):
        for fn in files:
            p = os.path.join(dirpath, fn)
            try:
                data = None
                if fn.lower().endswith('.png'):
                    j = png_card(p)
                    data = (j.get('data') or j) if j else None
                elif fn.lower().endswith('.json'):
                    j = json.load(open(p, encoding='utf-8-sig'))
                    data = j.get('data') or j
                if isinstance(data, dict):
                    add(p, data.get('name') or fn, data.get('mes_example') or '')
            except Exception:
                pass

rows.sort(key=lambda x: -x[1])
for r in rows[:20]:
    print(r[0], r[1], r[3], r[2])
    print(r[4][:300].replace('\n', ' / '))
    print('-' * 80)
