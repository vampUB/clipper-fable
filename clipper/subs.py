"""Генерация ASS-субтитров в стиле TikTok: крупно, по 2-3 слова, текущее слово подсвечено."""
import re

from .util import ass_color

PUNCT = re.compile(r"[.,!?;:…«»\"“”„()\[\]]+")
_EDGE = ".,!?;:…«»\"“”„()[]"

# Мат → звёздочка (subs.censor). Корни привязаны к началу слова или приставке,
# чтобы не задеть «плохую», «хлеб», «небо», «бляшку».
_PFX_H = r"(?:на|по|ни|не|о|а|до|за|от|при|у|вы|в|с|рас|раз|пере|про)?"
_PFX_E = r"(?:за|на|вы|у|по|до|про|при|не|пере|разъ|отъ|объ|подъ|въ|съ|долбо|зло)?"
_V = "аеёиоуыя"
_CENSOR = [
    (re.compile(rf"^({_PFX_H}х)у([йяеёию])", re.I), r"\1*\2"),
    (re.compile(r"(п)и(зд)", re.I), r"\1*\2"),
    (re.compile(r"^(бл)я(?!ш)", re.I), r"\1*"),
    (re.compile(rf"^({_PFX_E}[её]б)[{_V}]", re.I), r"\1*"),
    (re.compile(rf"^({_PFX_E})[её](б)(?![{_V}])", re.I), r"\1*\2"),
    (re.compile(r"^(п)и(д[оа]?р)", re.I), r"\1*\2"),
]


def censor(word: str) -> str:
    """«пиздец» → «п*здец», «заебал» → «заеб*л». Уже замазанные слова не трогает."""
    if "*" in word:
        return word
    for rx, rep in _CENSOR:
        new = rx.sub(rep, word, count=1)
        if new != word:
            return new
    return word


def _ts(t: float) -> str:
    t = max(0.0, t)
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")").replace("\n", " ")


def _ml(text: str) -> str:
    """Многострочный текст: перевод строки -> \\N."""
    return "\\N".join(_esc(x) for x in str(text).split("\n"))


def norm_word(w: str) -> str:
    """Слово для сравнения: без пунктуации по краям, строчными, ё → е."""
    return w.strip().strip(_EDGE).lower().replace("ё", "е")


def apply_replace(words: list[dict], replace: dict | None) -> list[dict]:
    """Правки распознавания. Ключ — слово или фраза из нескольких слов («быть бум»: Бетбум),
    значение — как писать; пустая строка — убрать. Фразы ищутся по соседним словам,
    длинные ключи важнее коротких. Знаки ?! в конце исходного слова сохраняются."""
    rules = []
    for k, v in (replace or {}).items():
        key = [norm_word(x) for x in str(k).split()]
        key = [x for x in key if x]
        if key:
            rules.append((key, "" if v is None else str(v)))
    if not rules:
        return [dict(w) for w in words]
    rules.sort(key=lambda r: -len(r[0]))
    norm = [norm_word(w["word"]) for w in words]
    out, i = [], 0
    while i < len(words):
        for key, val in rules:
            n = len(key)
            if norm[i:i + n] == key:
                # фраза не должна перешагивать склейку
                if len({w.get("seg") for w in words[i:i + n]}) > 1:
                    continue
                tail = re.search(r"[?!]+$", words[i + n - 1]["word"].strip())
                text = val + (tail.group(0) if tail and val and not re.search(r"[?!]$", val) else "")
                if text:
                    out.append({**words[i], "word": text, "end": words[i + n - 1]["end"], "fixed": True})
                i += n
                break
        else:
            out.append(dict(words[i]))
            i += 1
    return out


def _clean_word(w: str, scfg: dict) -> str:
    if scfg.get("strip_punctuation"):
        # оставляем ?! — они передают эмоцию
        w = re.sub(r"[.,;:…«»\"“”„()\[\]]+", "", w)
    if scfg.get("censor", True):
        w = censor(w)
    if scfg.get("uppercase"):
        w = w.upper()
    return w.strip()


def group_words(words: list[dict], max_words: int, max_chars: int) -> list[list[dict]]:
    groups, cur = [], []
    for w in words:
        if cur:
            gap = w["start"] - cur[-1]["end"]
            chars = sum(len(x["text"]) + 1 for x in cur) + len(w["text"])
            ends_sentence = bool(re.search(r"[.!?…]$", cur[-1]["raw"]))
            new_seg = w.get("seg") != cur[-1].get("seg")   # склейка: новая сцена — новая строка
            if len(cur) >= max_words or chars > max_chars or gap > 0.6 or ends_sentence or new_seg:
                groups.append(cur)
                cur = []
        cur.append(w)
    if cur:
        groups.append(cur)
    # одинокое слово-хвост («ИСТОРИЯ БКБ НЕ | НАЖАЛ») читается хуже, чем 2 + 2
    for a, b in zip(groups, groups[1:]):
        if (len(b) == 1 and len(a) >= 3 and a[-1].get("seg") == b[0].get("seg")
                and b[0]["start"] - a[-1]["end"] <= 0.6 and not re.search(r"[.!?…]$", a[-1]["raw"])
                and len(a[-1]["text"]) + 1 + len(b[0]["text"]) <= max_chars):
            b.insert(0, a.pop())
    return groups


