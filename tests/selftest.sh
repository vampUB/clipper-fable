#!/usr/bin/env bash
# Самопроверка пайплайна без YouTube и без whisper: синтетическое видео (ffmpeg) + написанный
# руками транскрипт. Запуск: ./tests/selftest.sh   (проект work/selftest пересоздаётся)
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PYTHON:-python3}
mkdir -p work
SRC=work/.selftest-src.mp4
F=fonts/Montserrat-ExtraBold.ttf
rm -rf work/selftest

echo "→ генерирую видео 150 с (вебка слева сверху, два громких места)"
ffmpeg -v error -y -f lavfi -i "testsrc2=s=1280x720:r=30:d=150" \
  -f lavfi -i "sine=f=220:d=150,volume='if(between(t,58,63)+between(t,100,104),1.0,0.08)':eval=frame" \
  -filter_complex "[0:v]drawbox=0:0:320:216:blue@1:fill,drawtext=fontfile=$F:text='CAM %{pts\:hms}':x=10:y=90:fontsize=28:fontcolor=white,drawtext=fontfile=$F:text='GAME %{pts\:hms}':x=500:y=400:fontsize=48:fontcolor=white:borderw=3[v]" \
  -map "[v]" -map 1:a -c:v libx264 -preset ultrafast -crf 28 -c:a aac -shortest "$SRC"

$PY -m clipper fetch "$SRC" --name selftest
$PY tests/make_transcript.py work/selftest
cp tests/selftest.clips.yaml work/selftest/clips.yaml

$PY -m clipper analyze selftest --top 4 >/dev/null
grep -q "тихий" work/selftest/moments.txt        || { echo "✗ analyze: нет тихих кандидатов"; exit 1; }
grep -q "арбуз" work/selftest/talk.txt           || { echo "✗ talk: не найден разговорный блок"; exit 1; }
$PY -m clipper check selftest >/dev/null
grep -q "режет слово" work/selftest/check.txt    || { echo "✗ check: не пойман срез посреди слова"; exit 1; }
grep -q "хук запаздывает" work/selftest/check.txt || { echo "✗ check: не пойман запоздалый хук"; exit 1; }
$PY -m clipper frames selftest --at 30 --grid >/dev/null
$PY -m clipper frames selftest --from 59 --to 61 --step 0.25 >/dev/null
$PY -m clipper preview selftest --at 30 --safe >/dev/null
$PY -m clipper render selftest 2>/dev/null | grep "✓"
$PY -m clipper review selftest >/dev/null

n=$(ls work/selftest/out/*.mp4 | wc -l)
[ "$n" = 4 ] || { echo "✗ render: клипов $n, ожидалось 4"; exit 1; }
grep -q "БЕТБУМ ОЧЕНЬ" work/selftest/out/01-pravila.ass || { echo "✗ subs: замена фразой не сработала"; exit 1; }
grep -q "П\*ЗДЕЦ" work/selftest/out/*.ass \
  || { echo "✗ subs: цензура не сработала"; exit 1; }
for f in work/selftest/out/*.mp4; do
  ffprobe -v error -show_entries stream=codec_type,duration -of csv=p=0 "$f" | tr '\n' ' '; echo " $(basename "$f")"
done
echo "✓ selftest пройден: work/selftest/ (отчёт out/report.md, листы frames/review_*.jpg)"
