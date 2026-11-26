"""Browser direct upload, OSS settings, and the durable validation queue."""
import json
import os
import re
import secrets
import time
from pathlib import PurePosixPath
from urllib.parse import urlsplit

import click
import oss2
import requests
from flask import Blueprint, abort, flash, g, jsonify, redirect, render_template, request, url_for
from flask.cli import with_appcontext
from werkzeug.exceptions import HTTPException

from execution_artifacts import MAX_ARTIFACT, json_object
from oss_objects import HASH_HEADER, confirm_object, declaration, declarations, file_type
from oss_storage import OBJECTS, BUCKET, PART_BYTES, PREFIX, REGION, URL_SECONDS, Location, cipher, platform_bucket, settings


class ProcessorUnavailable(Exception):
    pass


def setup_oss(app, db, write, requires, assert_session, create_source, publish_work, manual_batch):
    ui = Blueprint('oss', __name__)
    processor_url = app.config.get('OSS_PROCESSOR_URL') or os.environ.get('MOVIE_REVIEW_OSS_PROCESSOR_URL', '')
    processor_token = app.config.get('OSS_PROCESSOR_TOKEN') or os.environ.get('MOVIE_REVIEW_OSS_PROCESSOR_TOKEN', '')
    address = urlsplit(processor_url)
    processor_ready = (address.scheme == 'https' and address.hostname and len(processor_token) >= 32) or bool(
        app.config.get('TESTING') and app.config.get('OSS_PROCESSOR'))

    def access():
        if not request.is_secure and not app.config.get('EXECUTION_TEST_HTTP'):
            abort(403, '请通过 HTTPS 配置或使用 OSS')

    def get_bucket(location, config=None):
        builder = app.config.get('OSS_BUCKET_FACTORY', platform_bucket)
        return builder(location, config or settings(db()), app.secret_key)

    def live_settings():
        config = settings(db())
        if not config:
            abort(409, '请先完成 OSS 设置')
        return config

    def view_settings(config, message=None, status=200):
        # Pass only the fields that the template may show.
        current = dict(region=config['region'], source_bucket=config['source_bucket'],
                       review_bucket=config['review_bucket'], masked_key='••••' + config['access_key_id'][-4:],
                       direct_upload=bool(config['direct_upload'])) if config else dict(direct_upload=True)
        return render_template('oss_settings.html', config=current, processor_ready=processor_ready,
                               message=message), status

    @ui.get('/admin/execution/oss')
    @requires('admin')
    def show():
        access()
        return view_settings(settings(db()))

    @ui.post('/admin/execution/oss')
    @requires('admin')
    def configure():
        access()
        config = settings(db())
        region = request.form.get('region', '').strip().lower()
        source = request.form.get('source_bucket', '').strip()
        review = request.form.get('review_bucket', '').strip()
        key = request.form.get('access_key_id', '').strip()
        secret = request.form.get('access_key_secret', '').strip()
        transport = request.form.get('transport', OBJECTS if config and config['direct_upload'] else 'zip_v1')
        if transport not in (OBJECTS,'zip_v1'):
            abort(422, '上传方式无效')
        direct = transport == OBJECTS
        if not (REGION.fullmatch(region) and BUCKET.fullmatch(source) and BUCKET.fullmatch(review) and source != review):
            return view_settings(config, '请填写正确的地域和两个不同的 Bucket。', 422)
        if key or secret:
            if not (re.fullmatch(r'[A-Za-z0-9]{8,128}', key) and re.fullmatch(r'[A-Za-z0-9+/=_-]{8,128}', secret)):
                return view_settings(config, '更换凭据时请同时填写 AccessKey ID 和 Secret。', 422)
            encrypted = cipher(app.secret_key).encrypt(secret.encode()).decode()
        elif config:
            key, encrypted = config['access_key_id'], config['encrypted_secret']
        else:
            return view_settings(config, '首次配置请填写 AccessKey ID 和 Secret。', 422)
        if request.form.get('action') == 'test':
            draft = dict(access_key_id=key, encrypted_secret=encrypted)
            try:
                source_client, review_client = [
                    get_bucket(Location(region, bucket, f'{PREFIX}/incoming/test'), draft)
                    for bucket in (source, review)
                ]
                acl = [source_client.get_bucket_acl().acl, review_client.get_bucket_acl().acl]
                versioned = not direct or review_client.get_bucket_versioning().status == 'Enabled'
            except oss2.exceptions.OssError:
                return view_settings(config, 'OSS 连接失败。请核对地域、凭据及读取 Bucket ACL 的权限。', 422)
            if acl != ['public-read', 'private']:
                return view_settings(config, '工单 Bucket 应为公共读，审核 Bucket 应为私有。', 422)
            if not versioned:
                return view_settings(config, '审核 Bucket 须开启版本控制。', 422)
            message = '两个 Bucket 均可访问，ACL 与用途匹配。'
            return view_settings(config, message + (' 审核 Bucket 版本控制已开启。' if direct else ''), 200)
        if request.form.get('action') != 'save':
            abort(422)
        with write() as conn:
            assert_session('admin')
            conn.execute("""INSERT INTO oss_settings VALUES(1,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                         region=excluded.region,source_bucket=excluded.source_bucket,review_bucket=excluded.review_bucket,
                         access_key_id=excluded.access_key_id,encrypted_secret=excluded.encrypted_secret,
                         direct_upload=excluded.direct_upload""",
                         (region, source, review, key, encrypted, direct))
        flash('OSS 设置已保存。', 'success')
        return redirect(url_for('oss.show'), code=303)

    @ui.post('/admin/execution/oss/uploads')
    @requires('admin')
    def begin():
        access()
        config = live_settings()
        filename = PurePosixPath(request.form.get('filename', '').replace('\\','/')).name
        size = request.form.get('size_bytes', type=int)
        if not filename or len(filename) > 255 or not size or not 0 < size <= MAX_ARTIFACT:
            abort(422, '文件名或大小无效')
        transfer = 'transfer_' + secrets.token_hex(16)
        work = request.form.get('work_id')
        attempt = None
        if work:
            batch = manual_batch(db(), work)
            if not batch or PurePosixPath(filename).suffix.lower() != '.zip':
                abort(409, '仅可给等待手动交付的当前批次上传 ZIP')
            root = Location(**json.loads(batch['location']))
            attempt = batch['attempt']
            options, mime = {}, 'application/zip'
            if root.key.startswith(f'{PREFIX}/jobs/'):
                kind = batch['kind']
                location = root.at(transfer)
                options = dict(protocol=OBJECTS, work_kind=kind)
                if kind == 'finalize':
                    sha = request.form.get('sha256')
                    declaration(dict(size_bytes=size,sha256=sha),MAX_ARTIFACT,versioned=False)
                    verification = json_object(request.form.get('final_verification',''))
                    if request.form.get('input_manifest_sha256') != batch['manifest_hash']:
                        abort(409, '交付 ZIP 与当前工单清单不符')
                    options.update(sha256=sha, final_verification=verification)
            else:
                if not processor_ready:
                    abort(409, 'ZIP 交付需要配置云端校验服务')
                location = Location(root.region,root.bucket,root.key.removesuffix('.zip') + f'/{transfer}.zip')
        else:
            options, mime = create_source(None, filename, request.form)
            if config['direct_upload']:
                if options['mode'] != 'node_local_v1':
                    abort(422, '逐图直传工单使用同机审片')
                sha = request.form.get('sha256')
                declaration(dict(size_bytes=size,sha256=sha),MAX_ARTIFACT,versioned=False)
                name = 'source' + PurePosixPath(filename).suffix.lower()
                location = Location(config['region'],config['source_bucket'],f'{PREFIX}/sources/{transfer}/{name}')
                options.update(protocol=OBJECTS,sha256=sha)
            else:
                if not processor_ready:
                    abort(409, 'ZIP 交付需要配置云端校验服务')
                location = Location(config['region'],config['source_bucket'],f'{PREFIX}/incoming/sources/{transfer}')
        target = multipart_location(location, options)
        headers = {'Content-Type': mime}
        if 'sha256' in options:
            headers[HASH_HEADER] = options['sha256']
        bucket = get_bucket(target, config) if target else None
        upload = bucket.init_multipart_upload(target.key, headers=headers).upload_id if bucket else None
        try:
            with write() as conn:
                assert_session('admin')
                if work and not manual_batch(conn, work, attempt):
                    abort(409, '工单已更新，请刷新')
                timestamp = int(time.time())
                conn.execute('INSERT INTO oss_transfers VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                             (transfer, g.user['id'], work, attempt, json.dumps(location.public()), filename,
                              size, upload, mime, json.dumps(options), 'uploading', None, None, timestamp, timestamp))
        except BaseException:
            if bucket:
                bucket.abort_multipart_upload(target.key, upload)
            raise
        return jsonify(id=transfer, part_size_bytes=PART_BYTES, multipart=bool(upload),
                       input_manifest_sha256=batch['manifest_hash'] if work else None), 201

    def multipart_location(location, options):
        if options.get('protocol') != OBJECTS or not options.get('work_kind'):
            return location
        return location.at('delivery.zip') if options['work_kind'] == 'finalize' else None

    def upload_target(row):
        return multipart_location(Location(**json.loads(row['location'])),json.loads(row['options']))

    def browser_transfer(transfer):
        row = db().execute('SELECT * FROM oss_transfers WHERE id=? AND actor=?', (transfer,g.user['id'])).fetchone()
        if not row:
            abort(404)
        return row

    @ui.post('/admin/execution/oss/uploads/<transfer>/parts/<int:number>')
    @requires('admin')
    def part(transfer, number):
        access()
        row = browser_transfer(transfer)
        if row['state'] != 'uploading' or not row['multipart_id'] or not 1 <= number <= (row['size_bytes'] + PART_BYTES - 1)//PART_BYTES:
            abort(409, '上传已结束或分片编号无效')
        if row['work'] and not manual_batch(db(), row['work'], row['attempt']):
            abort(409, '工单已更新，请刷新')
        location = upload_target(row)
        url = get_bucket(location).sign_url('PUT', location.key, URL_SECONDS,slash_safe=True,
                                            params={'uploadId': row['multipart_id'], 'partNumber': str(number)})
        return jsonify(url=url)

    @ui.post('/admin/execution/oss/uploads/<transfer>/complete')
    @requires('admin')
    def finish_upload(transfer):
        access()
        row = browser_transfer(transfer)
        if row['state'] != 'uploading':
            return jsonify(state=row['state']), 202
        if row['work'] and not manual_batch(db(), row['work'], row['attempt']):
            abort(409, '工单已更新，请刷新')
        if not row['multipart_id']:
            abort(409, '请使用逐图提交接口')
        location = upload_target(row)
        bucket = get_bucket(location)
        options = json.loads(row['options'])
        try:
            parts = list(oss2.PartIterator(bucket, location.key, row['multipart_id']))
            expected = (row['size_bytes'] + PART_BYTES - 1)//PART_BYTES
            if (len(parts) != expected or any(
                    part.part_number != i or part.size != min(PART_BYTES, row['size_bytes'] - (i-1)*PART_BYTES)
                    for i, part in enumerate(parts, 1))):
                abort(409, '上传分片不完整，请重试')
            kwargs = {'headers': {'x-oss-forbid-overwrite':'true'}} if not row['work'] and options.get('protocol') == OBJECTS else {}
            completed = bucket.complete_multipart_upload(location.key, row['multipart_id'], parts, **kwargs)
        except oss2.exceptions.NoSuchUpload:
            # The multipart request may have completed before the browser received its response.
            completed = bucket.head_object(location.key)
            if completed.content_length != row['size_bytes']:
                abort(409, '未找到完整上传')
        if options.get('protocol') == OBJECTS:
            if row['work']:
                version = completed.headers.get('x-oss-version-id')
                item = dict(version_id=version,size_bytes=row['size_bytes'],sha256=options['sha256'])
                declaration(item, MAX_ARTIFACT)
                options['objects'] = {'delivery.zip':item}
            else:
                version = completed.headers.get('x-oss-version-id')
                if version and version != 'null':
                    options['version_id'] = version
                    location = Location(location.region,location.bucket,location.key,version)
                confirm_object(bucket,location,row['size_bytes'],options['sha256'],row['media_type'])
        return queue_browser(row,options)

    def queue_browser(row, options):
        with write() as conn:
            assert_session('admin')
            current = browser_transfer(row['id'])
            if current['state'] != 'uploading':
                return jsonify(state=current['state']), 202
            if row['work']:
                if not manual_batch(conn,row['work'],row['attempt']):
                    abort(409, '工单已更新，请刷新')
                conn.execute("UPDATE execution_work SET state='verifying',stage='oss_verifying',updated=? WHERE id=?",
                             (int(time.time()),row['work']))
            conn.execute("UPDATE oss_transfers SET state='ready',options=?,updated=? WHERE id=?",
                         (json.dumps(options),int(time.time()),row['id']))
        return jsonify(state='ready'), 202

    def manual_objects(transfer):
        access()
        row = browser_transfer(transfer)
        options = json.loads(row['options'])
        kind = options.get('work_kind')
        if not kind or kind == 'finalize' or options.get('protocol') != OBJECTS:
            abort(409, '该批次使用整包上传')
        return row, options, kind

    @ui.post('/admin/execution/oss/uploads/<transfer>/objects/sign')
    @requires('admin')
    def sign_objects(transfer):
        row, _, kind = manual_objects(transfer)
        if row['state'] != 'uploading' or not manual_batch(db(),row['work'],row['attempt']):
            abort(409, '工单已更新，请刷新')
        names = json_object(request.form.get('objects',''))
        if not 1 <= len(names) <= 24:
            abort(422, '每批请请求 1 到 24 个文件')
        location = Location(**json.loads(row['location']))
        bucket = get_bucket(location)
        signed = {}
        for name,item in names.items():
            _, mime, limit = file_type(kind,name)
            declaration(item,limit,versioned=False)
            ref = location.at(name)
            headers = {'Content-Type':mime,HASH_HEADER:item['sha256']}
            signed[name] = dict(url=bucket.sign_url('PUT',ref.key,URL_SECONDS,slash_safe=True,headers=headers),
                                headers=headers)
        return jsonify(signed)

    @ui.post('/admin/execution/oss/uploads/<transfer>/objects/complete')
    @requires('admin')
    def finish_objects(transfer):
        row,options,kind = manual_objects(transfer)
        if row['state'] != 'uploading':
            return jsonify(state=row['state']), 202
        objects = json_object(request.form.get('objects',''))
        declarations(kind,objects)
        return queue_browser(row,options | {'objects':objects})

    @ui.get('/admin/execution/oss/uploads/<transfer>')
    @requires('admin')
    def status(transfer):
        access()
        row = browser_transfer(transfer)
        return jsonify(state=row['state'], error=row['error'], result=json.loads(row['result']) if row['result'] else None)

    @ui.errorhandler(HTTPException)
    def upload_error(error):
        if request.endpoint in {'oss.begin','oss.part','oss.finish_upload','oss.status','oss.sign_objects','oss.finish_objects'}:
            return jsonify(message=error.description), error.code
        return error

    @ui.errorhandler(ValueError)
    def invalid_upload(error):
        return jsonify(message=str(error)[:240]), 422

    @ui.errorhandler(oss2.exceptions.OssError)
    def oss_error(_error):
        return jsonify(message='OSS 暂不可用，请检查设置后重试。'), 503

    def cloud(task):
        mock = app.config.get('OSS_PROCESSOR')
        if app.testing and mock:
            return mock(task)
        if not processor_ready:
            raise ProcessorUnavailable()
        try:
            result = requests.post(processor_url.rstrip('/') + '/process', json=task, timeout=(10, 7200),
                                   headers={'Authorization': 'Bearer ' + processor_token}, allow_redirects=False)
        except requests.RequestException as error:
            raise ProcessorUnavailable() from error
        if result.status_code == 422:
            raise ValueError(result.json()['message'])
        if result.status_code != 200:
            raise ProcessorUnavailable()
        return json_object(result.content)

    @app.cli.command('oss-process')
    @click.option('--watch', is_flag=True, help='Process queued uploads continuously.')
    @with_appcontext
    def process_queue(watch):
        import fcntl
        from pathlib import Path
        path = Path(app.config['DATA_DIR']) / '.oss-process.lock'
        with path.open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with write() as conn:
                conn.execute("UPDATE oss_transfers SET state='ready' WHERE state='processing'")
            while True:
                processed = process_one()
                if not watch:
                    return
                if not processed:
                    time.sleep(3)

    def process_one():
        with write() as conn:
            row = conn.execute("""SELECT * FROM oss_transfers WHERE state='ready' AND updated<=?
                                  ORDER BY updated,created LIMIT 1""", (int(time.time()),)).fetchone()
            if not row:
                return False
            conn.execute("UPDATE oss_transfers SET state='processing' WHERE id=?", (row['id'],))
        try:
            result = (publish_work(row, cloud) if row['work'] else create_source(row, cloud))
        except (ValueError, HTTPException) as error:
            message = (error.description if isinstance(error, HTTPException) else str(error))[:240] or '上传核验失败'
            with write() as conn:
                conn.execute("UPDATE oss_transfers SET state='rejected',error=? WHERE id=? AND state='processing'",
                             (message,row['id']))
                if row['work']:
                    conn.execute("""UPDATE execution_work SET state=?,stage=?,failure=?
                        WHERE id=? AND attempt=? AND state='verifying'""",
                        ('awaiting_manual' if row['actor'] else 'failed',
                         'awaiting_manual' if row['actor'] else 'oss_rejected',
                         'integrity_failed', row['work'], row['attempt']))
        except Exception:
            # Retry external or transient failures without logging signed URLs or keys.
            app.logger.warning('OSS transfer %s will retry', row['id'])
            with write() as conn:
                conn.execute("UPDATE oss_transfers SET state='ready',updated=? WHERE id=? AND state='processing'",
                             (int(time.time())+30,row['id']))
        else:
            with write() as conn:
                conn.execute("UPDATE oss_transfers SET state='done',result=? WHERE id=? AND state='processing'",
                             (json.dumps(result),row['id']))
        return True

    app.register_blueprint(ui)
    return processor_ready
