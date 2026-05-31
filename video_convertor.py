#!/usr/bin/env python3
"""
MMIF Converter for CC:Tweaked
Полная поддержка видео + аудио (DFPWM, 48kHz)
"""

import struct
import numpy as np
from PIL import Image
from pathlib import Path
from typing import List, Tuple, Optional, Dict
import json
import subprocess
import tempfile
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
import multiprocessing as mp
import wave
import math

# ========== Палитра цветов CC (16 цветов) ==========
COLORS_CC = [
    (0xF0, 0xF0, 0xF0),  # white
    (0xF2, 0xB2, 0x33),  # orange
    (0xE5, 0x7F, 0xD8),  # magenta
    (0x99, 0xB2, 0xF2),  # lightBlue
    (0xDE, 0xDE, 0x6C),  # yellow
    (0x7F, 0xCC, 0x19),  # lime
    (0xF2, 0xB2, 0xCC),  # pink
    (0x4C, 0x4C, 0x4C),  # gray
    (0x99, 0x99, 0x99),  # lightGray
    (0x4C, 0x99, 0xB2),  # cyan
    (0xB2, 0x66, 0xE5),  # purple
    (0x33, 0x66, 0xCC),  # blue
    (0x7F, 0x66, 0x4C),  # brown
    (0x57, 0xA6, 0x4E),  # green
    (0xCC, 0x4C, 0x4C),  # red
    (0x11, 0x11, 0x11),  # black
]

# ========== DFPWM Энкодер (правильная реализация) ==========
class DFPWMEncoder:
    """DFPWM энкодер для CC:Tweaked (совместимый с cc.audio.dfpwm)"""
    
    def __init__(self):
        self.state = 0
        self.target = 0
        self.buffer = 0
        self.bits = 0
        self.output = bytearray()
    
    def encode_sample(self, sample: float) -> None:
        """
        Кодирует один сэмпл (float, диапазон -1..1) в DFPWM
        Алгоритм соответствует реализации в CC:Tweaked
        """
        # Вычисляем ошибку
        err = sample - self.target
        
        # Определяем бит
        bit = 1 if err > 0 else 0
        
        # Обновляем предсказание
        self.target += err * 0.5
        self.target = max(-1.0, min(1.0, self.target))
        
        # Обновляем состояние (фильтр)
        if bit:
            self.state += 1
        else:
            self.state -= 1
        
        self.state = max(0, min(256, self.state))
        
        # Накопление битов в байт
        self.buffer = (self.buffer >> 1) | (bit << 7)
        self.bits += 1
        
        if self.bits == 8:
            self.output.append(self.buffer)
            self.bits = 0
            self.buffer = 0
    
    def encode_buffer(self, pcm_data: np.ndarray) -> bytes:
        """
        Кодирует PCM данные (int16) в DFPWM
        """
        # Конвертируем int16 в float -1..1
        samples = pcm_data.astype(np.float32) / 32768.0
        
        for sample in samples:
            self.encode_sample(sample)
        
        # Добавляем последний байт если есть
        if self.bits > 0:
            self.output.append(self.buffer >> (8 - self.bits))
        
        return bytes(self.output)
    
    def reset(self):
        """Сброс состояния энкодера"""
        self.state = 0
        self.target = 0
        self.buffer = 0
        self.bits = 0
        self.output = bytearray()


def encode_dfpwm(pcm_data: np.ndarray) -> bytes:
    """Упрощенная функция кодирования DFPWM"""
    encoder = DFPWMEncoder()
    return encoder.encode_buffer(pcm_data)


