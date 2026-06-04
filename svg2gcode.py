#!/usr/bin/env python3
import os
import sys
import argparse
import subprocess
import yaml
import xml.etree.ElementTree as ET
from PIL import Image

MM_PER_INCH = 25.4
TEMP_PNG = "temp_board_render.png"
FAST_TRAVEL_SPEED = 2500 # Скорость G00 для калькулятора

def get_svg_dimensions(file_path):
    try:
        tree = ET.parse(file_path)
        root = tree.getroot()
        width_str = root.attrib.get('width', '')
        height_str = root.attrib.get('height', '')
        w_mm = float(width_str.replace('mm', '').replace('in', '').strip())
        h_mm = float(height_str.replace('mm', '').replace('in', '').strip())
        if 'in' in width_str: w_mm *= MM_PER_INCH
        if 'in' in height_str: h_mm *= MM_PER_INCH
        return w_mm, h_mm
    except Exception as e:
        print(f"Ошибка при автоматическом чтении размеров SVG: {e}")
        sys.exit(1)

parser = argparse.ArgumentParser(description="Автоматический конвертер векторных плат SVG в лазерный растр GRBL.")
parser.add_argument("input_svg", help="Путь к исходному SVG-файлу из KiCad")
parser.add_argument("-c", "--config", default="config.yaml", help="Путь к файлу конфигурации YAML")
parser.add_argument("-o", "--output", default="front_raster.nc", help="Имя итогового G-код файла")

args = parser.parse_args()

if not os.path.exists(args.config):
    print(f"Ошибка: Конфигурационный файл '{args.config}' не найден!")
    sys.exit(1)

with open(args.config, "r") as f:
    try: config = yaml.safe_load(f)
    except yaml.YAMLError as exc: sys.exit(1)

SPEED = config["laser"]["speed"]
POWER = config["laser"]["power"]
BEAM_DIA = config["laser"]["beam_diameter"]
OVERLAP = config["laser"]["overlap_percent"]
NEGATIVE = config["board"]["negative"]
MIRROR_X = config["board"]["mirror_x"]
X_OFFSET = config["offset"]["x"]
Y_OFFSET = config["offset"]["y"]

TARGET_WIDTH_MM, TARGET_HEIGHT_MM = get_svg_dimensions(args.input_svg)

STEP = BEAM_DIA * (1.0 - (OVERLAP / 100.0))
PIXELS_W = int(TARGET_WIDTH_MM / STEP)
PIXELS_H = int(TARGET_HEIGHT_MM / STEP)
DPI = round(MM_PER_INCH / STEP)

print("=== СТАРТ АВТОМАТИЧЕСКОЙ ОБРАБОТКИ ===")
print(f"1. Реальные размеры платы: {TARGET_WIDTH_MM:.2f} x {TARGET_HEIGHT_MM:.2f} мм")

