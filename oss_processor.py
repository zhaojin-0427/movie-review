"""Run on an Alibaba Cloud host in the OSS region. The web app sends metadata only."""
import base64
import hmac
import os
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from zipfile import BadZipFile, ZipFile

import oss2
import requests
from flask import Flask, abort, jsonify, request

from execution import OUTPUTS, ROLE_TYPES, identifier, validate_delivery
from execution_artifacts import (MAX_ARTIFACT, MAX_JSON, SHA, bundle, check_entries, file_hash,
                                 frame_size, json_object, read_json, require, stage_review_frames)
from oss_storage import PREFIX, Location, cloud_bucket


def fetch(location, path, expected_size=None, expected_sha=None, limit=MAX_ARTIFACT):
    bucket = cloud_bucket(location)
    head = bucket.head_object(location.key)
    size = head.content_length
    require(0 < size <= limit and (expected_size is None or size == expected_size), 'OSS 对象大小不匹配')
    headers = {'If-Match': head.headers['ETag']}
    with bucket.get_object(location.key, headers=headers) as stream, path.open('wb') as target:
        shutil.copyfileobj(stream, target, 4 * 1024**2)
    require(path.stat().st_size == size, 'OSS 对象传输不完整')
    if expected_sha:
        require(file_hash(path) == expected_sha, 'OSS 对象 SHA-256 不匹配')


def publish(location, path, content_type):
    oss2.resumable_upload(cloud_bucket(location), location.key, str(path),
                          headers={'Content-Type': content_type},
                          multipart_threshold=64 * 1024**2, part_size=32 * 1024**2, num_threads=3)


def output_location(base, run, name):
    return Location(base.region, base.bucket, f'{PREFIX}/published/{run}/{name}')


def validate_submission(location, folder, task, run):
    work, order = task['work'], task['order']
    transport = folder / 'submission.zip'
    fetch(location, transport, task.get('size_bytes'), task.get('sha256'))
    roles = OUTPUTS[work['kind']]
    if order.get('review_storage'):
        roles = roles - {'scene_bundle'}
    paths = {}
    outputs = {}
    try:
        with ZipFile(transport) as archive:
            entries = check_entries(archive)
            header = entries.pop('submission.json', None)
            require(header is not None and header.file_size <= 1024**2, '交付 ZIP 缺少有效的 submission.json')
            submission = json_object(archive.read(header))
            expected = task['assignment']
            require(all(submission.get(field) == value for field, value in expected.items()),
                    '交付 ZIP 与当前领取或返修基版不匹配')
            names = submission.get('outputs')
            require(isinstance(names, dict) and set(names) == roles
                    and all(isinstance(name, str) and name in entries for name in names.values())
                    and len(set(names.values())) == len(entries) == len(roles), '交付 ZIP 产物角色不匹配')
            for role, name in names.items():
                entry = entries[name]
                mime = ROLE_TYPES[role]
                require(0 < entry.file_size and (mime != 'application/json' or entry.file_size <= MAX_JSON),
                        '交付 ZIP 内产物大小不合法')
                path = folder / role
                with archive.open(entry) as source, path.open('wb') as target:
                    shutil.copyfileobj(source, target, 4 * 1024**2)
                item = dict(artifact_id=identifier('artifact'), role=role, size_bytes=entry.file_size,
                            sha256=file_hash(path), media_type=mime)
                outputs[role] = item
                paths[item['artifact_id']] = path
    except BadZipFile as error:
        raise ValueError('交付 ZIP 无法读取') from error
    transport.unlink()
    inputs = {item['role']: item for item in order['artifacts']}

    def path_for(item):
        return paths[item['artifact_id']]

    def input_file(role):
        item = inputs[role]
        path = folder / f'input-{role}'
        data = task['input_json'].get(role)
        if data is not None:
            path.write_bytes(base64.b64decode(data, validate=True))
            require(file_hash(path) == item['sha256'], '冻结输入摘要不匹配')
        else:
            fetch(Location(**task['input_locations'][role]), path, item['size_bytes'], item['sha256'], MAX_JSON)
        paths[item['artifact_id']] = path
        return path

    result = dict(outputs=outputs, locations={})
    if work['kind'] == 'finalize':
        for role in ('version_manifest', 'approval_manifest'):
            input_file(role)
        validate_delivery(path_for, order, inputs, outputs, submission.get('final_verification'), work)
    else:
        timeline = (path_for(outputs['source_timeline']) if work['kind'] == 'prepare'
                    else input_file('source_timeline'))
        version = read_json(path_for(outputs['version_manifest']))
        ticks = read_json(timeline)
        timeline_item = (outputs.get('source_timeline') or inputs['source_timeline'])
        expected = dict(job_id=str(work['job']), version=work['target'], source_sha256=inputs['source_video']['sha256'],
                        timeline_sha256=timeline_item['sha256'], production_method='codex_trial',
                        input_manifest_sha256=expected['input_manifest_sha256'],
                        **{key+'_sha256': outputs[key]['sha256']
                           for key in ('review_bundle','shot_metadata','production_report')})
        require(all(version.get(key) == value for key, value in expected.items())
                and ticks['source_sha256'] == expected['source_sha256'], '送审版本与冻结工单或产物不一致')
        storage = order.get('review_storage')
        require(version.get('review_storage') == storage, '送审版本与审片存储模式不一致')
        if storage:
            require(all(isinstance(version.get(key), str) and SHA.fullmatch(version[key])
                        for key in ('scene_sha256','scene_bundle_sha256')), '本机工程摘要无效')
        else:
            require(version['scene_bundle_sha256'] == outputs['scene_bundle']['sha256'], '工程包摘要不匹配')
            scene = bundle(path_for(outputs['scene_bundle']))
            require(scene['scene_sha256'] == version['scene_sha256']
                    and any(member['path'] == scene.get('scene_path')
                            and member['sha256'] == scene['scene_sha256'] and member['path'].endswith('.blend')
                            for member in scene['files']), '工程摘要与送审版本不匹配')
        frame_size(version['final_size'])
        destination = folder / 'frames'
        destination.mkdir()
        source, frames = stage_review_frames(path_for, outputs, ticks, version, destination)
        def publish_frame(frame):
            ref = output_location(location, run, 'frames/' + frame.asset_name)
            publish(ref, destination / frame.asset_name, frame.mimetype)
            return str(frame.number), ref.public()

        with ThreadPoolExecutor(max_workers=6) as pool:
            locations = dict(pool.map(publish_frame, frames))
        result.update(source_hash=source, frames=[asdict(frame) for frame in frames],
                      frame_locations=locations, version=version,
                      version_bytes=base64.b64encode(path_for(outputs['version_manifest']).read_bytes()).decode())
    for role, item in outputs.items():
        ref = output_location(location, run, 'artifacts/' + item['artifact_id'])
        publish(ref, path_for(item), item['media_type'])
        result['locations'][role] = ref.public()
    return result


