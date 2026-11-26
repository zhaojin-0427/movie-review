"""Validate the object-only submission contract without reading JPG or ZIP bodies."""
import hashlib
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict

import oss2

from execution_artifacts import MAX_ARTIFACT, MAX_JSON, SHA, frame_size, json_object, object_hash, require
from oss_storage import OBJECTS, VERSION_ID
from packages import Frame, MAX_FRAME, MAX_FRAMES, MAX_PIXELS, read_timeline

REVIEW_FILES = {
    'review-index.json': ('review_index', 'application/json'),
    'version.json': ('version_manifest', 'application/json'),
    'timeline.json': ('source_timeline', 'application/json'),
    'meta.jsonl': ('shot_metadata', 'application/x-ndjson'),
    'production.json': ('production_report', 'application/json'),
}
DELIVERY_FILES = {
    'delivery.zip': ('delivery_bundle', 'application/zip'),
    'white.mp4': ('white_video', 'video/mp4'),
    'comparison.mp4': ('comparison_video', 'video/mp4'),
}
PAIR = re.compile(r'pairs/([0-9]{6})[.]jpg\Z')
HASH_HEADER = 'x-oss-meta-sha256'


def pair_name(number):
    return f'pairs/{number:06d}.jpg'


def file_type(kind, name):
    files = DELIVERY_FILES if kind == 'finalize' else REVIEW_FILES
    if name in files and (name != 'timeline.json' or kind == 'prepare'):
        role, mime = files[name]
        return role, mime, MAX_ARTIFACT if kind == 'finalize' else MAX_JSON
    pair = PAIR.fullmatch(name)
    if kind != 'finalize' and pair and 1 <= int(pair[1]) <= MAX_FRAMES:
        return None, 'image/jpeg', MAX_FRAME
    raise ValueError('提交包含不允许的文件路径')


def declaration(value, limit, *, versioned=True):
    needed = {'size_bytes', 'sha256', 'version_id'} if versioned else {'size_bytes', 'sha256'}
    require(isinstance(value, dict) and set(value) == needed, '对象声明字段不完整')
    size, sha = value['size_bytes'], value['sha256']
    require(type(size) is int and 0 < size <= limit and isinstance(sha, str) and SHA.fullmatch(sha),
            '对象大小或 SHA-256 无效')
    if versioned:
        version = value['version_id']
        require(isinstance(version, str) and VERSION_ID.fullmatch(version) and version != 'null',
                'OSS 审核桶须开启版本控制，提交须包含对象 version_id')


def declarations(kind, values):
    require(isinstance(values, dict) and 0 < len(values) <= MAX_FRAMES + len(REVIEW_FILES), '对象数量无效')
    for name, value in values.items():
        _, _, limit = file_type(kind, name)
        declaration(value, limit)


def object_head(bucket, ref):
    try:
        return bucket.head_object(ref.key, params=ref.params)
    except oss2.exceptions.NotFound as error:
        raise ValueError('OSS 对象或指定版本不存在') from error


def confirm_object(bucket, ref, size, sha, mime):
    head = object_head(bucket, ref)
    require(head.content_length == size and head.headers.get(HASH_HEADER) == sha
            and head.headers.get('Content-Type', '').split(';')[0].strip() == mime
            and (not ref.version_id or head.headers.get('x-oss-version-id') == ref.version_id),
            'OSS 对象版本、大小、类型或摘要元数据不匹配')
    return head


def metadata_bytes(bucket, ref, item):
    require(0 < item['size_bytes'] <= MAX_JSON, '清单超过大小限制')
    with bucket.get_object(ref.key, params=ref.params) as response:
        data = response.read(item['size_bytes'] + 1)
    require(len(data) == item['size_bytes'] and hashlib.sha256(data).hexdigest() == item['sha256'],
            '清单文件 SHA-256 校验失败')
    return data


def inspect_objects(bucket, directory, kind, objects):
    declarations(kind, objects)

    def inspect(entry):
        name, value = entry
        role, mime, _ = file_type(kind, name)
        ref = directory.at(name, value['version_id'])
        confirm_object(bucket, ref, value['size_bytes'], value['sha256'], mime)
        return name, (role, mime, ref)

    # Reuse one OSS SDK connection pool. Per-frame requests only read headers.
    with ThreadPoolExecutor(max_workers=8) as pool:
        return dict(pool.map(inspect, objects.items()))


