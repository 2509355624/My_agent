import json
import re
import urllib.request

d = json.load(urllib.request.urlopen(
    "http://127.0.0.1:5174/api/persona/session?qq=2509355624", timeout=20))
msgs = [m for m in d.get("messages", []) if m.get("role") == "assistant"]
t = msgs[-1]["text"] if msgs else ""
lines = [l.strip() for l in t.splitlines() if l.strip()]
print("total_chars", len(t), "lines", len(lines))
print("paragraph_chars", sorted([len(l) for l in lines if not l.startswith("画面")], reverse=True)[:10])
print("short_lines<=12", sum(1 for l in lines if len(l) <= 12))

# 句子长度：按中文句末和换行粗切
units = [u.strip() for u in re.split(r"[。！？!?\n]+", t) if u.strip()]
print("units", len(units), "avg_len", round(sum(map(len, units)) / max(1, len(units)), 1))
print("units<=8", sum(1 for u in units if len(u) <= 8))
print("units<=15", sum(1 for u in units if len(u) <= 15))

patterns = {
    "停顿/停": r"停|顿|卡住|卡了",
    "沉默/安静": r"沉默|安静|静了|没声",
    "不是...是": r"不是.{0,18}是",
    "像/像什么": r"像|像是|像什么",
    "忽然/突然": r"忽然|突然",
    "然后": r"然后",
    "指节/指尖/喉结": r"指节|指尖|喉结",
    "呼吸/气": r"呼吸|气",
    "半/一寸/几秒": r"半秒|几秒|一寸|半寸",
    "重复句号": r"。{2,}",
}
for name, pat in patterns.items():
    print(name, len(re.findall(pat, t)))
print("commas", t.count("，"), "periods", t.count("。"), "ellipsis", t.count("……"))
