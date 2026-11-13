"""Проверка плана до рендера (check) и готовых клипов после (review).

check ловит то, за что пользователь ругал прошлые версии: обрубки без начала и конца, срез
посреди слова или фразы, «воздух» внутри, запоздалый хук, однообразную подборку, подписи по
запрещённым формулам. Смысл шутки и правдивость подписи он не проверит — это делаешь ты."""
import re
import tempfile
from collections import Counter
from pathlib import Path

from .render import clip_config, clip_segments, clip_words, latest_output, load_plan, safe_id, snap_segments
from .subs import ass_text, prepare_words
from .util import FONTS_DIR, deep_merge, find_source, fmt_time, probe, read_json, run

_END = re.compile(r"[.!?…]$")

# Формулы подписей, которые пользователь забраковал (см. CLAUDE.md, «Подпись»)
BAD_TITLE = [
    (r"^когда\b", "«когда …» — разжёвывание, формула забракована"),
    (r"\b(смотрите|смотри|зацените|вы не поверите|шок)\b", "рассказчик-дед / кликбейт"),
    (r"^и вот\b", "«и вот …» — рассказчик-дед"),
    (r"=\s*(любовь|имба|смерть)", "«X + Y = …» — натянутая формула"),
    (r"по мечте", "каламбур на известное название — кринж"),
    (r":$", "обрывается двоеточием — размыто, без зацепки"),
]


def _source_words(pdir: Path) -> list[dict] | None:
    p = pdir / "transcript.json"
    if not p.exists():
        return None
    return [w for s in read_json(p)["segments"] for w in s.get("words") or []]


def _around(all_words, t: float, before: bool, n: int = 4) -> list[dict]:
    if before:
        return [w for w in all_words if w["end"] <= t + 0.05][-n:]
    return [w for w in all_words if w["start"] >= t - 0.05][:n]


