import json
import urllib.request

d = json.load(urllib.request.urlopen(
    "http://127.0.0.1:5174/api/persona/session?qq=2509355624", timeout=20))
print("session keys", sorted(d.keys()))
for key in ("affection", "aff", "state", "card", "mode", "writing", "turns"):
    val = d.get(key)
    if isinstance(val, dict):
        print(key, {k: val.get(k) for k in val if k not in ("levels", "history")})
    elif isinstance(val, list):
        print(key, "list", len(val))
    else:
        print(key, repr(val))
msgs = [m for m in d.get("messages", []) if m.get("role") == "assistant"]
print("last keys", sorted(msgs[-1].keys()) if msgs else [])
for key in ("aff", "mood", "intensity", "state", "day", "phase", "preg"):
    print("last", key, repr(msgs[-1].get(key)) if msgs else None)
