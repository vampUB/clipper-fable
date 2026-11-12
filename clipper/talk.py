"""Поиск разговорных моментов по транскрипту — того, что не видно по громкости:
законченные разговорные блоки, повторяющиеся фразы (суперкат, сквозные шутки),
обещания/прогнозы и их возможные развязки (сетап → развязка).

Это карта для чтения, а не готовый выбор: шутку всё равно надо понять самому."""
import re
from collections import defaultdict
from pathlib import Path

import numpy as np

from .subs import norm_word
from .util import fmt_time, read_json, write_json

# Служебные слова: по ним повтор фразы ничего не значит
STOP = set("""и в во не что он на я с со как а то все она так его но да ты к у же вы за бы по
только ее мне было вот от меня еще нет о из ему теперь когда даже ну вдруг ли если уже или ни
быть был него до вас нибудь опять уж вам ведь там потом себя ничего ей может они тут где есть
надо ней для мы тебя их чем была сам чтоб без будто чего раз тоже себе под будет ж тогда кто
этот того потому этого какой совсем ним здесь этом один почти мой тем чтобы нее сейчас были куда
зачем всех никогда можно при наконец два об другой хоть после над больше тот через эти нас про
всего них какая много разве три эту моя впрочем хорошо свою этой перед иногда лучше чуть том
нельзя такой им более всегда конечно всю между это вообще просто типа короче блин че чё щас
ладно давай давайте тебе очень прям прямо реально кстати значит ага угу ой ах эх вон эта эти
такое такая такие какие какое который которая которые мои твой твоя наш ваш свой своя свои""".split())


def _words(segments: list) -> list[dict]:
    out = []
    for si, s in enumerate(segments):
        ws = s.get("words") or []
        if not ws and s.get("text"):  # транскрипт без слов: делим время фразы поровну
            toks = s["text"].split()
            step = (s["end"] - s["start"]) / max(1, len(toks))
            ws = [{"start": s["start"] + i * step, "end": s["start"] + (i + 1) * step, "word": t}
                  for i, t in enumerate(toks)]
        for wi, w in enumerate(ws):
            out.append({"start": w["start"], "end": w["end"], "word": w["word"],
                        "n": norm_word(w["word"]), "first": wi == 0})
    out = [w for w in out if w["n"]]
    for prev, w in zip(out, out[1:]):   # начало предложения — заглавная буква там ничего не значит
        if re.search(r"[.!?…]$", prev["word"]):
            w["first"] = True
    return out


def _hits(text: str, keys: list[str]) -> list[str]:
    low = " " + text.lower().replace("ё", "е") + " "
    return [k for k in keys if k.lower().replace("ё", "е") in low]


def _phrases(words: list[dict], gap: float = 0.7) -> list[dict]:
    """Фразы: до паузы или до конца предложения."""
    out, cur = [], []
    for w in words:
        if cur and (w["start"] - cur[-1]["end"] > gap or re.search(r"[.!?…]$", cur[-1]["word"])):
            out.append(cur)
            cur = []
        cur.append(w)
    if cur:
        out.append(cur)
    return [{"start": p[0]["start"], "end": p[-1]["end"], "text": " ".join(x["word"] for x in p)} for p in out]


