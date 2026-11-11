"""Кадры из исходника, чтобы посмотреть, что происходит в моменте, и найти координаты вебки."""
import tempfile
from pathlib import Path

from .util import FONTS_DIR, find_source, fmt_time, parse_time, probe, run

FONT = FONTS_DIR / "Montserrat-ExtraBold.ttf"


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def _grid_filter(w: int, h: int) -> str:
    f = [f"drawgrid=w={w / 10:.2f}:h={h / 10:.2f}:t=2:c=red@0.7"]
    fs = max(16, h // 40)
    for i in range(1, 10):
        v = f"0.{i}"
        f.append(f"drawtext=fontfile='{_esc(str(FONT))}':text='{v}':x={w * i / 10 + 4:.0f}:y=4:"
                 f"fontsize={fs}:fontcolor=yellow:borderw=2")
        f.append(f"drawtext=fontfile='{_esc(str(FONT))}':text='{v}':x=4:y={h * i / 10 + 4:.0f}:"
                 f"fontsize={fs}:fontcolor=yellow:borderw=2")
    return ",".join(f)


def frames(pdir: Path, at=None, start=None, end=None, n: int = 6, span: float = 20.0,
           grid: bool = False, width: int = 640) -> Path:
    src = find_source(pdir)
    info = probe(src)
    out_dir = pdir / "frames"
    out_dir.mkdir(exist_ok=True)

    if grid:
        t = parse_time(at or start or 0)
        out = out_dir / f"grid_{int(t)}.jpg"
        run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", src, "-frames:v", "1",
             "-vf", _grid_filter(info["width"], info["height"]), "-q:v", "3", out])
        print(f"✓ {out}  (сетка через 0.1 — координаты в долях кадра для config.yaml)")
        return out

    if at is not None:
        c = parse_time(at)
        a, b = max(0.0, c - span / 2), min(info["duration"], c + span / 2)
    else:
        a, b = parse_time(start), parse_time(end)
    n = max(1, n)
    times = [a + (b - a) * (i + 0.5) / n for i in range(n)]
    cols = 3 if n > 4 else n if n < 3 else 2
    rows = (n + cols - 1) // cols
    with tempfile.TemporaryDirectory() as td:
        for i, t in enumerate(times):
            label = _esc(fmt_time(t))
            run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", src, "-frames:v", "1",
                 "-vf", f"scale={width}:-2,drawtext=fontfile='{_esc(str(FONT))}':text='{label}':"
                        f"x=8:y=8:fontsize={width // 18}:fontcolor=white:borderw=3",
                 Path(td) / f"f{i:03d}.png"])
        out = out_dir / f"sheet_{int(a)}_{int(b)}.jpg"
        pad = cols * rows - n
        vf = f"tile={cols}x{rows}:padding=4:color=black"
        run(["ffmpeg", "-v", "error", "-y", "-framerate", "1", "-i", Path(td) / "f%03d.png",
             "-vf", ("tpad=stop=" + str(pad) + ":stop_mode=add," if pad else "") + vf,
             "-frames:v", "1", "-q:v", "3", out])
    print(f"✓ {out}  ({n} кадров {fmt_time(a)}–{fmt_time(b)})")
    return out