def create_processor():
    app = Flask(__name__)
    app.config['MAX_CONTENT_LENGTH'] = 48 * 1024**2
    secret = os.environ.get('MOVIE_REVIEW_OSS_PROCESSOR_TOKEN', '')
    if len(secret) < 32:
        raise RuntimeError('MOVIE_REVIEW_OSS_PROCESSOR_TOKEN requires at least 32 characters')

    @app.post('/process')
    def process():
        auth = request.authorization
        if not auth or auth.type != 'bearer' or not hmac.compare_digest(auth.token or '', secret):
            abort(401)
        try:
            task = request.get_json()
            location = Location(**task['location'])
            require(location.key.startswith(f'{PREFIX}/incoming/'), '需要 OSS 暂存对象')
            run = identifier('publish')
            with tempfile.TemporaryDirectory(prefix='movie-review-oss-') as directory:
                folder = Path(directory)
                if task['kind'] == 'source':
                    source = folder / 'source'
                    fetch(location, source, task['size_bytes'])
                    target = output_location(location, run, 'source')
                    sha = file_hash(source)
                    publish(target, source, task['media_type'])
                    return jsonify(location=target.public(), sha256=sha)
                require(task['kind'] == 'submission', '未知 OSS 任务')
                return jsonify(validate_submission(location, folder, task, run))
        except (ValueError, KeyError, TypeError, RecursionError, BadZipFile, IndexError) as error:
            return jsonify(message=str(error)[:240]), 422
        except oss2.exceptions.NoSuchKey:
            return jsonify(message='OSS 暂存对象不存在'), 422
        except (oss2.exceptions.OssError, OSError, requests.RequestException):
            app.logger.warning('OSS processor transport or storage failure')
            return jsonify(message='OSS 校验暂不可用，请重试'), 503

    return app