def talk_blocks(words: list[dict], tcfg: dict, loud: np.ndarray | None) -> list[dict]:
    """Блоки речи между паузами — кандидаты в «цельный отрезок»."""
    gap = float(tcfg.get("block_gap", 2.0))
    lo, hi = float(tcfg.get("min_len", 8)), float(tcfg.get("max_len", 70))
    runs, cur = [], []
    for w in words:
        if cur and w["start"] - cur[-1]["end"] >= gap:
            runs.append(cur)
            cur = []
        cur.append(w)
    if cur:
        runs.append(cur)

    def split(run):  # слишком длинный блок режем по самой большой внутренней паузе
        if run[-1]["end"] - run[0]["start"] <= hi or len(run) < 8:
            return [run]
        gaps = [(run[i + 1]["start"] - run[i]["end"], i) for i in range(3, len(run) - 4)]
        if not gaps:
            return [run]
        _, i = max(gaps)
        return split(run[:i + 1]) + split(run[i + 1:])

    base = float(np.median(loud)) if loud is not None and loud.size else None
    blocks = []
    for run in (r for big in runs for r in split(big)):
        a, b = run[0]["start"], run[-1]["end"]
        dur = b - a
        if dur < lo or len(run) < 12:
            continue
        text = " ".join(w["word"] for w in run)
        tags, score = [], 0.0
        for name, keys in (tcfg.get("markers") or {}).items():
            h = _hits(text, keys)
            if h:
                tags.append(f"{name}: {', '.join(h[:4])}")
                score += 1.0 + 0.3 * min(len(h), 4)
        q = text.count("?")
        if q:
            tags.append(f"вопросов: {q}")
            score += min(q, 3) * 0.6
        if re.search(r"\b(а?ха-?ха|ахах|хах|хе-хе)", text.lower()):
            tags.append("смех")
            score += 1.5
        calls = _hits(text, tcfg.get("callouts") or [])
        call_share = len(calls) / max(1, len(tcfg.get("callouts") or [1]))
        dens = len(run) / dur
        score += min(dens, 3.5) * 0.4
        if len(calls) >= 4:
            tags.append("много игровых команд")
            score -= 1.0 + call_share
        quiet = None
        if base is not None:
            seg = loud[int(a): int(b) + 1]
            if seg.size:
                quiet = float(np.mean(seg) - base)
                if quiet < 1.5:
                    tags.append("тихий (по громкости не найти)")
                    score += 0.8
        ph = _phrases(run)
        blocks.append({
            "start": round(a, 2), "end": round(b, 2), "dur": round(dur, 1), "score": round(score, 2),
            "tags": tags, "first": ph[0]["text"][:140], "last": ph[-1]["text"][:140],
            "text": text[:700], "words_per_sec": round(dens, 2),
        })
    blocks.sort(key=lambda x: -x["score"])
    return blocks


def repeats(words: list[dict], tcfg: dict) -> tuple[list[dict], list[dict]]:
    """Повторы: (фразы из 2–4 слов, имена/термины). Материал для суперката и сквозных шуток."""
    min_count = int(tcfg.get("repeat_min", 3))
    apart = float(tcfg.get("repeat_apart", 20))
    stem = lambda n: n[:6] if len(n) > 7 else n  # грубая лемма: «правило/правилом/правила»
    st = [stem(w["n"]) for w in words]
    seen = defaultdict(list)
    for n in (4, 3, 2):
        for i in range(len(words) - n + 1):
            ws = words[i:i + n]
            if ws[-1]["end"] - ws[0]["start"] > 4:
                continue
            content = [x for x in ws if x["n"] not in STOP and len(x["n"]) > 2]
            if len(content) < (2 if n > 2 else 1) or ws[0]["n"] in STOP and ws[-1]["n"] in STOP:
                continue
            if n == 2 and len(content) < 2:
                continue
            seen[(n, tuple(st[i:i + n]))].append(i)
    phrases, covered = [], set()
    for (n, key), idxs in sorted(seen.items(), key=lambda kv: (-kv[0][0], -len(kv[1]))):
        times = []
        for i in idxs:
            if not times or words[i]["start"] - times[-1][0] >= apart:
                times.append((words[i]["start"], i))
        if len(times) < min_count:
            continue
        if all(any((i + k) in covered for k in range(n)) for _, i in times):
            continue  # это кусок уже найденной более длинной фразы
        for _, i in times:
            covered.update(range(i, i + n))
        i0 = times[0][1]
        phrases.append({"phrase": " ".join(w["n"] for w in words[i0:i0 + n]), "count": len(times),
                        "times": [round(t, 1) for t, _ in times]})
    phrases.sort(key=lambda x: (-x["count"] * len(x["phrase"].split()), x["times"][0]))

    # имя/термин: слово, которое хоть раз написано с заглавной НЕ в начале фразы (Егор, БКБ, Наруто)
    proper = set()
    for w in words:
        raw = w["word"].strip("«»\"“”„(")
        if not w["first"] and len(w["n"]) >= 3 and w["n"] not in STOP and raw[:1].isupper():
            proper.add(stem(w["n"]))
    names = defaultdict(list)
    for w in words:
        if stem(w["n"]) in proper:
            names[stem(w["n"])].append(w)
    terms = []
    for key, ws in names.items():
        times = []
        for w in ws:
            if not times or w["start"] - times[-1] >= apart:
                times.append(w["start"])
        if len(times) >= min_count:
            terms.append({"word": ws[0]["word"].strip(".,!?"), "count": len(ws),
                          "times": [round(t, 1) for t in times]})
    terms.sort(key=lambda x: -x["count"])
    return phrases[: int(tcfg.get("repeat_top", 30))], terms[: int(tcfg.get("terms_top", 30))]