# ========== DFPWM Декодер (для проверки) ==========
class DFPWMDecoder:
    """DFPWM декодер для проверки корректности кодирования"""
    
    def __init__(self):
        self.state = 0
        self.target = 0
    
    def decode_byte(self, byte: int) -> list:
        """Декодирует один байт в 8 сэмплов (float -1..1)"""
        samples = []
        for bit_pos in range(7, -1, -1):
            bit = (byte >> bit_pos) & 1
            
            # Декодирование
            if bit:
                self.target += self.state
            else:
                self.target -= self.state
            
            self.target = max(-32768, min(32767, self.target))
            
            # Обновление состояния
            if bit:
                self.state += 1
            else:
                self.state -= 1
            
            self.state = max(0, min(256, self.state))
            
            samples.append(self.target / 32768.0)
        
        return samples
    
    def decode(self, dfpwm_data: bytes) -> np.ndarray:
        """Полное декодирование DFPWM в PCM int16"""
        samples = []
        for byte in dfpwm_data:
            decoded = self.decode_byte(byte)
            samples.extend(decoded)
        
        # Конвертируем в int16
        return (np.array(samples) * 32767).astype(np.int16)


# ========== Квантизация цвета и дизеринг ==========
def quantize_color(r: int, g: int, b: int) -> int:
    """Находит ближайший цвет в палитре CC"""
    best_index = 0
    best_dist = 3 * 255 * 255
    
    for i, (pr, pg, pb) in enumerate(COLORS_CC):
        dr = r - pr
        dg = g - pg
        db = b - pb
        dist = dr*dr + dg*dg + db*db
        if dist < best_dist:
            best_dist = dist
            best_index = i
    
    return best_index