def check_clip(cfg: dict, pdir: Path, clip: dict, src_words: list[dict] | None, src_dur: float,
               ccfg_check: dict, asr: bool) -> dict:
    ccfg = clip_config(cfg, clip)
    fps = float(ccfg["output"]["fps"])
    segs = snap_segments(clip_segments(clip), fps, src_dur, clip.get("id"))
    total = sum(g["out"] for g in segs)
    warn, info = [], []
    lim = ccfg_check

    # --- слова клипа: те же, что возьмёт render (общий кэш распознавания)
    words, can_words = [], True
    try:
        if asr:                       # ровно те слова, что будут в субтитрах (clip_model, кэш общий с render)
            words = clip_words(ccfg, pdir, clip, segs)
        elif src_words is not None:   # быстро: общий транскрипт, тяжёлую модель не запускаем
            words = clip_words(deep_merge(ccfg, {"whisper": {"clip_model": None}}), pdir, clip, segs)
            if ccfg["whisper"].get("clip_model"):
                info.append("слова из общего транскрипта (как в субтитрах, точной моделью — ключ --asr)")
        else:
            can_words = False
            info.append("нет транскрипта — границы по словам не проверены (transcribe или --asr)")
    except Exception as e:  # нет модели/сети — проверяем без слов
        can_words = False
        warn.append(f"не удалось получить слова: {e}")

    scfg = dict(ccfg["subs"], uppercase=False)
    shown = prepare_words(words, scfg, 0.0, total) if words else []
    text_by_seg = []
    for i, g in enumerate(segs):
        t = " ".join(w["text"] for w in shown if w["seg"] == i)
        text_by_seg.append(t)

    # --- длина
    if total < lim["min_len"]:
        warn.append(f"длина {total:.1f} с — короче {lim['min_len']} с: клип не обрезан? добавь, что было до "
                    f"(повод, расклад), реакцию после, второй бит той же темы из другой части видео — "
                    f"короткий клип только если добавить правда нечего")
    elif total > lim["max_len"]:
        warn.append(f"длина {total:.1f} с — длиннее {lim['max_len']} с: держит ли внимание всю длину?")

    # --- история одной фразой
    story = str(clip.get("story") or "")
    if not story:
        warn.append("нет поля story: напиши «клип о том, как … и в итоге …» — без «в итоге» клип не закончен")
    elif "в итоге" not in story.lower():
        warn.append("в story нет «в итоге …» — чем всё кончилось? Нет развязки — расширь границы или не бери")
    if not clip.get("format"):
        info.append("нет поля format (формат клипа из каталога) — подборку на разнообразие не проверить")

    # --- первая секунда и концовка
    if shown:
        first, last = shown[0], shown[-1]
        if first["start"] > lim["hook_delay"]:
            src_t = segs[0]["a"] + first["start"] * segs[0]["speed"]
            warn.append(f"первое слово только на {first['start']:.1f} с — хук запаздывает. "
                        f"Начни с {fmt_time(max(segs[0]['a'], src_t - 0.15))} "
                        f"(если до слов не показывается причина — момент в игре)")
        tail = total - last["end"]
        if last["seg"] != len(segs) - 1:
            pass  # клип кончается сегментом без слов (кадр-развязка, реакция) — хвост не меряем
        elif tail > lim["tail_max"]:
            warn.append(f"после последнего слова {tail:.1f} с хвоста — не тяни после панчлайна "
                        f"(оставь {lim['tail_min']}–{lim['tail_keep']} с реакции), если там не смех/реакция/результат на экране")
        elif tail < lim["tail_min"] and segs[-1]["subs"]:
            warn.append(f"после последнего слова {tail:.2f} с — конец обрублен, дай {lim['tail_min']}–{lim['tail_keep']} с воздуха")
        # воздух внутри сегментов
        for a, b in zip(shown, shown[1:]):
            if a["seg"] == b["seg"] and b["start"] - a["end"] > lim["air"]:
                g = segs[a["seg"]]
                off = sum(x["out"] for x in segs[:a["seg"]])
                s1 = g["a"] + (a["end"] - off) * g["speed"]
                s2 = g["a"] + (b["start"] - off) * g["speed"]
                warn.append(f"пауза {b['start'] - a['end']:.1f} с внутри: {fmt_time(s1)}–{fmt_time(s2)} "
                            f"(после «{a['text']}») — вырежи, если это не комедийная пауза и не действие на экране")
        low = [w for w in shown if w.get("p") is not None and w["p"] < lim["low_prob"] and not w.get("fixed")]
        if low:
            info.append("слова, в которых модель не уверена (сверь на слух → subs.replace / subs_skip): "
                        + ", ".join(f"{w['text']}({w['p']:.2f})" for w in low[:12]))
    elif words == [] and can_words and any(g["subs"] for g in segs):
        info.append("в клипе нет распознанных слов — проверь, что это задумано (хайлайт без речи?)")

    # --- срезы: посреди слова / посреди фразы
    if src_words:
        for i, g in enumerate(segs):
            if not g["subs"]:
                continue
            for t, side in ((g["a"], "начало"), (g["b"], "конец")):
                cut = next((w for w in src_words if w["start"] + 0.06 < t < w["end"] - 0.06), None)
                if cut:
                    warn.append(f"сегмент {i + 1}: {side} {fmt_time(t)} режет слово «{cut['word']}» "
                                f"({fmt_time(cut['start'])}–{fmt_time(cut['end'])})")
            prev = _around(src_words, g["a"], True, 3)
            if i == 0 and prev and g["a"] - prev[-1]["end"] < lim["phrase_gap"] and not _END.search(prev[-1]["word"]):
                warn.append("начало с середины фразы: «… " + " ".join(w["word"] for w in prev)
                            + " ‖ " + " ".join(text_by_seg[0].split()[:4]) + " …» — начни с первой реплики бита")
            nxt = _around(src_words, g["b"], False, 3)
            inside = [w for w in src_words if g["a"] <= (w["start"] + w["end"]) / 2 < g["b"]]
            if (i == len(segs) - 1 and nxt and inside and nxt[0]["start"] - g["b"] < lim["phrase_gap"]
                    and not _END.search(inside[-1]["word"])):
                warn.append("конец на середине фразы: «… " + " ".join(text_by_seg[-1].split()[-4:])
                            + " ‖ " + " ".join(w["word"] for w in nxt) + " …» — доведи мысль до точки")

    # --- подпись
    title = str(clip.get("title") or "")
    if not title:
        info.append("нет подписи (title)")
    else:
        flat = title.replace("\n", " ").strip()
        nw = len(flat.split())
        if nw > 7:
            warn.append(f"подпись из {nw} слов — длинно (2–6 слов)")
        if flat.endswith("."):
            warn.append("подпись с точкой в конце")
        if flat.isupper() and len(flat) > 4:
            warn.append("подпись КАПСОМ")
        for rx, why in BAD_TITLE:
            if re.search(rx, flat.lower()):
                warn.append(f"подпись «{flat}»: {why}")
        td = ccfg["title"].get("duration")
        if not td or float(td) > 5:
            info.append("подпись висит дольше 5 с / весь клип — пользователю нравится 3–4 с")
        tw = set(re.findall(r"\w{4,}", flat.lower()))
        cw = [w["text"].lower() for w in shown]
        if nw >= 3 and tw and len(tw & set(cw)) >= max(3, len(tw) - 1):
            info.append("подпись почти целиком из слов клипа — это цитата героя; проверь, что в подборке не все подписи такие")

    return {"id": clip.get("id"), "dur": total, "segs": segs, "warn": warn, "info": info, "title": title,
            "format": clip.get("format"), "layout": clip.get("layout", "stream"), "story": story,
            "text": text_by_seg, "n_words": len(shown)}


