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


def density_signal(segments: list, n: int) -> np.ndarray:
    """Слов в секунду: плотный разговор без крика — то, что громкость не ловит."""
    sig = np.zeros(n, dtype=np.float32)
    for s in segments:
        for w in s.get("words") or []:
            i = int(w["start"])
            if 0 <= i < n:
                sig[i] += 1.0
    return sig


def _pick(score: np.ndarray, top: int, min_gap: int, taken: list[int], floor: float | None = None) -> list[int]:
    peaks = []
    for i in np.argsort(-score):
        if len(peaks) >= top or (floor is not None and score[i] <= floor):
            break
        if all(abs(int(i) - p) >= min_gap for p in [*taken, *peaks]):
            peaks.append(int(i))
    return peaks


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
    if loud.size == 0:  # видео без звука: анализ только по чату и речи
        print("  ! в видео нет звука — громкость не учитывается")
        loud = np.zeros(max(1, int(dur)), dtype=np.float32)
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

    peaks = _pick(score, top, acfg["min_gap"], [], floor=0.0) or _pick(score, 1, acfg["min_gap"], [])
    kinds = {p: "громкий" for p in peaks}

    # Тихие кандидаты: чат/речь активны, а громкость обычная. Основной рейтинг их топит,
    # потому что крики в драках всегда громче разговорного рофла.
    n_quiet = acfg.get("quiet_top")
    n_quiet = max(3, top // 3) if n_quiet is None else int(n_quiet)
    if n_quiet and (segments or "chat" in comps):
        q = np.zeros(n, dtype=np.float32)
        if "chat" in comps:
            q += w.get("chat", 1.5) * comps["chat"]
        if segments:
            q += w.get("speech", 0.7) * comps["speech"]
            q += float(acfg.get("density_weight", 0.6)) * np.clip(
                robust_z(smooth(density_signal(segments, n), 15)), -2, 4)
        q = np.where(comps["loudness"] < float(acfg.get("quiet_max_loudness", 1.0)), q, -99.0)
        for p in _pick(q, n_quiet, max(20, acfg["min_gap"] // 2), peaks, floor=0.5):
            peaks.append(p)
            kinds[p] = "тихий"
            score[p] = q[p]

    moments = []
    for rank, p in enumerate(peaks, 1):
        a = max(0.0, p - acfg["before"])
        b = min(dur, p + acfg["after"])
        m = {
            "rank": rank, "kind": kinds[p], "peak": p, "peak_tc": fmt_time(p),
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
            f.write(f"#{m['rank']:02d}  [{m['kind']}]  {m['start_tc']} – {m['end_tc']}  пик {m['peak_tc']}  "
                    f"score {m['score']}  {m['signals']}\n")
            if m.get("speech"):
                f.write(f"    речь: {m['speech']}\n")
            if m.get("chat_top"):
                f.write(f"    чат ({m['chat_count']}): {', '.join(m['chat_top'])}\n")
            f.write("\n")
    print((pdir / "moments.txt").read_text(encoding="utf-8"))
    print(f"✓ {len(moments)} кандидатов → moments.json / moments.txt")
    if segments:
        from .talk import talk
        talk(cfg, pdir, quiet=True)
        print("  разговорные блоки, повторы и сетапы → talk.txt (прочитай его: лучшие рофлы — тихие)")
    else:
        print("  ! нет транскрипта: найдены только громкие моменты. Разговорные рофлы ищутся по "
              "transcript.json (`transcribe`, затем `talk`).")
