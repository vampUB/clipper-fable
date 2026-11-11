"""Генерация ASS-субтитров в стиле TikTok: крупно, по 2-3 слова, текущее слово подсвечено."""
import re

from .util import ass_color

PUNCT = re.compile(r"[.,!?;:…«»\"“”„()\[\]]+")


def _ts(t: float) -> str:
    t = max(0.0, t)
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")").replace("\n", " ")


def _clean_word(w: str, scfg: dict) -> str:
    for k, v in (scfg.get("replace") or {}).items():
        if w.strip(".,!?").lower() == str(k).lower():
            w = str(v)
    if scfg.get("strip_punctuation"):
        # оставляем ?! — они передают эмоцию
        w = re.sub(r"[.,;:…«»\"“”„()\[\]]+", "", w)
    if scfg.get("uppercase"):
        w = w.upper()
    return w.strip()


def group_words(words: list[dict], max_words: int, max_chars: int) -> list[list[dict]]:
    groups, cur = [], []
    for i, w in enumerate(words):
        if cur:
            gap = w["start"] - cur[-1]["end"]
            chars = sum(len(x["text"]) + 1 for x in cur) + len(w["text"])
            ends_sentence = bool(re.search(r"[.!?…]$", cur[-1]["raw"]))
            if len(cur) >= max_words or chars > max_chars or gap > 0.6 or ends_sentence:
                groups.append(cur)
                cur = []
        cur.append(w)
    if cur:
        groups.append(cur)
    return groups


def build_ass(words: list[dict], clip_start: float, clip_end: float, W: int, H: int,
              scfg: dict, tcfg: dict, title: str | None, subs_y: int, title_y: int) -> str:
    dur = clip_end - clip_start
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}",
        "WrapStyle: 0", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Sub,{scfg['font']},{scfg['size']},{ass_color(scfg['color'])},&H000000FF,"
        f"{ass_color(scfg['outline_color'])},{ass_color('#000000', 0x60)},0,0,0,0,100,100,0,0,1,"
        f"{scfg['outline']},{scfg['shadow']},5,60,60,0,1",
        f"Style: Title,{tcfg['font']},{tcfg['size']},{ass_color(tcfg['color'])},&H000000FF,"
        f"{ass_color(tcfg['box_color'])},{ass_color(tcfg['box_color'])},0,0,0,0,100,100,0,0,3,"
        f"14,0,5,80,80,0,1",
        "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    if title and tcfg.get("enabled", True):
        t = title.upper() if tcfg.get("uppercase") else title
        t_end = dur if not tcfg.get("duration") else min(dur, float(tcfg["duration"]))
        anim = r"\fad(150,250)\t(0,180,\fscx108\fscy108)\t(180,320,\fscx100\fscy100)"
        lines.append(f"Dialogue: 1,{_ts(0)},{_ts(t_end)},Title,,0,0,0,,"
                     f"{{\\an5\\pos({W // 2},{title_y}){anim}}}{_esc(t)}")

    if scfg.get("enabled", True) and words:
        prepared = []
        for w in words:
            txt = _clean_word(w["word"], scfg)
            if not txt:
                continue
            if txt.startswith("-") and prepared:  # whisper режет «что-то» на «что» + «-то»
                prepared[-1]["text"] += txt
                prepared[-1]["raw"] += w["word"]
                prepared[-1]["end"] = min(dur, w["end"] - clip_start)
                continue
            prepared.append({"text": txt, "raw": w["word"],
                             "start": max(0.0, w["start"] - clip_start),
                             "end": min(dur, w["end"] - clip_start)})
        hl = ass_color(scfg["highlight"])
        pop = r"\fscx112\fscy112" if scfg.get("pop") else ""
        groups = group_words(prepared, scfg["max_words"], scfg["max_chars"])
        for gi, g in enumerate(groups):
            g_end = g[-1]["end"] + 0.25
            if gi + 1 < len(groups):
                g_end = min(g_end, groups[gi + 1][0]["start"])
            for wi, w in enumerate(g):
                a = w["start"] if wi else g[0]["start"]
                b = g[wi + 1]["start"] if wi + 1 < len(g) else g_end
                if b - a < 0.02:
                    continue
                parts = []
                for wj, x in enumerate(g):
                    if wj == wi:
                        parts.append(f"{{\\c{hl}{pop}}}{_esc(x['text'])}{{\\r}}")
                    else:
                        parts.append(_esc(x["text"]))
                lines.append(f"Dialogue: 0,{_ts(a)},{_ts(b)},Sub,,0,0,0,,"
                             f"{{\\an5\\pos({W // 2},{subs_y})}}" + " ".join(parts))
    return "\n".join(lines) + "\n"