def check(cfg: dict, pdir: Path, plan_path: str | None, only: list[str] | None, asr: bool = False) -> int:
    plan = load_plan(pdir, plan_path)
    pcfg = deep_merge(cfg, plan.get("defaults"))
    lim = {"min_len": 20, "max_len": 60, "hook_delay": 0.6, "tail_min": 0.25, "tail_keep": 0.8, "tail_max": 1.5,
           "air": 1.2, "phrase_gap": 0.35, "low_prob": 0.45, **(pcfg.get("check") or {})}
    clips = plan.get("clips") or []
    if only:
        clips = [c for c in clips if str(c.get("id")) in only]
    if not clips:
        print("в плане нет клипов")
        return 0
    src_words = _source_words(pdir)
    src_dur = probe(find_source(pdir))["duration"]
    res = [check_clip(pcfg, pdir, c, src_words, src_dur, lim, asr) for c in clips]

    L = []
    for r in res:
        segs = r["segs"]
        L.append(f"=== {r['id']}  {r['dur']:.1f} с  сегментов: {len(segs)}  {r['layout']}"
                 + (f"  [{r['format']}]" if r["format"] else ""))
        if r["title"]:
            L.append("  подпись: " + r["title"].replace("\n", " / "))
        if r["story"]:
            L.append("  история: " + r["story"])
        for i, (g, t) in enumerate(zip(segs, r["text"]), 1):
            L.append(f"  {i}. {fmt_time(g['a'])}–{fmt_time(g['b'])} ({g['out']:.1f} с)  {t or '—'}")
        flat = " ".join(t for t in r["text"] if t).split()
        if flat:
            L.append("  ХУК (первые слова):  " + " ".join(flat[:8]))
            L.append("  ФИНАЛ (последние):   " + " ".join(flat[-8:]))
        for w in r["warn"]:
            L.append("  ⚠ " + w)
        for w in r["info"]:
            L.append("  · " + w)
        L.append("")

    # --- подборка целиком
    S = []
    ids = [safe_id(r["id"] or "clip") for r in res]
    for d in sorted({i for i in ids if ids.count(i) > 1}):
        S.append(f"⚠ повторяется id {d}")
    if len(res) >= 3:
        fm = Counter(str(r["format"]).lower() for r in res if r["format"])
        for f, n in fm.items():
            if n >= 2:
                S.append(f"⚠ формат «{f}» у {n} клипов ({', '.join(str(r['id']) for r in res if str(r['format']).lower() == f)})"
                         " — два клипа одного формата только по просьбе или если материала мало")
        durs = [r["dur"] for r in res]
        if max(durs) - min(durs) < 8 and len(res) >= 4:
            S.append(f"⚠ все клипы почти одной длины ({min(durs):.0f}–{max(durs):.0f} с) — разнообразь")
        multi = sum(1 for r in res if len(r["segs"]) >= 4)
        if len(res) >= 6 and multi < 2:
            S.append(f"⚠ сложных клипов из многих кусков: {multi} — в подборке должно быть 2–3 (лучший формат по оценке пользователя)")
        if len(res) >= 6 and all(len(r["segs"]) >= 2 for r in res):
            S.append("⚠ вся подборка — склейки; добавь цельный законченный отрезок")
        lays = {r["layout"] for r in res} | {g["layout"] for r in res for g in r["segs"] if g["layout"]}
        if len(lays) == 1 and len(res) >= 6:
            S.append(f"· одна раскладка на всю подборку ({next(iter(lays))}) — нормально для видео без вебки, иначе разнообразь")
    for i, a in enumerate(res):          # одна сцена в двух клипах
        for b in res[i + 1:]:
            ov = sum(max(0.0, min(x["b"], y["b"]) - max(x["a"], y["a"])) for x in a["segs"] for y in b["segs"])
            if ov > 1.0:
                S.append(f"⚠ {a['id']} и {b['id']} делят {ov:.1f} с одной сцены — одна сцена не попадает в два клипа (кроме суперката)")
    heads = Counter(r["title"].split()[0].lower() for r in res if r["title"].split())
    for h, n in heads.items():
        if n >= 3:
            S.append(f"⚠ {n} подписи начинаются с «{h}» — однообразно")

    n_warn = sum(len(r["warn"]) for r in res) + sum(1 for x in S if x.startswith("⚠"))
    L.append(f"=== ПОДБОРКА: клипов {len(res)}, общая длина {sum(r['dur'] for r in res):.0f} с")
    L += ["  " + x for x in S] or ["  замечаний к составу нет"]
    L += ["", f"Итого предупреждений: {n_warn}. Это подсказки, а не приговор: комедийную паузу, реакцию без слов",
          "или действие на экране оставляй осознанно. Чего скрипт не видит — проверь сам:",
          "  □ шутку понял (в чём именно прикол, это подкол или факт?)",
          "  □ зритель без контекста поймёт, кто чего хочет, с первых секунд",
          "  □ есть результат: чем кончилась драка / спор / обещание",
          "  □ подпись — правда о том, что на экране (игровое действие сверено по кадрам с шагом 0.25–0.5 с)",
          "  □ подпись добавляет второй слой, а не пересказывает и не объясняет"]
    text = "\n".join(L)
    (pdir / "check.txt").write_text(text + "\n", encoding="utf-8")
    print(text)
    return n_warn


