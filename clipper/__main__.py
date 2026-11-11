"""python -m clipper <команда> ...  —  см. README.md"""
import argparse
import sys

from .util import find_source, load_config, need, project_dir, die


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m clipper", description="Нарезка длинных видео в вертикальные клипы")
    p.add_argument("--config", help="путь к config.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="скачать видео (+чат) или подключить локальный файл")
    f.add_argument("source", help="ссылка YouTube/Twitch/... или путь к файлу")
    f.add_argument("--name", help="имя проекта (папка в work/)")
    f.add_argument("--max-height", type=int, default=1080)
    f.add_argument("--from", dest="start", help="скачать только кусок: начало (1:20:00)")
    f.add_argument("--to", dest="end", help="конец куска")
    f.add_argument("--cookies", help="cookies.txt для yt-dlp (если YouTube просит войти)")
    f.add_argument("--no-chat", action="store_true")
    f.add_argument("--ytdlp", default="", help='доп. аргументы yt-dlp одной строкой, напр. --ytdlp="--proxy socks5://..."')

    t = sub.add_parser("transcribe", help="распознать речь всего видео")
    t.add_argument("project")
    t.add_argument("--model")
    t.add_argument("--language")
    t.add_argument("--from", dest="start", help="распознать только окно: начало (быстрый режим)")
    t.add_argument("--to", dest="end", help="конец окна")

    a = sub.add_parser("analyze", help="найти кандидатов в хайлайты")
    a.add_argument("project")
    a.add_argument("--top", type=int)

    fr = sub.add_parser("frames", help="лист кадров вокруг момента / кадр с сеткой")
    fr.add_argument("project")
    fr.add_argument("--at", help="центр (1:02:30)")
    fr.add_argument("--from", dest="start")
    fr.add_argument("--to", dest="end")
    fr.add_argument("--span", type=float, default=20.0, help="ширина окна вокруг --at, сек")
    fr.add_argument("-n", type=int, default=6, help="сколько кадров")
    fr.add_argument("--grid", action="store_true", help="один полный кадр с сеткой координат")

    pv = sub.add_parser("preview", help="кадр готовой раскладки 9:16 (проверить вебку/субтитры)")
    pv.add_argument("project")
    pv.add_argument("--at", required=True)
    pv.add_argument("--layout", choices=["stream", "blur", "crop"])
    pv.add_argument("--plan")

    r = sub.add_parser("render", help="смонтировать клипы по clips.yaml")
    r.add_argument("project")
    r.add_argument("--plan", help="путь к плану (по умолчанию work/<проект>/clips.yaml)")
    r.add_argument("--only", nargs="+", help="только эти id")
    r.add_argument("--fast", action="store_true",
                   help="быстрый режим: субтитры моделью turbo (если в плане не задана другая), кодирование veryfast")

    args = p.parse_args(argv)
    from pathlib import Path
    cfg = load_config(Path(args.config) if args.config else None)
    need("ffmpeg")
    need("ffprobe")

    if args.cmd == "fetch":
        from .download import fetch
        import shlex
        extra = shlex.split(args.ytdlp)
        fetch(cfg, args.source, args.name, args.max_height, args.start, args.end,
              args.cookies, args.no_chat, extra)
        return

    pdir = project_dir(cfg, args.project)
    if not pdir.exists():
        die(f"нет проекта {pdir}")
    find_source(pdir)

    if args.cmd == "transcribe":
        if args.model:
            cfg["whisper"]["model"] = args.model
        if args.language:
            cfg["whisper"]["language"] = args.language
        from .transcribe import transcribe_project
        from .util import parse_time
        transcribe_project(cfg, pdir, parse_time(args.start) if args.start else None,
                           parse_time(args.end) if args.end else None)
    elif args.cmd == "analyze":
        from .analyze import analyze
        analyze(cfg, pdir, args.top)
    elif args.cmd == "frames":
        if not args.grid and not args.at and not (args.start and args.end):
            die("укажи --at или --from/--to")
        from .frames import frames
        frames(pdir, args.at, args.start, args.end, args.n, args.span, args.grid)
    elif args.cmd == "preview":
        from .render import preview
        preview(cfg, pdir, args.at, args.layout, args.plan)
    elif args.cmd == "render":
        from .render import render
        if args.fast:
            cfg["whisper"]["clip_model"] = cfg["whisper"].get("clip_model") or "turbo"
            cfg["whisper"]["fast"] = True
            cfg["output"]["preset"] = "veryfast"
        render(cfg, pdir, args.plan, args.only)


if __name__ == "__main__":
    sys.exit(main())
