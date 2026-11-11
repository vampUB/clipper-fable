"""Распознавание речи (faster-whisper) с таймкодами слов."""
import tempfile
import time
from pathlib import Path

from .util import find_source, fmt_time, read_json, run, write_json

_MODEL = None


def _load_model(wcfg: dict):
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    from faster_whisper import WhisperModel

    device = wcfg.get("device", "auto")
    if device == "auto":
        try:
            import ctranslate2
            device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
        except Exception:
            device = "cpu"
    compute = wcfg.get("compute_type", "auto")
    if compute == "auto":
        compute = "float16" if device == "cuda" else "int8"
    model = wcfg.get("model", "auto")
    if model == "auto":
        model = "large-v3" if device == "cuda" else "small"
    print(f"→ whisper: модель {model}, {device}/{compute}")
    _MODEL = WhisperModel(model, device=device, compute_type=compute)
    _MODEL._clipper_name = model
    return _MODEL


def _transcribe_file(audio: Path, wcfg: dict, offset: float = 0.0, progress_total: float | None = None):
    model = _load_model(wcfg)
    # читаем wav сами: так не зависим от версии PyAV, через которую faster-whisper открывает файлы
    import wave

    import numpy as np
    with wave.open(str(audio), "rb") as wf:
        pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
    samples = pcm.astype(np.float32) / 32768.0
    segments, _info = model.transcribe(
        samples, language=wcfg.get("language") or None, word_timestamps=True,
        vad_filter=True, beam_size=5 if "large" in model._clipper_name else 1,
        condition_on_previous_text=False,
    )
    out = []
    t0 = time.time()
    for seg in segments:
        words = [{"start": round(w.start + offset, 2), "end": round(w.end + offset, 2),
                  "word": w.word.strip(), "p": round(w.probability, 2)}
                 for w in (seg.words or []) if w.word.strip()]
        out.append({"start": round(seg.start + offset, 2), "end": round(seg.end + offset, 2),
                    "text": seg.text.strip(), "words": words})
        if progress_total:
            pct = 100 * seg.end / progress_total
            print(f"\r  {fmt_time(seg.end)} / {fmt_time(progress_total)}  ({pct:4.1f}%, {time.time() - t0:.0f}s)",
                  end="", flush=True)
    if progress_total:
        print()
    return out


def _extract_audio(src: Path, dst: Path, start: float | None = None, end: float | None = None):
    cmd = ["ffmpeg", "-v", "error", "-y"]
    if start is not None:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", src]
    if end is not None:
        cmd += ["-t", f"{end - (start or 0):.3f}"]
    cmd += ["-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", dst]
    run(cmd)


def transcribe_project(cfg: dict, pdir: Path):
    src = find_source(pdir)
    meta = read_json(pdir / "meta.json") if (pdir / "meta.json").exists() else {}
    with tempfile.TemporaryDirectory() as td:
        wav = Path(td) / "a.wav"
        print("→ извлекаю звук")
        _extract_audio(src, wav)
        segs = _transcribe_file(wav, cfg["whisper"], progress_total=meta.get("duration"))
    write_json(pdir / "transcript.json", {"segments": segs})
    with open(pdir / "transcript.txt", "w", encoding="utf-8") as f:
        for s in segs:
            f.write(f"[{fmt_time(s['start'])}] {s['text']}\n")
    print(f"✓ {len(segs)} фраз → transcript.json / transcript.txt")


def words_in_range(cfg: dict, pdir: Path, start: float, end: float) -> list[dict]:
    """Слова в [start, end] (секунды исходника). Берёт из полного транскрипта
    или распознаёт только этот кусок (с кэшем)."""
    full = pdir / "transcript.json"
    if full.exists():
        segs = read_json(full)["segments"]
        return [w for s in segs for w in s["words"] if w["end"] > start and w["start"] < end]
    cache = pdir / "cache" / f"words_{start:.2f}_{end:.2f}.json"
    if cache.exists():
        return read_json(cache)
    cache.parent.mkdir(exist_ok=True)
    pad = 1.0
    a = max(0.0, start - pad)
    with tempfile.TemporaryDirectory() as td:
        wav = Path(td) / "a.wav"
        _extract_audio(find_source(pdir), wav, a, end + pad)
        segs = _transcribe_file(wav, cfg["whisper"], offset=a)
    words = [w for s in segs for w in s["words"] if w["end"] > start and w["start"] < end]
    write_json(cache, words)
    return words
