"""Device administration and the outbound production-worker protocol."""
import fcntl
import hashlib
import json
import re
import secrets
import shutil
import tempfile
import time
import unicodedata
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from uuid import UUID

from flask import Blueprint, abort, flash, g, jsonify, redirect, render_template, request, send_file, url_for
from werkzeug.exceptions import HTTPException

from execution_artifacts import (MAX_ARTIFACT, SHA, bundle, canonical, file_hash, frame_size,
                                 json_object, object_hash, read_json, require, stage_review_frames)

PART_SIZE = 8388608
LEASE_SECONDS = 90
ONLINE_SECONDS = 100
LOCAL_REVIEW = 'node_local_v1'
REVIEW_STORAGE_HEADER = 'X-Film-Worker-Review-Storage'
EXPORT_QUALITIES = {'1080p': '1080p', '4k': '4K'}
KINDS = {'prepare': '首版', 'revise': '返修', 'finalize': '出片'}
ROLE_TYPES = {'source_timeline': 'application/json', 'scene_bundle': 'application/zip',
              'shot_metadata': 'application/x-ndjson', 'review_bundle': 'application/zip',
              'version_manifest': 'application/json', 'production_report': 'application/json',
              'white_video': 'video/mp4', 'comparison_video': 'video/mp4', 'delivery_bundle': 'application/zip'}
REVIEW_OUTPUTS = {'scene_bundle', 'shot_metadata', 'review_bundle', 'version_manifest', 'production_report'}
OUTPUTS = {'prepare': REVIEW_OUTPUTS | {'source_timeline'}, 'revise': REVIEW_OUTPUTS,
           'finalize': {'white_video', 'comparison_video', 'delivery_bundle'}}
DIRECTIVES = {'paused': 'pause', 'stopped': 'stop', 'deleted': 'delete'}
TERMINAL = {'succeeded', 'failed', 'stopped', 'deleted'}
VIDEO_TYPES = {'.mp4': 'video/mp4', '.mov': 'video/quicktime', '.mkv': 'video/x-matroska',
               '.webm': 'video/webm', '.m4v': 'video/mp4'}
FAILURES = {'dependency_unavailable', 'model_unavailable', 'disk_full', 'render_failed',
            'input_invalid', 'profile_mismatch', 'integrity_failed', 'other'}

def now():
    return int(time.time())


def stamp(value=None):
    return datetime.fromtimestamp(now() if value is None else value, UTC).isoformat()


def identifier(prefix):
    return prefix + '_' + secrets.token_hex(16)


def json_text(value):
    return canonical(value).decode()


def uint(value, minimum=0, maximum=2**53 - 1):
    return type(value) is int and minimum <= value <= maximum


def bounded(value, limit):
    return isinstance(value, str) and 0 < len(value.strip()) <= limit and len(value) <= limit


def export_size(native, quality):
    width, height = frame_size(native)
    if quality == '4k':
        require(width == 3840, '4K 出片需要基版原生宽度为 3840')
    if quality != '1080p':
        return native
    max_width, max_height = (1920, 1080) if width >= height else (1080, 1920)
    scale = min(Fraction(1), Fraction(max_width, width), Fraction(max_height, height))
    return dict(width=max(2, 2 * round(round(width * scale) / 2)),
                height=max(2, 2 * round(round(height * scale) / 2)))


def export_options(conn, objects, job):
    spec = conn.execute('SELECT export_quality FROM execution_jobs WHERE job=?', (job,)).fetchone()
    if spec is None:
        return None
    frozen = conn.execute('''SELECT manifest FROM execution_work WHERE job=? AND kind='finalize'
                            AND target=(SELECT max(number) FROM versions WHERE job_id=?)''', (job, job)).fetchone()
    saved = conn.execute('SELECT artifacts FROM execution_versions WHERE job=? ORDER BY version DESC LIMIT 1',
                         (job,)).fetchone()
    native = None
    if saved:
        item = json.loads(saved['artifacts'])['version_manifest']
        native = read_json(objects / item['artifact_id'])['final_size']
    return dict(selected=spec['export_quality'] or '1080p', allow_4k=native is None or native['width'] == 3840,
                locked=frozen is not None, frozen=json.loads(frozen['manifest']).get('export_quality') if frozen else None,
                labels=EXPORT_QUALITIES)


class ProtocolProblem(Exception):
    def __init__(self, status, code, message):
        self.status = status
        self.code = code
        self.message = message


def problem(status, code, message):
    raise ProtocolProblem(status, code, message)


