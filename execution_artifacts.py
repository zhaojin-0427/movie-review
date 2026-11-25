"""Validation for immutable artifacts exchanged with the production worker."""
import hashlib
import json
import re
import stat
import unicodedata
from pathlib import PurePosixPath
from PIL import Image
from zipfile import BadZipFile, ZipFile, ZIP_STORED

from packages import MAX_FRAMES, MAX_PACKAGE

SHA = re.compile(r'[a-f0-9]{64}\Z')
MAX_ARTIFACT = 16 * 1024**3
MAX_JSON = 16 * 1024**2


def json_object(data):
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError('需要 JSON 对象')
    return value


def canonical(value):
    # Protocol manifests contain strings, booleans, null and safe integers only.
    def ordered(item):
        if isinstance(item, dict):
            return {k: ordered(item[k]) for k in sorted(item, key=lambda k: k.encode('utf-16-be'))}
        if isinstance(item, list):
            return [ordered(v) for v in item]
        if item is None or isinstance(item, (str, bool)) or (type(item) is int and abs(item) < 2**53):
            return item
        raise ValueError('JSON 含不支持的数值或字段类型')
    return json.dumps(ordered(value), ensure_ascii=False, separators=(',', ':')).encode()


def object_hash(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def file_hash(path):
    with path.open('rb') as file:
        return hashlib.file_digest(file, 'sha256').hexdigest()


def require(ok, message):
    if not ok:
        raise ValueError(message)


def read_json(path):
    require(path.stat().st_size <= MAX_JSON, 'JSON 工件超过大小限制')
    return json_object(path.read_bytes())


def check_entries(archive):
    entries = archive.infolist()
    require(0 < len(entries) <= 50000 and sum(x.file_size for x in entries) <= MAX_ARTIFACT,
            'ZIP 文件数量或展开大小超限')
    files, names = {}, set()
    for entry in entries:
        name = entry.orig_filename.removesuffix('/') if entry.is_dir() else entry.orig_filename
        parts = name.split('/')
        folded = unicodedata.normalize('NFC', name).casefold()
        require(all(p and p not in ('.', '..') and p[-1] not in ' .' for p in parts)
                and not re.search(r'[<>:"\\|?*\x00-\x1f]', name) and folded not in names
                and stat.S_IFMT(entry.external_attr >> 16) in (0, stat.S_IFDIR if entry.is_dir() else stat.S_IFREG)
                and not entry.flag_bits & 1 and entry.file_size <= 1000 * max(1, entry.compress_size), 'ZIP 路径或文件属性无效')
        names.add(folded)
        if not entry.is_dir():
            files[name] = entry
    for name in names:
        require(not any(str(parent) in files for parent in PurePosixPath(name).parents), 'ZIP 文件与目录重叠')
    return files


def bundle(path, consume=None, *, indexed=True):
    try:
        with ZipFile(path) as archive:
            files = check_entries(archive)
            if not indexed:
                return None
            index_entry = files.pop('index.json', None)
            require(index_entry is not None and index_entry.file_size <= MAX_JSON, '工件缺少有效的 index.json')
            index = json_object(archive.read(index_entry))
            indexed_files = index['files']
            expected = {entry['path']: entry for entry in indexed_files}
            require(len(expected) == len(indexed_files) and expected.keys() == files.keys(), '工件文件与索引不匹配')
            for name, entry in files.items():
                record = expected[name]
                with archive.open(entry) as stream:
                    checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
                require(record['size_bytes'] == entry.file_size and record['sha256'] == checksum, '工件成员大小或摘要不匹配')
            if consume:
                consume(archive, files, index)
            return index
    except (BadZipFile, RuntimeError, NotImplementedError) as error:
        raise ValueError('无法读取工件 ZIP') from error


def frame_size(value):
    width, height = value['width'], value['height']
    require(type(width) is int and type(height) is int and 0 < width <= 16384 and 0 < height <= 16384, '画幅无效')
    return width, height


def review_package(path_for, outputs, timeline, version, target):
    frames = version['frames']
    ticks = [frame['pts'] for frame in timeline['frames']]
    count = len(frames)
    require(0 < count <= MAX_FRAMES and version['frame_count'] == count and len(ticks) == count,
            '审核帧数与时间线不符')
    width, height = frame_size(version['review_size'])
    storage = version.get('review_storage')
    if storage:
        limit = (1920, 1080) if width >= height else (1080, 1920)
        require(width <= limit[0] and height <= limit[1], '同机审片图超过 1080p 范围')

    def convert(archive, files, index):
        fields = ('job_id', 'source_sha256', 'timeline_sha256', 'review_size', 'final_size', 'frames')
        require(all(index.get(key) == version[key] for key in fields), '审核包索引与版本清单不符')
        require(index.get('review_storage') == storage, '审核包索引的审片存储模式不符')
        if storage:
            require(index.get('target_version') == version['version'] and index.get('scene_sha256') == version['scene_sha256']
                    and len(archive.infolist()) == len(files) + 1, '同机审片索引的版号、工程摘要或文件列表无效')
        used = set()
        white_paths = set()
        length = 0
        with ZipFile(target, 'w', ZIP_STORED) as output:
            for n, (frame, tick) in enumerate(zip(frames, ticks, strict=True), 1):
                image, white = frame['pair_jpeg_path'], frame['white_png_path']
                require(frame['number'] == n and frame['pts'] == tick and SHA.fullmatch(frame['review_signature'])
                        and image not in used and white not in white_paths and image != white and image in files,
                        '审核帧路径、签名或序号无效')
                used.add(image)
                white_paths.add(white)
                if storage:
                    require(re.fullmatch(r'pairs/[0-9]+[.]jpg', image)
                            and white.endswith('.png') and not re.search(r'[<>:"\\|?*\x00-\x1f]', white)
                            and all(part and part not in ('.', '..') and part[-1] not in ' .' for part in white.split('/')),
                            '同机审片包只接受索引和对比 JPG')
                else:
                    require(white in files and white not in used, '审核包缺少白模帧或帧路径重复')
                    used.add(white)
                length += files[image].file_size
                require(length <= MAX_PACKAGE and files[image].file_size <= 32 * 1024**2, '审核图超过平台大小限制')
                with archive.open(image) as stream, Image.open(stream, formats=['JPEG']) as pair:
                    require(pair.size == (width, 2 * height), '审核对比图画幅不符')
                if not storage:
                    with archive.open(white) as stream, Image.open(stream, formats=['PNG']) as image_file:
                        require(image_file.size == (width, height), '白模帧画幅不符')
                output.writestr(f'pairs/{n:04d}.jpg', archive.read(image))
            require(used == files.keys(), '审核包含额外文件')
            output.writestr('manifest.json', json.dumps(dict(source_sha256=timeline['source_sha256'],
                time_base=timeline['time_base'], pts=ticks, end_pts=timeline['end_pts'])))
    bundle(path_for(outputs['review_bundle']), convert)
