"""Скачивание видео (yt-dlp) и чата (YouTube live chat / Twitch)."""
import json
import re
import shutil
import subprocess
import sys
import time
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
        if start_s or end_s:
            print("  ! --from/--to для локального файла не применяются — файл подключён целиком")
            meta["offset"] = 0.0
    elif "twitch.tv" in src and (start_s is not None or end_s is not None) and \
            _twitch_section(src, pdir, start_s or 0.0, end_s, max_height, meta, extra):
        pass  # кусок VOD собран из фрагментов HLS параллельно (см. _twitch_section)
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
        # решатель n-challenge YouTube (EJS) не входит в pip-пакет yt-dlp: без него остаются
        # только картинки («Requested format is not available»)
        if "youtu" in src:
            cmd += ["--remote-components", "ejs:github"]
        if cookies:
            cmd += ["--cookies", cookies]
        if not no_chat and "twitch.tv" not in src:
            cmd += ["--write-subs", "--sub-langs", "live_chat"]
        cmd += list(extra or [])
        cmd.append(src)
        print("→ скачиваю:", src)
        try:
            run(cmd)
        except subprocess.CalledProcessError:
            # YouTube часто режет DASH-поток с серверных IP (403 после ~20–30 МБ),
            # а HLS (m3u8) с тем же качеством отдаёт. Пробуем его.
            if "youtu" not in src:
                raise
            print("→ DASH-поток оборвался, пробую HLS (m3u8)")
            for p in pdir.glob("source.f*.part"):
                p.unlink()
            hls = (f"b[protocol^=m3u8][height<={max_height}]"
                   f"/bv*[protocol^=m3u8][height<={max_height}]+ba")
            # m3u8-форматы YouTube отдаёт не на каждый запрос (после 429/403 на странице
            # список бывает без них → «Requested format is not available»). Повторяем.
            for attempt in range(4):
                try:
                    run([hls if c == fmt else c for c in cmd])
                    break
                except subprocess.CalledProcessError:
                    if attempt == 3:
                        raise
                    print(f"→ HLS не отдали, повтор {attempt + 2}/4 через 20 с")
                    time.sleep(20)
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
            n = download_twitch_chat(src, pdir / "chat.jsonl", meta["offset"] or start_s, end_s)
        print(f"→ сообщений чата: {n}" if n else "→ чат не найден (это ок, анализ будет по звуку и речи)")

    from .util import find_source
    info = probe(find_source(pdir))
    meta["duration"] = info["duration"]
    meta["resolution"] = f'{info["width"]}x{info["height"]}'
    write_json(pdir / "meta.json", meta)
    print(f"✓ проект: {name}  ({fmt_time(info['duration'])}, {meta['resolution']})  → {pdir}")
    return name


def _twitch_section(src: str, pdir: Path, start: float, end: float | None, max_height: int,
                    meta: dict, extra: list | None) -> bool:
    """Кусок Twitch VOD: качаем только нужные фрагменты HLS параллельно и склеиваем без перекодирования.

    `yt-dlp --download-sections` отдаёт такой кусок ffmpeg-у, а тот тянет фрагменты по одному
    (~0.5 МБ/с: 40 минут 1080p60 — больше часа). Параллельно — десятки секунд.
    Начало округляется вниз до границы фрагмента (~10 с); точный сдвиг пишется в meta["offset"].
    Не вышло — возвращаем False, и fetch идёт обычным путём через yt-dlp.
    """
    import concurrent.futures as cf
    import urllib.parse
    if not shutil.which("curl"):
        return False
    try:
        out = subprocess.run([sys.executable, "-m", "yt_dlp", "--print", "title", "--print", "urls",
                              "-f", f"b[height<={max_height}]", *(extra or []), src],
                             capture_output=True, text=True, check=True).stdout.strip().splitlines()
        title, url = out[0], out[-1]
        playlist = subprocess.run(["curl", "-sSfL", "--retry", "5", url], capture_output=True,
                                  text=True, check=True).stdout
    except (subprocess.CalledProcessError, IndexError) as e:
        print(f"  ! быстрый путь Twitch не сработал ({e}), качаю через yt-dlp")
        return False
    lines = playlist.splitlines()
    frags, t, first = [], 0.0, None
    for i, line in enumerate(lines):
        if line.startswith("#EXTINF:") and i + 1 < len(lines):
            d = float(line[8:].split(",")[0])
            if t + d > start and (end is None or t < end):
                if first is None:
                    first = t
                frags.append(urllib.parse.urljoin(url, lines[i + 1].strip()))
            t += d
    if not frags:
        return False
    tmp = pdir / "_fragments"
    tmp.mkdir(exist_ok=True)
    print(f"→ Twitch: {len(frags)} фрагментов HLS с {fmt_time(first)}, качаю параллельно")

    def get(item):
        n, u = item
        out = tmp / f"{n:05d}.ts"
        if not out.exists() or out.stat().st_size == 0:
            subprocess.run(["curl", "-sSfL", "--retry", "5", "-o", str(out), u], check=True)
        return out

    try:
        with cf.ThreadPoolExecutor(16) as ex:
            parts = list(ex.map(get, enumerate(frags)))
    except subprocess.CalledProcessError as e:
        print(f"  ! фрагмент не скачался ({e}), качаю через yt-dlp")
        shutil.rmtree(tmp, ignore_errors=True)
        return False
    lst = tmp / "list.txt"
    lst.write_text("".join(f"file '{p.resolve()}'\n" for p in parts), encoding="utf-8")
    run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", lst,
         "-c", "copy", "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart", "-y", pdir / "source.mp4"])
    shutil.rmtree(tmp, ignore_errors=True)
    meta.update(offset=first, url=src, title=title)
    print(f"  начало куска сдвинуто к границе фрагмента: {fmt_time(first)} (offset в meta.json)")
    return True


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