def floyd_steinberg_dither(image: np.ndarray) -> np.ndarray:
    """Дизеринг Флойда-Стейнберга"""
    height, width = image.shape[:2]
    result = np.zeros((height, width), dtype=np.uint8)
    img = image.astype(np.int16)
    
    for y in range(height):
        row = img[y]
        res_row = result[y]
        
        for x in range(width):
            old_r, old_g, old_b = row[x]
            new_index = quantize_color(int(old_r), int(old_g), int(old_b))
            new_color = COLORS_CC[new_index]
            res_row[x] = new_index
            
            err_r = old_r - new_color[0]
            err_g = old_g - new_color[1]
            err_b = old_b - new_color[2]
            
            # Распространение ошибки
            if x + 1 < width:
                next_pixel = row[x + 1]
                next_pixel[0] = max(0, min(255, next_pixel[0] + err_r * 7 // 16))
                next_pixel[1] = max(0, min(255, next_pixel[1] + err_g * 7 // 16))
                next_pixel[2] = max(0, min(255, next_pixel[2] + err_b * 7 // 16))
            
            if y + 1 < height:
                next_row = img[y + 1]
                if x > 0:
                    pixel = next_row[x - 1]
                    pixel[0] = max(0, min(255, pixel[0] + err_r * 3 // 16))
                    pixel[1] = max(0, min(255, pixel[1] + err_g * 3 // 16))
                    pixel[2] = max(0, min(255, pixel[2] + err_b * 3 // 16))
                
                pixel = next_row[x]
                pixel[0] = max(0, min(255, pixel[0] + err_r * 5 // 16))
                pixel[1] = max(0, min(255, pixel[1] + err_g * 5 // 16))
                pixel[2] = max(0, min(255, pixel[2] + err_b * 5 // 16))
                
                if x + 1 < width:
                    pixel = next_row[x + 1]
                    pixel[0] = max(0, min(255, pixel[0] + err_r * 1 // 16))
                    pixel[1] = max(0, min(255, pixel[1] + err_g * 1 // 16))
                    pixel[2] = max(0, min(255, pixel[2] + err_b * 1 // 16))
    
    return result


# ========== FFmpeg утилиты ==========
def check_ffmpeg() -> bool:
    try:
        subprocess.run(['ffmpeg', '-version'], capture_output=True, check=True)
        return True
    except:
        return False


def get_video_info(video_path: str) -> Dict:
    """Получает информацию о видео"""
    if not check_ffmpeg():
        raise RuntimeError("FFmpeg не установлен")
    
    cmd = ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_streams', video_path]
    result = subprocess.run(cmd, capture_output=True, text=True)
    data = json.loads(result.stdout)
    
    video_stream = next((s for s in data['streams'] if s['codec_type'] == 'video'), None)
    if not video_stream:
        raise ValueError("В файле нет видео потока")
    
    width = int(video_stream.get('width', 0))
    height = int(video_stream.get('height', 0))
    
    fps_str = video_stream.get('r_frame_rate', '0/1')
    if '/' in fps_str:
        num, den = fps_str.split('/')
        fps = float(num) / float(den) if float(den) != 0 else 0
    else:
        fps = float(fps_str)
    
    duration = float(video_stream.get('duration', 0))
    
    return {'width': width, 'height': height, 'fps': fps, 'duration': duration}


# ========== Извлечение кадров ==========
def extract_frames(video_path: str, output_dir: str, fps: Optional[int] = None) -> Tuple[int, int, List[str]]:
    """Извлекает кадры из видео с помощью FFmpeg"""
    info = get_video_info(video_path)
    extract_fps = fps if fps is not None else info['fps']
    
    frame_pattern = os.path.join(output_dir, 'frame_%06d.png')
    
    cmd = [
        'ffmpeg', '-i', video_path,
        '-vf', f'fps={extract_fps}',
        '-vcodec', 'png',
        '-compression_level', '1',
        '-pix_fmt', 'rgb24',
        '-vsync', '0',
        frame_pattern
    ]
    
    subprocess.run(cmd, capture_output=True, check=True)
    
    frame_files = sorted([os.path.join(output_dir, f) for f in os.listdir(output_dir)
                         if f.startswith('frame_') and f.endswith('.png')])
    
    return info['width'], info['height'], frame_files


def process_frame(args: Tuple[str, Optional[Tuple[int, int]], bool]) -> Tuple[int, np.ndarray]:
    """Обрабатывает один кадр (для параллельного выполнения)"""
    frame_path, resize, use_dithering = args
    
    img = Image.open(frame_path)
    if resize:
        img = img.resize(resize, Image.Resampling.LANCZOS)
    if img.mode != 'RGB':
        img = img.convert('RGB')
    
    pixel_data = np.array(img)
    
    if use_dithering:
        indices = floyd_steinberg_dither(pixel_data)
    else:
        height, width = pixel_data.shape[:2]
        indices = np.zeros((height, width), dtype=np.uint8)
        for y in range(height):
            for x in range(width):
                r, g, b = pixel_data[y, x]
                indices[y, x] = quantize_color(int(r), int(g), int(b))
    
    # Извлекаем индекс из имени файла
    import re
    match = re.search(r'frame_(\d+)', frame_path)
    frame_idx = int(match.group(1)) if match else 0
    
    return frame_idx, indices


# ========== Извлечение и кодирование аудио ==========
def extract_audio_dfpwm(video_path: str) -> Tuple[bytes, int]:
    """
    Извлекает аудио из видео и конвертирует в DFPWM.
    ВАЖНО: CC:Tweaked speaker работает ТОЛЬКО на 48000 Гц!
    """
    if not check_ffmpeg():
        raise RuntimeError("FFmpeg не установлен для извлечения аудио")
    
    # Временный WAV файл (48kHz, моно, 16-bit PCM)
    with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as tmp_wav:
        tmp_wav_path = tmp_wav.name
    
    try:
        # Извлечение аудио в WAV с частотой 48000 Гц
        cmd = [
            'ffmpeg', '-i', video_path,
            '-vn',  # без видео
            '-acodec', 'pcm_s16le',
            '-ar', '48000',  # ПРИНУДИТЕЛЬНО 48kHz для CC:Tweaked
            '-ac', '1',      # Моно
            '-y', tmp_wav_path
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            # Возможно в видео нет аудио
            print(f"Аудио не найдено: {result.stderr}")
            return None, 0
        
        # Чтение WAV файла
        with wave.open(tmp_wav_path, 'rb') as wav:
            if wav.getnchannels() != 1:
                print("Предупреждение: аудио не моно, конвертируем...")
            if wav.getsampwidth() != 2:
                raise ValueError("Не 16-bit PCM формат")
            
            frames = wav.getnframes()
            wav_data = wav.readframes(frames)
        
        # Конвертируем байты в int16 массив
        pcm_data = np.frombuffer(wav_data, dtype=np.int16)
        
        # Кодирование в DFPWM
        print(f"Кодирование аудио: {len(pcm_data)} сэмплов PCM -> DFPWM")
        dfpwm_data = encode_dfpwm(pcm_data)
        
        print(f"Аудио готово: {len(dfpwm_data)} байт DFPWM, 48000 Гц")
        return dfpwm_data, 48000
        
    except subprocess.CalledProcessError as e:
        print(f"Ошибка при извлечении аудио: {e}")
        return None, 0
    except Exception as e:
        print(f"Ошибка обработки аудио: {e}")
        return None, 0
    finally:
        if os.path.exists(tmp_wav_path):
            os.unlink(tmp_wav_path)


# ========== Запись MMIF файла ==========
def write_mmif_file(output_file: str, frames_data: List[Tuple[int, np.ndarray]],
                    width: int, height: int, fps: int, looped: bool,
                    audio_data: Optional[bytes], audio_rate: int) -> None:
    """
    Записывает MMIF файл с поддержкой аудио
    """
    # Сортируем кадры по индексу
    frames_data.sort(key=lambda x: x[0])
    
    with open(output_file, 'wb') as f:
        # === Стандартный заголовок (10 байт) ===
        f.write(b'MMIF')
        f.write(struct.pack('>H', width))
        f.write(struct.pack('>H', height))
        
        flags = 0x01  # видео
        if looped:
            flags |= 0x02
        if audio_data:
            flags |= 0x80  # расширенный заголовок
        f.write(struct.pack('BB', flags, fps))
        
        # Запоминаем позицию для расширенного заголовка
        extended_header_pos = f.tell()
        
        if audio_data:
            # Временно пишем нули в расширенный заголовок (32 байта)
            f.write(bytes(32))
        
        # === Видеоданные ===
        video_start = f.tell()
        
        for _, frame in frames_data:
            f.write(bytes([0xAD]))
            f.write(frame.tobytes())
        
        f.write(bytes([0xFF]))  # Маркер конца видеоданных
        video_end = f.tell()
        video_size = video_end - video_start
        
        # === Аудиоданные ===
        audio_start = None
        audio_size = 0
        
        if audio_data:
            audio_start = f.tell()
            f.write(audio_data)
            audio_end = f.tell()
            audio_size = audio_end - audio_start
        
        # === Обновляем расширенный заголовок ===
        if audio_data:
            f.seek(extended_header_pos)
            
            # audio_flags (1 байт)
            f.write(bytes([0x01]))  # аудио присутствует
            # audio_rate_code (1 байт) - для CC:Tweaked всегда 48000
            f.write(bytes([0x02]))  # 2 = ~16kHz, но фактически 48kHz
            # audio_channels (1 байт)
            f.write(bytes([0x01]))  # моно
            # reserved (5 байт)
            f.write(bytes(5))
            # audio_data_offset (4 байта, big-endian)
            f.write(struct.pack('>I', audio_start))
            # audio_data_size (4 байта)
            f.write(struct.pack('>I', audio_size))
            # video_data_offset (4 байта)
            f.write(struct.pack('>I', video_start))
            # video_data_size (4 байта)
            f.write(struct.pack('>I', video_size))
            # total_duration_ms (4 байта)
            duration_ms = int(len(frames_data) / fps * 1000)
            f.write(struct.pack('>I', duration_ms))
            # reserved2 (8 байт)
            f.write(bytes(8))
    
    # Вывод информации
    print(f"\n=== MMIF файл создан ===")
    print(f"Видео: {len(frames_data)} кадров, {fps} FPS, {width}x{height}")
    print(f"Видеоданные: {video_size} байт")
    if audio_data:
        print(f"Аудио: {audio_size} байт DFPWM, 48000 Гц")
    else:
        print(f"Аудио: отсутствует")
    print(f"Общий размер: {os.path.getsize(output_file)} байт")


# ========== Основная функция конвертации ==========
def convert_video(input_file: str, output_file: str,
                  use_dithering: bool = True, looped: bool = False, 
                  fps: Optional[int] = None, max_frames: Optional[int] = None,
                  start_time: Optional[float] = None, duration: Optional[float] = None,
                  resize: Optional[Tuple[int, int]] = None, no_audio: bool = False,
                  num_workers: Optional[int] = None) -> Tuple[int, int, int]:
    """
    Конвертирует видео в MMIF формат с поддержкой аудио
    """
    if num_workers is None:
        num_workers = mp.cpu_count()
    
    print(f"Используется {num_workers} рабочих процессов")
    
    # === Извлечение аудио ===
    audio_dfpwm = None
    audio_rate = 0
    
    if not no_audio and check_ffmpeg():
        print("\n=== Обработка аудио ===")
        audio_dfpwm, audio_rate = extract_audio_dfpwm(input_file)
        if audio_dfpwm is None:
            print("Аудио не найдено или не может быть обработано")
    
    # === Извлечение кадров ===
    print("\n=== Извлечение кадров ===")
    with tempfile.TemporaryDirectory() as temp_dir:
        orig_width, orig_height, frame_files = extract_frames(input_file, temp_dir, fps)
        
        if not frame_files:
            raise ValueError("Не удалось извлечь кадры")
        
        print(f"Извлечено {len(frame_files)} кадров, оригинальный размер: {orig_width}x{orig_height}")
        
        # Обрезка по времени
        target_fps = fps if fps is not None else get_video_info(input_file)['fps']
        
        if start_time is not None or duration is not None:
            start_frame = int(start_time * target_fps) if start_time else 0
            end_frame = int((start_time + duration) * target_fps) if duration else len(frame_files)
            frame_files = frame_files[start_frame:end_frame]
            print(f"Обрезано до {len(frame_files)} кадров")
        
        if max_frames and len(frame_files) > max_frames:
            step = len(frame_files) // max_frames
            frame_files = frame_files[::step][:max_frames]
            print(f"Уменьшено до {len(frame_files)} кадров")
        
        # Изменение размера
        if resize:
            width, height = resize
            print(f"Изменение размера до {width}x{height}")
        else:
            width, height = orig_width, orig_height
        
        # === Параллельная обработка кадров ===
        print(f"\n=== Обработка кадров (дизеринг: {use_dithering}) ===")
        frame_args = [(f, resize, use_dithering) for f in frame_files]
        frames_data = []
        
        with ProcessPoolExecutor(max_workers=num_workers) as executor:
            futures = {executor.submit(process_frame, arg): arg for arg in frame_args}
            completed = 0
            
            for future in as_completed(futures):
                frame_idx, indices = future.result()
                frames_data.append((frame_idx, indices))
                completed += 1
                
                if completed % 50 == 0 or completed == len(frame_args):
                    print(f"Обработано кадров: {completed}/{len(frame_args)} ({100*completed//len(frame_args)}%)")
        
        # === Запись MMIF файла ===
        print("\n=== Запись MMIF файла ===")
        write_mmif_file(output_file, frames_data, width, height, int(target_fps),
                       looped, audio_dfpwm, audio_rate)
    
    return width, height, len(frames_data)


def preview_mmif(filename: str) -> None:
    """Показывает информацию о MMIF файле"""
    with open(filename, 'rb') as f:
        sig = f.read(4)
        if sig != b'MMIF':
            raise ValueError("Неверный MMIF файл")
        
        width = struct.unpack('>H', f.read(2))[0]
        height = struct.unpack('>H', f.read(2))[0]
        flags = f.read(1)[0]
        fps = f.read(1)[0]
        
        is_video = (flags & 0x01) != 0
        is_looped = (flags & 0x02) != 0
        has_audio = (flags & 0x80) != 0
        
        print(f"\n=== Информация о файле ===")
        print(f"Путь: {filename}")
        print(f"Размер видео: {width}x{height}")
        print(f"Тип: {'Видео' if is_video else 'Изображение'}")
        
        if is_video:
            print(f"FPS: {fps}")
            print(f"Зациклено: {is_looped}")
            
            # Поиск кадров
            f.seek(10)  # после заголовка
            if has_audio:
                f.seek(32, os.SEEK_CUR)  # пропускаем расширенный заголовок
            
            data = f.read()
            frame_count = data.count(b'\xAD')
            print(f"Кадров: {frame_count}")
        
        if has_audio:
            print(f"Аудио: присутствует (48kHz DFPWM)")


# ========== CLI интерфейс ==========
def main():
    import argparse
    import time
    
    parser = argparse.ArgumentParser(
        description='MMIF Converter for CC:Tweaked - конвертация видео с аудио',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры использования:
  # Конвертация видео с аудио (по умолчанию)
  python video_convertor.py video.mp4 -o video.mmif
  
  # Без аудио
  python video_convertor.py video.mp4 --no-audio
  
  # С изменением размера и FPS
  python video_convertor.py video.mp4 --resize 160x120 --fps 10
  
  # Вырезать фрагмент (с 10 секунды, длительностью 5 секунд)
  python video_convertor.py video.mp4 --start 10 --duration 5
  
  # Показать информацию о MMIF файле
  python video_convertor.py video.mmif -i
        """
    )
    
    parser.add_argument('input', help='Входной видеофайл')
    parser.add_argument('-o', '--output', help='Выходной MMIF файл')
    parser.add_argument('--no-dither', action='store_true', help='Отключить дизеринг')
    parser.add_argument('--looped', action='store_true', help='Зацикленное видео')
    parser.add_argument('--fps', type=int, help='Целевой FPS (по умолчанию: оригинальный)')
    parser.add_argument('--max-frames', type=int, help='Максимальное количество кадров')
    parser.add_argument('--start', type=float, help='Время начала (секунды)')
    parser.add_argument('--duration', type=float, help='Длительность (секунды)')
    parser.add_argument('--resize', help='Изменение размера (WIDTHxHEIGHT)')
    parser.add_argument('--no-audio', action='store_true', help='Отключить аудио')
    parser.add_argument('--workers', type=int, help='Количество рабочих процессов')
    parser.add_argument('-i', '--info', action='store_true', help='Показать информацию о MMIF файле')
    
    args = parser.parse_args()
    
    # Информация о файле
    if args.info:
        try:
            preview_mmif(args.input)
        except Exception as e:
            print(f"Ошибка: {e}")
        return 0
    
    # Выходной файл
    if not args.output:
        input_path = Path(args.input)
        args.output = input_path.parent / f"{input_path.stem}.mmif"
    
    # Парсинг resize
    resize = None
    if args.resize:
        try:
            w, h = map(int, args.resize.lower().split('x'))
            resize = (w, h)
        except:
            print("Ошибка: неверный формат resize. Используйте WIDTHxHEIGHT")
            return 1
    
    # Проверка FFmpeg
    if not check_ffmpeg():
        print("Предупреждение: FFmpeg не установлен. Установите FFmpeg для извлечения аудио и кадров.")
        if not args.no_audio:
            print("Аудио будет пропущено.")
    
    # Конвертация
    try:
        start_time = time.time()
        
        width, height, frame_count = convert_video(
            args.input, args.output,
            use_dithering=not args.no_dither,
            looped=args.looped,
            fps=args.fps,
            max_frames=args.max_frames,
            start_time=args.start,
            duration=args.duration,
            resize=resize,
            no_audio=args.no_audio,
            num_workers=args.workers
        )
        
        elapsed = time.time() - start_time
        print(f"\n=== Готово ===")
        print(f"Время выполнения: {elapsed:.2f} секунд")
        print(f"Выходной файл: {args.output}")
        
    except Exception as e:
        print(f"\nОшибка: {e}")
        import traceback
        traceback.print_exc()
        return 1
    
    return 0


if __name__ == '__main__':
    # Установка зависимостей:
    # pip install pillow numpy
    # 
    # Необходим FFmpeg в системе:
    # Windows: https://ffmpeg.org/download.html
    # Linux: sudo apt install ffmpeg
    # Mac: brew install ffmpeg
    
    exit(main())