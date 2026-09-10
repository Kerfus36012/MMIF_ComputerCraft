#!/usr/bin/env python3
"""
mmif_convert.py — конвертер изображений/видео (+ аудио) в MMIF для CC:Tweaked.

Аудио:
  • Если вход — видео (mp4/mkv/avi/webm/...), аудиодорожка извлекается
    автоматически и кодируется в DFPWM через ffmpeg.
  • Если вход — картинка/гиф, аудио нет (разве что указать --audio).
  • Можно указать --audio <file> чтобы наложить свой звук.
  • Можно указать --dfpwm <file> чтобы взять готовый DFPWM.
  • --no-audio отключает автоизвлечение.

Требуется ffmpeg 5.1+ для `-c:a dfpwm`.
"""

import argparse
import struct
import sys
import os
import subprocess
import tempfile
import json
import math
from PIL import Image, ImageSequence

# ─── Палитра CC:Tweaked ────────────────────────────────────────────────
CC_PALETTE = [
    (0xF0, 0xF0, 0xF0), (0xF2, 0xB2, 0x33), (0xE5, 0x7F, 0xD8), (0x99, 0xB2, 0xF2),
    (0xDE, 0xDE, 0x6C), (0x7F, 0xCC, 0x19), (0xF2, 0xB2, 0xB2), (0x4C, 0x4C, 0x4C),
    (0x99, 0x99, 0x99), (0x4C, 0x99, 0xB2), (0x7F, 0x2F, 0xB2), (0x33, 0x33, 0xCC),
    (0x7F, 0x66, 0x4C), (0x57, 0xA6, 0x4C), (0xCC, 0x4C, 0x4C), (0x11, 0x11, 0x11),
]

BAYER4 = [
    [ 0,  8,  2, 10],
    [12,  4, 14,  6],
    [ 3, 11,  1,  9],
    [15,  7, 13,  5],
]

