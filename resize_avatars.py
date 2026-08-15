# resize_avatars.py — correr con: pip install Pillow
from PIL import Image, ImageSequence
import os

SRC_DIR = "static/img/avatars"
TARGET_SIZE = 112
QUALITY = 82
MAX_GIF_FRAMES = 40  # tope de frames para no generar webp animados gigantes


def process_frame(frame, target_size):
    frame = frame.convert("RGBA")
    w, h = frame.size
    side = min(w, h)
    left = (w - side) // 2
    top  = (h - side) // 2
    frame = frame.crop((left, top, left + side, top + side))
    if side > target_size:
        frame = frame.resize((target_size, target_size), Image.LANCZOS)
    return frame


for fname in os.listdir(SRC_DIR):
    path = os.path.join(SRC_DIR, fname)
    if not os.path.isfile(path):
        continue

    name, ext = os.path.splitext(fname)
    if ext.lower() not in (".png", ".webp", ".jpg", ".jpeg", ".gif"):
        continue

    out_path = os.path.join(SRC_DIR, f"{name}.webp")

    try:
        img = Image.open(path)
        is_animated = getattr(img, "is_animated", False)

        if is_animated:
            frames = []
            durations = []

            for i, frame in enumerate(ImageSequence.Iterator(img)):
                if i >= MAX_GIF_FRAMES:
                    break
                frames.append(process_frame(frame, TARGET_SIZE))
                durations.append(frame.info.get("duration", 100))

            # Cargamos todo en memoria antes de escribir, por si el archivo
            # de entrada y salida son el mismo path (ej: ya era .webp)
            for f in frames:
                f.load()

            frames[0].save(
                out_path,
                "WEBP",
                save_all=True,
                append_images=frames[1:],
                duration=durations,
                loop=0,
                quality=QUALITY,
                method=6
            )
            print(f"OK (animado, {len(frames)} frames): {fname} -> {name}.webp")

        else:
            frame = process_frame(img, TARGET_SIZE)
            frame.load()
            frame.save(out_path, "WEBP", quality=QUALITY)
            print(f"OK (estático): {fname} -> {name}.webp")

    except Exception as e:
        print(f"ERROR {fname}: {e}")