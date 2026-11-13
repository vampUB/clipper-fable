"""Монтаж вертикальных клипов 9:16 по плану clips.yaml."""
import re
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .subs import build_ass
from .transcribe import words_in_range
from .util import FONTS_DIR, deep_merge, die, find_source, fmt_time, parse_time, probe, run


def _even(v: float) -> int:
    return max(2, int(round(v / 2)) * 2)


def fit_rect(rect, sw: int, sh: int, tw: int, th: int, focus_x=0.5, focus_y=0.5):
    """Подрезать прямоугольник (в долях кадра) под пропорции tw:th. Возвращает px (w, h, x, y)."""
    x, y, w, h = rect
    x, y, w, h = x * sw, y * sh, w * sw, h * sh
    ta = tw / th
    if w / h > ta:
        nw = h * ta
        x += (w - nw) * focus_x
        w = nw
    else:
        nh = w / ta
        y += (h - nh) * focus_y
        h = nh
    w, h = _even(w), _even(h)
    x = min(max(0, int(x)), sw - w)
    y = min(max(0, int(y)), sh - h)
    return w, h, x, y


def layout_filter(name: str, lcfg: dict, sw: int, sh: int, W: int, H: int,
                  src: str = "[vc]", dst: str = "[lay]", tag: str = "") -> str:
    """Цепочка фильтров src -> dst. tag — суффикс внутренних меток (раскладка на каждый сегмент)."""
    t = tag
    if name == "stream":
        top = _even(lcfg["top_height"])
        cw, ch, cx, cy = fit_rect(lcfg["cam"], sw, sh, W, top)
        gw, gh, gx, gy = fit_rect(lcfg["game"], sw, sh, W, H - top, lcfg.get("game_focus_x", 0.5))
        return (f"{src}split=2[s1{t}][s2{t}];"
                f"[s1{t}]crop={cw}:{ch}:{cx}:{cy},scale={W}:{top}:flags=lanczos,setsar=1[cam{t}];"
                f"[s2{t}]crop={gw}:{gh}:{gx}:{gy},scale={W}:{H - top}:flags=lanczos,setsar=1[game{t}];"
                f"[cam{t}][game{t}]vstack=inputs=2{dst}")
    if name == "blur":
        z = float(lcfg.get("zoom", 1.0))
        d = float(lcfg.get("darken", 0.35))
        b = max(1, int(lcfg.get("blur", 30)) // 4)
        yo = int(lcfg.get("y_offset", 0))
        sw4, sh4 = _even(W / 4), _even(H / 4)
        if lcfg.get("height"):  # крупнее: масштаб по высоте, края по бокам обрезаются
            fx = min(1.0, max(0.0, float(lcfg.get("focus_x", 0.5))))  # куда смотреть, если герой не в центре
            fg = (f"[s2{t}]crop=iw/{z}:ih/{z},scale=-2:{_even(lcfg['height'])}:flags=lanczos,"
                  f"crop='min(iw,{W})':ih:'(iw-ow)*{fx:.3f}':0,setsar=1[fg{t}];")
        else:
            fg = f"[s2{t}]crop=iw/{z}:ih/{z},scale={W}:-2:flags=lanczos,setsar=1[fg{t}];"
        return (f"{src}split=2[s1{t}][s2{t}];"
                f"[s1{t}]scale={sw4}:{sh4}:force_original_aspect_ratio=increase,crop={sw4}:{sh4},"
                f"boxblur={b}:2,scale={W}:{H},eq=brightness={-d:.2f},setsar=1[bg{t}];"
                + fg +
                f"[bg{t}][fg{t}]overlay=(W-w)/2:(H-h)/2+{yo}{dst}")
    if name == "crop":
        w, h, x, y = fit_rect(lcfg.get("rect", [0, 0, 1, 1]), sw, sh, W, H, lcfg.get("focus_x", 0.5))
        return f"{src}crop={w}:{h}:{x}:{y},scale={W}:{H}:flags=lanczos,setsar=1{dst}"
    die(f"неизвестная раскладка {name!r} (есть: stream, blur, crop)")


def clip_segments(clip: dict) -> list[dict]:
    """Сегменты клипа. Элемент — [начало, конец] или словарь
    {start, end, speed, zoom, focus: [x, y], subs, text, layout, cam, layouts}.
    layout/cam/layouts — своя раскладка на сегмент (вебка переехала, сцена сменилась)."""
    raw = clip["segments"] if "segments" in clip else [[clip["start"], clip["end"]]]
    segs = []
    for r in raw:
        if isinstance(r, dict):
            d = dict(r)
            a, b = parse_time(d.pop("start")), parse_time(d.pop("end"))
        else:
            a, b, d = parse_time(r[0]), parse_time(r[1]), {}
        if b <= a:
            die(f"клип {clip.get('id')}: конец {fmt_time(b)} раньше начала {fmt_time(a)}")
        speed = float(d.get("speed", 1.0))
        if not 0.25 <= speed <= 4:
            die(f"клип {clip.get('id')}: speed должен быть 0.25..4")
        segs.append({
            "a": a, "b": b, "speed": speed,
            "zoom": float(d.get("zoom", 1.0)),
            "focus": d.get("focus", [0.5, 0.5]),
            # в ускоренных/замедленных кусках субтитры по умолчанию выключены
            "subs": d.get("subs", speed == 1.0),
            "text": d.get("text"),
            "who": d.get("who"),          # маленький тег «кто говорит» над субтитрами
            "layout": d.get("layout"),
            "cam": d.get("cam"),
            "layouts": d.get("layouts") if isinstance(d.get("layouts"), dict) else None,
            "out": (b - a) / speed,
        })
    return segs


def snap_segments(segs: list[dict], fps: float, src_dur: float | None = None, cid=None) -> list[dict]:
    """Длину каждого сегмента — к целому числу кадров. Иначе видео и звук расходятся на долю
    кадра на каждой склейке, и в суперкате из 8 кусков субтитры заметно «уезжают»."""
    for g in segs:
        if src_dur and g["a"] >= src_dur:
            die(f"клип {cid}: сегмент {fmt_time(g['a'])} за концом видео ({fmt_time(src_dur)})")
        if src_dur and g["b"] > src_dur + 0.05:
            print(f"  ! клип {cid}: сегмент до {fmt_time(g['b'])} обрезан по концу видео ({fmt_time(src_dur)})")
            g["b"] = src_dur
        n = max(1, round((g["b"] - g["a"]) / g["speed"] * fps))
        g["out"] = n / fps
        g["b"] = g["a"] + g["out"] * g["speed"]
    return segs


def clip_words(ccfg: dict, pdir: Path, clip: dict, segs: list[dict], fake_words: list | None = None) -> list[dict]:
    """Слова клипа с временем в таймлайне клипа. У каждого слова seg — номер сегмента,
    src — время в исходнике (для subs_skip и отчёта check)."""
    skip = [(parse_time(x), parse_time(y)) for x, y in clip.get("subs_skip") or []]
    # близкие сегменты распознаём одним окном с запасом: короткому куску без контекста
    # модель слышит слова хуже
    spans = []
    for g in sorted(segs, key=lambda g: g["a"]):
        if g["subs"]:
            if spans and g["a"] - spans[-1][1] < 60:
                spans[-1][1] = max(spans[-1][1], g["b"])
            else:
                spans.append([g["a"], g["b"]])
    span_words = []
    if fake_words is None:
        for x, y in spans:
            span_words += words_in_range(ccfg, pdir, round(max(0.0, x - 5), 1), round(y + 5, 1))
    words, off = [], 0.0
    for i, g in enumerate(segs):
        a, b, sp = g["a"], g["b"], g["speed"]
        if g["subs"]:
            raw = fake_words if fake_words is not None else span_words
            for w in raw:
                mid = (w["start"] + w["end"]) / 2
                if any(x <= mid < y for x, y in skip):
                    continue
                if a <= mid < b:
                    words.append({**w, "seg": i, "src": w["start"],
                                  "start": off + max(0.0, w["start"] - a) / sp,
                                  "end": off + (min(b, w["end"]) - a) / sp})
        off += g["out"]
    words.sort(key=lambda w: (w["seg"], w["start"]))
    return words


def clip_config(cfg: dict, clip: dict) -> dict:
    """Настройки клипа: config.yaml + defaults плана (уже в cfg) + переопределения клипа."""
    over = {k: clip[k] for k in ("output", "layouts", "subs", "whisper") if isinstance(clip.get(k), dict)}
    if isinstance(clip.get("title_style"), dict):  # title — текст заголовка, title_style — его настройки
        over["title"] = clip["title_style"]
    ccfg = deep_merge(cfg, over)
    if clip.get("subs") is False:
        ccfg["subs"]["enabled"] = False
    return ccfg


def _atempo(speed: float) -> str:
    """atempo принимает 0.5..2 — раскладываем на цепочку."""
    parts = []
    while speed < 0.5:
        parts.append("atempo=0.5")
        speed /= 0.5
    while speed > 2.0:
        parts.append("atempo=2.0")
        speed /= 2.0
    parts.append(f"atempo={speed:.4f}")
    return ",".join(parts)


def load_plan(pdir: Path, plan: str | None) -> dict:
    p = Path(plan) if plan else pdir / "clips.yaml"
    if not p.exists():
        die(f"нет плана {p} — создай его (пример: clips.example.yaml)")
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def safe_id(s: str) -> str:
    return re.sub(r"[^\w\-]+", "_", str(s), flags=re.U).strip("_") or "clip"


def render_clip(cfg: dict, pdir: Path, clip: dict, out_dir: Path, preview_png: bool = False,
                fake_words: list | None = None) -> Path:
    ccfg = clip_config(cfg, clip)
    o = ccfg["output"]
    W, H = int(o["width"]), int(o["height"])
    src = find_source(pdir)
    info = probe(src)
    sw, sh = info["width"], info["height"]
    layout = clip.get("layout", "stream")
    if layout not in ccfg["layouts"]:
        die(f"клип {clip.get('id')}: неизвестная раскладка {layout!r}")
    lcfg = ccfg["layouts"][layout]
    fps = float(o["fps"])

    segs = snap_segments(clip_segments(clip), fps, None if fake_words is not None else info["duration"],
                         clip.get("id"))
    total = sum(g["out"] for g in segs)

    words = []
    if ccfg["subs"].get("enabled", True):
        words = clip_words(ccfg, pdir, clip, segs, fake_words)

    # надписи на отдельных сегментах
    labels, tags, off = [], [], 0.0
    for g in segs:
        if g["text"]:
            labels.append((off, off + g["out"], g["text"]))
        if g["who"]:
            tags.append((off, off + g["out"], g["who"]))
        off += g["out"]

    top = _even(lcfg.get("top_height", 0)) if layout == "stream" else 0
    subs_y = ccfg["subs"].get("y") or (top + 230 if layout == "stream" else int(H * 0.72))
    title_y = ccfg["title"].get("y") or (top if layout == "stream" else 300)

    cid = safe_id(clip.get("id", "clip"))
    out_dir.mkdir(parents=True, exist_ok=True)
    ass_name = f"{cid}.ass"
    (out_dir / ass_name).write_text(
        build_ass(words, 0.0, total, W, H, ccfg["subs"], ccfg["title"], clip.get("title"), subs_y, title_y,
                  labels=labels, label_y=ccfg["title"].get("label_y") or int(H * 0.5),
                  tags=tags, tag_y=ccfg["subs"].get("who_y") or subs_y - int(ccfg["subs"]["size"] * 1.3),
                  cuts=[sum(g["out"] for g in segs[:i]) for i in range(1, len(segs))]),
        encoding="utf-8")

    has_a = info["has_audio"] and not preview_png
    # каждый сегмент — отдельный вход с быстрой перемоткой: не держим в памяти видео между кусками
    inputs, parts, concat_in = [], [], []
    for i, g in enumerate(segs):
        a, b, sp, z = g["a"], g["b"], g["speed"], g["zoom"]
        # +0.2 с запаса на входе: точную длину отрезают trim/atrim ниже
        inputs += ["-ss", f"{a:.3f}", "-t", f"{b - a + 0.2:.3f}", "-i", src.resolve()]
        vf = f"[{i}:v]"
        if z > 1.0:  # резкий зум на сегмент (jump-zoom), focus — центр в долях кадра
            fx, fy = g["focus"]
            cw, ch = _even(sw / z), _even(sh / z)
            cx = min(max(0, int(fx * sw - cw / 2)), sw - cw)
            cy = min(max(0, int(fy * sh - ch / 2)), sh - ch)
            vf += f"crop={cw}:{ch}:{cx}:{cy},scale={sw}:{sh}:flags=lanczos,"
        # сразу в частоту кадров результата и ровно out секунд: раскладка не считает лишние кадры,
        # а длина видео совпадает со звуком кадр в кадр
        vf += (f"setpts=(PTS-STARTPTS)/{sp:.4f},fps={o['fps']},trim=duration={g['out']:.5f},"
               f"setpts=PTS-STARTPTS,setsar=1[v{i}]")
        parts.append(vf)
        g_layout = g["layout"] or layout
        g_lcfg = deep_merge(ccfg["layouts"][g_layout], (g["layouts"] or {}).get(g_layout))
        if g["cam"]:
            g_lcfg = dict(g_lcfg, cam=g["cam"])
        parts.append(layout_filter(g_layout, g_lcfg, sw, sh, W, H, f"[v{i}]", f"[l{i}]", f"_{i}"))
        concat_in.append(f"[l{i}]")
        if has_a:
            af = f"[{i}:a]asetpts=PTS-STARTPTS"
            if sp != 1.0:
                af += "," + _atempo(sp)
            af += f",aresample=48000,atrim=duration={g['out']:.5f},asetpts=PTS-STARTPTS"
            fd = min(float(o.get("audio_fade", 0.012) or 0), g["out"] / 4)
            if fd > 0:  # микро-фейд на каждой склейке: без него стык щёлкает
                af += f",afade=t=in:d={fd:.3f},afade=t=out:st={g['out'] - fd:.3f}:d={fd:.3f}"
            parts.append(af + f"[a{i}]")
            concat_in.append(f"[a{i}]")
    parts.append("".join(concat_in) + f"concat=n={len(segs)}:v=1:a={1 if has_a else 0}"
                 + ("[lay][ac]" if has_a else "[lay]"))
    fonts = str(FONTS_DIR).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
    pb = ccfg["output"].get("progress_bar")
    vtail = "[vs]" if pb else "[v]"
    safe = ""
    if preview_png and clip.get("_safe"):
        # зоны, которые закрывает интерфейс TikTok/Shorts/Reels: шапка, колонка кнопок, описание
        z = ccfg.get("safe_zone") or {}
        st, sb, sr = int(z.get("top", 130)), int(z.get("bottom", 380)), int(z.get("right", 150))
        safe = (f",drawbox=0:0:{W}:{st}:red@0.28:fill,drawbox=0:{H - sb}:{W}:{sb}:red@0.28:fill,"
                f"drawbox={W - sr}:{st}:{sr}:{H - st - sb}:red@0.28:fill")
    parts.append(f"[lay]fps={o['fps']},format=yuv420p,ass={ass_name}:fontsdir='{fonts}'{safe}{vtail}")
    if pb:  # полоска прогресса внизу
        ph = int(pb.get("height", 12)) if isinstance(pb, dict) else 12
        pc = (pb.get("color") if isinstance(pb, dict) else None) or ccfg["subs"]["highlight"]
        parts.append(f"color=c={pc.replace('#', '0x')}:s={W}x{ph}:r={o['fps']}[pbar]")
        parts.append(f"[vs][pbar]overlay=x='-w+w*t/{total:.3f}':y={H - ph}:shortest=1,format=yuv420p[v]")
    if has_a:
        parts.append("[ac]" + ("loudnorm=I=-14:TP=-1.5:LRA=11," if o.get("loudnorm") else "")
                     + "aresample=48000[a]")

    cmd = ["ffmpeg", "-v", "error", *([] if preview_png else ["-stats"]), "-y", *inputs, "-filter_complex", ";".join(parts), "-map", "[v]"]
    if preview_png:
        out = out_dir / f"{cid}.png"
        cmd += ["-ss", "0.5", "-frames:v", "1", out.name]
    else:
        if has_a:
            cmd += ["-map", "[a]", "-c:a", "aac", "-b:a", o.get("audio_bitrate", "160k")]
        codec = o.get("codec", "libx264")
        cmd += ["-c:v", codec]
        if "nvenc" in codec:
            cmd += ["-preset", "p5", "-rc", "vbr", "-cq", str(o["crf"]), "-b:v", "0"]
        else:
            cmd += ["-preset", o.get("preset", "medium"), "-crf", str(o["crf"])]
        now = datetime.now(timezone.utc)
        cmd += ["-t", f"{total:.3f}", "-movflags", "+faststart",
                "-map_metadata", "-1", "-metadata", f"creation_time={now.strftime('%Y-%m-%dT%H:%M:%SZ')}"]
        # Уникальное имя: проект + id + время рендера. Повторная заливка файла с тем же
        # именем выглядит как дубликат, а так каждая версия клипа — новый файл.
        out = out_dir / f"{pdir.name}_{cid}_{now.strftime('%y%m%d-%H%M%S')}.mp4"
        cmd.append(out.name)
    run(cmd, cwd=out_dir)
    if not preview_png:  # старые версии этого клипа больше не нужны
        # только версии ЭТОГО клипа: <проект>_<id>_<ГГММДД-ЧЧММСС>.mp4 (а не «01» ↔ «01_b»)
        stamp = re.compile(re.escape(f"{pdir.name}_{cid}_") + r"\d{6}-\d{6}\.mp4$")
        for old in [*(p for p in out_dir.glob(f"{pdir.name}_{cid}_*.mp4") if stamp.match(p.name)),
                    out_dir / f"{cid}.mp4"]:
            if old.exists() and old != out:
                old.unlink()
    return out


def latest_output(out_dir: Path, project: str, cid: str) -> Path | None:
    stamp = re.compile(re.escape(f"{project}_{cid}_") + r"\d{6}-\d{6}\.mp4$")
    files = [p for p in out_dir.glob(f"{project}_{cid}_*.mp4") if stamp.match(p.name)]
    return max(files, key=lambda p: p.name) if files else None


def write_report(pdir: Path, plan: dict, out_dir: Path) -> Path:
    """out/report.md — готовый отчёт: файл, длина, формат, о чём клип, подпись, описание, хэштеги.
    Поля format / story / desc / tags / title_alt в clips.yaml на монтаж не влияют — они для отчёта."""
    lines = [f"# {pdir.name}: готовые клипы", ""]
    for c in plan.get("clips") or []:
        cid = safe_id(c.get("id", "clip"))
        f = latest_output(out_dir, pdir.name, cid)
        if not f:
            continue
        dur = probe(f)["duration"]
        lines.append(f"## {c.get('id')} — {dur:.1f} с" + (f" · {c['format']}" if c.get("format") else ""))
        lines.append(f"- файл: `{f.name}`")
        if c.get("title"):
            lines.append("- подпись: " + str(c["title"]).replace("\n", " / "))
        alt = c.get("title_alt")
        if alt:
            lines.append("- варианты подписи: " + "; ".join(alt if isinstance(alt, list) else [str(alt)]))
        if c.get("story"):
            lines.append(f"- о чём: {c['story']}")
        if c.get("desc"):
            lines.append(f"- описание: {c['desc']}")
        tags = c.get("tags")
        if tags:
            tags = tags if isinstance(tags, list) else str(tags).split()
            lines.append("- хэштеги: " + " ".join(t if str(t).startswith("#") else f"#{t}" for t in tags))
        lines.append("")
    p = out_dir / "report.md"
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def render(cfg: dict, pdir: Path, plan_path: str | None, only: list[str] | None):
    plan = load_plan(pdir, plan_path)
    pcfg = deep_merge(cfg, plan.get("defaults"))
    clips = plan.get("clips") or []
    ids = [safe_id(c.get("id", "clip")) for c in clips]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        die(f"в плане повторяются id: {', '.join(dup)} — клипы затрут друг друга")
    if only:
        known = {str(c.get("id")) for c in clips}
        for x in only:
            if x not in known:
                print(f"  ! --only {x}: такого id в плане нет")
        clips = [c for c in clips if str(c.get("id")) in only]
    if not clips:
        die("в плане нет клипов (или --only ничего не выбрал)")
    out_dir = pdir / "out"
    for i, c in enumerate(clips, 1):
        segs = clip_segments(c)
        dur = sum(g["out"] for g in segs)
        print(f"[{i}/{len(clips)}] {c.get('id')}  {fmt_time(segs[0]['a'])}  {dur:.1f}s  ({c.get('layout', 'stream')})")
        out = render_clip(pcfg, pdir, c, out_dir)
        print(f"  ✓ {out}")
    rep_p = write_report(pdir, plan, out_dir)
    print(f"✓ готово: {out_dir}  (отчёт: {rep_p.name}; проверка кадров: python -m clipper review {pdir.name})")


def preview(cfg: dict, pdir: Path, at: str, layout: str | None, plan_path: str | None, safe: bool = False):
    """Один кадр готовой раскладки с примером заголовка и субтитров — проверить координаты."""
    pcfg = cfg
    p = Path(plan_path) if plan_path else pdir / "clips.yaml"
    if p.exists():
        pcfg = deep_merge(cfg, (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("defaults"))
    t = parse_time(at)
    layouts = [layout] if layout else ["stream", "blur", "crop"]
    fake = [{"start": t + i * 0.3, "end": t + i * 0.3 + 0.3, "word": w}
            for i, w in enumerate(["пример", "субтитров", "тут"])]
    outs = []
    for lay in layouts:
        clip = {"id": f"preview_{lay}", "start": t, "end": t + 1.0, "layout": lay, "title": "Пример заголовка",
                "_safe": safe}
        outs.append(render_clip(pcfg, pdir, clip, pdir / "frames", preview_png=True, fake_words=fake))
    for o in outs:
        print(f"✓ {o}")
