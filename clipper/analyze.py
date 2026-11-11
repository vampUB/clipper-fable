"""Поиск кандидатов в хайлайты: громкость + активность чата + ключевые слова в речи."""
import json
import subprocess
from collections import Counter
from pathlib import Path

import numpy as np

from .util import find_source, fmt_time, probe, read_json, write_json

SR = 8000


def loudness_per_second(src: Path) -> np.ndarray:
    """RMS-громкость (дБ) по секундам, потоково — работает и на многочасовых VOD."""
    proc = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"],
        stdout=subprocess.PIPE,
    )
    vals = []
    chunk = SR * 2
    while True:
        buf = proc.stdout.read(chunk)
        if not buf:
            break
        a = np.frombuffer(buf[: len(buf) // 2 * 2], dtype=np.int16).astype(np.float32)
        rms = np.sqrt(np.mean(a * a)) if a.size else 0.0
        vals.append(20 * np.log10(rms + 1.0))
    proc.wait()
    return np.array(vals, dtype=np.float32)


def smooth(x: np.ndarray, win: int) -> np.ndarray:
    if win <= 1 or x.size == 0:
        return x
    k = np.ones(win, dtype=np.float32) / win
    return np.convolve(x, k, mode="same")


def local_excess(x: np.ndarray, win: int) -> np.ndarray:
    """Насколько значение выше локальной медианы (вокруг ±win/2 секунд)."""
    if x.size == 0:
        return x
    step = 10  # медиану считаем грубо, раз в 10 секунд, потом интерполируем
    idx = np.arange(0, x.size, step)
    half = win // 2
    med = np.array([np.median(x[max(0, i - half): i + half + 1]) for i in idx])
    base = np.interp(np.arange(x.size), idx, med)
    return x - base


def robust_z(x: np.ndarray) -> np.ndarray:
    if x.size == 0:
        return x
    med = np.median(x)
    mad = np.median(np.abs(x - med)) * 1.4826
    if mad < 1e-6:
        mad = np.std(x) or 1.0
    return (x - med) / mad


def chat_signal(path: Path, n: int, delay: float, keywords: list[str]):
    counts = np.zeros(n, dtype=np.float32)
    msgs = []
    kw = [k.lower() for k in keywords]
    with open(path, encoding="utf-8") as f:
        for line in f:
            m = json.loads(line)
            i = int(m["t"] - delay)
            if 0 <= i < n:
                text = m.get("text", "")
                low = text.lower()
                hype = any(k in low for k in kw)
                counts[i] += 1.0 + (1.0 if hype else 0.0)
                msgs.append((m["t"], text))
    return counts, msgs


def speech_signal(segments: list, n: int, keywords: list[str]) -> np.ndarray:
    sig = np.zeros(n, dtype=np.float32)
    kw = [k.lower() for k in keywords]
    for s in segments:
        low = s["text"].lower()
        hits = sum(low.count(k) for k in kw)
        if hits:
            i = int(s["start"])
            j = max(i + 1, int(s["end"]))
            sig[i:min(j, n)] += hits
        if "!" in s["text"]:
            sig[int(s["start"]):min(int(s["end"]) + 1, n)] += 0.5
    return sig


def analyze(cfg: dict, pdir: Path, top: int | None = None):
    acfg = cfg["analyze"]
    top = top or acfg["top"]
    src = find_source(pdir)
    dur = probe(src)["duration"]

    cache = pdir / "cache" / "loudness.npy"
    cache.parent.mkdir(exist_ok=True)
    if cache.exists():
        loud = np.load(cache)
    else:
        print("→ считаю громкость (на длинных VOD это пара минут)")
        loud = loudness_per_second(src)
        np.save(cache, loud)
    n = loud.size
    w = acfg["weights"]

    comps = {}
    comps["loudness"] = np.clip(robust_z(local_excess(smooth(loud, 3), 180)), -2, 8)

    chat_msgs = []
    chat_p = pdir / "chat.jsonl"
    if chat_p.exists() and chat_p.stat().st_size:
        counts, chat_msgs = chat_signal(chat_p, n, acfg["chat_delay"], acfg["chat_keywords"])
        comps["chat"] = np.clip(robust_z(local_excess(smooth(counts, 10), 300)), -2, 10)

    segments = []
    tr_p = pdir / "transcript.json"
    if tr_p.exists():
        segments = read_json(tr_p)["segments"]
        comps["speech"] = np.clip(robust_z(smooth(speech_signal(segments, n, acfg["speech_keywords"]), 5)), -1, 8)

    score = sum(w.get(k, 1.0) * v for k, v in comps.items())
    used = ", ".join(comps)
    print(f"→ сигналы: {used}")

    order = np.argsort(-score)
    peaks = []
    for i in order:
        if len(peaks) >= top:
            break
        if all(abs(i - p) >= acfg["min_gap"] for p in peaks):
            peaks.append(int(i))

    moments = []
    for rank, p in enumerate(peaks, 1):
        a = max(0.0, p - acfg["before"])
        b = min(dur, p + acfg["after"])
        m = {
            "rank": rank, "peak": p, "peak_tc": fmt_time(p),
            "start": round(a, 1), "end": round(b, 1),
            "start_tc": fmt_time(a), "end_tc": fmt_time(b),
            "score": round(float(score[p]), 2),
            "signals": {k: round(float(v[p]), 2) for k, v in comps.items()},
        }
        if segments:
            m["speech"] = " ".join(s["text"] for s in segments if s["end"] > a and s["start"] < b)[:600]
        if chat_msgs:
            lo, hi = a + acfg["chat_delay"], b + acfg["chat_delay"]
            win = [t for (tt, t) in chat_msgs if lo <= tt <= hi]
            m["chat_count"] = len(win)
            m["chat_top"] = [f"{t} ×{c}" for t, c in Counter(x.strip().lower() for x in win if x.strip()).most_common(8)]
        moments.append(m)

    write_json(pdir / "moments.json", moments)
    with open(pdir / "moments.txt", "w", encoding="utf-8") as f:
        for m in moments:
            f.write(f"#{m['rank']:02d}  {m['start_tc']} – {m['end_tc']}  пик {m['peak_tc']}  score {m['score']}  {m['signals']}\n")
            if m.get("speech"):
                f.write(f"    речь: {m['speech']}\n")
            if m.get("chat_top"):
                f.write(f"    чат ({m['chat_count']}): {', '.join(m['chat_top'])}\n")
            f.write("\n")
    print((pdir / "moments.txt").read_text(encoding="utf-8"))
    print(f"✓ {len(moments)} кандидатов → moments.json / moments.txt")
