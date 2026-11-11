import copy
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
FONTS_DIR = ROOT / "fonts"


def die(msg: str):
    print(f"ошибка: {msg}", file=sys.stderr)
    sys.exit(1)


def deep_merge(base: dict, over: dict | None) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_config(path: Path | None = None) -> dict:
    path = path or ROOT / "config.yaml"
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def project_dir(cfg: dict, name: str) -> Path:
    base = Path(cfg.get("work_dir", "work"))
    if not base.is_absolute():
        base = ROOT / base
    return base / name


def find_source(pdir: Path) -> Path:
    for ext in ("mp4", "mkv", "webm", "mov"):
        p = pdir / f"source.{ext}"
        if p.exists():
            return p
    die(f"в {pdir} нет source.* — сначала `python -m clipper fetch`")


def parse_time(v) -> float:
    """'1:02:03.5' / '62:03' / '3723.5' / 3723 -> секунды."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return float(s)
    parts = s.split(":")
    if not 2 <= len(parts) <= 3:
        raise ValueError(f"не понимаю время: {v!r}")
    sec = 0.0
    for p in parts:
        sec = sec * 60 + float(p)
    return sec


def fmt_time(sec: float) -> str:
    sec = max(0.0, float(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{int(h)}:{int(m):02d}:{s:04.1f}"
    return f"{int(m)}:{s:04.1f}"


def run(cmd: list, **kw):
    kw.setdefault("check", True)
    return subprocess.run([str(c) for c in cmd], **kw)


def need(binary: str):
    if not shutil.which(binary):
        die(f"не найден {binary} — запусти ./setup.sh")


def probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    data = json.loads(out)
    v = next((s for s in data["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in data["streams"] if s["codec_type"] == "audio"), None)
    return {
        "duration": float(data["format"].get("duration", 0)),
        "width": int(v["width"]) if v else 0,
        "height": int(v["height"]) if v else 0,
        "has_audio": a is not None,
    }


def ass_color(hex_rgb: str, alpha: int = 0) -> str:
    h = hex_rgb.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def write_json(path: Path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))
