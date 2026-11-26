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
from oss_storage import BUCKET, PART_BYTES, PREFIX, REGION, URL_SECONDS, Location, cipher, platform_bucket, settings


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
        if not config or not processor_ready:
            abort(409, '请先完成 OSS 设置和云端校验服务配置')
        return config

    def view_settings(config, message=None, status=200):
        # Pass only the fields that the template may show.
        current = dict(region=config['region'], source_bucket=config['source_bucket'],
                       review_bucket=config['review_bucket'], masked_key='••••' + config['access_key_id'][-4:]) if config else {}
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
                acl = [
                    get_bucket(Location(region, bucket, f'{PREFIX}/incoming/test'), draft).get_bucket_acl().acl
                    for bucket in (source, review)
                ]
            except oss2.exceptions.OssError:
                return view_settings(config, 'OSS 连接失败。请核对地域、凭据及读取 Bucket ACL 的权限。', 422)
            if acl != ['public-read', 'private']:
                return view_settings(config, '工单 Bucket 应为公共读，审核 Bucket 应为私有。', 422)
            return view_settings(config, '两个 Bucket 均可访问，ACL 与用途匹配。', 200)
        if request.form.get('action') != 'save':
            abort(422)
        with write() as conn:
            assert_session('admin')
            conn.execute("""INSERT INTO oss_settings VALUES(1,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                         region=excluded.region,source_bucket=excluded.source_bucket,review_bucket=excluded.review_bucket,
                         access_key_id=excluded.access_key_id,encrypted_secret=excluded.encrypted_secret""",
                         (region, source, review, key, encrypted))
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
            location = Location(**json.loads(batch['location']))
            # Each retry has a different object key, even within the same work attempt.
            location = Location(location.region, location.bucket, location.key.removesuffix('.zip') + f'/{transfer}.zip')
            attempt = batch['attempt']
            options, mime = {}, 'application/zip'
        else:
            options, mime = create_source(None, filename, request.form)
            location = Location(config['region'], config['source_bucket'], f'{PREFIX}/incoming/sources/{transfer}')
        bucket = get_bucket(location, config)
        upload = bucket.init_multipart_upload(location.key, headers={'Content-Type': mime}).upload_id
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
            bucket.abort_multipart_upload(location.key, upload)
            raise
        return jsonify(id=transfer, part_size_bytes=PART_BYTES), 201

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
        if row['state'] != 'uploading' or not 1 <= number <= (row['size_bytes'] + PART_BYTES - 1)//PART_BYTES:
            abort(409, '上传已结束或分片编号无效')
        if row['work'] and not manual_batch(db(), row['work'], row['attempt']):
            abort(409, '工单已更新，请刷新')
        location = Location(**json.loads(row['location']))
        url = get_bucket(location).sign_url('PUT', location.key, URL_SECONDS,
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
        location = Location(**json.loads(row['location']))
        bucket = get_bucket(location)
        try:
            parts = list(oss2.PartIterator(bucket, location.key, row['multipart_id']))
            expected = (row['size_bytes'] + PART_BYTES - 1)//PART_BYTES
            if (len(parts) != expected or any(
                    part.part_number != i or part.size != min(PART_BYTES, row['size_bytes'] - (i-1)*PART_BYTES)
                    for i, part in enumerate(parts, 1))):
                abort(409, '上传分片不完整，请重试')
            bucket.complete_multipart_upload(location.key, row['multipart_id'], parts)
        except oss2.exceptions.NoSuchUpload:
            # The multipart request may have completed before the browser received its response.
            if bucket.head_object(location.key).content_length != row['size_bytes']:
                abort(409, '未找到完整上传')
        with write() as conn:
            assert_session('admin')
            current = browser_transfer(transfer)
            if current['state'] == 'uploading':
                if row['work']:
                    if not manual_batch(conn,row['work'],row['attempt']):
                        abort(409, '工单已更新，请刷新')
                    conn.execute("UPDATE execution_work SET state='verifying',stage='oss_verifying',updated=? WHERE id=?",
                                 (int(time.time()), row['work']))
                conn.execute("UPDATE oss_transfers SET state='ready',updated=? WHERE id=?", (int(time.time()),transfer))
        return jsonify(state='ready'), 202

    @ui.get('/admin/execution/oss/uploads/<transfer>')
    @requires('admin')
    def status(transfer):
        access()
        row = browser_transfer(transfer)
        return jsonify(state=row['state'], error=row['error'], result=json.loads(row['result']) if row['result'] else None)

    @ui.errorhandler(HTTPException)
    def upload_error(error):
        if request.endpoint in {'oss.begin','oss.part','oss.finish_upload','oss.status'}:
            return jsonify(message=error.description), error.code
        return error

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
