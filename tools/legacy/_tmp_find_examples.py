# -*- coding: utf-8 -*-
import glob, json, os, re

bad = ['先动的是','这个认知','那种哑','是早晨那种','眼中闪过','不是……而是','那一刻','意识到',
       '空气凝固','指尖泛白','指节泛白','微微','轻轻','仿佛','似乎','好像','顿了顿','停了','卡了',
       '脚趾头缩','心中涌起','复杂的情绪','不易察觉','一丝','一抹','终于明白','像被','像是','泛白']
sex = ['手指','体内','穴','乳','腿根','腰窝','裸','性','射','插','湿','喘','颤','臀','胸','吻','舌','硬','胀']

files = glob.glob(r'D:\AI\my_agent_restructure\agents\qq\sessions\persona_*izumi_sagiri*.jsonl')
rows_out = []
for path in files:
    try:
        rows = [json.loads(l) for l in open(path, encoding='utf-8') if l.strip()]
    except Exception:
        continue
    for i, r in enumerate(rows):
        if r.get('role') != 'assistant':
            continue
        text = (r.get('content') or '').strip()
        body = re.split(r'\n(?:画面|服饰|心情|状态)：', text)[0].strip()
        if not (300 <= len(body) <= 900):
            continue
        if '「' not in body:
            continue
        hit = [p for p in bad if p in body]
        if len(hit) > 1 or any(s in body for s in sex):
            continue
        rows_out.append((len(hit), -body.count('「'), len(body), os.path.basename(path), i, hit, body))

rows_out.sort(key=lambda x: (x[0], x[1], -x[2]))
for c in rows_out[:20]:
    score, nd, ln, name, idx, hit, body = c
    print('=' * 80)
    print('bad', score, 'dial', -nd, 'len', ln, name, 'line', idx, hit)
    print(body[:700])