def setup_execution(app, data, db, write, requires, assert_session, import_version, event):
    admin = Blueprint('execution', __name__)
    machine = Blueprint('machine', __name__, url_prefix='/api/v1')
    objects = data / 'assets' / 'execution'
    parts_dir = data / 'execution-incoming'
    objects.mkdir(exist_ok=True)
    parts_dir.mkdir(exist_ok=True)

    def artifact(conn, key):
        row = conn.execute('SELECT * FROM execution_artifacts WHERE id=?', (key,)).fetchone()
        if row is None:
            problem(404, 'artifact_not_found', '工件不存在')
        return dict(artifact_id=row['id'], role=row['role'], sha256=row['sha256'],
                    size_bytes=row['size_bytes'], media_type=row['media_type'])

    def path_for(item):
        return objects / item['artifact_id']

    def save_artifact(conn, key, role, sha, size, mimetype, filename):
        conn.execute('INSERT INTO execution_artifacts VALUES(?,?,?,?,?,?)', (key, role, sha, size, mimetype, filename))
        return artifact(conn, key)

    def store(conn, path, role, mimetype, filename, sha=None):
        key = identifier('artifact')
        sha, size = sha or file_hash(path), path.stat().st_size
        result = save_artifact(conn, key, role, sha, size, mimetype, filename)
        path.rename(path_for(result))
        return result

    def freeze(conn, role, payload):
        content = json.dumps(payload,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode()
        key = identifier('artifact')
        result = save_artifact(conn, key, role, hashlib.sha256(content).hexdigest(), len(content),
                               'application/json', role + '.json')
        path_for(result).write_bytes(content)
        return result

    def job_inputs(conn, job):
        return conn.execute('''SELECT e.*,j.title,j.owner_id
            FROM execution_jobs e JOIN jobs j ON j.id=e.job WHERE e.job=?''',
                            (job,)).fetchone()

    def work_row(conn, work):
        row = conn.execute('SELECT * FROM execution_work WHERE id=?', (work,)).fetchone()
        if not row:
            problem(404, 'work_not_found', '工单不存在')
        return row

    def enqueue(conn, spec, kind, target, base=None, extras=()):
        work = identifier('work')
        manifest = dict(schema_version=1, job_id=str(spec['job']), work_id=work, kind=kind,
                        base_version=base, target_version=target,
                        artifacts=[artifact(conn, spec['source']), *extras])
        if spec['review_instance']:
            manifest['review_storage'] = dict(mode=LOCAL_REVIEW, instance_id=spec['review_instance'])
        if kind == 'finalize' and spec['export_quality']:
            manifest['export_quality'] = spec['export_quality']
        conn.execute('''INSERT INTO execution_work(id,job,kind,base,target,manifest,manifest_hash,updated)
                         VALUES(?,?,?,?,?,?,?,?)''', (work, spec['job'], kind, base, target,
                                                        json_text(manifest), object_hash(manifest), now()))

    def after_review(conn, job, version_id, kind):
        spec = job_inputs(conn, job)
        if not spec:
            return
        version = conn.execute('SELECT number FROM versions WHERE id=?', (version_id,)).fetchone()['number']
        saved = conn.execute('''WITH RECURSIVE lineage(id) AS (
                SELECT ? UNION ALL SELECT s.base_id FROM supplements s JOIN lineage l ON l.id=s.version_id
            ) SELECT e.artifacts,e.version FROM lineage l JOIN versions v ON v.id=l.id
              JOIN execution_versions e ON e.job=v.job_id AND e.version=v.number ORDER BY e.version DESC LIMIT 1''',
                             (version_id,)).fetchone()
        if not saved or (kind == 'finalize' and saved['version'] != version):
            return
        package_version = saved['version']
        target = version + 1 if kind == 'revise' else version
        if conn.execute('SELECT 1 FROM execution_work WHERE job=? AND kind=? AND target=?', (job,kind,target)).fetchone():
            return
        items = json.loads(saved['artifacts'])
        original = read_json(path_for(items['version_manifest']))
        if kind == 'finalize' and spec['export_quality'] == '4k' and original['final_size']['width'] != 3840:
            conn.execute("UPDATE execution_jobs SET export_quality='1080p' WHERE job=?", (job,))
            conn.execute('INSERT INTO execution_management_events VALUES(?,?,?,?,?)',
                         (job,spec['actor'],'export_quality','基版原生宽度不足 3840，使用 1080p',now()))
            spec = job_inputs(conn, job)
        frames = {frame['number']: frame for frame in original['frames']}
        approved, changes = [], []
        for row in conn.execute('SELECT * FROM frames WHERE version_id=? ORDER BY number', (version_id,)):
            n = row['number']
            identity = dict(number=n, review_signature=frames[n]['review_signature'])
            if row['state'] == 'approved':
                origin = conn.execute('SELECT number FROM versions WHERE id=?',(row['approved_from'] or version_id,)).fetchone()
                approved.append(dict(identity,approved_version=origin['number']))
            elif row['state'] == 'changes':
                changes.append(dict(identity, note=row['note'] or '', regions=json.loads(row['regions'])))
        common = dict(schema_version=1, job_id=str(job), base_version=package_version,
                      original_annotator_id=str(spec['owner_id']), timeline_sha256=original['timeline_sha256'],
                      base_manifest_sha256=items['version_manifest']['sha256'])
        approval = dict(common, source_sha256=original['source_sha256'],
                        scene_sha256=original['scene_sha256'], frame_count=len(frames), frames=approved,
                        scope='all_frames' if kind == 'finalize' else 'approved_subset')
        excluded = {'production_report'}
        if spec['review_instance']:
            excluded.update(('scene_bundle', 'review_bundle'))
        inputs = [v for k,v in items.items() if k not in excluded]
        inputs.append(freeze(conn, 'approval_manifest', approval))
        if kind == 'revise':
            feedback = dict(common, review_size=original['review_size'], frames=changes,
                            origin='supplement' if package_version != version else 'review')
            inputs.append(freeze(conn, 'feedback_manifest', feedback))
        enqueue(conn, spec, kind, target, package_version, inputs)

    def apply_control(conn, job, action):
        if action == 'resume':
            conn.execute('''UPDATE execution_work SET state='queued',node=NULL,token=NULL,instance=NULL,
                         stage=NULL,completed=NULL,total=NULL,failure=NULL,updated=?,available_at=0
                         WHERE job=? AND state='paused' AND EXISTS
                         (SELECT 1 FROM execution_jobs e WHERE e.job=execution_work.job AND e.review_instance IS NOT NULL)''',
                         (now(),job))
            conn.execute('''UPDATE execution_work SET state=CASE WHEN node IS NULL THEN 'queued' ELSE 'failed' END,
                         updated=?,available_at=0 WHERE job=? AND state='paused' ''', (now(),job))
            return
        before, after = {'pause': (('queued','leased'), 'paused'),
                         'withdraw': (('queued','leased','paused','failed'), 'stopped')}[action]
        placeholders = ','.join('?' for _ in before)
        conn.execute(f'''UPDATE execution_work SET state=?,lease=NULL,expires=NULL,updated=?,available_at=0,
                         failure=CASE WHEN state='leased' AND ?='paused' THEN 'job_paused' ELSE failure END
                         WHERE job=? AND state IN ({placeholders})''', (after, now(), after, job, *before))

    def expire_leases(conn):
        cutoff = now()
        conn.execute('''UPDATE execution_work SET state='failed', lease=NULL, expires=NULL, updated=?, available_at=0,
            failure=CASE WHEN expires<=? THEN 'lease_expired' ELSE 'credential_inactive' END WHERE state='leased' AND
            (expires<=? OR EXISTS(SELECT 1 FROM execution_tokens t WHERE t.id=execution_work.token
                                   AND (t.revoked IS NOT NULL OR t.paused_at IS NOT NULL OR t.expires<=?)))''',
                     (cutoff, cutoff, cutoff, cutoff))

    @machine.before_request
    def authenticate():
        if not request.is_secure and not app.config.get('EXECUTION_TEST_HTTP'):
            problem(403, 'https_required', '请通过 HTTPS 连接执行端')
        auth = request.authorization
        if not auth or auth.type != 'bearer' or not re.fullmatch(r'fw_[A-Za-z0-9_-]{43}', auth.token or ''):
            problem(401, 'invalid_token', '需要有效的设备密钥')
        g.execution_token = hashlib.sha256(auth.token.encode()).hexdigest()
        current_token(db())

    def current_token(conn):
        token = conn.execute('''SELECT id,node FROM execution_tokens
                             WHERE hash=? AND revoked IS NULL AND paused_at IS NULL AND expires>?''',
                             (g.execution_token, now())).fetchone()
        if not token:
            problem(401, 'invalid_token', '设备密钥无效、已暂停、已过期或已撤销')
        g.node, g.token_id = token['node'], token['id']

    @machine.errorhandler(ProtocolProblem)
    def protocol_error(error):
        request_id = identifier('request')
        if error.status == 422 and request.endpoint == 'machine.complete':
            app.logger.warning('Execution completion rejected: work=%s request=%s error=%r',
                               request.view_args['work_id'], request_id, error.message[:400])
        response = jsonify(code=error.code, message=error.message, request_id=request_id,
                           retryable=error.status in (429, 503))
        response.status_code = error.status
        if error.status == 401:
            response.headers['WWW-Authenticate'] = 'Bearer realm="film-worker"'
        return response

    @machine.errorhandler(HTTPException)
    def http_error(error):
        return protocol_error(ProtocolProblem(error.code, 'request_rejected', error.description))

    @machine.errorhandler(ValueError)
    @machine.errorhandler(RecursionError)
    def invalid(error):
        return protocol_error(ProtocolProblem(422, 'invalid_request', str(error)))

    def payload():
        if not request.is_json or request.content_length is None or request.content_length > 1024**2:
            problem(400, 'bad_request', '需要不超过 1 MiB 的 JSON 对象')
        return json_object(request.get_data())

    def operation(conn, body):
        key = request.headers.get('Idempotency-Key', '')
        if not re.fullmatch(r'[!-~]{1,128}', key):
            problem(400, 'bad_request', '缺少有效的 Idempotency-Key')
        headers = [request.headers.get(h) for h in ('X-Lease-Id','X-Fence','X-Attempt')]
        legacy_digest = object_hash([body, headers])
        mode = request.headers.get(REVIEW_STORAGE_HEADER) if request.endpoint == 'machine.register' else None
        digest = object_hash([body, headers + [mode]]) if mode else legacy_digest
        scope = (g.node, request.path, key)
        row = conn.execute('SELECT * FROM execution_requests WHERE node=? AND operation=? AND key=?', scope).fetchone()
        if row and row['fingerprint'] != digest:
            if not mode or row['fingerprint'] != legacy_digest:
                problem(409, 'idempotency_conflict', '同一幂等键已用于不同请求')
            conn.execute('UPDATE execution_requests SET fingerprint=? WHERE node=? AND operation=? AND key=?',
                         (digest, *scope))
        return scope, digest, row

    def saved_response(conn, op, value=None, status=200):
        scope, digest, old = op
        if old:
            value, status = json.loads(old['response']), old['status']
        else:
            if request.endpoint == 'machine.claim' and status == 204:
                conn.execute('DELETE FROM execution_requests WHERE node=? AND operation=? AND status=204 AND created<?',
                             (g.node,request.path,now()-86400))
            conn.execute('INSERT INTO execution_requests VALUES(?,?,?,?,?,?,?)',
                         (*scope, digest, status, json_text(value), now()))
        if status == 204:
            return '', 204, {'Retry-After': '3'}
        return jsonify(value), status

    def get_node(conn, worker, instance):
        if worker != g.node:
            problem(403, 'forbidden', '设备身份不匹配')
        node = conn.execute('SELECT * FROM execution_nodes WHERE id=?', (g.node,)).fetchone()
        binding = conn.execute('SELECT instance FROM execution_node_bindings WHERE node=?', (g.node,)).fetchone()
        if binding and binding['instance'] != instance:
            problem(403, 'instance_bound', '设备已绑定到另一个执行实例，请在后台解除绑定后再连接')
        if not node['instance'] or node['instance'] != instance:
            problem(409, 'worker_unknown', '需重新注册此实例')
        return node

    @machine.post('/workers/register')
    def register():
        body = payload()
        review_mode = request.headers.get(REVIEW_STORAGE_HEADER)
        require(review_mode in (None, LOCAL_REVIEW), '不支持的审片存储模式')
        if body.get('node_key') != g.node:
            problem(403, 'forbidden', '设备标识与密钥不匹配')
        try:
            instance = str(UUID(body['instance_id']))
            cap = body['capabilities']
            environment = cap.get('environment') or {}
            kinds = cap['work_kinds']
            render_backends = cap['render_backends']
            require(instance == body['instance_id'] and uint(cap['slots'], 1, 128)
                    and uint(cap['max_frame_width'], 1, 16384) and isinstance(kinds,list) and kinds
                    and set(kinds) <= KINDS.keys() and isinstance(render_backends,list)
                    and 0 < len(render_backends) <= 16 and all(bounded(x,32) for x in render_backends)
                    and isinstance(environment, dict), '设备能力、槽位或实例标识无效')
            if environment.get('os') and not re.fullmatch(r'(?:macOS|Windows) \d+(?:\.\d+){0,3}', environment['os']):
                problem(422, 'invalid_capability', '当前仅支持 macOS 或 Windows 制作节点')
            env_keys = ('os','arch','gpu','render_backend','model_evidence')
            known = {k: environment[k] for k in env_keys if bounded(environment.get(k),200)}
            cap = {k: cap[k] for k in ('slots','max_frame_width','render_backends','work_kinds')}
            cap['environment'] = known
            if review_mode:
                cap['review_storage'] = review_mode
        except (KeyError, TypeError, AttributeError) as error:
            raise ValueError('节点注册字段不完整') from error
        with write() as conn:
            current_token(conn)
            op = operation(conn, body)
            node = conn.execute('SELECT * FROM execution_nodes WHERE id=?', (g.node,)).fetchone()
            binding = conn.execute('SELECT instance FROM execution_node_bindings WHERE node=?', (g.node,)).fetchone()
            if binding and binding['instance'] != instance:
                problem(403, 'instance_bound', '设备已绑定到另一个执行实例，请在后台解除绑定后再更换')
            expire_leases(conn)
            if node['instance'] != instance and conn.execute('''SELECT 1 FROM execution_work
                WHERE node=? AND state='leased' LIMIT 1''', (g.node,)).fetchone():
                problem(409, 'instance_busy', '该设备的原实例仍有有效工单')
            if op[2]:
                if node['instance'] != instance:
                    problem(409, 'worker_unknown', '注册实例已被替换')
                previous = json.loads(node['capabilities'])
                if previous.get('review_storage') != review_mode:
                    conn.execute('UPDATE execution_nodes SET capabilities=? WHERE id=?', (json_text(cap), g.node))
                response = saved_response(conn,op)
                response[0].headers['X-Film-Worker-Storage'] = '1'
                return response
            conn.execute('''UPDATE execution_nodes SET instance=?,capabilities=?,seen=?,slots=0,
                             disk_free=NULL,disk_reserve=NULL WHERE id=?''', (instance, json_text(cap), now(), g.node))
            response = saved_response(conn, op, dict(worker_id=g.node, server_time=stamp(), heartbeat_seconds=30,
                claim_wait_max_seconds=25, lease_seconds=LEASE_SECONDS, lease_heartbeat_seconds=20, part_size_bytes=PART_SIZE))
            response[0].headers['X-Film-Worker-Storage'] = '1'
            return response

    def accepts(node):
        return (not node['draining'] and node['seen'] > now() - ONLINE_SECONDS and node['slots'] > 0
                and (node['disk_free'] is None or node['disk_free'] >= node['disk_reserve']))

    @machine.post('/workers/<worker>/heartbeat')
    def heartbeat(worker):
        body = payload()
        slots, active = body.get('available_slots'), body.get('active_work_ids')
        require(uint(slots, 0, 128) and isinstance(active, list) and len(active) <= 128
                and all(bounded(x,128) for x in active), '设备心跳字段无效')
        storage = body.get('storage')
        free = reserve = None
        if storage is not None:
            require(isinstance(storage,dict) and uint(storage.get('free_bytes')) and uint(storage.get('reserve_bytes')),
                    '磁盘容量字段无效')
            free, reserve = storage['free_bytes'], storage['reserve_bytes']
        with write() as conn:
            current_token(conn)
            node = get_node(conn, worker, body.get('instance_id'))
            require(slots <= json.loads(node['capabilities'])['slots'], '可用槽位超过设备容量')
            conn.execute('UPDATE execution_nodes SET seen=?,slots=?,disk_free=?,disk_reserve=? WHERE id=?',
                         (now(), slots, free, reserve, g.node))
            node = get_node(conn, worker, body.get('instance_id'))
            return jsonify(server_time=stamp(), accept_claims=accepts(node))

    @machine.post('/work/claim')
    def claim():
        body = payload()
        wait, kinds = body.get('wait_seconds'), body.get('work_kinds', list(KINDS))
        require(uint(wait, 0, 25) and isinstance(kinds,list) and kinds and all(isinstance(k,str) and k in KINDS for k in kinds), '领取条件无效')
        deadline = time.monotonic() + wait
        while True:
            with write() as conn:
                current_token(conn)
                node = get_node(conn, body.get('worker_id'), body.get('instance_id'))
                op = operation(conn, body)
                expire_leases(conn)
                if op[2]:
                    if op[2]['status'] == 200:
                        prior = json.loads(op[2]['response'])
                        current = work_row(conn, prior['work_id'])
                        local = prior['input_manifest'].get('review_storage')
                        if (current['state'] != 'leased' or current['lease'] != prior['lease']['lease_id']
                                or (local and (local['instance_id'] != node['instance']
                                    or json.loads(node['capabilities']).get('review_storage') != LOCAL_REVIEW))):
                            return '', 204, {'Retry-After': '3'}
                    return saved_response(conn, op)
                cap = json.loads(node['capabilities'])
                active = conn.execute("SELECT count(*) FROM execution_work WHERE node=? AND state='leased'", (g.node,)).fetchone()[0]
                if accepts(node) and active < cap['slots']:
                    rows = conn.execute('''SELECT w.*,e.local_review,e.review_node,e.review_instance
                        FROM execution_work w JOIN execution_jobs e ON e.job=w.job LEFT JOIN job_controls c ON c.job_id=w.job
                        WHERE w.state='queued' AND w.node IS NULL AND w.deleted_at IS NULL
                        AND w.available_at<=? AND coalesce(c.state,'active')='active'
                        ORDER BY w.updated,w.rowid''', (now(),))
                    for row in rows:
                        if row['kind'] not in kinds or row['kind'] not in cap['work_kinds']:
                            continue
                        manifest = json.loads(row['manifest'])
                        if row['local_review']:
                            if cap.get('review_storage') != LOCAL_REVIEW:
                                continue
                            if row['review_instance']:
                                if (row['review_node'], row['review_instance']) != (g.node, node['instance']):
                                    continue
                            else:
                                if row['kind'] != 'prepare':
                                    continue
                                conn.execute('UPDATE execution_jobs SET review_node=?,review_instance=? WHERE job=?',
                                             (g.node,node['instance'],row['job']))
                                manifest['review_storage'] = dict(mode=LOCAL_REVIEW, instance_id=node['instance'])
                        elif manifest.get('export_quality') == '1080p' and cap.get('review_storage') != LOCAL_REVIEW:
                            item = next(item for item in manifest['artifacts'] if item['role'] == 'version_manifest')
                            native = read_json(path_for(item))['final_size']
                            if export_size(native, '1080p') != native:
                                continue
                        claimed = now()
                        attempt, lease, expires = row['attempt'] + 1, identifier('lease'), claimed + LEASE_SECONDS
                        digest = object_hash(manifest)
                        conn.execute('''UPDATE execution_work SET state='leased',attempt=?,node=?,token=?,instance=?,lease=?,
                                     expires=?,stage=NULL,completed=NULL,total=NULL,updated=?,failure=NULL,manifest=?,manifest_hash=?
                                     WHERE id=?''', (attempt,g.node,g.token_id,node['instance'],lease,expires,claimed,
                                                      json_text(manifest),digest,row['id']))
                        conn.execute('INSERT INTO execution_attempts(work,attempt,node,claimed_at) VALUES(?,?,?,?)',
                                     (row['id'],attempt,g.node,claimed))
                        conn.execute('UPDATE execution_nodes SET slots=slots-1 WHERE id=?',(g.node,))
                        return saved_response(conn, op, dict(work_id=row['id'], job_id=str(row['job']), kind=row['kind'],
                            lease=dict(lease_id=lease, attempt=attempt, fence=attempt, worker_id=g.node, expires_at=stamp(expires)),
                            input_manifest=manifest,input_manifest_sha256=digest))
                if time.monotonic() >= deadline or not accepts(node):
                    return saved_response(conn, op, status=204)
            time.sleep(min(.5, max(0, deadline - time.monotonic())))

    def held(conn, work):
        current_token(conn)
        row = work_row(conn, work)
        lease = (request.headers.get('X-Lease-Id'), request.headers.get('X-Fence'), request.headers.get('X-Attempt'))
        if row['node'] != g.node or row['state'] != 'leased' or row['expires'] <= now() or lease != (
                row['lease'], str(row['attempt']), str(row['attempt'])):
            problem(409, 'lease_lost', '当前工单租约已失效')
        token = conn.execute('SELECT revoked,paused_at,expires FROM execution_tokens WHERE id=?', (row['token'],)).fetchone()
        if token['revoked'] is not None or token['paused_at'] is not None or token['expires'] <= now():
            problem(409, 'lease_lost', '签发工单的设备密钥已失效')
        if json.loads(row['manifest']).get('review_storage'):
            node = get_node(conn, g.node, row['instance'])
            if json.loads(node['capabilities']).get('review_storage') != LOCAL_REVIEW:
                problem(409, 'lease_lost', '此工单需要支持同机审片的原执行实例')
        return row

    def output_roles(work, manifest):
        roles = OUTPUTS[work['kind']]
        return roles - {'scene_bundle'} if manifest.get('review_storage') else roles

    @machine.post('/work/<work>/heartbeat')
    def work_heartbeat(work):
        body = payload()
        stage, completed, total = body.get('stage'), body.get('completed'), body.get('total')
        require(bounded(stage,120) and uint(completed) and (total is None or uint(total,completed)), '工单进度无效')
        with write() as conn:
            current_token(conn)
            row = work_row(conn, work)
            if row['state'] in DIRECTIVES and conn.execute('SELECT 1 FROM execution_attempts WHERE work=? AND node=?',
                                                            (work,g.node)).fetchone():
                return jsonify(directive=DIRECTIVES[row['state']], server_time=stamp(), expires_at=None)
            held(conn, work)
            expires = now() + LEASE_SECONDS
            conn.execute('UPDATE execution_work SET expires=?,stage=?,completed=?,total=?,updated=? WHERE id=?',
                         (expires,stage,completed,total,now(),work))
            return jsonify(directive='continue',server_time=stamp(),expires_at=stamp(expires))

    @machine.post('/work/<work>/events')
    def append_events(work):
        body = payload()
        entries = body.get('events')
        require(isinstance(entries,list) and 0 < len(entries) <= 100, '事件批次无效')
        with write() as conn:
            row = held(conn, work)
            op = operation(conn, body)
            if op[2]:
                return saved_response(conn, op)
            where = (work, row['attempt'])
            last = conn.execute('SELECT coalesce(max(sequence),0) FROM execution_events WHERE work=? AND attempt=?', where).fetchone()[0]
            for entry in entries:
                require(isinstance(entry, dict) and uint(entry.get('sequence'),1,100000) and len(canonical(entry)) <= 8192,
                        '事件序号或内容无效')
                seq = entry['sequence']
                if seq > last + 1:
                    problem(409,'event_gap','事件序号必须连续')
                if seq <= last:
                    stored = conn.execute('SELECT payload FROM execution_events WHERE work=? AND attempt=? AND sequence=?',
                                          (*where,seq)).fetchone()
                    if stored['payload'] != json_text(entry):
                        problem(409,'event_conflict','事件内容发生变化')
                else:
                    conn.execute('INSERT INTO execution_events VALUES(?,?,?,?)',(*where,seq,json_text(entry)))
                    last = seq
            return saved_response(conn, op, dict(accepted_through=min(last, entries[-1]['sequence'])))

    @machine.get('/artifacts/<key>/content')
    def download(key):
        conn = db()
        work = held(conn, request.args.get('work_id'))
        permitted = {item['artifact_id']: item for item in json.loads(work['manifest'])['artifacts']}
        if key not in permitted:
            problem(404, 'artifact_not_found', '该工件不在当前工单的冻结输入中')
        item = permitted[key]
        return send_file(path_for(item), mimetype=item['media_type'], conditional=True, etag=item['sha256'])

    def part_count(row):
        return (row['size_bytes'] + PART_SIZE - 1) // PART_SIZE

    def part_bounds(row, number):
        start = (number - 1) * PART_SIZE
        return start, min(start + PART_SIZE, row['size_bytes']) - 1

    def upload_info(conn, row):
        parts = []
        for part in conn.execute('SELECT * FROM execution_parts WHERE upload=? ORDER BY part_no', (row['id'],)):
            start, end = part_bounds(row, part['part_no'])
            parts.append(dict(part_no=part['part_no'], start=start, end=end, sha256=part['sha256']))
        info = {k: row[k] for k in ('role','size_bytes','sha256')}
        info.update(upload_id=row['id'], work_id=row['work'], part_count=part_count(row), part_size_bytes=PART_SIZE,
                    state='complete' if row['artifact'] else 'open', received_parts=parts)
        if row['artifact']:
            info['artifact'] = artifact(conn, row['artifact'])
        return info

    def upload_row(conn, key):
        row = conn.execute('SELECT * FROM execution_uploads WHERE id=?', (key,)).fetchone()
        if not row:
            problem(404,'upload_not_found','上传会话不存在')
        if held(conn,row['work'])['attempt'] != row['attempt']:
            problem(409,'lease_lost','无法写入上次尝试的上传会话')
        return row

    @machine.post('/work/<work>/uploads')
    def begin_upload(work):
        body = payload()
        role, sha, size, mimetype = (body.get(k) for k in ('role','sha256','size_bytes','media_type'))
        require(isinstance(role,str) and isinstance(sha,str) and SHA.fullmatch(sha) and uint(size,1,MAX_ARTIFACT), '上传参数无效')
        with write() as conn:
            row = held(conn,work)
            require(role in output_roles(row, json.loads(row['manifest'])) and mimetype == ROLE_TYPES[role],
                    '工单不接受此产物或媒体类型')
            op = operation(conn,body)
            if op[2]:
                return saved_response(conn,op)
            found = conn.execute('SELECT * FROM execution_uploads WHERE work=? AND attempt=? AND role=?',
                                 (work,row['attempt'],role)).fetchone()
            if found:
                if (found['sha256'],found['size_bytes'],found['media_type']) != (sha,size,mimetype):
                    problem(409,'upload_conflict','该尝试已提交此角色的不同工件')
            else:
                key = identifier('upload')
                conn.execute('INSERT INTO execution_uploads VALUES(?,?,?,?,?,?,?,NULL)',
                             (key,work,row['attempt'],role,sha,size,mimetype))
                found = upload_row(conn,key)
            return saved_response(conn,op,upload_info(conn,found),201)

    @machine.get('/uploads/<key>')
    def get_upload(key):
        conn = db()
        return jsonify(upload_info(conn,upload_row(conn,key)))

    @machine.put('/uploads/<key>/parts/<int:number>')
    def put_part(key,number):
        row = upload_row(db(),key)
        require(1 <= number <= part_count(row), '分块编号超出范围')
        start,end = part_bounds(row,number)
        sha = request.headers.get('X-Part-SHA256','')
        require(SHA.fullmatch(sha) and request.content_length == end-start+1
                and request.headers.get('Content-Range') == f'bytes {start}-{end}/{row["size_bytes"]}', '分块范围、长度或摘要无效')
        content = request.get_data()
        require(hashlib.sha256(content).hexdigest() == sha, '分块 SHA-256 校验失败')
        with write() as conn:
            row = upload_row(conn,key)
            old = conn.execute('SELECT sha256 FROM execution_parts WHERE upload=? AND part_no=?', (key,number)).fetchone()
            if old:
                if old['sha256'] != sha:
                    problem(409,'part_conflict','此分块已写入不同内容')
            else:
                if row['artifact']:
                    problem(409,'upload_conflict','工件已封存')
                folder = parts_dir / key
                folder.mkdir(exist_ok=True)
                (folder / str(number)).write_bytes(content)
                conn.execute('INSERT INTO execution_parts VALUES(?,?,?)', (key,number,sha))
        return '', 204

    @machine.post('/uploads/<key>/complete')
    def seal_upload(key):
        body = payload()
        conn = db()
        row = upload_row(conn,key)
        require(body.get('sha256') == row['sha256'] and body.get('part_count') == part_count(row), '封存参数与会话不一致')
        op = operation(conn,body)
        if op[2]:
            return saved_response(conn,op)
        if row['artifact']:
            with write() as conn:
                row = upload_row(conn,key)
                return saved_response(conn,operation(conn,body),artifact(conn,row['artifact']))
        parts = upload_info(conn,row)['received_parts']
        if len(parts) != part_count(row):
            problem(409,'not_ready','请补齐未接收的分块')
        with tempfile.TemporaryDirectory(dir=parts_dir) as folder:
            assembled = Path(folder) / 'artifact'
            try:
                with assembled.open('wb') as out:
                    for part in parts:
                        with (parts_dir / key / str(part['part_no'])).open('rb') as file:
                            shutil.copyfileobj(file,out,PART_SIZE)
                require(assembled.stat().st_size == row['size_bytes'] and file_hash(assembled) == row['sha256'], '工件字节或 SHA-256 不匹配')
            except FileNotFoundError:
                # A simultaneous seal may already have committed and reclaimed the parts.
                if not upload_row(db(),key)['artifact']:
                    problem(409,'not_ready','上传分块暂不可用')
            with write() as conn:
                op = operation(conn,body)
                row = upload_row(conn,key)
                if op[2]:
                    return saved_response(conn,op)
                if row['artifact']:
                    item = artifact(conn,row['artifact'])
                else:
                    item = save_artifact(conn,identifier('artifact'),row['role'],row['sha256'],row['size_bytes'],
                                         row['media_type'],row['role'])
                    assembled.rename(path_for(item))
                    conn.execute('UPDATE execution_uploads SET artifact=? WHERE id=?',(item['artifact_id'],key))
                result = saved_response(conn,op,item)
        shutil.rmtree(parts_dir / key, ignore_errors=True)
        return result

    def output_set(conn,work,body):
        order = json.loads(work['manifest'])
        roles = output_roles(work, order)
        require(body.get('input_manifest_sha256') == work['manifest_hash'], '完成请求与冻结工单不符')
        outputs = body.get('outputs')
        require(isinstance(outputs,list) and len(outputs) == len(roles), '缺少规定的产物')
        sealed = conn.execute('SELECT artifact FROM execution_uploads WHERE work=? AND attempt=? AND artifact IS NOT NULL',
                              (work['id'],work['attempt'])).fetchall()
        items = {item['role']:item for item in (artifact(conn,r['artifact']) for r in sealed)}
        require(items.keys() == roles and all(isinstance(o,dict) and o.get('role') in items
                and items[o['role']] == o for o in outputs) and len({o['role'] for o in outputs}) == len(outputs),
                '产物与本次租约下已封存的对象不符')
        return order, {item['role']:item for item in order['artifacts']}, items

    def finish(conn,work,op,outputs):
        receipt = dict(receipt_id=identifier('receipt'),job_id=str(work['job']),work_id=work['id'],attempt=work['attempt'],kind=work['kind'],state='succeeded',
                       version=work['target'],committed_at=stamp(),artifacts=list(outputs.values()))
        conn.execute("UPDATE execution_work SET state='succeeded',updated=?,receipt=?,lease=NULL,expires=NULL,failure=NULL WHERE id=?",
                     (now(),json_text(receipt),work['id']))
        return saved_response(conn,op,receipt)

    def validate_delivery(order,inputs,outputs,verification,work):
        version = read_json(path_for(inputs['version_manifest']))
        approvals = read_json(path_for(inputs['approval_manifest']))
        count = version['frame_count']
        final_size = export_size(version['final_size'], order.get('export_quality'))
        common = dict(version_manifest_sha256=inputs['version_manifest']['sha256'],
                      approval_manifest_sha256=inputs['approval_manifest']['sha256'],
                      timeline_sha256=inputs['source_timeline']['sha256'])
        require(isinstance(verification,dict) and all(verification.get(k) == v for k,v in common.items())
                and verification.get('scene_sha256') == version['scene_sha256']
                and len(approvals['frames']) == count and approvals['scope'] == 'all_frames'
                and all(verification.get(k) == count for k in ('frame_count','verified_review_frames',
                        'white_frame_count','comparison_frame_count'))
                and verification.get('pts_verified') is True and verification.get('audio_verified') is True
                and verification.get('white_size') == final_size, '出片核验记录与已审核版本或出片画幅不一致')
        w,h = frame_size(final_size)
        require(verification.get('comparison_size') == dict(width=w,height=2*h)
                and (verification.get('render_source') == 'locked_scene_native_render' or
                     (verification.get('render_source') == 'approved_native_white' and version['review_size'] == final_size)),
                '出片画幅或渲染来源不匹配')
        def check_delivery(archive,files,index):
            indexed = index['files']
            for prefix, item in (('target/',inputs['source_video']), ('white_model/',outputs['white_video']),
                                 ('comparison/',outputs['comparison_video'])):
                selected = [record for record in indexed if record['path'].startswith(prefix)]
                require(len(selected)==1 and selected[0]['sha256']==item['sha256'] and selected[0]['size_bytes']==item['size_bytes'],
                        '交付包视频与封存对象不一致')
            require(any(k.startswith('model_3d/') and k.endswith('.blend') and files[k].file_size > 0 for k in files)
                    and any(k.startswith('meta/') and k.endswith('.jsonl') and files[k].file_size > 0 for k in files),
                    '交付包缺少可编辑工程或镜头描述')
            require(any(record['path'].startswith('model_3d/') and record['sha256'] == version['scene_sha256'] for record in indexed)
                    and any(record['path'].startswith('meta/') and record['sha256'] == inputs['shot_metadata']['sha256'] for record in indexed),
                    '交付工程或镜头描述与批准版本不一致')
            if order.get('review_storage'):
                require(any(record['path'] == 'model_3d/scene.blend' and record['sha256'] == version['scene_sha256']
                            for record in indexed), '交付工程 scene.blend 与审核版不符')
        index = bundle(path_for(outputs['delivery_bundle']),check_delivery)
        require(all(index.get(k) == v for k,v in common.items()) and index.get('job_id') == str(work['job'])
                and index.get('version') == work['target'] and index.get('final_size') == final_size, '交付包索引不匹配')

    @machine.post('/work/<work_id>/complete')
    def complete(work_id):
        body = payload()
        conn = db()
        op = operation(conn,body)
        if op[2]:
            return saved_response(conn,op)
        try:
            work = held(conn,work_id)
            order,inputs,outputs = output_set(conn,work,body)
            if work['kind'] == 'finalize':
                validate_delivery(order,inputs,outputs,body.get('final_verification'),work)
                with write() as conn:
                    current_token(conn)
                    op = operation(conn,body)
                    if op[2]:
                        return saved_response(conn,op)
                    work = held(conn,work_id)
                    conn.execute('INSERT INTO execution_deliveries VALUES(?,?,?)', (work['job'],work['target'],json_text(outputs)))
                    return finish(conn,work,op,outputs)
            timeline_item = outputs.get('source_timeline') or inputs['source_timeline']
            version = read_json(path_for(outputs['version_manifest']))
            timeline = read_json(path_for(timeline_item))
            expected = dict(job_id=str(work['job']),version=work['target'],source_sha256=inputs['source_video']['sha256'],
                            timeline_sha256=timeline_item['sha256'],
                            production_method='codex_trial',input_manifest_sha256=work['manifest_hash'],
                            **{role+'_sha256':outputs[role]['sha256'] for role in
                               ('review_bundle','shot_metadata','production_report')})
            require(all(version.get(k) == v for k,v in expected.items()) and timeline['source_sha256'] == expected['source_sha256'],
                    '送审版本与原片、时间线、产物或工单不一致')
            storage = order.get('review_storage')
            require(version.get('review_storage') == storage, '送审版本与工单的审片存储模式不一致')
            if storage:
                require(all(isinstance(version.get(key),str) and SHA.fullmatch(version[key])
                            for key in ('scene_sha256','scene_bundle_sha256')), '本机工程摘要无效')
            else:
                require(version.get('scene_bundle_sha256') == outputs['scene_bundle']['sha256'], '工程包摘要不匹配')
                scene = bundle(path_for(outputs['scene_bundle']))
                require(any(f['path'] == scene.get('scene_path') and f['sha256'] == scene.get('scene_sha256')
                            and f['path'].endswith('.blend') for f in scene['files']) and scene.get('scene_sha256') == version['scene_sha256'],
                        '工程摘要与送审版本不匹配')
            frame_size(version['final_size'])
            spec = job_inputs(conn,work['job'])
            permitted = set()
            if work['kind'] == 'revise':
                approvals = read_json(path_for(inputs['approval_manifest']))
                signatures = {f['number']:f['review_signature'] for f in approvals['frames']}
                permitted = {f['number'] for f in version['frames'] if signatures.get(f['number']) == f['review_signature']}
            inactive_base = None if work['kind'] == 'prepare' else work['target'] - 1

            def publish(conn, version_id, number):
                nonlocal result
                fresh = held(conn,work_id)
                require(number == work['target'], '审核版本号已变化')
                conn.execute('INSERT INTO execution_versions VALUES(?,?,?)',
                             (work['job'],number,json_text(outputs | {'source_timeline':timeline_item})))
                result = finish(conn,fresh,operation(conn,body),outputs)
                if conn.execute("SELECT 1 FROM versions WHERE id=? AND state='approved'",(version_id,)).fetchone():
                    after_review(conn,work['job'],version_id,'finalize')

            result = None
            review_bundle = outputs['review_bundle']
            # The client can retry before a large package finishes decoding. Share
            # the lock across server processes without blocking work heartbeats.
            with path_for(review_bundle).open('rb') as sealed:
                try:
                    fcntl.flock(sealed, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    problem(503, 'review_import_in_progress', '审核包正在入库，请稍后重试')
                import_version(None, work['job'], spec['title'], spec['actor'], inactive_base, channel='execution',
                               publish=publish, check=lambda conn: held(conn,work_id), inherit_only=permitted,
                               stage_frames=lambda destination: stage_review_frames(path_for, outputs, timeline, version, destination),
                               package_hash=review_bundle['sha256'])
            return result
        except (KeyError, TypeError, IndexError, UnicodeError, RecursionError, OSError) as error:
            raise ValueError('产物清单缺失必需字段或格式错误') from error
        except ProtocolProblem as error:
            if error.code == 'lease_lost':
                op = operation(db(),body)
                if op[2]:
                    return saved_response(db(),op)
            raise

    @machine.post('/work/<work_id>/fail')
    def fail(work_id):
        body = payload()
        code = body.get('code')
        require(isinstance(code,str) and code in FAILURES and bounded(body.get('message'),4096)
                and type(body.get('retryable')) is bool, '失败状态字段无效')
        with write() as conn:
            current_token(conn)
            op = operation(conn,body)
            if op[2]:
                return saved_response(conn,op)
            work = held(conn,work_id)
            conn.execute('''UPDATE execution_work SET state='failed',lease=NULL,expires=NULL,updated=?,available_at=0,failure=?
                         WHERE id=?''', (now(),code,work_id))
            conn.execute('UPDATE execution_nodes SET failure=? WHERE id=?',(code,g.node))
            return saved_response(conn,op,dict(work_state='failed',attempt=work['attempt'],retry_after_seconds=None))

    @machine.get('/jobs/<int:job>/status')
    def job_status(job):
        work_id = request.args.get('work_id','')
        with write() as conn:
            current_token(conn)
            expire_leases(conn)
            work = work_row(conn,work_id)
            if work['job'] != job or not conn.execute('SELECT 1 FROM execution_attempts WHERE work=? AND node=?',(work_id,g.node)).fetchone():
                problem(404,'work_not_found','设备未领取过此工单')
            control = conn.execute('SELECT state FROM job_controls WHERE job_id=?',(job,)).fetchone()
            current = conn.execute('SELECT state,number,id FROM versions WHERE job_id=? ORDER BY number DESC LIMIT 1',(job,)).fetchone()
            latest = conn.execute('SELECT state FROM execution_work WHERE job=? ORDER BY rowid DESC LIMIT 1',(job,)).fetchone()
            deliveries = [r['version'] for r in conn.execute('SELECT version FROM execution_deliveries WHERE job=? ORDER BY version',(job,))]
            state = work['state']
            job_state = {'leased':'running','succeeded':'review'}.get(latest['state'], latest['state'])
            directive = DIRECTIVES.get(state,'continue')
            if current and current['state'] == 'approved' and deliveries and latest['state'] == 'succeeded':
                job_state = 'done'
            if current and current['state'] == 'review' and conn.execute('SELECT 1 FROM supplements WHERE version_id=?',(current['id'],)).fetchone():
                job_state = 'supplement'
            if control and control['state'] != 'active':
                job_state = 'stopped' if control['state'] == 'withdrawn' else 'paused'
                if state != 'deleted':
                    directive = DIRECTIVES[job_state]
            status = dict(job_id=str(job),job_state=job_state,work_id=work_id,work_state=state,attempt=work['attempt'],
                          directive=directive,current_version=current['number'] if current else 0,
                          delivery_versions=deliveries,terminal=state in TERMINAL,server_time=stamp())
            if work['receipt']:
                status['receipt'] = json.loads(work['receipt'])
            return jsonify(status)

    def manage_page(secret=None, issued_node=None):
        with write() as conn:
            expire_leases(conn)
        conn = db()
        nodes = [dict(row, cap=json.loads(row['capabilities']) if row['capabilities'] else None,
                      online=bool(row['seen'] and row['seen'] > now()-ONLINE_SECONDS),
                      last_seen=stamp(row['seen']) if row['seen'] else None,
                      binding_state='已绑定' if row['bound_instance'] else ('可绑定' if row['instance'] else '等待注册'))
                 for row in conn.execute('''
                SELECT n.*, b.instance AS bound_instance, b.bound_at, bu.name AS bound_by_name,
                    (SELECT count(*) FROM execution_work w WHERE w.node=n.id AND w.state='leased') AS active,
                    (SELECT count(*) FROM execution_tokens t WHERE t.node=n.id AND t.revoked IS NULL
                        AND t.paused_at IS NULL AND t.expires>?) AS active_key_count
                FROM execution_nodes n
                LEFT JOIN execution_node_bindings b ON b.node=n.id
                LEFT JOIN users bu ON bu.id=b.bound_by
                WHERE n.deleted_at IS NULL
                ORDER BY n.rowid DESC''',(now(),))]
        history = conn.execute('''
            SELECT t.id,t.node,t.prefix,t.issued,t.expires,n.name,n.deleted_at AS node_deleted_at,u.name AS issuer,
                   CASE WHEN t.revoked IS NOT NULL OR n.deleted_at IS NOT NULL THEN '已撤销'
                        WHEN t.expires<=? THEN '已过期'
                        WHEN t.paused_at IS NOT NULL THEN '已暂停' ELSE '有效' END AS status
            FROM execution_tokens t JOIN execution_nodes n ON n.id=t.node JOIN users u ON u.id=t.issued_by
            WHERE t.deleted_at IS NULL ORDER BY t.id DESC''', (now(),)).fetchall()
        work = conn.execute('''SELECT w.*,j.title,n.name AS device,c.state AS control,
                            e.local_review,e.review_instance,b.name AS review_device,
                            coalesce(b.deleted_at IS NULL AND b.seen>? AND b.instance=e.review_instance
                                AND json_extract(b.capabilities,'$.review_storage')=?,0) AS review_online,
                            json_extract(w.manifest,'$.export_quality') AS export_quality,
                            EXISTS(SELECT 1 FROM execution_deliveries d WHERE d.job=w.job AND d.version=w.target) AS delivered,
                            EXISTS(SELECT 1 FROM versions v WHERE v.job_id=w.job) AS review_exists
                            FROM execution_work w JOIN jobs j ON j.id=w.job LEFT JOIN execution_nodes n ON n.id=w.node
                            JOIN execution_jobs e ON e.job=w.job LEFT JOIN execution_nodes b ON b.id=e.review_node
                            LEFT JOIN job_controls c ON c.job_id=w.job
                            WHERE w.deleted_at IS NULL AND w.state!='deleted'
                            ORDER BY w.rowid DESC LIMIT 100''',(now()-ONLINE_SECONDS,LOCAL_REVIEW)).fetchall()
        attempts = {row['id']: [] for row in work}
        if attempts:
            ids = ','.join('?' for _ in attempts)
            for row in conn.execute(f'''SELECT a.*,n.name AS device,u.name AS returned_by_name FROM execution_attempts a
                JOIN execution_nodes n ON n.id=a.node LEFT JOIN users u ON u.id=a.returned_by
                WHERE a.work IN ({ids}) ORDER BY a.attempt DESC''', tuple(attempts)):
                attempts[row['work']].append(row)
        return render_template('execution.html', nodes=nodes, tokens=history, work=work, attempts=attempts,
                               kinds=KINDS, secret=secret, issued_node=issued_node, stamp=stamp,
                               register_request=secrets.token_hex(16), issue_request=secrets.token_hex(16))

    @admin.get('/admin/execution')
    @requires('admin')
    def panel():
        return manage_page()

    def issue(conn,node,days):
        token = 'fw_' + secrets.token_urlsafe(32)
        timestamp = now()
        conn.execute('''INSERT INTO execution_tokens(node,hash,prefix,expires,issued,issued_by) VALUES(?,?,?,?,?,?)''',
                     (node,hashlib.sha256(token.encode()).hexdigest(),token[:12],timestamp+86400*days,timestamp,g.user['id']))
        return token

    def update_credential(conn, action, key):
        timestamp = now()
        token = conn.execute('''SELECT t.revoked,t.paused_at,t.expires,n.deleted_at AS node_deleted_at
                            FROM execution_tokens t JOIN execution_nodes n ON n.id=t.node
                            WHERE t.id=? AND t.deleted_at IS NULL''', (key,)).fetchone()
        if not token or (action == 'revoke' and token['revoked'] is not None):
            abort(404, '密钥不存在或已撤销')
        usable = token['revoked'] is None and token['expires'] > timestamp and token['node_deleted_at'] is None
        paused = token['paused_at'] is not None
        if action == 'pause':
            if not usable or paused:
                abort(409, '只能暂停有效的密钥，请刷新后再试。')
            conn.execute('UPDATE execution_tokens SET paused_at=? WHERE id=?', (timestamp,key))
            expire_leases(conn)
            return '密钥已暂停。原工单租约已失效。'
        if action == 'resume':
            if not usable or not paused:
                abort(409, '只能恢复未过期且已暂停的密钥，请刷新后再试。')
            conn.execute('UPDATE execution_tokens SET paused_at=NULL WHERE id=?', (key,))
            return '密钥已恢复，可继续使用原密钥。'
        if action == 'delete':
            if usable and not paused:
                abort(409, '请先暂停密钥，再删除该记录。')
            conn.execute('UPDATE execution_tokens SET deleted_at=? WHERE id=?', (timestamp,key))
        if token['revoked'] is None:
            conn.execute('UPDATE execution_tokens SET revoked=?,revoked_by=? WHERE id=?', (timestamp,g.user['id'],key))
            expire_leases(conn)
        return '密钥记录已删除。' if action == 'delete' else '密钥已撤销。该密钥的租约已失效。'

    @admin.post('/admin/execution/credentials')
    @requires('admin')
    def credentials():
        if not request.is_secure and not app.config.get('EXECUTION_TEST_HTTP'):
            abort(403,'请使用 HTTPS 管理设备密钥')
        action = request.form.get('action')
        node = request.form.get('node','')
        with write() as conn:
            assert_session('admin')
            if action in ('pause','resume','delete','revoke'):
                message = update_credential(conn, action, request.form.get('token_id', type=int))
                flash(message,'success')
                return redirect(url_for('execution.panel',_anchor='credentials'),code=303)
            days = request.form.get('days',type=int)
            if days not in (7,30,90,365) or action not in ('create','issue'):
                abort(422,'请选择密钥有效期')
            request_id = request.form.get('submission_id', '')
            section = 'devices' if action == 'create' else 'credentials'
            if not re.fullmatch(r'[a-f0-9]{32}', request_id):
                flash('表单已更新，请重新填写并提交。', 'error')
                return redirect(url_for('execution.panel', _anchor=section), code=303)
            submission = (g.user['id'], request_id)
            if conn.execute('SELECT 1 FROM execution_credential_submissions WHERE actor=? AND request_id=?',
                            submission).fetchone():
                flash('这次提交已处理。请在密钥列表查看原记录。', 'success')
                return redirect(url_for('execution.panel', _anchor='credentials'), code=303)
            if action == 'create':
                name = request.form.get('name','').strip()
                if not bounded(name,80):
                    abort(422,'请输入 1–80 字的设备名称')
                name_key = unicodedata.normalize('NFC', name).casefold()
                names = conn.execute('SELECT name FROM execution_nodes WHERE deleted_at IS NULL')
                if any(unicodedata.normalize('NFC', row['name']).casefold() == name_key for row in names):
                    flash(f'设备「{name}」已登记。如需更换密钥，请在密钥页选择这台设备。', 'error')
                    return redirect(url_for('execution.panel', _anchor='devices'), code=303)
                node = identifier('node')
                conn.execute('INSERT INTO execution_nodes(id,name) VALUES(?,?)',(node,name))
            elif not conn.execute('SELECT 1 FROM execution_nodes WHERE id=? AND deleted_at IS NULL',(node,)).fetchone():
                abort(404,'设备不存在')
            secret = issue(conn,node,days)
            conn.execute('INSERT INTO execution_credential_submissions(actor,request_id) VALUES(?,?)', submission)
        return manage_page(secret,node),201

    @admin.post('/admin/execution/devices/<node>/binding')
    @requires('admin')
    def device_binding(node):
        if not request.is_secure and not app.config.get('EXECUTION_TEST_HTTP'):
            abort(403, '请使用 HTTPS 管理设备绑定')
        action = request.form.get('action')
        if action not in ('bind', 'unbind'):
            abort(422, '设备绑定操作无效')
        with write() as conn:
            assert_session('admin')
            current = conn.execute('SELECT instance FROM execution_nodes WHERE id=? AND deleted_at IS NULL', (node,)).fetchone()
            if not current:
                abort(404, '设备不存在')
            existing = conn.execute('SELECT instance FROM execution_node_bindings WHERE node=?', (node,)).fetchone()
            expected = existing['instance'] if existing else current['instance']
            if not expected or request.form.get('instance') != expected:
                abort(409, '执行实例已更新或尚未注册，请刷新页面')
            if action == 'unbind':
                if not existing:
                    abort(409, '该设备已解除绑定，请刷新页面')
                conn.execute('DELETE FROM execution_node_bindings WHERE node=?', (node,))
                flash('设备实例绑定已解除。重新注册后可绑定新的执行实例。', 'success')
            else:
                if not existing:
                    conn.execute('INSERT INTO execution_node_bindings(node,instance,bound_at,bound_by) VALUES(?,?,?,?)',
                                 (node, expected, now(), g.user['id']))
                flash('当前执行实例已绑定。后续更换实例前请先解除绑定。', 'success')
        return redirect(url_for('execution.panel', _anchor='devices'), code=303)

    @admin.post('/admin/execution/devices/<node>/control')
    @requires('admin')
    def device_control(node):
        action = request.form.get('action')
        if action not in ('drain','resume'):
            abort(422,'设备操作无效')
        with write() as conn:
            assert_session('admin')
            if not conn.execute('UPDATE execution_nodes SET draining=? WHERE id=? AND deleted_at IS NULL',
                                (action=='drain',node)).rowcount:
                abort(404,'设备不存在')
        flash('设备接单状态已更新。','success')
        return redirect(url_for('execution.panel',_anchor='devices'),code=303)

    @admin.post('/admin/execution/devices/<node>/delete')
    @requires('admin')
    def delete_device(node):
        with write() as conn:
            assert_session('admin')
            timestamp = now()
            expire_leases(conn)
            if conn.execute("SELECT 1 FROM execution_work WHERE node=? AND state='leased'", (node,)).fetchone():
                abort(409, '此设备仍在执行工单。请等待工单完成，或暂停工单后再删除设备。')
            if not conn.execute('''UPDATE execution_nodes SET deleted_at=?,draining=1,slots=0
                                   WHERE id=? AND deleted_at IS NULL''', (timestamp,node)).rowcount:
                abort(404, '设备不存在')
            conn.execute('''UPDATE execution_tokens SET revoked=?,revoked_by=?
                            WHERE node=? AND revoked IS NULL''', (timestamp,g.user['id'],node))
        flash('设备已删除，关联密钥已撤销。', 'success')
        return redirect(url_for('execution.panel',_anchor='devices'),code=303)

    def stage_file(folder, field, suffixes):
        file = request.files.get(field)
        if not file or Path(file.filename).suffix.lower() not in suffixes:
            abort(422,'请检查上传文件类型')
        path = Path(folder) / field
        file.save(path)
        if not path.stat().st_size:
            abort(422,'上传的文件为空')
        return path,Path(file.filename).name

    def stage_files(folder, field, suffixes):
        files = request.files.getlist(field)
        if not files:
            abort(422, '请至少选择一个视频文件')
        staged = []
        for index, file in enumerate(files, 1):
            filename = Path(file.filename or '').name
            suffix = Path(filename).suffix.lower()
            if not filename or suffix not in suffixes:
                abort(422, f'第 {index} 个文件类型不支持，请上传 MP4、MOV、MKV、WEBM 或 M4V')
            path = Path(folder) / f'{field}-{index}{suffix}'
            file.save(path)
            if not path.stat().st_size:
                abort(422, f'第 {index} 个文件为空，请重新选择')
            staged.append((path, filename, suffixes[suffix]))
        return staged

    @admin.post('/admin/execution/jobs')
    @requires('admin')
    def create_job():
        quality = request.form.get('export_quality', '1080p')
        review_mode = request.form.get('review_mode', 'central')
        if quality not in EXPORT_QUALITIES or review_mode not in ('central', LOCAL_REVIEW):
            abort(422, '成片质量或审片存储模式无效')
        with tempfile.TemporaryDirectory(dir=parts_dir) as folder:
            staged = stage_files(folder, 'source', VIDEO_TYPES)
            stored = []
            created = []
            skipped = []
            try:
                with write() as conn:
                    assert_session('admin')
                    for source, filename, mimetype in staged:
                        sha = file_hash(source)
                        existing = conn.execute('''SELECT e.job FROM execution_jobs e JOIN execution_artifacts a ON a.id=e.source
                                                  LEFT JOIN execution_work w ON w.job=e.job AND w.kind='prepare' AND w.target=1
                                                  WHERE a.sha256=? ORDER BY CASE WHEN w.deleted_at IS NULL
                                                  AND w.state NOT IN ('deleted','stopped') THEN 0 ELSE 1 END,e.job LIMIT 1''',
                                                (sha,)).fetchone()
                        if existing:
                            skipped.append((filename, existing['job']))
                            continue
                        item = store(conn, source, 'source_video', mimetype, filename, sha)
                        stored.append(path_for(item))
                        title = Path(filename).stem.strip()[:120] or '原片'
                        job = conn.execute('INSERT INTO jobs(title) VALUES(?)', (title,)).lastrowid
                        conn.execute('''INSERT INTO execution_jobs(job,actor,source,local_review,export_quality)
                                        VALUES(?,?,?,?,?)''',
                                     (job, g.user['id'], item['artifact_id'], review_mode == LOCAL_REVIEW, quality))
                        enqueue(conn, job_inputs(conn, job), 'prepare', 1)
                        created.append(job)
            except BaseException:
                for path in stored:
                    path.unlink(missing_ok=True)
                raise
        messages = []
        if created:
            messages.append(f'已创建 {len(created)} 条工单')
        if skipped:
            messages.append(f'跳过 {len(skipped)} 个重复视频')
        flash('，'.join(messages) + '，等待空闲设备领取。' if created else '所选视频均已上传过，未创建新工单。',
              'success' if created else 'info')
        return redirect(url_for('execution.panel', _anchor='queue'), code=303)

    @admin.post('/admin/execution/jobs/<int:job>/export-quality')
    @requires('admin')
    def set_export_quality(job):
        quality = request.form.get('export_quality')
        if quality not in EXPORT_QUALITIES:
            abort(422, '请选择 1080p 或 4K')
        with write() as conn:
            assert_session('admin')
            options = export_options(conn, objects, job)
            if options is None:
                abort(404)
            if options['locked']:
                abort(409, '出片工单已创建，成片质量已冻结')
            if quality == '4k' and not options['allow_4k']:
                abort(422, '当前审核版本不支持 4K 出片')
            conn.execute('UPDATE execution_jobs SET export_quality=? WHERE job=?', (quality,job))
        flash(f'成片质量已保存为 {EXPORT_QUALITIES[quality]}。', 'success')
        return redirect(url_for('review', job_id=job), code=303)

    @admin.post('/admin/execution/jobs/<int:job>/control')
    @requires('admin')
    def control(job):
        action = request.form.get('action')
        reason = request.form.get('reason','').strip()
        if action not in ('pause','resume','withdraw','return') or not bounded(reason,250):
            abort(422,'请选择操作并填写原因')
        with write() as conn:
            assert_session('admin')
            spec = job_inputs(conn,job)
            if not spec:
                abort(404)
            expire_leases(conn)
            last = conn.execute('SELECT * FROM execution_work WHERE job=? ORDER BY rowid DESC LIMIT 1',(job,)).fetchone()
            previous = conn.execute('SELECT state FROM job_controls WHERE job_id=?',(job,)).fetchone()
            old = previous['state'] if previous else 'active'
            if last['deleted_at'] is not None or last['state'] == 'deleted':
                abort(409,'该工单已删除')
            if request.form.get('work') != last['id'] or request.form.get('attempt',type=int) != last['attempt']:
                abort(409,'工单已更新，请刷新后再操作')
            expected = request.form.get('state')
            if expected != last['state'] and not (action == 'return' and expected == 'leased' and last['state'] == 'failed'):
                abort(409,'工单已更新，请刷新后再操作')
            if action == 'return':
                if old != 'active' or last['state'] not in ('leased','failed') or last['node'] is None:
                    abort(409,'只能退回已领取且未完成的活动工单')
                returned = now()
                conn.execute('''UPDATE execution_attempts SET returned_at=?,returned_by=?,return_reason=?
                                WHERE work=? AND attempt=?''', (returned,g.user['id'],reason,last['id'],last['attempt']))
                conn.execute('''UPDATE execution_work SET state='queued',node=NULL,token=NULL,instance=NULL,lease=NULL,
                                expires=NULL,stage=NULL,completed=NULL,total=NULL,failure=NULL,available_at=0,updated=?
                                WHERE id=?''', (returned,last['id']))
                conn.execute('INSERT INTO execution_management_events VALUES(?,?,?,?,?)',(job,g.user['id'],action,reason,returned))
            else:
                allowed = {'active':{'pause','withdraw'},'paused':{'resume','withdraw'},'withdrawn':set()}
                if action not in allowed[old]:
                    abort(409,'当前作业状态不允许该操作')
                state = {'pause':'paused','resume':'active','withdraw':'withdrawn'}[action]
                conn.execute('''INSERT INTO job_controls(job_id,state,reason) VALUES(?,?,?) ON CONFLICT(job_id)
                             DO UPDATE SET state=excluded.state,reason=excluded.reason''',(job,state,reason))
                apply_control(conn,job,action)
                version = conn.execute('SELECT id FROM versions WHERE job_id=? ORDER BY number DESC LIMIT 1',(job,)).fetchone()
                if version:
                    conn.execute('UPDATE versions SET revision=revision+1 WHERE id=?',(version['id'],))
                    event(conn,job,version['id'],g.user['id'],action,reason)
                conn.execute('INSERT INTO execution_management_events VALUES(?,?,?,?,?)',(job,g.user['id'],action,reason,now()))
        if action == 'return':
            flash('工单已退回，等待原执行实例领取。' if spec['review_instance'] else '工单已退回，等待设备领取。','success')
        else:
            flash('工单操作已生效。','success')
        return redirect(url_for('execution.panel',_anchor='queue'),code=303)

    @admin.post('/admin/execution/work/<work>/delete')
    @requires('admin')
    def delete_work(work):
        with write() as conn:
            assert_session('admin')
            expire_leases(conn)
            row = conn.execute("""SELECT job,state,attempt FROM execution_work
                                  WHERE id=? AND deleted_at IS NULL AND state!='deleted'""", (work,)).fetchone()
            if not row:
                abort(404, '工单不存在')
            if row['state'] == 'leased':
                abort(409, '工单正在执行，请暂停或等待执行完成后再删除。')
            if request.form.get('state') != row['state'] or request.form.get('attempt',type=int) != row['attempt']:
                abort(409, '工单已更新，请刷新后再操作。')
            timestamp = now()
            state = 'succeeded' if row['state'] == 'succeeded' else 'deleted'
            conn.execute('''UPDATE execution_work SET deleted_at=?,state=?,lease=NULL,expires=NULL,updated=?
                            WHERE id=?''', (timestamp,state,timestamp,work))
            conn.execute('INSERT INTO execution_management_events VALUES(?,?,?,?,?)',
                         (row['job'],g.user['id'],'delete',f'{work} ({row["state"]})',timestamp))
        flash('工单已删除。历史审核和交付记录已保留。', 'success')
        return redirect(url_for('execution.panel',_anchor='queue'),code=303)

    @admin.get('/admin/execution/jobs/<int:job>/delivery/<int:version>/<role>')
    @requires('admin')
    def delivery(job,version,role):
        row = db().execute('SELECT artifacts FROM execution_deliveries WHERE job=? AND version=?',(job,version)).fetchone()
        item = json.loads(row['artifacts']).get(role) if row else None
        if not item:
            abort(404)
        return send_file(path_for(item),mimetype=item['media_type'],conditional=True,etag=item['sha256'],
                         as_attachment=role=='delivery_bundle',download_name=role+('.zip' if role=='delivery_bundle' else '.mp4'))

    app.register_blueprint(admin)
    app.register_blueprint(machine)
    return after_review, apply_control
