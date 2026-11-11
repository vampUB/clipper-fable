#!/usr/bin/env bash
# Установка на Ubuntu 22.04/24.04. Запуск: ./setup.sh
set -euo pipefail
cd "$(dirname "$0")"

echo "→ системные пакеты"
sudo apt-get update -qq
sudo apt-get install -y -qq ffmpeg python3 python3-venv python3-pip git

echo "→ python-окружение (.venv)"
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

if command -v nvidia-smi >/dev/null 2>&1; then
  echo "→ найдена видеокарта NVIDIA — ставлю CUDA-библиотеки для whisper"
  .venv/bin/pip install -q nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*"
  SITE=$(.venv/bin/python -c 'import site; print(site.getsitepackages()[0])')
  LIBS="$SITE/nvidia/cublas/lib:$SITE/nvidia/cudnn/lib"
  grep -q "nvidia/cublas" .venv/bin/activate || echo "export LD_LIBRARY_PATH=\"$LIBS:\${LD_LIBRARY_PATH:-}\"" >> .venv/bin/activate
fi

mkdir -p work

if ! command -v deno >/dev/null 2>&1 && ! command -v node >/dev/null 2>&1; then
  echo
  echo "! Для YouTube yt-dlp нужен JS-рантайм. Поставь deno:"
  echo "    curl -fsSL https://deno.land/install.sh | sh"
fi

echo
echo "✓ готово. Дальше:"
echo "    source .venv/bin/activate"
echo "    claude          # и попроси: «нарежь <ссылка>»"
