"""Validation for immutable artifacts exchanged with the production worker."""
import hashlib
import json
import re
import shutil
import stat
import unicodedata
from pathlib import PurePosixPath
from PIL import Image
from zipfile import BadZipFile, ZipFile

from packages import MAX_FRAME, MAX_FRAMES, read_timeline, save_frame

SHA = re.compile(r'[a-f0-9]{64}\Z')
MAX_ARTIFACT = 16 * 1024**3
MAX_JSON = 16 * 1024**2
REJECTED_FRAMES_MODE = 'rejected_frames_v1'


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
                result = consume(archive, files, index)
                if result is not None:
                    return result
            return index
    except (BadZipFile, RuntimeError, NotImplementedError) as error:
        raise ValueError('无法读取工件 ZIP') from error


def frame_size(value):
    width, height = value['width'], value['height']
    require(type(width) is int and type(height) is int and 0 < width <= 16384 and 0 < height <= 16384, '画幅无效')
    return width, height


def review_index_matches(index, version):
    fields = ('job_id', 'source_sha256', 'timeline_sha256', 'review_size', 'final_size', 'frames')
    require(all(index.get(key) == version[key] for key in fields), '审核包索引与版本清单不符')
    require(index.get('review_storage') == version.get('review_storage'), '审核包索引的审片存储模式不符')


def materialize_review_delta(path, base_path, destination, version, base, feedback, base_hash):
    delta = version['review_delta']
    submitted = delta['frames']
    required_frames = {f['number'] for f in feedback['frames']}
    require(isinstance(submitted, list) and required_frames and all(type(n) is int for n in submitted)
            and submitted == sorted(set(submitted)) and required_frames <= set(submitted)
            and delta == dict(mode=REJECTED_FRAMES_MODE, base_version=base['version'], base_manifest_sha256=base_hash,
                              frames=submitted, full_files_sha256=delta['full_files_sha256'])
            and SHA.fullmatch(delta['full_files_sha256']), '增量帧或基版与冻结返修意见不匹配')
    frames, prior = version['frames'], base['frames']
    require(len(frames) == len(prior) and all(1 <= n <= len(frames) for n in submitted)
            and all(version[key] == base[key] for key in ('job_id', 'source_sha256', 'timeline_sha256',
                                                         'review_size', 'final_size'))
            and version.get('review_storage') == base.get('review_storage'), '增量审核包的基版或画幅不匹配')
    submitted = set(submitted)
    roles = ('pair_jpeg_path',) if version.get('review_storage') else ('pair_jpeg_path', 'white_png_path')
    changed, inherited = set(), set()
    for number, (frame, previous) in enumerate(zip(frames, prior, strict=True), 1):
        if number in submitted:
            changed.update(frame[role] for role in roles)
        else:
            require(frame == previous, '变化的帧须包含在增量审核包中，未提交的帧须与基版一致')
            inherited.update(frame[role] for role in roles)

    def merge(archive, files, index):
        review_index_matches(index, version)
        require(index.get('review_delta') == delta and index.get('target_version') == version['version']
                and index.get('scene_sha256') == version['scene_sha256']
                and files.keys() == changed and len(archive.infolist()) == len(changed) + 1,
                '增量审核包文件、索引或工程摘要不符')

        def copy_frames(previous_archive, previous_files, previous_index):
            review_index_matches(previous_index, base)
            require('review_delta' not in previous_index and inherited <= previous_files.keys()
                    and not changed & inherited, '基版全帧审核包缺失或有路径冲突')
            frozen = {item['path']: item for item in previous_index['files']}
            if base.get('review_delta'):
                require(object_hash([frozen[name] for name in sorted(frozen)]) == base['review_delta']['full_files_sha256'],
                        '基版审核包文件索引摘要不匹配')
            full_files = sorted(index['files'] + [frozen[name] for name in inherited], key=lambda f: f['path'])
            require(sum(f['size_bytes'] for f in full_files) <= MAX_ARTIFACT
                    and object_hash(full_files) == delta['full_files_sha256'], '合并后的审核包文件摘要或大小不符')
            full_index = dict(index, files=full_files)
            del full_index['review_delta']
            with ZipFile(destination, 'x') as merged:
                merged.writestr('index.json', json.dumps(full_index, ensure_ascii=False).encode())
                for item in full_files:
                    name = item['path']
                    source = archive if name in changed else previous_archive
                    with source.open(name) as src, merged.open(name, 'w', force_zip64=True) as dst:
                        shutil.copyfileobj(src, dst, 1024 * 1024)

        bundle(base_path, copy_frames)

    bundle(path, merge)


def stage_review_frames(path, timeline, version, destination):
    frames = version['frames']
    ticks = [frame['pts'] for frame in timeline['frames']]
    count = len(frames)
    require(0 < count <= MAX_FRAMES and version['frame_count'] == count and len(ticks) == count,
            '审核帧数与时间线不符')
    source_hash, timing = read_timeline(dict(source_sha256=timeline['source_sha256'],
        time_base=timeline['time_base'], pts=ticks, end_pts=timeline['end_pts']), count)
    width, height = frame_size(version['review_size'])
    storage = version.get('review_storage')
    if storage:
        limit = (1920, 1080) if width >= height else (1080, 1920)
        require(width <= limit[0] and height <= limit[1], '同机审片图超过 1080p 范围')

    def convert(archive, files, index):
        review_index_matches(index, version)
        require('review_delta' not in index, '审核包须先合并为完整版本')
        if storage:
            require(index.get('target_version') == version['version'] and index.get('scene_sha256') == version['scene_sha256']
                    and len(archive.infolist()) == len(files) + 1, '同机审片索引的版号、工程摘要或文件列表无效')
        used = set()
        white_paths = set()
        staged = []
        for n, (frame, tick, frame_timing) in enumerate(zip(frames, ticks, timing, strict=True), 1):
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
                with archive.open(white) as stream, Image.open(stream, formats=['PNG']) as image_file:
                    require(image_file.size == (width, height), '白模帧画幅不符')
            require(0 < files[image].file_size <= MAX_FRAME, f'第 {n} 张审核图文件大小超限')
            try:
                saved = save_frame(archive, files[image], destination, n, frame_timing, formats=('JPEG',))
            except (OSError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
                raise ValueError(f'第 {n} 张审核图不是有效的 JPEG') from error
            require((saved.width, saved.height) == (width, 2 * height), '审核对比图画幅不符')
            staged.append(saved)
        require(used == files.keys(), '审核包含额外文件')
        return source_hash, staged

    return bundle(path, convert)
