#!/usr/bin/env python3
# Использование: python tools/autosub_to_transcript.py work/<проект>  (нужен work/<проект>/autosub.ru-orig.json3:
#   yt-dlp --skip-download --write-auto-subs --sub-langs ru-orig --sub-format json3 -o work/<проект>/autosub <ссылка>)
"""YouTube json3 автосубтитры → transcript.json/txt в формате clipper (слова с таймкодами)."""
import json, sys
from pathlib import Path
pdir = Path(sys.argv[1])
d = json.load(open(pdir / "autosub.ru-orig.json3"))
evs = [e for e in d["events"] if e.get("segs") and any(s.get("utf8","").strip() for s in e["segs"])]
words = []
for e in evs:
    st = e["tStartMs"] / 1000
    for s in e["segs"]:
        w = s.get("utf8", "").strip()
        if not w or w == "\n": continue
        turn = w.startswith(">>")
        w = w.replace(">>", "").replace("\xa0", " ").strip()
        if not w or w.startswith("["):   # [музыка], [смех], [ __ ] (мат YouTube вырезает) — не слова
            if turn and words: words[-1]["turn_after"] = True
            continue
        words.append({"start": round(st + s.get("tOffsetMs", 0) / 1000, 2), "word": w, "p": 0.9, "turn": turn})
for i, w in enumerate(words):
    nxt = words[i + 1]["start"] if i + 1 < len(words) else w["start"] + 0.5
    w["end"] = round(min(nxt, w["start"] + max(0.25, 0.09 * len(w["word"]))), 2)
# сегменты: разрыв по паузе > 0.8 с или после конца предложения, не длиннее 12 с
segs, cur = [], []
for i, w in enumerate(words):
    if cur and (w.get("turn") or w["start"] - cur[-1]["end"] > 0.8 or (cur[-1]["word"][-1] in ".?!" and w["start"] - cur[0]["start"] > 2.5) or w["start"] - cur[0]["start"] > 12):
        segs.append(cur); cur = []
    cur.append(w)
if cur: segs.append(cur)
for w in words:
    w.pop("turn", None); w.pop("turn_after", None)
out = [{"start": s[0]["start"], "end": s[-1]["end"], "text": " ".join(x["word"] for x in s), "words": s} for s in segs]
json.dump({"segments": out, "source": "youtube-autosub"}, open(pdir / "transcript.json", "w"), ensure_ascii=False, indent=0)
def ft(t): return f"{int(t//3600)}:{int(t%3600//60):02d}:{t%60:04.1f}" if t >= 3600 else f"{int(t//60)}:{t%60:04.1f}"
with open(pdir / "transcript.txt", "w") as f:
    for s in out: f.write(f"[{ft(s['start'])}] {s['text']}\n")
print(pdir.name, len(out), "segments", len(words), "words")
