# clipper — нарезка стримов в TikTok / Shorts / Reels

Берёт длинное видео (YouTube, Twitch VOD или локальный файл), находит хайлайты
по громкости, активности чата и речи и монтирует вертикальные клипы 1080×1920:
вебка сверху, игра снизу, крупные субтитры с подсветкой слов и заголовок-хук.

Рассчитан на работу вместе с Claude Code на своём сервере: Claude читает `CLAUDE.md`,
сам гоняет пайплайн, смотрит кадры, выбирает моменты и пишет план монтажа.

## Установка (Ubuntu)

```bash
git clone <этот репозиторий> clipper && cd clipper
./setup.sh
source .venv/bin/activate
```

Claude Code на сервере:
```bash
curl -fsSL https://claude.ai/install.sh | bash
claude                       # работа по SSH
# или управление с телефона через приложение Claude:
tmux new -s clips 'claude remote-control'
```

Дальше просто пишешь Claude: «нарежь https://youtube.com/watch?v=..., 10 клипов про самые смешные моменты».

## Команды вручную

```bash
python -m clipper fetch <ссылка|файл> --name nix1 [--from 1:00:00 --to 2:00:00] [--cookies cookies.txt]
python -m clipper transcribe nix1            # речь → transcript.json/txt (GPU сильно быстрее)
python -m clipper analyze nix1 --top 30      # кандидаты → moments.txt
python -m clipper frames nix1 --at 1:02:30   # лист кадров вокруг момента
python -m clipper frames nix1 --at 10:00 --grid   # кадр с сеткой — найти координаты вебки
python -m clipper preview nix1 --at 10:00    # как выглядит раскладка 9:16
python -m clipper render nix1 [--only 01-x]  # монтаж по work/nix1/clips.yaml → work/nix1/out/
```

Формат плана — `clips.example.yaml`, все настройки (шрифты, цвета, размеры, веса анализа) — `config.yaml`.

## Что внутри

| файл | что делает |
|---|---|
| `clipper/download.py` | yt-dlp + чат YouTube/Twitch → `source.mp4`, `chat.jsonl` |
| `clipper/transcribe.py` | faster-whisper с таймкодами слов; умеет распознавать только нужный кусок |
| `clipper/analyze.py` | громкость/чат/ключевые слова → рейтинг моментов |
| `clipper/frames.py` | листы кадров и сетка координат (чтобы Claude «видел» видео) |
| `clipper/render.py` | склейка сегментов, раскладки stream/blur/crop, субтитры, нормализация звука |
| `clipper/subs.py` | ASS-субтитры в стиле TikTok |

Шрифт Montserrat (OFL) лежит в `fonts/`.

## Примечания

- Видеокарта NVIDIA: `setup.sh` сам поставит библиотеки CUDA для whisper. Для монтажа на GPU
  в `config.yaml` поставь `output.codec: h264_nvenc` (если ffmpeg собран с NVENC).
- YouTube с серверных IP иногда требует войти: экспортируй `cookies.txt` из браузера
  (расширение «Get cookies.txt LOCALLY») и передай `--cookies`.
- Нарезки чужого контента: проверь, как автор относится к клипам, особенно если монетизируешь.