VIDEO_EXTS = (".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".flv", ".wmv")


def is_video_file(path):
    return path.lower().endswith(VIDEO_EXTS)


def probe_video(path):
    """Возвращает dict с width, height, fps, nb_frames (или None)."""
    try:
        r = subprocess.run([
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,r_frame_rate,nb_frames,duration",
            "-of", "json", path
        ], capture_output=True, text=True)
        if r.returncode != 0:
            return None
        data = json.loads(r.stdout)
        streams = data.get("streams", [])
        if not streams:
            return None
        s = streams[0]

        # r_frame_rate в виде "30000/1001"
        fr = s.get("r_frame_rate", "0/1")
        num, den = fr.split("/")
        fps = float(num) / float(den) if float(den) != 0 else 0.0

        nb = s.get("nb_frames")
        nb_frames = int(nb) if nb and nb != "N/A" else None

        duration = s.get("duration")
        duration = float(duration) if duration and duration != "N/A" else None

        if nb_frames is None and duration and fps:
            nb_frames = int(duration * fps)

        return {
            "width": int(s["width"]),
            "height": int(s["height"]),
            "fps": fps,
            "nb_frames": nb_frames,
            "duration": duration,
        }
    except FileNotFoundError:
        sys.exit("ffprobe не найден. Установи ffmpeg.")
    except Exception as e:
        print(f"ffprobe warning: {e}")
        return None


def load_video_frames(path, target_w, target_h, target_fps):
    """
    Читает видео через ffmpeg пайп, возвращает список PIL.Image (RGB),
    уже отмасштабированных под target_w x target_h и с частотой target_fps.
    """
    info = probe_video(path)
    if info is None:
        sys.exit(f"Не удалось определить параметры видео: {path}")

    # Команда: разложить видео в raw RGB24 поток
    cmd = [
        "ffmpeg", "-v", "error",
        "-i", path,
        "-vf", f"fps={target_fps},scale={target_w}:{target_h}:flags=lanczos",
        "-f", "rawvideo",
        "-pix_fmt", "rgb24",
        "-"
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    frame_size = target_w * target_h * 3
    frames = []
    try:
        while True:
            raw = proc.stdout.read(frame_size)
            if len(raw) < frame_size:
                break
            img = Image.frombytes("RGB", (target_w, target_h), raw)
            frames.append(img)
    finally:
        proc.stdout.close()
        proc.wait()

    if proc.returncode != 0:
        err = proc.stderr.read().decode(errors="replace")
        sys.exit(f"ffmpeg ошибка при чтении видео: {err}")

    if not frames:
        sys.exit("ffmpeg не вернул ни одного кадра")

    return frames, info


def load_frames(path, target_w=None, target_h=None, target_fps=10):
    """
    Универсальный загрузчик:
      • Картинка → [PIL.Image]
      • GIF      → кадры через Pillow
      • Видео    → кадры через ffmpeg pipe
    """
    if is_video_file(path):
        if target_w is None or target_h is None:
            sys.exit("Для видео нужно указать --width и --height")
        frames, info = load_video_frames(path, target_w, target_h, target_fps)
        return frames, True

    # Картинка / GIF — старый путь через Pillow
    img = Image.open(path)
    is_animated = getattr(img, "is_animated", False)
    frames = []
    if is_animated:
        for frame in ImageSequence.Iterator(img):
            frames.append(frame.convert("RGB"))
        return frames, True
    return [img.convert("RGB")], False

# ─── Утилиты ───────────────────────────────────────────────────────────
def nearest_color(r, g, b):
    best, best_d = 0, 1e18
    for i, (pr, pg, pb) in enumerate(CC_PALETTE):
        d = (r-pr)**2 + (g-pg)**2 + (b-pb)**2
        if d < best_d:
            best_d, best = d, i
    return best

# ─── Дизеринг ──────────────────────────────────────────────────────────
def quantize_floyd(img):
    w, h = img.size
    px = list(img.getdata())
    buf = [[list(px[y*w + x]) for x in range(w)] for y in range(h)]
    out = [[0]*w for _ in range(h)]
    for y in range(h):
        for x in range(w):
            r, g, b = buf[y][x]
            r = max(0, min(255, int(r)))
            g = max(0, min(255, int(g)))
            b = max(0, min(255, int(b)))
            idx = nearest_color(r, g, b)
            out[y][x] = idx
            pr, pg, pb = CC_PALETTE[idx]
            er, eg, eb = r-pr, g-pg, b-pb
            for dx, dy, k in ((1,0,7/16), (-1,1,3/16), (0,1,5/16), (1,1,1/16)):
                nx, ny = x+dx, y+dy
                if 0 <= nx < w and 0 <= ny < h:
                    buf[ny][nx][0] += er*k
                    buf[ny][nx][1] += eg*k
                    buf[ny][nx][2] += eb*k
    return out

def quantize_bayer(img):
    w, h = img.size
    px = list(img.getdata())
    out = [[0]*w for _ in range(h)]
    for y in range(h):
        for x in range(w):
            r, g, b = px[y*w + x]
            t = (BAYER4[y % 4][x % 4] + 0.5) / 16.0 - 0.5
            amp = 64
            r = max(0, min(255, int(r + t*amp)))
            g = max(0, min(255, int(g + t*amp)))
            b = max(0, min(255, int(b + t*amp)))
            out[y][x] = nearest_color(r, g, b)
    return out

def quantize_none(img):
    w, h = img.size
    px = list(img.getdata())
    return [[nearest_color(*px[y*w + x]) for x in range(w)] for y in range(h)]

def quantize(img, method):
    if method == "floyd": return quantize_floyd(img)
    if method == "bayer": return quantize_bayer(img)
    return quantize_none(img)

# ─── Упаковка кадра ────────────────────────────────────────────────────
def pack_frame(indices, w, h):
    data = bytearray()
    for y in range(h):
        for x in range(0, w, 2):
            hi = indices[y][x]
            lo = indices[y][x+1] if x+1 < w else 0
            data.append((hi << 4) | lo)
    return bytes(data)

def rle_encode(data):
    out = bytearray()
    i, n = 0, len(data)
    while i < n:
        v = data[i]
        c = 1
        while i+c < n and data[i+c] == v and c < 255:
            c += 1
        out.append(v)
        out.append(c)
        i += c
    return bytes(out)

# ─── Аудио: ffmpeg → DFPWM ─────────────────────────────────────────────
def _run_ffmpeg_dfpwm(input_path, sample_rate, skip_video):
    """Общий вызов ffmpeg → raw DFPWM. Возвращает bytes или None."""
    with tempfile.NamedTemporaryFile(suffix=".dfpwm", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        cmd = ["ffmpeg", "-y", "-i", input_path]
        if skip_video:
            cmd += ["-vn"]
        cmd += ["-ar", str(sample_rate), "-ac", "1",
                "-c:a", "dfpwm", "-f", "dfpwm", tmp_path]
        r = subprocess.run(cmd, capture_output=True)
        if r.returncode != 0:
            return None, r.stderr.decode(errors="replace")
        with open(tmp_path, "rb") as f:
            data = f.read()
        return (data if data else None), None
    except FileNotFoundError:
        return None, "ffmpeg не найден"
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def convert_audio_to_dfpwm(path, sample_rate=48000):
    """Явно указанный аудио-файл → DFPWM. Падает с ошибкой при неудаче."""
    if not os.path.exists(path):
        sys.exit(f"Аудио-файл не найден: {path}")
    data, err = _run_ffmpeg_dfpwm(path, sample_rate, skip_video=False)
    if data is None:
        sys.exit(f"ffmpeg не смог конвертировать {path} в DFPWM: {err}")
    return data


def extract_audio_from_video(path, sample_rate=48000):
    """Автоизвлечение аудио из видео. None, если аудио нет."""
    data, _ = _run_ffmpeg_dfpwm(path, sample_rate, skip_video=True)
    return data


def has_audio_stream(path):
    """True, если в файле есть аудиодорожка (ffprobe)."""
    try:
        r = subprocess.run([
            "ffprobe", "-v", "error",
            "-select_streams", "a",
            "-show_entries", "stream=index",
            "-of", "csv=p=0",
            path
        ], capture_output=True, text=True)
        return r.returncode == 0 and bool(r.stdout.strip())
    except FileNotFoundError:
        return False


# ─── Сборка MMIF v2 ────────────────────────────────────────────────────
def write_mmif(path, frames, w, h, fps, loop, video, compress,
               audio_bytes=None, audio_rate=48000):
    flags = 0
    if loop:      flags |= 0x01
    if video:     flags |= 0x02
    if compress:  flags |= 0x04
    if audio_bytes is not None:
        flags |= 0x08

    with open(path, "wb") as f:
        f.write(b"MMIF")
        f.write(struct.pack(">H", w))
        f.write(struct.pack(">H", h))
        f.write(struct.pack("B", fps))
        f.write(struct.pack("B", flags))

        if audio_bytes is not None:
            f.write(struct.pack(">I", len(audio_bytes)))
            f.write(struct.pack(">H", audio_rate))
            f.write(audio_bytes)

        for frame in frames:
            raw = pack_frame(frame, w, h)
            payload = rle_encode(raw) if compress else raw
            f.write(b"\xAD")
            f.write(payload)
        f.write(b"\xFF")


# ─── main ──────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("output")
    ap.add_argument("--dither", choices=["none", "floyd", "bayer"], default="floyd")
    ap.add_argument("--compress", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--fps", type=int, default=10)
    ap.add_argument("--width", type=int, default=0)
    ap.add_argument("--height", type=int, default=0)
    ap.add_argument("--audio", default=None,
                    help="наложить свой аудио-файл (перекрывает встроенное)")
    ap.add_argument("--dfpwm", default=None,
                    help="готовый DFPWM-файл (приоритетнее всего)")
    ap.add_argument("--audio-rate", type=int, default=48000)
    ap.add_argument("--no-audio", action="store_true",
                    help="не извлекать аудио из видео")
    args = ap.parse_args()

    # 1) Размеры — для видео их надо знать ДО чтения кадров
    w = args.width
    h = args.height

    if is_video_file(args.input):
        info = probe_video(args.input)
        if info is None:
            sys.exit("Не удалось прочитать параметры видео")
        if not w:
            w = info["width"]
        if not h:
            h = info["height"]
        print(f"Видео: {info['width']}x{info['height']} @ {info['fps']:.2f} fps, "
              f"кадров: {info['nb_frames'] or '?'}, длительность: "
              f"{info['duration']:.2f}с" if info['duration'] else "")
    else:
        # картинка/GIF — размеры возьмём из Pillow
        img = Image.open(args.input)
        if not w: w = img.width
        if not h: h = img.height

    if w % 2:
        w += 1
    if w > 65535 or h > 65535:
        sys.exit("слишком большой размер (макс 65535)")

    # 2) Кадры
    frames, is_video = load_frames(args.input, w, h, args.fps)

    # 2) Аудио (приоритет: --dfpwm > --audio > авто из видео)
    audio_bytes = None
    if args.dfpwm:
        with open(args.dfpwm, "rb") as f:
            audio_bytes = f.read()
        print(f"Аудио: {args.dfpwm} ({len(audio_bytes)} байт)")
    elif args.audio:
        audio_bytes = convert_audio_to_dfpwm(args.audio, args.audio_rate)
        print(f"Аудио: {args.audio} → DFPWM ({len(audio_bytes)} байт)")
    elif not args.no_audio and is_video and has_audio_stream(args.input):
        print("Аудио: обнаружено в видео, извлекаю в DFPWM...")
        audio_bytes = extract_audio_from_video(args.input, args.audio_rate)
        if audio_bytes:
            print(f"  извлечено {len(audio_bytes)} байт DFPWM")
        else:
            print("  не удалось (нужен ffmpeg 5.1+ с поддержкой dfpwm)")
    elif is_video:
        print("Аудио: в видео нет аудиодорожки (или --no-audio)")

    # 3) Информация
    print(f"Вход:    {args.input}")
    print(f"Кадров:  {len(frames)}  ({w}x{h}, видео={is_video})")
    print(f"Опции:   dither={args.dither}, compress={args.compress}, "
          f"loop={args.loop}, fps={args.fps}")

    # 4) Квантование
    packed = []
    for i, fr in enumerate(frames):
        fr = fr.resize((w, h), Image.LANCZOS)
        packed.append(quantize(fr, args.dither))
        print(f"  кадр {i+1}/{len(frames)}", end="\r")
    print()

    # 5) Запись
    write_mmif(args.output, packed, w, h, args.fps,
               args.loop, is_video, args.compress,
               audio_bytes, args.audio_rate)

    size = os.path.getsize(args.output)
    print(f"Готово: {args.output}  ({size} байт)")


if __name__ == "__main__":
    main()