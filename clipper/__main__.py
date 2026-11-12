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

    a = sub.add_parser("analyze", help="найти кандидатов в хайлайты")
    a.add_argument("project")
    a.add_argument("--top", type=int)

    tk = sub.add_parser("talk", help="разговорные моменты по транскрипту: блоки, повторы, сетапы")
    tk.add_argument("project")
    tk.add_argument("--top", type=int)

    fr = sub.add_parser("frames", help="лист кадров вокруг момента / кадр с сеткой")
    fr.add_argument("project")
    fr.add_argument("--at", help="центр (1:02:30)")
    fr.add_argument("--from", dest="start")
    fr.add_argument("--to", dest="end")
    fr.add_argument("--span", type=float, default=20.0, help="ширина окна вокруг --at, сек")
    fr.add_argument("-n", type=int, default=6, help="сколько кадров")
    fr.add_argument("--step", type=float, help="кадр каждые N секунд (0.25–0.5 — разобрать драку по кадрам)")
    fr.add_argument("--grid", action="store_true", help="один полный кадр с сеткой координат")

    pv = sub.add_parser("preview", help="кадр готовой раскладки 9:16 (проверить вебку/субтитры)")
    pv.add_argument("project")
    pv.add_argument("--at", required=True)
    pv.add_argument("--layout", choices=["stream", "blur", "crop"])
    pv.add_argument("--plan")
    pv.add_argument("--safe", action="store_true", help="закрасить зоны под интерфейсом TikTok/Shorts")

    ck = sub.add_parser("check", help="проверить план до рендера: границы, хук, хвост, паузы, подписи, состав")
    ck.add_argument("project")
    ck.add_argument("--plan")
    ck.add_argument("--only", nargs="+")
    ck.add_argument("--asr", action="store_true", help="распознать куски моделью клипа (точно как в субтитрах)")

    rv = sub.add_parser("review", help="готовые клипы: лист кадров + текст субтитров для вычитки")
    rv.add_argument("project")
    rv.add_argument("--plan")
    rv.add_argument("--only", nargs="+")

    r = sub.add_parser("render", help="смонтировать клипы по clips.yaml")
    r.add_argument("project")
    r.add_argument("--plan", help="путь к плану (по умолчанию work/<проект>/clips.yaml)")
    r.add_argument("--only", nargs="+", help="только эти id")

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
        transcribe_project(cfg, pdir)
    elif args.cmd == "analyze":
        from .analyze import analyze
        analyze(cfg, pdir, args.top)
    elif args.cmd == "frames":
        if not args.grid and not args.at and not (args.start and args.end):
            die("укажи --at или --from/--to")
        from .frames import frames
        frames(pdir, args.at, args.start, args.end, args.n, args.span, args.grid, step=args.step)
    elif args.cmd == "talk":
        from .talk import talk
        talk(cfg, pdir, args.top)
    elif args.cmd == "check":
        from .check import check
        check(cfg, pdir, args.plan, args.only, args.asr)
    elif args.cmd == "review":
        from .check import review
        review(cfg, pdir, args.plan, args.only)
    elif args.cmd == "preview":
        from .render import preview
        preview(cfg, pdir, args.at, args.layout, args.plan, args.safe)
    elif args.cmd == "render":
        from .render import render
        render(cfg, pdir, args.plan, args.only)


if __name__ == "__main__":
    sys.exit(main())
