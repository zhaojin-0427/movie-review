"""Generate deterministic, original comparison frames for the internal demo."""
import hashlib
import io
import json
import sys
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from PIL import Image, ImageDraw

WIDTH, HEIGHT = 960, 540


def palette(white, color, gray):
    return gray if white else color


def scene(name, frame, white=False, corrected=False):
    street = name == "街角转弯"
    image = Image.new("RGB", (WIDTH, HEIGHT))
    draw = ImageDraw.Draw(image)
    upper = palette(white, (116, 167, 184) if street else (91, 105, 116), (148, 156, 161))
    lower = palette(white, (240, 213, 173) if street else (167, 144, 121), (207, 209, 208))
    for y in range(HEIGHT):
        t = y / HEIGHT
        draw.line((0, y, WIDTH, y), fill=tuple(round(a + (b - a) * t) for a, b in zip(upper, lower)))
    ground = palette(white, "#81766a" if street else "#75685f", "#969c9c")
    draw.polygon([(0, 407), (415, 258), (594, 258), (960, 377), (960, 540), (0, 540)], fill=ground)
    for x in range(-1200, 1800, 150):
        draw.line((500, 267, x, 540), fill=palette(white, "#95877a", "#a4abab"), width=2)
    for y in [303, 334, 380, 444, 525]:
        draw.line((0, y, 960, y), fill=palette(white, "#948476", "#a4abab"), width=2)
    for side in [0, 1]:
        for level in range(4):
            left = (15 + 96 * level) if side == 0 else (770 - 69 * level)
            bottom = 376 - 26 * level
            top = 67 + 31 * level
            width = 160 - 21 * level
            facade = palette(white, ["#bd8b6d", "#b88d76", "#c9a48a", "#a3a292"][level] if street
                             else ["#b5915f", "#77665e", "#a28058", "#977755"][level], "#c8c9c2")
            draw.polygon([(left, top), (left + width, top + 25), (left + width, bottom - 4), (left, bottom)], fill=facade)
            draw.polygon([(left+width, top+25), (left+width+34, top+39), (left+width+34, bottom-19),
                          (left+width, bottom-4)], fill=palette(white, "#746753", "#9c9f9c"))
            for window in range(2):
                y = top + 50 + 49 * window
                draw.rounded_rectangle((left + 12, y, left + width - 16, y + 27), 2,
                                       fill=palette(white, "#52737b" if street else "#4e4140", "#89948f"))
    actor_x = (310 if street else 295) + frame * 28
    actor_y = 367 + round(frame * 1.8)
    mismatch = white and ((frame == 2 and not corrected) or (frame == 5 and corrected))
    stride = 23 * (1 if frame % 2 else -1)
    actor_color = palette(white, "#437a72" if street else "#aa654c", "#edeee7")
    dark_actor = palette(white, "#2e4a51", "#bec5bc")
    draw.ellipse((actor_x - 71, actor_y + 52, actor_x + 65, actor_y + 75), fill=palette(white, "#62594f", "#727f7b"))
    draw.line((actor_x - 11, actor_y+10, actor_x-14-stride, actor_y+59), fill=dark_actor, width=16)
    draw.line((actor_x + 9, actor_y+10, actor_x+17+stride, actor_y+54), fill=actor_color, width=16)
    draw.rounded_rectangle((actor_x - 26, actor_y-57, actor_x+22, actor_y+18), radius=12, fill=actor_color)
    draw.line((actor_x+12, actor_y-45, actor_x+39, actor_y-34+(25 if mismatch else 0)), fill=dark_actor, width=15)
    draw.ellipse((actor_x-18, actor_y-95, actor_x+17, actor_y-61), fill=palette(white, "#d9a382", "#e0e0d7"))
    box_x = actor_x + (74 if mismatch else 47)
    y = actor_y - 43
    draw.polygon([(box_x-25,y-8), (box_x+11,y-17), (box_x+33,y-3), (box_x-6,y+6)],
                 fill=palette(white, "#e5b671", "#f4f0dc"))
    draw.polygon([(box_x-25,y-8), (box_x-6,y+6), (box_x-6,y+44), (box_x-25,y+27)],
                 fill=palette(white, "#b98550", "#b4b8ab"))
    draw.polygon([(box_x-6,y+6), (box_x+33,y-3), (box_x+33,y+31), (box_x-6,y+44)],
                 fill=palette(white, "#d39f60", "#d4d5c9"))
    return image


def generate(destination, width=WIDTH):
    destination.mkdir(parents=True, exist_ok=True)
    height = round(width * HEIGHT / WIDTH)
    for name, total in (("街角转弯", 12), ("仓库搬运", 8)):
        source_frames = [scene(name, n).resize((width, height), Image.Resampling.BICUBIC)
                         for n in range(1, total + 1)]
        source_identity = hashlib.sha256()
        for frame in source_frames:
            source_identity.update(frame.tobytes())
        for version in (1, 2):
            with ZipFile(destination / f"{name}-V{version}.zip", "w", ZIP_DEFLATED) as archive:
                start_ticks = [index * 1001 + (300 if index % 3 == 1 else 0) for index in range(total)]
                archive.writestr("manifest.json", json.dumps({"source_sha256": source_identity.hexdigest(),
                    "time_base": "1/30000", "pts": start_ticks, "end_pts": total * 1001}))
                for n in range(1, total + 1):
                    combined = Image.new("RGB", (width, height * 2))
                    combined.paste(source_frames[n - 1], (0, 0))
                    white = scene(name, n, white=True, corrected=version == 2)
                    combined.paste(white.resize((width, height), Image.Resampling.BICUBIC), (0, height))
                    buffer = io.BytesIO()
                    combined.save(buffer, "JPEG", quality=89, subsampling=0)
                    archive.writestr(f"pairs/{n:04}.jpg", buffer.getvalue())


if __name__ == "__main__":
    generate(Path(sys.argv[1] if len(sys.argv) > 1 else "demo"), int(sys.argv[2]) if len(sys.argv) > 2 else WIDTH)
