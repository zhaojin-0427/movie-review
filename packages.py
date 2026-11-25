"""Validate manually submitted comparison frames and prepare private assets."""
import hashlib
import io
import json
import re
import stat
import warnings
from dataclasses import dataclass
from fractions import Fraction
from pathlib import PurePosixPath
from zipfile import BadZipFile, ZipFile

from PIL import Image

MAX_PACKAGE = 256 * 1024 * 1024
MAX_FRAME = 14 * 1024 * 1024
MAX_FRAMES = 2400
MAX_PIXELS = 12_000_000
FRAME_NAME = re.compile(r"pairs/(\d{1,6})\.(?:jpe?g|png)", re.IGNORECASE)


@dataclass(frozen=True)
class Frame:
    number: int
    asset_name: str
    mimetype: str
    width: int
    height: int
    signature: str
    time_ms: int | None
    duration_ms: int | None


def read_manifest(archive, entry, count):
    if entry is None:
        return None, [(None, None, None, None)] * count
    if entry.file_size > 512 * 1024:
        raise ValueError("时间线文件超过 512 KiB")
    try:
        value = json.loads(archive.read(entry))
    except (ValueError, RecursionError) as error:
        raise ValueError("manifest.json 无法解析") from error
    if not isinstance(value, dict):
        raise ValueError("manifest.json 应为 JSON 对象")
    source = value.get("source_sha256")
    if source is not None:
        if not isinstance(source, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", source):
            raise ValueError("source_sha256 应为原片文件的 64 位十六进制 SHA-256")
        source = source.lower()
    millisecond_fields = {"time_ms", "end_ms"}
    pts_fields = {"time_base", "pts", "end_pts"}
    fields = value.keys()
    if not fields & (millisecond_fields | pts_fields):
        return source, [(None, None, None, None)] * count
    if fields & pts_fields:
        raw_base = value.get("time_base")
        if (fields & millisecond_fields or not isinstance(raw_base, str)
                or not re.fullmatch(r"[1-9][0-9]{0,8}/[1-9][0-9]{0,8}", raw_base)):
            raise ValueError("请填写正有理数 time_base 和 pts、end_pts，或只用毫秒时间线")
        base = Fraction(raw_base)
        times, end = value.get("pts"), value.get("end_pts")
    else:
        base = Fraction(1, 1000)
        times, end = value.get("time_ms"), value.get("end_ms")
    if (not isinstance(times, list) or len(times) != count
            or any(type(tick) is not int or not 0 <= tick <= 2**53-1 for tick in times)
            or type(end) is not int or not 1 <= end <= 2**53-1 or end * base > 7 * 24 * 3600):
        raise ValueError("时间线需要与帧数一致的整数起点和最后一帧的结束时间")
    endpoints = [*times, end]
    if times[0] != 0 or any(a >= b for a, b in zip(endpoints, endpoints[1:])):
        raise ValueError("时间线必须从 0 开始递增，结束时间晚于末帧")
    return source, [(str(start * base), str(stop * base), round(start * base * 1000),
                     round(stop * base * 1000) - round(start * base * 1000))
                    for start, stop in zip(endpoints, endpoints[1:])]


def save_frame(archive, entry, destination, number, timing):
    data = archive.read(entry)
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(data), formats=["JPEG", "PNG"]) as image:
            if image.width * image.height > MAX_PIXELS or getattr(image, "is_animated", False):
                raise ValueError(f"第 {number} 帧尺寸过大或是动图")
            width, height = image.size
            if width < 2 or height < 2 or height % 2:
                raise ValueError(f"第 {number} 帧需要等高的上下对比图")
            orientation = image.getexif().get(274, 1)
            if orientation != 1:
                raise ValueError(f"第 {number} 帧请先应用 EXIF 旋转再上传")
            # Restrict sources to opaque comparison images so the digest describes the display.
            if image.mode not in {"RGB", "L"} or "transparency" in image.info:
                raise ValueError(f"第 {number} 帧需要无透明通道的普通 RGB 或灰度图片")
            display_metadata = [hashlib.sha256(image.info.get("icc_profile") or b"").hexdigest(),
                                image.info.get("gamma"), image.info.get("srgb"), image.info.get("chromaticity")]
            scheme = json.dumps([number, *timing[:2], width, height, display_metadata], separators=(",", ":"))
            digest = hashlib.sha256(scheme.encode())
            digest.update(image.convert("RGB").tobytes())
            extension = "jpg" if image.format == "JPEG" else "png"
    asset_name = f"{number:06}.{extension}"
    (destination / asset_name).write_bytes(data)
    return Frame(number, asset_name, f"image/{'jpeg' if extension == 'jpg' else 'png'}",
                 width, height, digest.hexdigest(), *timing[2:])


def read_package(stream, destination):
    """Expect a ZIP with pairs/0001.jpg (or PNG) and an optional manifest.json."""
    try:
        with ZipFile(stream) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_FRAMES + 5 or sum(e.file_size for e in entries) > MAX_PACKAGE:
                raise ValueError("压缩包展开后超过大小或文件数上限")
            files, timing_entry, names = {}, None, set()
            for entry in entries:
                name = entry.filename
                path = PurePosixPath(name)
                file_type = stat.S_IFMT(entry.external_attr >> 16)
                if (path.is_absolute() or ".." in path.parts or "\\" in name or name.casefold() in names
                        or file_type not in {0, stat.S_IFDIR, stat.S_IFREG} or entry.flag_bits & 1):
                    raise ValueError("压缩包包含重复文件、加密文件或异常路径")
                names.add(name.casefold())
                if entry.is_dir() and name == "pairs/":
                    continue
                if name == "manifest.json":
                    timing_entry = entry
                    continue
                match = FRAME_NAME.fullmatch(name)
                if not match:
                    raise ValueError("压缩包根目录只接受 pairs/ 和可选 manifest.json")
                number = int(match[1])
                if number in files or not 0 < entry.file_size <= MAX_FRAME:
                    raise ValueError(f"第 {number} 帧编号重复或文件过大")
                files[number] = entry
            if not files or len(files) > MAX_FRAMES or sorted(files) != list(range(1, len(files) + 1)):
                raise ValueError(f"对比图须从 1 连续编号，最多 {MAX_FRAMES} 帧")
            source_sha256, timing = read_manifest(archive, timing_entry, len(files))
            frames = [save_frame(archive, files[n], destination, n, tick)
                      for n, tick in enumerate(timing, 1)]
            if len({(frame.width, frame.height) for frame in frames}) != 1:
                raise ValueError("同轮审核的对比图尺寸必须一致")
            return source_sha256, frames
    except (BadZipFile, SyntaxError, OSError, NotImplementedError, RuntimeError,
            Image.DecompressionBombError, Image.DecompressionBombWarning) as e:
        raise ValueError("读取失败，请检查 ZIP 文件及包内的 JPEG/PNG 对比图") from e
