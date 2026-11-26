"""Build a version-specific, annotated feedback export."""
import io
import json
import tempfile
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

from PIL import Image, ImageDraw


def annotated_preview(path, regions):
    with Image.open(path) as source:
        source.thumbnail((1280, 1440), Image.Resampling.LANCZOS)
        image = source.convert("RGB")
    draw = ImageDraw.Draw(image)
    width, height = image.size
    weight = max(2, round(width / 320))
    for number, region in enumerate(regions, 1):
        x, y = region["x"] * width, region["y"] * height
        right = (region["x"] + region["w"]) * width
        bottom = (region["y"] + region["h"]) * height
        draw.ellipse((x, y, right, bottom), outline="#ffb83f", width=weight)
        label = str(number)
        draw.rounded_rectangle((x, y, x + 14 + len(label) * 7, y + 17), radius=4, fill="#ffb83f")
        draw.text((x + 6, y + 1), label, fill="#30220d")
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=84)
    return buffer.getvalue()


def make_feedback_bundle(payload, frame_source):
    result = tempfile.TemporaryFile()
    try:
        with ZipFile(result, "w", ZIP_STORED) as archive:
            manifest = []
            for row in payload["frames"]:
                name = f"marked/{row['number']:06}.jpg"
                archive.writestr(name, annotated_preview(frame_source(row), row["regions"]))
                manifest.append({key: value for key, value in row.items() if key != "asset_name"} | {"preview": name})
            archive.writestr("feedback.json", json.dumps(payload | {"frames": manifest}, ensure_ascii=False, indent=2),
                             compress_type=ZIP_DEFLATED)
        length = result.tell()
        result.seek(0)
        return result, length
    except BaseException:
        result.close()
        raise
