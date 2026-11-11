"""Скачивание видео (yt-dlp) и чата (YouTube live chat / Twitch)."""
import json
import re
import shutil
import sys
from pathlib import Path

from .util import die, fmt_time, parse_time, probe, project_dir, read_json, run, write_json


def slugify(text: str) -> str:
    s = re.sub(r"[^\w\-]+", "-", text.lower(), flags=re.U).strip("-")
    return s[:50] or "video"


def fetch(cfg: dict, src: str, name: str | None, max_height: int, start=None, end=None,
          cookies: str | None = None, no_chat: bool = False, extra: list | None = None):
    is_local = Path(src).exists()
    name = name or slugify(Path(src).stem if is_local else src.rstrip("/").split("/")[-1].split("=")[-1])
    pdir = project_dir(cfg, name)
    pdir.mkdir(parents=True, exist_ok=True)
    start_s = parse_time(start) if start else None
    end_s = parse_time(end) if end else None
    meta = {"source": src, "offset": start_s or 0.0}

    if is_local:
        dst = pdir / f"source{Path(src).suffix.lower()}"
        if not dst.exists():
            try:
                dst.symlink_to(Path(src).resolve())
            except OSError:
                shutil.copy2(src, dst)
        meta["title"] = Path(src).stem
    else:
        fmt = f"bv*[height<={max_height}]+ba/b[height<={max_height}]"
        cmd = [sys.executable, "-m", "yt_dlp", "-f", fmt, "--merge-output-format", "mp4",
               "-o", str(pdir / "source.%(ext)s"), "--write-info-json", "--no-playlist",
               "--retries", "10", "--fragment-retries", "10", "--concurrent-fragments", "4"]
        if start_s is not None or end_s is not None:
            a = fmt_time(start_s or 0)
            b = fmt_time(end_s) if end_s is not None else "inf"
            cmd += ["--download-sections", f"*{a}-{b}", "--force-keyframes-at-cuts"]
        # yt-dlp по умолчанию включает только deno; если его нет — подсунуть node
        if not shutil.which("deno") and shutil.which("node"):
            cmd += ["--js-runtimes", f"node:{shutil.which('node')}"]
        if cookies:
            cmd += ["--cookies", cookies]
        if not no_chat and "twitch.tv" not in src:
            cmd += ["--write-subs", "--sub-langs", "live_chat"]
        cmd += list(extra or [])
        cmd.append(src)
        print("→ скачиваю:", src)
        run(cmd)
        info_p = pdir / "source.info.json"
        if info_p.exists():
            info = read_json(info_p)
            meta.update(title=info.get("title"), uploader=info.get("uploader"),
                        full_duration=info.get("duration"), url=info.get("webpage_url"))

    if not is_local and not no_chat:
        n = 0
        yt_chat = pdir / "source.live_chat.json"
        if yt_chat.exists():
            n = convert_youtube_chat(yt_chat, pdir / "chat.jsonl", meta["offset"])
        elif "twitch.tv" in src:
            n = download_twitch_chat(src, pdir / "chat.jsonl", start_s, end_s)
        print(f"→ сообщений чата: {n}" if n else "→ чат не найден (это ок, анализ будет по звуку и речи)")

    from .util import find_source
    info = probe(find_source(pdir))
    meta["duration"] = info["duration"]
    meta["resolution"] = f'{info["width"]}x{info["height"]}'
    write_json(pdir / "meta.json", meta)
    print(f"✓ проект: {name}  ({fmt_time(info['duration'])}, {meta['resolution']})  → {pdir}")
    return name


def _runs_text(runs) -> str:
    out = []
    for r in runs or []:
        if "text" in r:
            out.append(r["text"])
        elif "emoji" in r:
            e = r["emoji"]
            sc = (e.get("shortcuts") or [None])[0]
            out.append(sc or e.get("emojiId", ""))
    return "".join(out)


def convert_youtube_chat(src: Path, dst: Path, offset: float) -> int:
    n = 0
    with open(src, encoding="utf-8") as f, open(dst, "w", encoding="utf-8") as out:
        for line in f:
            try:
                obj = json.loads(line)
                rep = obj.get("replayChatItemAction", {})
                t = int(rep.get("videoOffsetTimeMsec", 0)) / 1000 - offset
                for act in rep.get("actions", []):
                    item = act.get("addChatItemAction", {}).get("item", {})
                    r = item.get("liveChatTextMessageRenderer") or item.get("liveChatPaidMessageRenderer")
                    if not r:
                        continue
                    text = _runs_text(r.get("message", {}).get("runs"))
                    if t >= 0:
                        out.write(json.dumps({"t": round(t, 2), "text": text}, ensure_ascii=False) + "\n")
                        n += 1
            except (ValueError, AttributeError):
                continue
    return n


def download_twitch_chat(url: str, dst: Path, start=None, end=None) -> int:
    try:
        from chat_downloader import ChatDownloader
    except ImportError:
        print("  (для чата Twitch: pip install chat-downloader)")
        return 0
    n = 0
    try:
        kw = {}
        if start is not None:
            kw["start_time"] = start
        if end is not None:
            kw["end_time"] = end
        chat = ChatDownloader().get_chat(url, **kw)
        with open(dst, "w", encoding="utf-8") as out:
            for m in chat:
                t = m.get("time_in_seconds")
                if t is None:
                    continue
                t -= start or 0
                out.write(json.dumps({"t": round(t, 2), "text": m.get("message", "")}, ensure_ascii=False) + "\n")
                n += 1
    except Exception as e:  # библиотека неофициальная и иногда ломается
        print(f"  не удалось скачать чат Twitch: {e}")
    return n
