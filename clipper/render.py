"""Монтаж вертикальных клипов 9:16 по плану clips.yaml."""
import re
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


def layout_filter(name: str, lcfg: dict, sw: int, sh: int, W: int, H: int) -> str:
    """Цепочка фильтров [vc] -> [lay]."""
    if name == "stream":
        top = _even(lcfg["top_height"])
        cw, ch, cx, cy = fit_rect(lcfg["cam"], sw, sh, W, top)
        gw, gh, gx, gy = fit_rect(lcfg["game"], sw, sh, W, H - top, lcfg.get("game_focus_x", 0.5))
        return (f"[vc]split=2[s1][s2];"
                f"[s1]crop={cw}:{ch}:{cx}:{cy},scale={W}:{top}:flags=lanczos,setsar=1[cam];"
                f"[s2]crop={gw}:{gh}:{gx}:{gy},scale={W}:{H - top}:flags=lanczos,setsar=1[game];"
                f"[cam][game]vstack=inputs=2[lay]")
    if name == "blur":
        z = float(lcfg.get("zoom", 1.0))
        d = float(lcfg.get("darken", 0.35))
        b = max(1, int(lcfg.get("blur", 30)) // 4)
        yo = int(lcfg.get("y_offset", 0))
        sw4, sh4 = _even(W / 4), _even(H / 4)
        if lcfg.get("height"):  # крупнее: масштаб по высоте, края по бокам обрезаются
            fg = (f"[s2]crop=iw/{z}:ih/{z},scale=-2:{_even(lcfg['height'])}:flags=lanczos,"
                  f"crop='min(iw,{W})':ih,setsar=1[fg];")
        else:
            fg = f"[s2]crop=iw/{z}:ih/{z},scale={W}:-2:flags=lanczos,setsar=1[fg];"
        return (f"[vc]split=2[s1][s2];"
                f"[s1]scale={sw4}:{sh4}:force_original_aspect_ratio=increase,crop={sw4}:{sh4},"
                f"boxblur={b}:2,scale={W}:{H},eq=brightness={-d:.2f},setsar=1[bg];"
                + fg +
                f"[bg][fg]overlay=(W-w)/2:(H-h)/2+{yo}[lay]")
    if name == "crop":
        w, h, x, y = fit_rect(lcfg.get("rect", [0, 0, 1, 1]), sw, sh, W, H, lcfg.get("focus_x", 0.5))
        return f"[vc]crop={w}:{h}:{x}:{y},scale={W}:{H}:flags=lanczos,setsar=1[lay]"
    die(f"неизвестная раскладка {name!r} (есть: stream, blur, crop)")


def clip_segments(clip: dict) -> list[tuple[float, float]]:
    if "segments" in clip:
        segs = [(parse_time(a), parse_time(b)) for a, b in clip["segments"]]
    else:
        segs = [(parse_time(clip["start"]), parse_time(clip["end"]))]
    for a, b in segs:
        if b <= a:
            die(f"клип {clip.get('id')}: конец {fmt_time(b)} раньше начала {fmt_time(a)}")
    return segs


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
    ccfg = deep_merge(cfg, {k: clip[k] for k in ("output", "layouts", "subs", "title", "whisper") if isinstance(clip.get(k), dict)})
    if clip.get("subs") is False:
        ccfg["subs"]["enabled"] = False
    o = ccfg["output"]
    W, H = int(o["width"]), int(o["height"])
    src = find_source(pdir)
    info = probe(src)
    sw, sh = info["width"], info["height"]
    layout = clip.get("layout", "stream")
    lcfg = ccfg["layouts"][layout]

    segs = clip_segments(clip)
    s0 = min(a for a, _ in segs)
    s1 = max(b for _, b in segs)
    total = sum(b - a for a, b in segs)

    # слова с пересчётом времени в таймлайн клипа
    words = []
    if ccfg["subs"].get("enabled", True):
        skip = [(parse_time(x), parse_time(y)) for x, y in clip.get("subs_skip", [])]
        off = 0.0
        for a, b in segs:
            raw = fake_words if fake_words is not None else words_in_range(ccfg, pdir, a, b)
            for w in raw:
                mid = (w["start"] + w["end"]) / 2
                if any(x <= mid < y for x, y in skip):
                    continue
                if a <= mid < b:
                    words.append({**w, "start": off + max(0.0, w["start"] - a), "end": off + min(b, w["end"]) - a})
            off += b - a
        words.sort(key=lambda w: w["start"])

    top = _even(lcfg.get("top_height", 0)) if layout == "stream" else 0
    subs_y = ccfg["subs"].get("y") or (top + 230 if layout == "stream" else int(H * 0.72))
    title_y = ccfg["title"].get("y") or (top if layout == "stream" else 300)

    cid = safe_id(clip.get("id", "clip"))
    out_dir.mkdir(parents=True, exist_ok=True)
    ass_name = f"{cid}.ass"
    (out_dir / ass_name).write_text(
        build_ass(words, 0.0, total, W, H, ccfg["subs"], ccfg["title"], clip.get("title"), subs_y, title_y),
        encoding="utf-8")

    has_a = info["has_audio"] and not preview_png
    # каждый сегмент — отдельный вход с быстрой перемоткой: не держим в памяти видео между кусками
    inputs, parts, concat_in = [], [], []
    for i, (a, b) in enumerate(segs):
        inputs += ["-ss", f"{a:.3f}", "-t", f"{b - a:.3f}", "-i", src.resolve()]
        parts.append(f"[{i}:v]setpts=PTS-STARTPTS[v{i}]")
        concat_in.append(f"[v{i}]")
        if has_a:
            parts.append(f"[{i}:a]asetpts=PTS-STARTPTS[a{i}]")
            concat_in.append(f"[a{i}]")
    parts.append("".join(concat_in) + f"concat=n={len(segs)}:v=1:a={1 if has_a else 0}"
                 + ("[vc][ac]" if has_a else "[vc]"))
    parts.append(layout_filter(layout, lcfg, sw, sh, W, H))
    fonts = str(FONTS_DIR).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
    parts.append(f"[lay]fps={o['fps']},format=yuv420p,ass={ass_name}:fontsdir='{fonts}'[v]")
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
        cmd += ["-t", f"{total:.3f}", "-movflags", "+faststart"]
        out = out_dir / f"{cid}.mp4"
        cmd.append(out.name)
    run(cmd, cwd=out_dir)
    return out


def render(cfg: dict, pdir: Path, plan_path: str | None, only: list[str] | None):
    plan = load_plan(pdir, plan_path)
    pcfg = deep_merge(cfg, plan.get("defaults"))
    clips = plan.get("clips") or []
    if only:
        clips = [c for c in clips if str(c.get("id")) in only]
    if not clips:
        die("в плане нет клипов (или --only ничего не выбрал)")
    out_dir = pdir / "out"
    for i, c in enumerate(clips, 1):
        segs = clip_segments(c)
        dur = sum(b - a for a, b in segs)
        print(f"[{i}/{len(clips)}] {c.get('id')}  {fmt_time(segs[0][0])}  {dur:.1f}s  ({c.get('layout', 'stream')})")
        out = render_clip(pcfg, pdir, c, out_dir)
        print(f"  ✓ {out}")
    print(f"✓ готово: {out_dir}")


def preview(cfg: dict, pdir: Path, at: str, layout: str | None, plan_path: str | None):
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
        clip = {"id": f"preview_{lay}", "start": t, "end": t + 1.0, "layout": lay, "title": "Пример заголовка"}
        outs.append(render_clip(pcfg, pdir, clip, pdir / "frames", preview_png=True, fake_words=fake))
    for o in outs:
        print(f"✓ {o}")