def setups(words: list[dict], tcfg: dict) -> tuple[list[dict], list[dict]]:
    ph = _phrases(words)
    s_keys, p_keys = tcfg.get("setup_keywords") or [], tcfg.get("payoff_keywords") or []
    ss = [{"t": round(p["start"], 1), "text": p["text"][:160], "hit": h[0]}
          for p in ph if (h := _hits(p["text"], s_keys)) and len(p["text"].split()) >= 3]
    pp = [{"t": round(p["start"], 1), "text": p["text"][:160], "hit": h[0]}
          for p in ph if (h := _hits(p["text"], p_keys))]
    return ss, pp


def talk(cfg: dict, pdir: Path, top: int | None = None, quiet: bool = False) -> dict | None:
    tr_p = pdir / "transcript.json"
    if not tr_p.exists():
        print("→ talk: нет transcript.json — сначала `python -m clipper transcribe` (без речи разговоры не найти)")
        return None
    tcfg = cfg.get("talk") or {}
    top = top or int(tcfg.get("top", 25))
    words = _words(read_json(tr_p)["segments"])
    if not words:
        print("→ talk: транскрипт пустой")
        return None
    loud_p = pdir / "cache" / "loudness.npy"
    loud = np.load(loud_p) if loud_p.exists() else None

    blocks = talk_blocks(words, tcfg, loud)[:top]
    phrases, terms = repeats(words, tcfg)
    ss, pp = setups(words, tcfg)
    data = {"blocks": blocks, "repeat_phrases": phrases, "repeat_terms": terms, "setups": ss, "payoffs": pp}
    write_json(pdir / "talk.json", data)

    tc = lambda ts, n=12: ", ".join(fmt_time(t) for t in ts[:n]) + (" …" if len(ts) > n else "")
    L = ["# Разговорные кандидаты (по транскрипту, а не по громкости)",
         "# Это карта для чтения. Границы уточни по словам, шутку пойми сам, кадры проверь.", "",
         f"## 1. Разговорные блоки — кандидаты в цельный отрезок ({len(blocks)})", ""]
    for i, b in enumerate(blocks, 1):
        L.append(f"#{i:02d}  {fmt_time(b['start'])} – {fmt_time(b['end'])}  {b['dur']} с  score {b['score']}"
                 f"  {b['words_per_sec']} сл/с")
        if b["tags"]:
            L.append("    метки: " + "; ".join(b["tags"]))
        L.append(f"    начало: {b['first']}")
        L.append(f"    конец:  {b['last']}")
        L.append(f"    текст:  {b['text']}")
        L.append("")
    L += ["## 2. Повторяющиеся фразы — суперкат / сквозная шутка / сетап → развязка", ""]
    for p in phrases:
        L.append(f"  ×{p['count']}  «{p['phrase']}»  —  {tc(p['times'])}")
    L += ["", "## 3. Имена и термины, которые звучат чаще всего (темы видео)", ""]
    for t in terms:
        L.append(f"  ×{t['count']}  {t['word']}  —  {tc(t['times'], 8)}")
    L += ["", f"## 4. Обещания / прогнозы / правила — возможные СЕТАПЫ ({len(ss)})", ""]
    for x in ss[:80]:
        L.append(f"  [{fmt_time(x['t'])}] {x['text']}")
    L += ["", f"## 5. Признания / итоги / переобувки — возможные РАЗВЯЗКИ ({len(pp)})", ""]
    for x in pp[:80]:
        L.append(f"  [{fmt_time(x['t'])}] {x['text']}")
    L.append("")
    (pdir / "talk.txt").write_text("\n".join(L), encoding="utf-8")
    if not quiet:
        print("\n".join(L))
    print(f"✓ блоков: {len(blocks)}, повторов: {len(phrases)}, терминов: {len(terms)}, "
          f"сетапов: {len(ss)}, развязок: {len(pp)} → talk.txt / talk.json")
    return data