def prepare_words(words: list[dict], scfg: dict, clip_start: float, dur: float) -> list[dict]:
    """Слова → то, что реально попадёт на экран (после replace, цензуры, склейки «что-то»)."""
    prepared = []
    for w in apply_replace(words, scfg.get("replace")):
        txt = _clean_word(w["word"], scfg)
        if not txt:
            continue
        if txt.startswith("-") and prepared and prepared[-1].get("seg") == w.get("seg"):
            # whisper режет «что-то» на «что» + «-то»
            prepared[-1]["text"] += txt
            prepared[-1]["raw"] += w["word"]
            prepared[-1]["end"] = min(dur, w["end"] - clip_start)
            continue
        prepared.append({"text": txt, "raw": w["word"], "seg": w.get("seg"), "p": w.get("p"),
                         "fixed": w.get("fixed", False),
                         "start": max(0.0, w["start"] - clip_start),
                         "end": min(dur, w["end"] - clip_start)})
    return prepared


def build_ass(words: list[dict], clip_start: float, clip_end: float, W: int, H: int,
              scfg: dict, tcfg: dict, title: str | None, subs_y: int, title_y: int,
              labels: list | None = None, label_y: int | None = None,
              cuts: list | None = None, tags: list | None = None, tag_y: int | None = None) -> str:
    """cuts — времена склеек в таймлайне клипа: строка субтитров не висит поверх следующей сцены."""
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
        f"Style: Label,{tcfg['font']},{int(tcfg['size'] * 2)},{ass_color(scfg['highlight'])},&H000000FF,"
        f"{ass_color('#000000')},{ass_color('#000000', 0x60)},0,0,0,0,100,100,0,0,1,"
        f"8,3,5,60,60,0,1",
        # тег «кто говорит» (сегмент who:): мелко, белым по полупрозрачной плашке
        f"Style: Who,{tcfg['font']},{int(scfg['size'] * 0.55)},{ass_color('#FFFFFF')},&H000000FF,"
        f"{ass_color('#000000', 0x50)},{ass_color('#000000', 0x50)},0,0,0,0,100,100,0,0,3,"
        f"10,0,5,60,60,0,1",
        "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    if title and tcfg.get("enabled", True):
        t = title.upper() if tcfg.get("uppercase") else title
        t_end = dur if not tcfg.get("duration") else min(dur, float(tcfg["duration"]))
        anim = r"\t(0,180,\fscx108\fscy108)\t(180,320,\fscx100\fscy100)"
        lines.append(f"Dialogue: 1,{_ts(0)},{_ts(t_end)},Title,,0,0,0,,"
                     f"{{\\an5\\pos({W // 2},{title_y}){anim}}}{_ml(t)}")
    for (a, b, text) in labels or []:
        t = text.upper() if tcfg.get("uppercase") else text
        anim = r"\fad(80,120)\t(0,150,\fscx110\fscy110)\t(150,280,\fscx100\fscy100)"
        lines.append(f"Dialogue: 2,{_ts(a)},{_ts(b)},Label,,0,0,0,,"
                     f"{{\\an5\\pos({W // 2},{label_y}){anim}}}{_ml(t)}")

    for (a, b, text) in tags or []:
        lines.append(f"Dialogue: 2,{_ts(a)},{_ts(b)},Who,,0,0,0,,"
                     f"{{\\an5\\pos({W // 2},{tag_y})\\fad(60,60)}}{_ml(text)}")

    if scfg.get("enabled", True) and words:
        prepared = prepare_words(words, scfg, clip_start, dur)
        hl = ass_color(scfg["highlight"])
        pop = r"\fscx112\fscy112" if scfg.get("pop") else ""
        groups = group_words(prepared, scfg["max_words"], scfg["max_chars"])
        cuts = sorted(cuts or [])
        for gi, g in enumerate(groups):
            g_end = g[-1]["end"] + 0.25
            if gi + 1 < len(groups):
                g_end = min(g_end, groups[gi + 1][0]["start"])
            nxt = next((c for c in cuts if c > g[-1]["start"] + 0.01), None)
            if nxt is not None:
                g_end = min(g_end, nxt)
            g_end = min(g_end, dur)
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


def ass_text(ass: str) -> list[tuple[str, str]]:
    """Текст субтитров из готового .ass: [(время, строка)] — для вычитки глазами."""
    out, last = [], None
    for line in ass.splitlines():
        if not line.startswith("Dialogue:"):
            continue
        f = line.split(",", 9)
        if len(f) < 10 or f[3] != "Sub":
            continue
        text = re.sub(r"\{[^}]*\}", "", f[9]).strip()
        if text != last:
            out.append((f[1], text))
            last = text
    return out
