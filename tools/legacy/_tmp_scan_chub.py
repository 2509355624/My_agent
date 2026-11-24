import json, urllib.request, urllib.parse, struct, base64, re, os, time

queries = ['中文', '角色扮演', '恋爱', '校园', '都市', '日常']
nodes = []
seen = set()
for q in queries:
    url = 'https://api.chub.ai/search?' + urllib.parse.urlencode({'search': q, 'first': 40, 'page': 1, 'nsfw': 'false'})
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            j = json.load(r)
        for n in (j.get('data') or {}).get('nodes') or []:
            fp = n.get('fullPath')
            if fp and fp not in seen and n.get('max_res_url'):
                seen.add(fp)
                nodes.append(n)
    except Exception as e:
        print('search fail', q, e)

nodes.sort(key=lambda n: -(n.get('nTokens') or 0))
print('nodes', len(nodes))

def parse_png(path):
    d = open(path, 'rb').read()
    i = 8
    out = {}
    while i + 8 <= len(d):
        n = struct.unpack('>I', d[i:i+4])[0]
        t = d[i+4:i+8].decode('latin1')
        b = d[i+8:i+8+n]
        i += 12 + n
        if t == 'tEXt':
            k, _, v = b.partition(b'\x00')
            out[k.decode('latin1')] = v.decode('latin1', 'ignore')
    if 'chara' not in out:
        return None
    return json.loads(base64.b64decode(out['chara']))

rows = []
for idx, n in enumerate(nodes[:25]):
    path = r'C:\Users\gjl\AppData\Local\Temp\chub_%d.png' % idx
    try:
        urllib.request.urlretrieve(n['max_res_url'], path)
        j = parse_png(path)
        data = (j or {}).get('data') or j or {}
        s = data.get('first_mes') or ''
        cn = len(re.findall(r'[\u4e00-\u9fff]', s))
        if cn >= 100:
            rows.append((len(s), cn, n.get('name'), n.get('fullPath'), path, s[:120].replace('\n', ' ')))
            print(len(s), cn, n.get('name'), path)
    except Exception as e:
        pass
    finally:
        try:
            os.remove(path)
        except Exception:
            pass

rows.sort(key=lambda r: -r[0])
for r in rows[:10]:
    print('TOP', r[0], r[1], r[2], r[3], r[5])