try:
    subprocess.run([
        "inkscape", f"--export-filename={TEMP_PNG}", "--export-area-drawing",
        f"--export-width={PIXELS_W}", f"--export-height={PIXELS_H}",
        f"--export-dpi={DPI}", args.input_svg
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
except Exception: sys.exit(1)

try: img_raw = Image.open(TEMP_PNG)
except Exception: sys.exit(1)

img_white_bg = Image.new("RGBA", img_raw.size, "WHITE")
img_white_bg.paste(img_raw, (0, 0), img_raw if img_raw.mode == "RGBA" else None)
img_gray = img_white_bg.convert("L")

pixels = img_gray.load()
width, height = img_gray.size

STEP_X = TARGET_WIDTH_MM / width
STEP_Y = TARGET_HEIGHT_MM / height

total_g01_dist = 0.0
total_g00_dist = 0.0

print("2. Генерация сжатого растрового G-кода...")
with open(args.output, "w") as f:
    f.write("G21\nG90\nM4 S0\n")
    
    last_x, last_y = X_OFFSET, Y_OFFSET + (height * STEP_Y)
    
    for y in range(height):
        real_y = Y_OFFSET + ((height - y) * STEP_Y)
        start_x = X_OFFSET if y % 2 == 0 else (X_OFFSET + TARGET_WIDTH_MM)
        
        # Перелёт к началу новой строки (всегда G00)
        f.write(f"G00 X{start_x:.3f} Y{real_y:.3f}\n")
        total_g00_dist += ((start_x - last_x)**2 + (real_y - last_y)**2)**0.5
        last_x, last_y = start_x, real_y
        
        x_range = range(width) if y % 2 == 0 else range(width - 1, -1, -1)
        last_burn = None  # Сбрасываем флаг сжатия для каждой новой строки
        
        for x in x_range:
            real_x = X_OFFSET + (x * STEP_X)
            read_x = (width - 1 - x) if MIRROR_X else x
            
            is_dark_pixel = pixels[read_x, y] < 128
            should_burn = not is_dark_pixel if NEGATIVE else is_dark_pixel
            
            # ЖЕСТКИЙ АЛГОРИТМ СЖАТИЯ ПОТОКА КОМАНД
            if should_burn:
                if last_burn is not True:
                    f.write(f"G01 F{SPEED} X{real_x:.3f} S{POWER}\n")
                    total_g01_dist += abs(real_x - last_x)
                    last_x = real_x
                    last_burn = True
            else:
                if last_burn is not False:
                    f.write(f"G00 X{real_x:.3f}\n")
                    total_g00_dist += abs(real_x - last_x)
                    last_x = real_x
                    last_burn = False

        # Дописываем финиш строки движения
        end_x = X_OFFSET + (TARGET_WIDTH_MM if y % 2 == 0 else 0.0)
        if last_burn is True:
            total_g01_dist += abs(end_x - last_x)
        else:
            total_g00_dist += abs(end_x - last_x)
            
        f.write(f"G00 X{end_x:.3f}\n")
        last_x = end_x
                
    f.write("M5\n")
    f.write(f"G00 X{X_OFFSET:.3f} Y{Y_OFFSET:.3f}\n")
    f.write("M2\n")
    total_g00_dist += ((X_OFFSET - last_x)**2 + (Y_OFFSET - last_y)**2)**0.5

if os.path.exists(TEMP_PNG):
    os.remove(TEMP_PNG)

# Математика расчета времени (минуты)
time_g01_min = total_g01_dist / SPEED
time_g00_min = total_g00_dist / FAST_TRAVEL_SPEED
total_time_min_with_accel = (time_g01_min + time_g00_min) * 1.10

total_seconds = int(total_time_min_with_accel * 60)
hours = total_seconds // 3600
minutes = (total_seconds % 3600) // 60
seconds = total_seconds % 60
time_str = f"{minutes} мин {seconds} сек" if hours == 0 else f"{hours} ч {minutes} мин {seconds} сек"

print("\n" + "="*40)
print("  ГЕНЕРАЦИЯ УСПЕШНО ЗАВЕРШЕНА!")
print("="*40)
print(f" Входной вектор:   {args.input_svg}")
print(f" Разрешение сетки: {width}x{height} пикс")
print(f" Шаг лазера по X:  {STEP_X:.4f} мм")
print(f" ИСТИННЫЙ РАЗМЕР:  {TARGET_WIDTH_MM:.2f} x {TARGET_HEIGHT_MM:.2f} мм")
print(f" Длина ходов:      G01(выжигание) = {total_g01_dist/1000:.2f} м")
print(f"                   G00(перелёты)  = {total_g00_dist/1000:.2f} м")
print(f" РАСЧЕТНОЕ ВРЕМЯ:  ~ {time_str} (с учетом ускорений)")
print("="*40)
print(f" Чистый G-код сохранен в: {args.output}")
print("="*40 + "\n")