def review(cfg: dict, pdir: Path, plan_path: str | None, only: list[str] | None):
    """Готовые клипы: лист из 5 кадров (первый кадр, подпись, середина, финал) + текст субтитров."""
    plan = load_plan(pdir, plan_path)
    out_dir, fr_dir = pdir / "out", pdir / "frames"
    fr_dir.mkdir(exist_ok=True)
    font = str(FONTS_DIR / "Montserrat-ExtraBold.ttf").replace("\\", "\\\\").replace(":", "\\:")
    done = 0
    for c in plan.get("clips") or []:
        if only and str(c.get("id")) not in only:
            continue
        cid = safe_id(c.get("id", "clip"))
        f = latest_output(out_dir, pdir.name, cid)
        if not f:
            print(f"— {c.get('id')}: ещё не смонтирован")
            continue
        dur = probe(f)["duration"]
        times = [0.0, min(1.5, dur * 0.2), dur * 0.5, max(0.0, dur - 1.2), max(0.0, dur - 0.15)]
        names = ["1-й кадр", "подпись", "середина", "перед концом", "финал"]
        sheet = fr_dir / f"review_{cid}.jpg"
        with tempfile.TemporaryDirectory() as td:
            for i, (t, nm) in enumerate(zip(times, names)):
                run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", f, "-frames:v", "1",
                     "-vf", f"scale=360:-2,drawtext=fontfile='{font}':text='{nm} {t:.1f}s':x=8:y=8:"
                            f"fontsize=22:fontcolor=white:borderw=3", Path(td) / f"f{i}.png"])
            run(["ffmpeg", "-v", "error", "-y", "-framerate", "1", "-i", Path(td) / "f%d.png",
                 "-vf", "tile=5x1:padding=4:color=black", "-frames:v", "1", "-q:v", "3", sheet])
        print(f"=== {c.get('id')}  {dur:.1f} с  {f.name}")
        print(f"  кадры: {sheet}")
        if c.get("title"):
            print("  подпись: " + str(c["title"]).replace("\n", " / "))
        ass = out_dir / f"{cid}.ass"
        if ass.exists():
            rows = ass_text(ass.read_text(encoding="utf-8"))
            print("  субтитры: " + " | ".join(t for _, t in rows) if rows else "  субтитров нет")
        print()
        done += 1
    print(f"✓ листов: {done}. Открой каждый через Read: первый кадр цепляет? подпись читается и не "
          f"закрывает лицо? субтитры без ошибок? финальный кадр — точка, а не обрыв?")