def validate_review(bucket, work, order, inputs, objects, inspected, read_input, inherited):
    first = work['kind'] == 'prepare'
    roles = {role: name for name, (role, _, _) in inspected.items() if role}
    required = set(role for role, _ in REVIEW_FILES.values()) - (set() if first else {'source_timeline'})
    require(set(roles) == required, '送审清单或产物缺失')

    def value(role):
        name = roles[role]
        return json_object(metadata_bytes(bucket, inspected[name][2], objects[name]))

    raw_version = metadata_bytes(bucket, inspected[roles['version_manifest']][2], objects[roles['version_manifest']])
    version = json_object(raw_version)
    timeline_item = objects['timeline.json'] if first else inputs['source_timeline']
    timeline = value('source_timeline') if first else read_input('source_timeline')
    index = value('review_index')
    bindings = dict(job_id=str(work['job']), version=work['target'], source_sha256=inputs['source_video']['sha256'],
                    timeline_sha256=timeline_item['sha256'], input_manifest_sha256=work['manifest_hash'],
                    review_storage=order['review_storage'], review_media=OBJECTS)
    expected = bindings | dict(review_index_sha256=objects['review-index.json']['sha256'],
                               shot_metadata_sha256=objects['meta.jsonl']['sha256'],
                               production_report_sha256=objects['production.json']['sha256'],
                               production_method='codex_trial')
    require(all(version.get(key) == expected_value for key, expected_value in expected.items())
            and all(index.get(key) == expected_value for key, expected_value in bindings.items())
            and isinstance(version.get('scene_sha256'), str) and SHA.fullmatch(version['scene_sha256']),
            '送审清单与冻结工单或产物不一致')
    width, height = frame_size(version['review_size'])
    limit = (1920, 1080) if width >= height else (1080, 1920)
    require(width >= 2 and height >= 1 and width <= limit[0] and height <= limit[1]
            and width * height * 2 <= MAX_PIXELS, '同机审核图画幅无效')
    frame_size(version['final_size'])
    frames = version['frames']
    require(isinstance(frames,list) and 0 < len(frames) <= MAX_FRAMES, '审核帧清单无效')
    count = len(frames)
    require(type(version['frame_count']) is int and version['frame_count'] == count
            and len(timeline['frames']) == count and timeline['source_sha256'] == bindings['source_sha256'],
            '审核帧数或原片与时间线不符')
    ticks = [frame['pts'] for frame in timeline['frames']]
    source, timing = read_timeline(dict(source_sha256=timeline['source_sha256'], pts=ticks,
                                        time_base=timeline['time_base'], end_pts=timeline['end_pts']), count)
    if first:
        changed = set(range(1, count + 1))
        require(index.get('base_manifest_sha256') is None, '首版不能引用返修基版')
        old_frames = {}
    else:
        feedback = read_input('feedback_manifest')
        base = read_input('version_manifest')
        changed = {frame['number'] for frame in feedback['frames']}
        require(changed and all(type(n) is int and 1 <= n <= count for n in changed)
                and index.get('base_manifest_sha256') == inputs['version_manifest']['sha256']
                and len(base['frames']) == count
                and all(base[key] == version[key] for key in ('review_size','final_size','timeline_sha256',
                                                              'source_sha256','review_storage','review_media')),
                '返修基版、打回范围或画幅不一致')
        old_frames = {frame['number']:frame for frame in base['frames']}
    names = {pair_name(n) for n in changed}
    require(names == {name for name,(role,_,_) in inspected.items() if not role}, '仅可上传本轮要求的帧')
    expected_index = [dict(number=n, size_bytes=objects[pair_name(n)]['size_bytes'],
                           sha256=objects[pair_name(n)]['sha256']) for n in sorted(changed)]
    require(index.get('frames') == expected_index, '审核图清单的帧号、大小或摘要不匹配')

    result, locations = [], {}
    for n, (frame, tick, times) in enumerate(zip(frames, ticks, timing, strict=True), 1):
        signature = frame['review_signature']
        require(type(frame['number']) is int and frame['number'] == n and frame['pts'] == tick
                and frame['pair_jpeg_path'] == pair_name(n)
                and isinstance(signature,str) and SHA.fullmatch(signature), '审核帧号、时序、路径或签名无效')
        if n in changed:
            name = pair_name(n)
            sha = objects[name]['sha256']
            content_signature = object_hash([n, *times[:2], width, 2*height, sha])
            ref = inspected[name][2].public()
        else:
            require(frame == old_frames.get(n) and n in inherited, '打回范围外的帧必须沿用基版')
            content_signature, ref = inherited[n]
            require(ref.get('version_id'), '基版没有可继承的 OSS 对象版本')
        result.append(asdict(Frame(n, f'{n:06d}.jpg', 'image/jpeg', width, 2*height, content_signature, *times[2:])))
        locations[str(n)] = ref
    return dict(version=version, version_bytes=raw_version, source_hash=source, frames=result, frame_locations=locations)
