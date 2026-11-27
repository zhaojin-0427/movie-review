import fcntl
import hashlib
import io
import json
import os
import re
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Barrier
from pathlib import Path
from uuid import uuid4

from werkzeug.datastructures import MultiDict
from zipfile import ZipFile, ZipInfo

from PIL import Image

from app import create_app
from execution import (LOCAL_REVIEW, PART_SIZE, REJECTED_FRAMES_MODE, REVIEW_STORAGE_HEADER,
                       REVIEW_UPLOAD_HEADER, object_hash)

BASE = 'https://localhost'
PASSWORD = 'test-only-passphrase'
PROFILE = dict(compatibility=dict(runner_commit='a'*40,decoder_id='sim-decoder',blender='test',ffmpeg='test',codex_cli='test'),
               final_width=32,color_policy='bt709',signature_scheme='pair-rgb8-timing-v1',codex_provider='test',
               codex_model='test',reasoning_effort='max',review_landscape_limit=dict(width=32,height=18),
               review_portrait_limit=dict(width=18,height=32))


def to_json(value):
    return json.dumps(value,ensure_ascii=False).encode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def zip_bytes(files, index=None):
    data = io.BytesIO()
    with ZipFile(data,'w') as bundle:
        for name, content in files.items():
            bundle.writestr(name,content)
        if index is not None:
            bundle.writestr('index.json',to_json(index | dict(files=[dict(path=name,size_bytes=len(content),sha256=sha(content))
                                                                    for name,content in files.items()])))
    return data.getvalue()


def image(fmt, size, color):
    data = io.BytesIO()
    Image.new('RGB',size,color).save(data,fmt)
    return data.getvalue()


class ExecutionTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.app = create_app(dict(TESTING=True,SECRET_KEY='test',DATA_DIR=self.root))
        creds = self.root/'accounts.json'
        creds.write_bytes(to_json([dict(name=name,username=name,role=role,password=PASSWORD)
                                  for name,role in [('admin','admin'),('reviewer','reviewer'),('other','reviewer')]]))
        result = self.app.test_cli_runner().invoke(args=['provision',str(creds)])
        self.assertEqual(result.exit_code,0,result.output)
        self.admin = self.login('admin')
        self.reviewer = self.login('reviewer')
        self.other = self.login('other')
        self.api = self.app.test_client()
        self.node,self.token = self.issue('演练 Mac')
        self.instance = str(uuid4())

    def login(self,name):
        client = self.app.test_client()
        client.get('/login',base_url=BASE)
        self.assertEqual(self.post(client,'/login',username=name,password=PASSWORD).status_code,303)
        client.get('/',base_url=BASE)
        return client

    def credential_submission(self, client, action):
        page = client.get('/admin/execution', base_url=BASE).text
        return re.search(rf'name="action" value="{action}">\s*'
                         r'<input type="hidden" name="submission_id" value="([a-f0-9]{32})"', page)[1]

    def post(self,client,route,**data):
        if route == '/admin/execution/credentials' and data.get('action') in ('create', 'issue'):
            if 'submission_id' not in data:
                data['submission_id'] = self.credential_submission(client, data['action'])
        with client.session_transaction(base_url=BASE) as session:
            data['csrf'] = session['csrf']
        return client.post(route,data=data,base_url=BASE)

    def issue(self,name):
        response = self.post(self.admin,'/admin/execution/credentials',action='create',name=name,days='7')
        self.assertEqual(response.status_code,201,response.text)
        token = re.search(r'value="(fw_[A-Za-z0-9_-]{43})"',response.text)[1]
        node = re.search(r'value="(node_[a-f0-9]{32})"',response.text)[1]
        self.assertNotIn(token,self.admin.get('/admin/execution',base_url=BASE).text)
        return node,token

    def machine(self,method,path,body=None,*,status=200,key=None,lease=None,token=None,base=BASE,headers=None):
        auth = {'Authorization':'Bearer '+(token or self.token), **(headers or {})}
        if key:
            auth['Idempotency-Key'] = key
        if lease:
            auth.update({'X-Lease-Id':lease['lease_id'],'X-Fence':str(lease['fence']),'X-Attempt':str(lease['attempt'])})
        params = {'data':body} if isinstance(body,bytes) else {'json':body}
        response = self.api.open('/api/v1'+path,method=method,base_url=base,headers=auth,buffered=True,**params)
        self.assertEqual(response.status_code,status,response.text[:3000] if response.is_json else response.status)
        return response

    def setup_profile(self, **job_options):
        # The worker still sends these legacy fields to verify compatibility, but the
        # server must not persist or use them for dispatch.
        self.profile = {'profile_hash': 'a' * 64}
        self.environment = dict(os='macOS 15.7', arch='arm64', gpu='test', render_backend='cpu',
                                model_evidence='observed', compatibility={'legacy': 'ignored'},
                                codex_model='legacy-model', codex_provider='legacy-provider', reasoning_effort='max')
        self.source = b'synthetic-original'
        job_options.setdefault('review_mode', 'central')
        job = self.post(self.admin,'/admin/execution/jobs',source=(io.BytesIO(self.source),'模拟影片.mp4'), **job_options)
        self.assertEqual(job.status_code,303,job.text)

    def test_oss_auto_and_manual_share_version_validation(self):
        from types import SimpleNamespace
        from oss_processor import create_processor

        objects = {}
        uploaded_parts = {}
        queue_source = b'synthetic-oss-source'
        setup = dict(region='cn-beijing', source_bucket='video-public', review_bucket='video-gr',
                     access_key_id='testAccessKey123', access_key_secret='testSecretKey456', action='save')
        secret = 'worker-processor-test-token-' * 2

        class TestBucket:
            def __init__(self, location):
                self.location = location

            def init_multipart_upload(self, key, headers):
                return SimpleNamespace(upload_id=key)

            def sign_url(self, method, key, expires, **options):
                return self.location.url + '?signed=test'

            def complete_multipart_upload(self, key, upload_id, parts):
                objects[self.location.bucket, key] = uploaded_parts[upload_id]

        def fetch(location, path, expected_size=None, expected_sha=None, limit=16 * 1024**3):
            content = objects[location.bucket, location.key]
            if len(content) > limit or (expected_size is not None and len(content) != expected_size):
                raise ValueError('size mismatch')
            if expected_sha is not None and sha(content) != expected_sha:
                raise ValueError('checksum mismatch')
            path.write_bytes(content)

        def publish(location, path, content_type):
            objects[location.bucket, location.key] = path.read_bytes()

        with patch.dict(os.environ, {'MOVIE_REVIEW_OSS_PROCESSOR_TOKEN':secret}):
            processor = create_processor().test_client()

        def cloud(task):
            response = processor.post('/process', json=task, headers={'Authorization':'Bearer '+secret})
            if response.status_code != 200:
                raise ValueError(response.json['message'])
            return response.json

        app_config = dict(TESTING=True, SECRET_KEY='test', DATA_DIR=self.root,
                          OSS_PROCESSOR=cloud, OSS_BUCKET_FACTORY=lambda location, *_: TestBucket(location))
        self.app = create_app(app_config)
        self.admin, self.reviewer, self.other = (self.login(name) for name in ('admin','reviewer','other'))
        self.api = self.app.test_client()
        page = '/admin/execution/oss'
        self.assertEqual(self.post(self.reviewer,page,**setup).status_code,403)
        self.assertEqual(self.post(self.admin,page,**setup).status_code,303)
        response = self.admin.get(page,base_url=BASE)
        self.assertIn('y123',response.text)
        self.assertNotIn(setup['access_key_secret'],response.text)
        self.assertNotIn(setup['access_key_id'],response.text)
        with closing(sqlite3.connect(self.root/'review.sqlite3')) as conn:
            self.assertNotIn(setup['access_key_secret'],conn.execute('SELECT encrypted_secret FROM oss_settings').fetchone()[0])

        def process():
            result = self.app.test_cli_runner().invoke(args=['oss-process'])
            self.assertEqual(result.exit_code,0,result.output)

        def parts(_bucket, _key, upload):
            return [SimpleNamespace(part_number=1, size=len(uploaded_parts[upload]))]

        def browser_upload(filename, content, **fields):
            base = '/admin/execution/oss/uploads'
            begin = self.post(self.admin,base,filename=filename,size_bytes=len(content),**fields)
            self.assertEqual(begin.status_code,201,begin.text)
            path = base + '/' + begin.json['id']
            with closing(sqlite3.connect(self.root/'review.sqlite3')) as conn:
                multipart = conn.execute('SELECT multipart_id FROM oss_transfers WHERE id=?',(begin.json['id'],)).fetchone()[0]
            self.assertIn('.aliyuncs.com/',self.post(self.admin,path+'/parts/1').json['url'])
            uploaded_parts[multipart] = content
            completed = self.post(self.admin,path+'/complete')
            self.assertEqual(completed.status_code,202,completed.text)
            self.assertEqual(self.post(self.admin,path+'/complete').status_code,202)
            return path

        def transport(work, records, verification=None, **overrides):
            files = {role: payload for role,payload,mime in records}
            header = dict(job_id=work['job_id'],work_id=work['work_id'],attempt=work['lease']['attempt'],
                          input_manifest_sha256=work['input_manifest_sha256'],outputs={role: role for role in files})
            if verification:
                header['final_verification'] = verification
            return zip_bytes(files | {'submission.json':to_json(header | overrides)})

        def submit(work, key, content=None, status=202):
            payload = dict(input_manifest_sha256=work['input_manifest_sha256'])
            if content is not None:
                payload.update(size_bytes=len(content),sha256=sha(content))
            return self.machine('POST',f'/work/{work["work_id"]}/oss/submit',payload,
                                lease=work['lease'],key=key,status=status).json

        with patch('oss_processor.fetch',fetch), patch('oss_processor.publish',publish), patch('oss2.PartIterator',parts):
            source_path = browser_upload('云端原片.mp4', queue_source)
            process()
            job_id = self.admin.get(source_path,base_url=BASE).json['result']['job']
            job = f'/jobs/{job_id}'
            self.source = queue_source
            self.profile = {'profile_hash':'a'*64}
            self.environment = dict(os='macOS 15.7',render_backend='cpu')
            self.assertEqual(self.post(self.admin,f'/admin/execution/devices/{self.node}/oss-delivery',
                                       oss_delivery='auto').status_code,303)
            self.register(local=True)
            self.heartbeat()
            self.claim('legacy-no-oss',204)
            self.machine('POST','/workers/register',dict(node_key=self.node,instance_id=self.instance,
                         capabilities=dict(slots=1,max_frame_width=32,work_kinds=['prepare','revise','finalize'],
                                           render_backends=['cpu'],oss_delivery=True)),
                         key='register-oss',headers={REVIEW_STORAGE_HEADER:LOCAL_REVIEW, REVIEW_UPLOAD_HEADER:REJECTED_FRAMES_MODE})
            self.heartbeat()
            work = self.claim('oss-first')
            self.assertIn('/movie-review/published/',work['oss']['source_url'])
            self.assertEqual(work['oss']['delivery'],'auto')
            self.assertEqual(work['input_manifest']['review_storage']['instance_id'],self.instance)
            self.assertNotIn('access_key',json.dumps(work).lower())
            self.return_work(work,'leased')
            self.heartbeat()
            fresh = self.claim('oss-first-returned')
            self.assertNotEqual(work['oss']['upload']['key'],fresh['oss']['upload']['key'])
            content = transport(fresh,self.produce_review(fresh,1,return_records=True))
            submit(work,'stale-after-return',content,status=409)
            target = fresh['oss']['upload']
            objects[target['bucket'],target['key']] = content
            self.assertEqual(submit(fresh,'auto-complete',content),submit(fresh,'auto-complete',content))
            process()
            self.assertEqual(self.post(self.reviewer,job+'/claim').status_code,303)
            image_url = job+'/v/1/image/1'
            image_response = self.reviewer.get(image_url,base_url=BASE)
            self.assertEqual(image_response.status_code,302)
            self.assertIn('video-gr.',image_response.location)
            self.assertEqual(self.other.get(image_url,base_url=BASE).status_code,404)
            self.assertEqual(self.post(self.reviewer,job+'/v/1/frames/1',action='approve',revision='0').status_code,303)
            self.assertEqual(self.post(self.reviewer,job+'/v/1/frames/2',action='save',revision='1',note='调整').status_code,303)
            self.assertEqual(self.post(self.reviewer,job+'/v/1/return',revision='2').status_code,303)
            self.assertEqual(self.post(self.admin,f'/admin/execution/devices/{self.node}/oss-delivery',
                                       oss_delivery='manual').status_code,303)
            self.heartbeat()
            revise = self.claim('oss-manual-revise')
            self.assertEqual(revise['oss']['delivery'],'manual')
            self.assertEqual(revise['input_manifest']['review_upload'],REJECTED_FRAMES_MODE)
            self.assertIsNone(revise['oss']['upload'])
            self.assertEqual(revise['input_manifest']['review_storage']['instance_id'],self.instance)
            records = self.produce_review(revise,2,return_records=True)
            self.assertEqual(submit(revise,'manual-ready')['state'],'awaiting_manual')
            invalid = transport(revise,records,attempt=99)
            bad_zip = browser_upload('错误批次.zip',invalid,work_id=revise['work_id'])
            process()
            self.assertEqual(self.admin.get(bad_zip,base_url=BASE).json['state'],'rejected')
            correct = transport(revise,records)
            good_zip = browser_upload('返修.zip',correct,work_id=revise['work_id'])
            process()
            self.assertEqual(self.admin.get(good_zip,base_url=BASE).json['state'],'done')
            with closing(sqlite3.connect(self.root/'review.sqlite3')) as conn:
                artifacts = json.loads(conn.execute(
                    'SELECT artifacts FROM execution_versions WHERE job=? AND version=2',(job_id,)).fetchone()[0])
                location = json.loads(conn.execute('SELECT location FROM oss_artifacts WHERE artifact=?',
                                                   (artifacts['review_bundle']['artifact_id'],)).fetchone()[0])
            with ZipFile(io.BytesIO(objects[location['bucket'],location['key']])) as merged:
                self.assertIn('pairs/0001.jpg', merged.namelist())
                self.assertIn('pairs/0002.jpg', merged.namelist())
            frame = self.reviewer.get(job+'/v/2/frame-data/1?revision=0',base_url=BASE).json['frame']
            self.assertEqual(frame['state'],'approved')
            self.assertEqual(self.reviewer.get(image_url,base_url=BASE).location,image_response.location)
            self.assertEqual(self.post(self.reviewer,job+'/v/2/frames/2',action='approve',revision='0').status_code,303)
            self.heartbeat()
            final = self.claim('oss-final')
            inputs = self.artifacts(final)
            size = dict(width=32,height=18)
            hashes = dict(version_manifest_sha256=inputs['version_manifest']['sha256'],
                          approval_manifest_sha256=inputs['approval_manifest']['sha256'],
                          timeline_sha256=inputs['source_timeline']['sha256'])
            white, compare = b'white-video', b'compare-video'
            delivery = zip_bytes({'target/source.mp4':self.source,'white_model/white.mp4':white,
                                  'comparison/compare.mp4':compare,'model_3d/scene.blend':self.scene,
                                  'meta/scene.jsonl':self.meta},
                                 hashes | dict(job_id=str(job_id),version=2,final_size=size))
            verification = hashes | dict(scene_sha256=sha(self.scene),frame_count=2,verified_review_frames=2,
                white_frame_count=2,comparison_frame_count=2,pts_verified=True,audio_verified=True,white_size=size,
                comparison_size=dict(width=32,height=36),render_source='approved_native_white')
            submit(final,'manual-final-ready')
            content = transport(final,[('delivery_bundle',delivery,'application/zip'),
                                ('white_video',white,'video/mp4'),('comparison_video',compare,'video/mp4')],verification)
            final_zip = browser_upload('交付.zip',content,work_id=final['work_id'])
            process()
            self.assertEqual(self.admin.get(final_zip,base_url=BASE).json['state'],'done')
            download = self.admin.get('/admin/execution'+job+'/delivery/2/delivery_bundle',base_url=BASE)
            self.assertEqual(download.status_code,302)
            self.assertIn('video-gr.',download.location)

    def test_oss_objects_uses_pinned_images_and_whole_delivery_without_cloud_processor(self):
        from types import SimpleNamespace
        from oss_objects import file_type

        objects, uploads, reads = {}, {}, []
        parts = {}
        content = b'original-video'
        self.environment = dict(os='macOS 15.7',render_backend='cpu')
        artifact_versions = 0

        def uploaded(key, data, mime, headers=None):
            nonlocal artifact_versions
            artifact_versions += 1
            tag = f'version-{artifact_versions}'
            metadata = {'Content-Type':mime,'x-oss-version-id':tag,'x-oss-meta-sha256':sha(data)} | (headers or {})
            objects[key,tag] = (data,metadata)
            objects[key,None] = objects[key,tag]
            return dict(version_id=tag,size_bytes=len(data),sha256=sha(data))

        class FakeBucket:
            def __init__(self, location):
                self.location = location

            def init_multipart_upload(self, key, headers):
                uploads[key] = headers
                return SimpleNamespace(upload_id=key)

            def complete_multipart_upload(self, key, upload_id, result, **_):
                data = parts[upload_id]
                uploaded(key,data,uploads[key]['Content-Type'],uploads[key])
                return self.head_object(key)

            def sign_url(self, method, key, expires, **options):
                location = self.location
                url = f'https://{location.bucket}.oss-{location.region}.aliyuncs.com/{key}'
                version = (options.get('params') or {}).get('versionId')
                return url + ('?versionId='+version if version else '')

            def head_object(self,key,params=None):
                data, headers = objects[key,(params or {}).get('versionId')]
                return SimpleNamespace(content_length=len(data),headers=headers)

            def get_object(self,key,params=None):
                reads.append(key)
                self_data = objects[key,(params or {}).get('versionId')][0]
                return io.BytesIO(self_data)

        self.app = create_app(dict(TESTING=True,SECRET_KEY='test',DATA_DIR=self.root,
                                   OSS_BUCKET_FACTORY=lambda location,*_: FakeBucket(location)))
        self.admin, self.reviewer, self.other = (self.login(name) for name in ('admin','reviewer','other'))
        self.api = self.app.test_client()
        self.profile = {'profile_hash':'a'*64}
        self.assertEqual(self.post(self.admin,'/admin/execution/oss',region='cn-beijing',source_bucket='video-public',
            review_bucket='video-gr',access_key_id='testAccessKey123',access_key_secret='testSecretKey456',
            action='save',transport='objects_v1').status_code,303)
        browser = '/admin/execution/oss/uploads'

        def multipart(reply,data):
            self.assertEqual(reply.status_code,201,reply.text)
            key = reply.json['id']
            with closing(sqlite3.connect(self.root/'review.sqlite3')) as conn:
                upload = conn.execute('SELECT multipart_id FROM oss_transfers WHERE id=?',(key,)).fetchone()[0]
            parts[upload] = data
            response = self.post(self.admin,f'{browser}/{key}/complete')
            self.assertEqual(response.status_code,202,response.text)
            return self.finish_object_transfer(key,browser)

        def partials(_bucket, _key, upload):
            return [SimpleNamespace(part_number=1,size=len(parts[upload]))]

        with patch('oss2.PartIterator',partials):
            origin = multipart(self.post(self.admin,browser,filename='原片.mp4',size_bytes=len(content),
                                        sha256=sha(content)),content)
        job_id = str(origin['job'])
        job = '/jobs/'+job_id
        self.assertEqual(reads,[])
        page = self.admin.get('/admin/execution',base_url=BASE).text
        self.assertIn('data-oss-protocol="objects_v1"',page)
        self.assertIn('vendor/zip-native.min.js',page)
        self.register(local=True)
        self.heartbeat()
        self.claim('old-objects-capability',204)
        self.machine('POST','/workers/register',dict(node_key=self.node,instance_id=self.instance,
                      capabilities=dict(slots=1,max_frame_width=32,work_kinds=['prepare','revise','finalize'],
                                        render_backends=['cpu'],oss_objects=True)),
                     key='register-objects',headers={REVIEW_STORAGE_HEADER:LOCAL_REVIEW})
        self.assertEqual(self.post(self.admin,f'/admin/execution/devices/{self.node}/oss-delivery',
                                  oss_delivery='auto').status_code,303)
        self.heartbeat()
        first = self.claim('objects-first')
        self.assertEqual(first['oss']['protocol'],'objects_v1')
        self.assertIn('video-public.oss-cn-beijing.aliyuncs.com/movie-review/sources/',first['oss']['source_url'])

        def descriptor(work, files, target):
            return {name:uploaded(f'{target}/{name}',data,file_type(work['kind'],name)[1])
                    for name,data in files.items()}

        def batch(work,key):
            return self.machine('POST',f'/work/{work["work_id"]}/oss/objects',
                      dict(input_manifest_sha256=work['input_manifest_sha256']),
                      lease=work['lease'],key=key,status=201).json

        def submit(work,key,extra=None,status=202):
            response = self.machine('POST',f'/work/{work["work_id"]}/oss/submit',
                        dict(input_manifest_sha256=work['input_manifest_sha256'],**(extra or {})),
                        lease=work['lease'],key=key,status=status)
            return response.json

        old = batch(first,'begin-old')
        self.return_work(first,'leased')
        self.heartbeat()
        work = self.claim('objects-after-return')
        target = batch(work,'begin-new')
        self.assertEqual(batch(work,'begin-new'),target)
        self.assertNotEqual(old['key'],target['key'])
        self.assertIn('/review/v0001/',target['key'])
        submit(first,'stale-object-submit',status=409)
        self.machine('POST',f'/work/{work["work_id"]}/oss/submit',
                     dict(input_manifest_sha256=work['input_manifest_sha256'],upload_id=old['upload_id'],
                          objects={'pairs/000001.jpg':{'version_id':'old','size_bytes':1,'sha256':'a'*64}}),
                     key='use-old-directory',lease=work['lease'],status=422)
        size = dict(width=32,height=18)
        first_jpg = image('JPEG',(32,36),'red')
        second_jpg = image('JPEG',(32,36),'gray')
        timeline = to_json(dict(source_sha256=sha(content),time_base='1/1000',end_pts=80,
            frames=[dict(number=1,pts=0),dict(number=2,pts=40)]))
        shot = b'{"description":"scene"}\n'
        report = to_json(dict(frame_count=2))
        scene_hash = sha(b'model')
        frame_paths = [f'pairs/{n:06}.jpg' for n in (1,2)]
        frames = [dict(number=n,pts=(n-1)*40,pair_jpeg_path=frame_paths[n-1],
                       review_signature=sha(data)) for n,data in enumerate((first_jpg,second_jpg),1)]

        def review_files(work, changed, frame_list, base_sha=None):
            bound = dict(job_id=job_id,version=work['input_manifest']['target_version'],source_sha256=sha(content),
                         timeline_sha256=sha(timeline),input_manifest_sha256=work['input_manifest_sha256'],
                         review_storage=work['input_manifest']['review_storage'],review_media='objects_v1')
            index = to_json(bound | dict(base_manifest_sha256=base_sha,
                frames=[dict(number=n,size_bytes=len(data),sha256=sha(data)) for n,data in sorted(changed.items())]))
            version = to_json(bound | dict(shot_metadata_sha256=sha(shot),production_report_sha256=sha(report),
                review_index_sha256=sha(index),production_method='codex_trial',review_size=size,final_size=size,
                scene_sha256=scene_hash,frame_count=2,frames=frame_list))
            files = {'review-index.json':index,'version.json':version,'meta.jsonl':shot,'production.json':report}
            if work['kind']=='prepare':
                files['timeline.json'] = timeline
            return files | {frame_paths[n-1]:data for n,data in changed.items()}

        files = review_files(work,{1:first_jpg,2:second_jpg},frames)
        declarations = descriptor(work,files,target['key'])
        uploaded(f'{target["key"]}/{frame_paths[0]}',b'replaced at same key','image/jpeg')
        submission = submit(work,'submit-first',dict(upload_id=target['upload_id'],objects=declarations))
        receipt = self.finish_object_transfer(submission['transfer_id'])
        self.assertEqual(receipt['state'],'succeeded')
        self.assertEqual(self.post(self.reviewer,job+'/claim').status_code,303)
        prior = self.reviewer.get(job+'/v/1/image/1',base_url=BASE)
        self.assertIn('versionId='+declarations[frame_paths[0]]['version_id'],prior.location)
        sources = self.reviewer.get(job+'/v/1/image-sources',base_url=BASE)
        self.assertEqual(sources.json['sources']['1'],prior.location)
        self.assertIn('versionId='+declarations[frame_paths[1]]['version_id'],sources.json['sources']['2'])
        self.assertEqual(self.other.get(job+'/v/1/image-sources',base_url=BASE).status_code,404)

        self.assertEqual(self.other.get(job+'/v/1/image/1',base_url=BASE).status_code,404)
        self.assertEqual(self.post(self.reviewer,job+'/v/1/frames/1',action='approve',revision='0').status_code,303)
        self.assertEqual(self.post(self.reviewer,job+'/v/1/frames/2',action='save',revision='1',note='调整').status_code,303)
        self.assertEqual(self.post(self.reviewer,job+'/v/1/return',revision='2').status_code,303)
        self.assertEqual(self.post(self.admin,f'/admin/execution/devices/{self.node}/oss-delivery',
                                  oss_delivery='manual').status_code,303)
        self.heartbeat()
        revise = self.claim('objects-revise')
        self.assertEqual(revise['oss']['review_scope'],'rejected_frames')
        self.assertEqual(submit(revise,'manual-review-ready')['state'],'awaiting_manual')

        def reupload(files):
            reply = self.post(self.admin,browser,filename='review.zip',size_bytes=100,work_id=revise['work_id'])
            self.assertEqual(reply.status_code,201,reply.text)
            self.assertFalse(reply.json['multipart'])
            transfer = reply.json['id']
            upload_path = browser+'/'+transfer
            declarations = {}
            for name,data in files.items():
                size_sha = dict(size_bytes=len(data),sha256=sha(data))
                signed = self.post(self.admin,upload_path+'/objects/sign',objects=json.dumps({name:size_sha}))
                self.assertEqual(signed.status_code,200,signed.text)
                signed_ref = signed.json[name]
                key = signed_ref['url'].split('.aliyuncs.com/',1)[1]
                self.assertIn('/review/v0002/',key)
                declarations[name] = uploaded(key,data,signed_ref['headers']['Content-Type'],signed_ref['headers'])
            response = self.post(self.admin,upload_path+'/objects/complete',objects=json.dumps(declarations))
            self.assertEqual(response.status_code,202,response.text)
            return self.finish_object_transfer(transfer,browser,raw=True)

        third_jpg = image('JPEG',(32,36),'blue')
        revised = [frames[0], frames[1] | dict(review_signature=sha(third_jpg))]
        revise_files = review_files(revise,{2:third_jpg},revised,self.artifacts(revise)['version_manifest']['sha256'])
        self.assertEqual(reupload(revise_files | {frame_paths[0]:first_jpg})['state'],'rejected')
        self.assertEqual(reupload(revise_files)['state'],'done')
        self.assertEqual(self.reviewer.get(job+'/v/2/image/1',base_url=BASE).location,prior.location)
        self.assertEqual(self.reviewer.get(job+'/v/2/frame-data/1?revision=0',base_url=BASE).json['frame']['state'],'approved')
        self.assertEqual(self.post(self.reviewer,job+'/v/2/frames/2',action='approve',revision='0').status_code,303)
        self.heartbeat()
        final = self.claim('objects-final')
        self.assertEqual(submit(final,'manual-delivery-ready')['state'],'awaiting_manual')
        inputs = self.artifacts(final)
        verification = dict(version_manifest_sha256=inputs['version_manifest']['sha256'],
            approval_manifest_sha256=inputs['approval_manifest']['sha256'],timeline_sha256=inputs['source_timeline']['sha256'],
            scene_sha256=scene_hash,frame_count=2,verified_review_frames=2,white_frame_count=2,
            comparison_frame_count=2,pts_verified=True,audio_verified=True,white_size=size,
            comparison_size=dict(width=32,height=36),render_source='approved_native_white')
        # The submitted ZIP stays intact and only its object properties are inspected.
        file = zip_bytes({'delivery-proof.json':to_json(dict(final_verification=verification)),'media/render.mp4':b'delivery-video'})
        with patch('oss2.PartIterator',partials):
            delivered = multipart(self.post(self.admin,browser,filename='成片.zip',size_bytes=len(file),
                sha256=sha(file),work_id=final['work_id'],input_manifest_sha256=final['input_manifest_sha256'],
                final_verification=json.dumps(verification)),file)
        self.assertEqual([item['role'] for item in delivered['artifacts']],['delivery_bundle'])
        download = self.admin.get('/admin/execution'+job+'/delivery/2/delivery_bundle',base_url=BASE)
        self.assertIn('/delivery/from-v0002/',download.location)
        self.assertTrue(all(key.endswith(('version.json','review-index.json','timeline.json')) for key in reads),reads)
        self.assertEqual(objects[download.location.split('.aliyuncs.com/',1)[1].split('?',1)[0],None][0],file)

    def finish_object_transfer(self, transfer, portal_path=None, raw=False):
        result = self.app.test_cli_runner().invoke(args=['oss-process'])
        self.assertEqual(result.exit_code,0,result.output)
        if portal_path:
            data = self.admin.get(portal_path+'/'+transfer,base_url=BASE).json
        else:
            with closing(sqlite3.connect(self.root/'review.sqlite3')) as conn:
                state,error,payload = conn.execute('SELECT state,error,result FROM oss_transfers WHERE id=?',(transfer,)).fetchone()
            data = dict(state=state,error=error,result=json.loads(payload) if payload else None)
        if raw:
            return data
        self.assertEqual(data['state'],'done',data)
        return data['result']

    def test_devices_and_work_orders_are_compact_by_default(self):
        self.setup_profile()
        page = self.admin.get('/admin/execution#devices', base_url=BASE).text
        self.assertIn('演练 Mac', page)
        self.assertIn('模拟影片', page)
        for detail_class in ('execution-device-details', 'execution-work-details'):
            match = re.search(r'<dialog class="execution-dialog ' + detail_class + r'"[^>]*>', page)
            self.assertIsNotNone(match)
            self.assertNotIn(' open', match.group(0))
        self.assertIn('查看设备详情', page)
        self.assertIn('查看工单详情', page)
        self.assertIn('设备环境', page)
        self.assertIn('工单操作', page)

    def test_administratively_closed_first_work_is_displayed_completed_without_a_receipt(self):
        self.setup_profile()
        with closing(sqlite3.connect(self.root / 'review.sqlite3')) as conn, conn:
            conn.execute("UPDATE execution_work SET state='stopped' WHERE job=1")
            conn.execute("""INSERT INTO execution_management_events
                SELECT w.job,e.actor,'close_without_delivery','人工结案',w.updated
                FROM execution_work w JOIN execution_jobs e ON e.job=w.job WHERE w.job=1""")
            self.assertIsNone(conn.execute('SELECT receipt FROM execution_work WHERE job=1').fetchone()[0])
        page = self.admin.get('/admin/execution', base_url=BASE).text
        self.assertIn('已完成 · 人工结案', page)
        self.assertIn('data-search="模拟影片 #1" data-status="succeeded"', page)
        self.register()
        self.heartbeat()
        self.claim('administratively-closed', 204)

    def test_admin_can_switch_review_mode_after_return_before_review(self):
        self.setup_profile()
        with closing(sqlite3.connect(self.root / 'review.sqlite3')) as conn:
            job, work, manifest_hash = conn.execute('SELECT job,id,manifest_hash FROM execution_work').fetchone()
        path = f'/admin/execution/jobs/{job}/review-mode'
        page = self.admin.get('/admin/execution', base_url=BASE).text
        self.assertIn(f'action="{path}"', page)
        self.assertIn(f'<dialog class="execution-dialog execution-work-details" id="{work}"', page)
        self.assertEqual(self.post(self.reviewer,path,review_mode=LOCAL_REVIEW).status_code,403)
        self.assertEqual(self.admin.post(path,data={'review_mode':LOCAL_REVIEW},base_url=BASE).status_code,400)
        self.assertEqual(self.post(self.admin,path,review_mode='invalid').status_code,422)

        for mode in (LOCAL_REVIEW, 'central', LOCAL_REVIEW):
            response = self.post(self.admin,path,review_mode=mode)
            self.assertEqual(response.status_code,303)
            self.assertEqual(response.headers['Location'],f'/admin/execution#{work}')
            with closing(sqlite3.connect(self.root / 'review.sqlite3')) as conn:
                self.assertEqual(conn.execute('SELECT local_review,review_instance FROM execution_jobs').fetchone(),
                                 (mode == LOCAL_REVIEW,None))
                self.assertEqual(conn.execute('SELECT manifest_hash,attempt FROM execution_work').fetchone(),
                                 (manifest_hash,0))

        self.register()
        self.heartbeat()
        self.claim('old-worker-cannot-claim',204)
        self.register(key='local-worker',local=True)
        self.heartbeat()
        claimed = self.claim('first-claim')
        self.assertEqual(claimed['input_manifest']['review_storage'],dict(mode=LOCAL_REVIEW,instance_id=self.instance))
        self.assertEqual(object_hash(claimed['input_manifest']),claimed['input_manifest_sha256'])
        self.assertEqual(self.post(self.admin,path,review_mode='central').status_code,409)
        page = self.admin.get('/admin/execution', base_url=BASE).text
        self.assertNotIn(f'action="{path}"',page)
        self.assertIn('同机审片 · 已锁定',page)
        self.return_work(claimed,'leased')
        page = self.admin.get('/admin/execution', base_url=BASE).text
        self.assertIn(f'action="{path}"',page)
        self.assertIn('已退回',page)
        self.assertEqual(self.post(self.admin,path,review_mode='central').status_code,303)
        self.claim('first-claim',204)
        with closing(sqlite3.connect(self.root / 'review.sqlite3')) as conn:
            self.assertEqual(conn.execute('SELECT local_review,review_node,review_instance FROM execution_jobs').fetchone(),
                             (0,None,None))
            raw, digest = conn.execute('SELECT manifest,manifest_hash FROM execution_work').fetchone()
            self.assertNotIn('review_storage',json.loads(raw))
            self.assertEqual(object_hash(json.loads(raw)),digest)
            self.assertEqual(conn.execute('SELECT returned_at IS NOT NULL FROM execution_attempts').fetchone(),(1,))
        self.register(key='legacy-again')
        self.heartbeat()
        returned = self.claim('claim-after-return')
        self.assertEqual(returned['lease']['attempt'],2)
        self.assertEqual(returned['input_manifest_sha256'],manifest_hash)
        self.return_work(returned,'leased')
        self.assertEqual(self.post(self.admin,path,review_mode=LOCAL_REVIEW).status_code,303)
        self.register(key='local-again',local=True)
        self.heartbeat()
        new_attempt = self.claim('local-after-return')
        self.assertEqual(new_attempt['lease']['attempt'],3)
        self.assertEqual(new_attempt['input_manifest']['review_storage'],dict(mode=LOCAL_REVIEW,instance_id=self.instance))
        self.produce_review(new_attempt,1)
        self.assertEqual(self.post(self.admin,path,review_mode='central').status_code,409)
        with closing(sqlite3.connect(self.root / 'review.sqlite3')) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM execution_management_events WHERE action='review_mode'").fetchone(),(5,))

    def test_batch_job_uploads_create_individual_jobs_and_skip_duplicates(self):
        self.setup_profile()
        page = self.admin.get('/admin/execution', base_url=BASE).text
        self.assertIn('data-batch-upload', page)
        self.assertIn('data-upload-dropzone', page)
        self.assertIn('multiple', page)

        def batch(files):
            with self.admin.session_transaction(base_url=BASE) as session:
                csrf = session['csrf']
            payload = MultiDict([('csrf', csrf)])
            for content, filename in files:
                payload.add('source', (io.BytesIO(content), filename))
            return self.admin.post('/admin/execution/jobs', data=payload, base_url=BASE)

        response = batch([(b'batch-one', '批量一.mp4'),
                          (b'batch-two', '批量二.mov'),
                          (b'batch-one', '批量一-重复.mp4')])
        self.assertEqual(response.status_code, 303, response.text)
        with closing(sqlite3.connect(self.root / 'review.sqlite3')) as conn:
            rows = conn.execute("""SELECT j.title FROM execution_jobs e JOIN jobs j ON j.id=e.job
                                   ORDER BY j.id""").fetchall()
        self.assertEqual([row[0] for row in rows], ['模拟影片', '批量一', '批量二'])

        response = batch([(b'batch-two', '再次上传.mov')])
        self.assertEqual(response.status_code, 303, response.text)
        with closing(sqlite3.connect(self.root / 'review.sqlite3')) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM execution_jobs').fetchone()[0], 3)

        response = batch([(b'invalid', '不是视频.txt')])
        self.assertEqual(response.status_code, 422)
        with closing(sqlite3.connect(self.root / 'review.sqlite3')) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM execution_jobs').fetchone()[0], 3)

    def register(self,token=None,node=None,instance=None,key='register',status=200,local=False,incremental=False):
        headers = {REVIEW_STORAGE_HEADER: LOCAL_REVIEW} if local else {}
        if incremental:
            headers[REVIEW_UPLOAD_HEADER] = REJECTED_FRAMES_MODE
        return self.machine('POST','/workers/register',dict(node_key=node or self.node,instance_id=instance or self.instance,
            capabilities=dict(slots=1,max_frame_width=32,work_kinds=['prepare','revise','finalize'],
                              profile_hashes=[self.profile['profile_hash']],render_backends=['cpu'],environment=self.environment)),
                             key=key,status=status,token=token,headers=headers)

    def heartbeat(self,free=10,reserve=9,token=None):
        return self.machine('POST',f'/workers/{self.node}/heartbeat',dict(instance_id=self.instance,available_slots=1,
                            active_work_ids=[],storage=dict(free_bytes=free,reserve_bytes=reserve)),token=token)

    def claim(self,key,status=200,token=None):
        return self.machine('POST','/work/claim',dict(worker_id=self.node,instance_id=self.instance,wait_seconds=0),
                            key=key,status=status,token=token).json

    def return_work(self, work, state, user=None, status=303):
        path = f'/admin/execution/jobs/{work["job_id"]}/control'
        response = self.post(user or self.admin, path, work=work['work_id'], state=state,
                             attempt=work['lease']['attempt'], action='return', reason='设备检查后退回')
        self.assertEqual(response.status_code, status)

    def upload(self,work,role,data,mimetype):
        lease = work['lease']
        begin = self.machine('POST',f'/work/{work["work_id"]}/uploads',dict(role=role,sha256=sha(data),size_bytes=len(data),media_type=mimetype),
                             status=201,key='begin-'+role,lease=lease).json
        path = '/uploads/'+begin['upload_id']
        self.assertEqual(self.machine('GET',path,lease=lease).json['state'],'open')
        for n,start in enumerate(range(0,len(data),PART_SIZE),1):
            part = data[start:start+PART_SIZE]
            headers = {'X-Part-SHA256':sha(part),'Content-Range':f'bytes {start}-{start+len(part)-1}/{len(data)}'}
            self.machine('PUT',path+f'/parts/{n}',part,lease=lease,headers=headers,status=204)
        seal = dict(sha256=sha(data),part_count=begin['part_count'])
        item = self.machine('POST',path+'/complete',seal,key='seal-'+role,lease=lease).json
        self.assertEqual(self.machine('POST',path+'/complete',seal,key='seal-'+role,lease=lease).json,item)
        return item

    def artifacts(self,work):
        return {item['role']:item for item in work['input_manifest']['artifacts']}

    def fetch(self,work,item):
        return self.machine('GET',f'/artifacts/{item["artifact_id"]}/content?work_id={work["work_id"]}',lease=work['lease'])

    def complete(self,work,outputs,**more):
        data = dict(input_manifest_sha256=work['input_manifest_sha256'],profile_hash=self.profile['profile_hash'],
                    environment=self.environment,outputs=outputs,**more)
        path = f'/work/{work["work_id"]}/complete'
        response = self.machine('POST',path,data,lease=work['lease'],key='complete').json
        self.assertEqual(self.machine('POST',path,data,lease=work['lease'],key='complete').json,response)
        return response

    def produce_review(self,work,version,native_size=None,extra_white=False,check_retry_lock=False,
                       unexpected_delta_frame=False,return_records=False):
        inputs = self.artifacts(work)
        frame_size = dict(width=32,height=18)
        timeline = to_json(dict(source_sha256=sha(self.source),time_base='1/1000',end_pts=80,
                                frames=[dict(number=1,pts=0,duration_pts=40),dict(number=2,pts=40,duration_pts=40)]))
        self.scene = b'BLENDER synthetic scene'
        scene = zip_bytes({'scene.blend':self.scene},dict(scene_path='scene.blend',scene_sha256=sha(self.scene)))
        self.meta = b'{"description":"test"}\n'
        report = to_json(dict(frame_count=2))
        colors = ['red','gray' if version == 1 else 'green']
        frames = [dict(number=n,pts=(n-1)*40,review_signature=sha(color.encode()),pair_jpeg_path=f'pairs/{n:04d}.jpg',
                       white_png_path=f'white/{n:04d}.png') for n,color in enumerate(colors,1)]
        files = {path:image(fmt,size,color) for frame,color in zip(frames,colors) for path,fmt,size in (
            (frame['pair_jpeg_path'],'JPEG',(32,36)),(frame['white_png_path'],'PNG',(32,18)))}
        common = dict(job_id=work['job_id'],source_sha256=sha(self.source),timeline_sha256=sha(timeline),
                      profile_hash=self.profile['profile_hash'],review_size=frame_size,final_size=native_size or frame_size,frames=frames,
                      scene_sha256=sha(self.scene))
        local = work['input_manifest'].get('review_storage')
        if local:
            common['review_storage'] = local
            if not extra_white:
                files = {name:data for name,data in files.items() if name.startswith('pairs/')}
        if work['input_manifest'].get('review_upload') == REJECTED_FRAMES_MODE:
            feedback = self.fetch(work, inputs['feedback_manifest']).json
            rejected = [frame['number'] for frame in feedback['frames']]
            full_files = [dict(path=name, size_bytes=len(files[name]), sha256=sha(files[name])) for name in sorted(files)]
            common['review_delta'] = dict(mode=REJECTED_FRAMES_MODE, base_version=feedback['base_version'],
                                          base_manifest_sha256=feedback['base_manifest_sha256'], frames=rejected,
                                          full_files_sha256=object_hash(full_files))
            if not unexpected_delta_frame:
                files = {name: data for name, data in files.items() if int(Path(name).stem) in rejected}
        review = zip_bytes(files,common|dict(target_version=version))
        manifest = to_json(common|dict(version=version,frame_count=2,scene_bundle_sha256=sha(scene),review_bundle_sha256=sha(review),
            shot_metadata_sha256=sha(self.meta),production_report_sha256=sha(report),input_manifest_sha256=work['input_manifest_sha256'],
            environment=self.environment,production_method='codex_trial'))
        records = [('scene_bundle',scene,'application/zip'),('shot_metadata',self.meta,'application/x-ndjson'),
                   ('review_bundle',review,'application/zip'),('version_manifest',manifest,'application/json'),
                   ('production_report',report,'application/json')]
        if local:
            records = [record for record in records if record[0] != 'scene_bundle']
        if version == 1:
            records.append(('source_timeline',timeline,'application/json'))
        else:
            self.assertEqual(inputs['source_timeline']['sha256'],sha(timeline))
        if return_records:
            return records
        outputs = [self.upload(work,*record) for record in records]
        if extra_white or unexpected_delta_frame:
            return self.machine('POST',f'/work/{work["work_id"]}/complete',
                                dict(input_manifest_sha256=work['input_manifest_sha256'],outputs=outputs),
                                status=422,key='reject-unexpected-frame',lease=work['lease'])
        if check_retry_lock:
            item = next(item for item in outputs if item['role'] == 'review_bundle')
            with (self.root/'assets'/'execution'/item['artifact_id']).open('rb') as sealed:
                fcntl.flock(sealed, fcntl.LOCK_EX)
                blocked = self.machine('POST',f'/work/{work["work_id"]}/complete',
                                       dict(input_manifest_sha256=work['input_manifest_sha256'],outputs=outputs),
                                       status=503,key='complete',lease=work['lease'])
                self.assertEqual(blocked.json['code'],'review_import_in_progress')
                self.assertTrue(blocked.json['retryable'])
        return self.complete(work,outputs)

    def test_local_review_rejects_whites_in_the_zip(self):
        self.setup_profile(review_mode=LOCAL_REVIEW)
        self.register(local=True)
        self.heartbeat()
        work = self.claim('local-prepare')
        result = self.produce_review(work,1,extra_white=True)
        self.assertIn('额外文件',result.json['message'])
        self.assertEqual(self.admin.get('/jobs/'+work['job_id'],base_url=BASE).status_code,404)

    def test_sealed_worker_review_is_independent_of_manual_upload_limit(self):
        self.setup_profile()
        self.register()
        self.heartbeat()
        work = self.claim('large-worker-review')
        with patch('app.MAX_PACKAGE', 64), patch('packages.MAX_PACKAGE', 64):
            receipt = self.produce_review(work,1,check_retry_lock=True)
            manual = self.post(self.admin, '/admin/import', title='手动送审',
                               package=(io.BytesIO(zip_bytes({'pairs/0001.jpg':image('JPEG',(32,36),'red')})), 'review.zip'))
        self.assertEqual((receipt['state'],receipt['version']),('succeeded',1))
        self.assertEqual(manual.status_code,422)
        self.assertIn('上传包超过',manual.text)

    def test_local_review_stays_with_original_instance_through_revision_and_supplement(self):
        self.run_local_review('1080p', revision=True)

    def test_local_review_delivers_4k(self):
        self.run_local_review('4k')

    def run_local_review(self, quality, revision=False):
        self.setup_profile(review_mode=LOCAL_REVIEW)
        self.register(key='pre-upgrade')
        self.heartbeat()
        self.claim('legacy-cannot-claim',204)
        self.register(key='pre-upgrade',local=True)
        self.heartbeat()
        work = self.claim('local-prepare')
        storage = dict(mode=LOCAL_REVIEW,instance_id=self.instance)
        self.assertEqual(work['input_manifest']['review_storage'],storage)
        self.assertEqual(object_hash(work['input_manifest']),work['input_manifest_sha256'])
        self.assertEqual(set(self.artifacts(work)),{'source_video'})
        self.machine('POST',f'/work/{work["work_id"]}/uploads',
                     dict(role='scene_bundle',sha256='a'*64,size_bytes=1,media_type='application/zip'),
                     status=422,lease=work['lease'])
        self.return_work(work,'leased')
        self.heartbeat()
        work = self.claim('local-prepare-returned')
        self.assertEqual(work['input_manifest']['review_storage'],storage)
        control = f'/admin/execution/jobs/{work["job_id"]}/control'
        attempt = work['lease']['attempt']
        for action, state in [('pause','leased'),('resume','paused')]:
            self.assertEqual(self.post(self.admin,control,work=work['work_id'],attempt=attempt,
                                       state=state,action=action,reason='测试同机恢复').status_code,303)
        self.heartbeat()
        work = self.claim('local-prepare-resumed')
        self.assertEqual(work['input_manifest']['review_storage'],storage)
        native = dict(width=3840,height=2160)
        receipt = self.produce_review(work,1,native)
        job = '/jobs/'+work['job_id']
        quality_path = '/admin/execution'+job+'/export-quality'
        self.assertIn('name="export_quality"',self.admin.get(job,base_url=BASE).text)
        self.assertEqual(self.post(self.reviewer,quality_path,export_quality=quality).status_code,403)
        self.assertEqual(self.post(self.admin,quality_path,export_quality=quality).status_code,303)
        self.assertEqual(self.post(self.reviewer,job+'/claim').status_code,303)
        self.assertEqual(self.post(self.reviewer,job+'/v/1/frames/1',action='approve',revision='0').status_code,303)

        other_node,other_token = self.issue('另一台同机审片设备')
        other_instance = str(uuid4())
        self.register(token=other_token,node=other_node,instance=other_instance,local=True)
        self.machine('POST',f'/workers/{other_node}/heartbeat',
                     dict(instance_id=other_instance,available_slots=1,active_work_ids=[]),token=other_token)

        def verify_other_cannot_claim(key, node=other_node, instance=other_instance, token=other_token):
            self.machine('POST','/work/claim',dict(worker_id=node,instance_id=instance,wait_seconds=0),
                         key=key,status=204,token=token)

        if revision:
            self.assertEqual(self.post(self.reviewer,job+'/v/1/frames/2',action='save',revision='1',note='调整').status_code,303)
            self.assertEqual(self.post(self.reviewer,job+'/v/1/return',revision='2').status_code,303)
            verify_other_cannot_claim('other-revise')
            self.register(key='downgraded')
            self.heartbeat()
            self.claim('downgraded-cannot-revise',204)
            replacement = str(uuid4())
            self.register(instance=replacement,key='replacement',local=True)
            self.machine('POST',f'/workers/{self.node}/heartbeat',
                         dict(instance_id=replacement,available_slots=1,active_work_ids=[]))
            verify_other_cannot_claim('replacement-cannot-revise',self.node,replacement,self.token)
            self.register(local=True,key='original-restarted')
            self.heartbeat()
            revise = self.claim('local-revise')
            inputs = self.artifacts(revise)
            self.assertEqual(set(inputs),{'source_video','source_timeline','version_manifest','shot_metadata',
                                          'approval_manifest','feedback_manifest'})
            original = next(item for item in receipt['artifacts'] if item['role'] == 'version_manifest')
            self.assertEqual(inputs['version_manifest'],original)
            self.assertEqual(sha(self.fetch(revise,original).data),original['sha256'])
            self.assertEqual(revise['input_manifest']['review_storage'],storage)
            receipt = self.produce_review(revise,2,native)
        target = 2 if revision else 1
        current = f'{job}/v/{target}'
        self.assertEqual(self.post(self.reviewer,current+'/frames/2',action='approve',revision='0' if revision else '1').status_code,303)
        self.assertEqual(self.post(self.admin,quality_path,export_quality='1080p').status_code,409)
        verify_other_cannot_claim('other-finalize')
        self.heartbeat()
        final = self.claim('local-finalize')
        self.assertEqual(final['input_manifest']['review_storage'],storage)
        self.assertEqual(final['input_manifest']['export_quality'],quality)
        self.assertEqual(object_hash(final['input_manifest']),final['input_manifest_sha256'])
        inputs = self.artifacts(final)
        self.assertEqual(set(inputs),{'source_video','source_timeline','version_manifest','shot_metadata','approval_manifest'})
        self.assertEqual(inputs['version_manifest'],next(item for item in receipt['artifacts'] if item['role']=='version_manifest'))
        self.assertEqual(self.fetch(final,inputs['approval_manifest']).json['scope'],'all_frames')
        hashes = {k+'_sha256':inputs[k]['sha256'] for k in ('version_manifest','approval_manifest')}
        hashes['timeline_sha256'] = inputs['source_timeline']['sha256']
        size = native if quality == '4k' else dict(width=1920,height=1080)
        white, comparison = b'synthetic-white',b'synthetic-comparison'
        delivery = zip_bytes({'target/test.mp4':self.source,'white_model/white.mp4':white,
                              'comparison/compare.mp4':comparison,'model_3d/scene.blend':self.scene,
                              'meta/scene.jsonl':self.meta},hashes | dict(job_id=work['job_id'],version=target,final_size=size))
        outputs = [self.upload(final,*row) for row in [('white_video',white,'video/mp4'),
                   ('comparison_video',comparison,'video/mp4'),('delivery_bundle',delivery,'application/zip')]]
        verification = hashes | dict(scene_sha256=sha(self.scene),frame_count=2,verified_review_frames=2,
            white_frame_count=2,comparison_frame_count=2,pts_verified=True,audio_verified=True,
            white_size=size,comparison_size=dict(width=size['width'],height=2*size['height']),
            render_source='locked_scene_native_render')
        self.machine('POST',f'/work/{final["work_id"]}/complete',
                     dict(input_manifest_sha256=final['input_manifest_sha256'],outputs=outputs,
                          final_verification=verification | dict(white_size=dict(width=800,height=400))),
                     status=422,key='wrong-size',lease=final['lease'])
        self.complete(final,outputs,final_verification=verification)
        if revision:
            self.assertEqual(self.post(self.reviewer,current+'/supplement',revision='2').status_code,303)
            supplement = f'{job}/v/{target+1}'
            self.assertEqual(self.post(self.reviewer,supplement+'/frames/2',action='save',revision='0',note='补审').status_code,303)
            self.assertEqual(self.post(self.reviewer,supplement+'/return',revision='1').status_code,303)
            verify_other_cannot_claim('other-supplement')
            self.heartbeat()
            again = self.claim('local-supplement')
            self.assertEqual(again['input_manifest']['review_storage'],storage)
            self.assertEqual(self.artifacts(again)['version_manifest'],inputs['version_manifest'])

    def test_prepare_revision_delivery_and_disk_gate(self):
        self.setup_profile()
        self.assertEqual(self.admin.get('/admin/execution',base_url=BASE).status_code,200)
        self.assertEqual(self.reviewer.get('/admin/execution',base_url=BASE).status_code,403)
        self.assertEqual(self.register().headers['X-Film-Worker-Storage'],'1')
        self.assertFalse(self.heartbeat(free=8).json['accept_claims'])
        self.claim('disk-low',204)
        self.assertTrue(self.heartbeat().json['accept_claims'])
        work = self.claim('prepare')
        self.assertEqual(self.claim('prepare'),work)
        self.claim('capacity',204)
        self.assertEqual(object_hash(work['input_manifest']),work['input_manifest_sha256'])
        blob = self.artifacts(work)['source_video']
        self.assertEqual(self.fetch(work,blob).data,self.source)
        url = f'/artifacts/{blob["artifact_id"]}/content?work_id={work["work_id"]}'
        ranged = self.machine('GET',url,lease=work['lease'],status=206,headers={'Range':'bytes=2-','If-Range':f'"{blob["sha256"]}"'})
        self.assertEqual(ranged.data,self.source[2:])
        self.produce_review(work,1)
        job = '/jobs/'+work['job_id']
        self.assertEqual(self.post(self.reviewer,job+'/claim').status_code,303)
        self.assertEqual(self.post(self.reviewer,job+'/v/1/frames/1',action='approve',revision='0').status_code,303)
        regions = json.dumps([dict(x=.1,y=.2,w=.3,h=.4,note='细节')])
        self.assertEqual(self.post(self.reviewer,job+'/v/1/frames/2',action='save',revision='1',note='调整',regions=regions).status_code,303)
        self.assertEqual(self.post(self.reviewer,job+'/v/1/return',revision='2').status_code,303)
        self.heartbeat()
        revise = self.claim('revise')
        self.assertEqual(revise['kind'],'revise')
        feedback = self.fetch(revise,self.artifacts(revise)['feedback_manifest']).json
        self.assertEqual(feedback['frames'][0]['note'],'调整')
        self.produce_review(revise,2)
        self.assertEqual(self.other.get(job,base_url=BASE).status_code,404)
        self.assertEqual(self.post(self.reviewer,job+'/v/2/frames/2',action='approve',revision='0').status_code,303)
        self.assertEqual(self.post(self.reviewer,job+'/v/2/supplement',revision='2').status_code,409)
        self.heartbeat()
        final = self.claim('final')
        self.assertEqual(final['kind'],'finalize')
        final_input = self.artifacts(final)
        approval = self.fetch(final,final_input['approval_manifest']).json
        self.assertEqual([frame['approved_version'] for frame in approval['frames']],[1,2])
        size = dict(width=32,height=18)
        hashes = {k+'_sha256':final_input[k]['sha256'] for k in ('version_manifest','approval_manifest','source_timeline')}
        hashes['timeline_sha256'] = hashes.pop('source_timeline_sha256')
        delivery = zip_bytes({'target/test.mp4':self.source,'white_model/white.mp4':b'synthetic-white',
                              'comparison/compare.mp4':b'synthetic-comparison','model_3d/scene.blend':self.scene,
                              'meta/scene.jsonl':self.meta}, hashes|dict(job_id=final['job_id'],version=2,final_size=size))
        outputs = [self.upload(final,*row) for row in [('white_video',b'synthetic-white','video/mp4'),
                   ('comparison_video',b'synthetic-comparison','video/mp4'),('delivery_bundle',delivery,'application/zip')]]
        self.complete(final,outputs,final_verification=hashes|dict(scene_sha256=sha(self.scene),frame_count=2,verified_review_frames=2,
            white_frame_count=2,comparison_frame_count=2,pts_verified=True,audio_verified=True,white_size=size,
            comparison_size=dict(width=32,height=36),render_source='approved_native_white'))
        status = self.machine('GET',job+'/status?work_id='+final['work_id']).json
        self.assertEqual((status['job_state'],status['delivery_versions']),('done',[2]))
        self.assertEqual(self.admin.get('/admin/execution'+job+'/delivery/2/delivery_bundle',base_url=BASE,buffered=True).data,delivery)
        delete = f'/admin/execution/work/{final["work_id"]}/delete'
        self.assertEqual(self.post(self.admin,delete,state='succeeded',attempt='1').status_code,303)
        self.assertNotIn(delete,self.admin.get('/admin/execution',base_url=BASE).text)
        detail = self.admin.get(job+'?v=2',base_url=BASE)
        download = '/admin/execution'+job+'/delivery/2/delivery_bundle'
        self.assertIn(download,detail.text)
        self.assertEqual(self.admin.get(download,base_url=BASE,buffered=True).data,delivery)
        status = self.machine('GET',job+'/status?work_id='+final['work_id']).json
        self.assertEqual((status['job_state'],status['work_state'],status['delivery_versions']),('done','succeeded',[2]))
        self.assertEqual(self.post(self.reviewer,job+'/v/2/supplement',revision='2').status_code,303)

    def check_delta_revision(self, local, unexpected=False):
        self.setup_profile(review_mode=LOCAL_REVIEW if local else 'central')
        self.register(local=local, incremental=True)
        self.heartbeat()
        first = self.claim('first')
        self.assertNotIn('review_upload', first['input_manifest'])
        initial = {item['role']: item for item in self.produce_review(first, 1)['artifacts']}
        job = '/jobs/' + first['job_id']
        self.assertEqual(self.post(self.reviewer, job + '/claim').status_code, 303)
        self.assertEqual(self.post(self.reviewer, job + '/v/1/frames/1', action='approve', revision='0').status_code, 303)
        regions = json.dumps([dict(x=.1, y=.2, w=.3, h=.4, note='detail')])
        self.assertEqual(self.post(self.reviewer, job + '/v/1/frames/2', action='save', revision='1',
                                   note='adjust', regions=regions).status_code, 303)
        self.assertEqual(self.post(self.reviewer, job + '/v/1/return', revision='2').status_code, 303)
        self.heartbeat()
        revise = self.claim('revise')
        self.assertEqual(revise['input_manifest']['review_upload'], REJECTED_FRAMES_MODE)
        self.assertEqual(revise['input_manifest_sha256'], object_hash(revise['input_manifest']))
        receipt = self.produce_review(revise, 2, unexpected_delta_frame=unexpected)
        if unexpected:
            with closing(sqlite3.connect(self.root / 'review.sqlite3')) as conn:
                self.assertEqual(conn.execute('SELECT count(*) FROM execution_versions').fetchone()[0], 1)
            return

        item = next(item for item in receipt['artifacts'] if item['role'] == 'review_bundle')
        assets = self.root / 'assets' / 'execution'
        paths = {'pairs/0002.jpg'} if local else {'pairs/0002.jpg', 'white/0002.png'}
        with ZipFile(assets / item['artifact_id']) as uploaded:
            self.assertEqual(set(uploaded.namelist()), paths | {'index.json'})
        self.assertEqual(self.post(self.reviewer, job + '/v/2/frames/2', action='approve', revision='0').status_code, 303)
        self.register(key='downgrade', local=local)
        self.heartbeat()
        self.claim('old-worker', 204)
        self.register(key='upgrade', local=local, incremental=True)
        self.heartbeat()
        final = self.claim('final')
        self.assertNotIn('review_upload', final['input_manifest'])
        final_inputs = self.artifacts(final)
        approval = self.fetch(final, final_inputs['approval_manifest']).json
        self.assertEqual([frame['approved_version'] for frame in approval['frames']], [1, 2])
        if local:
            self.assertNotIn('review_bundle', final_inputs)
        else:
            full = self.fetch(final, final_inputs['review_bundle']).data
            self.assertEqual(sha(full), final_inputs['review_bundle']['sha256'])
            with ZipFile(io.BytesIO(full)) as merged, ZipFile(assets / initial['review_bundle']['artifact_id']) as base:
                index = json.loads(merged.read('index.json'))
                self.assertNotIn('review_delta', index)
                self.assertEqual(set(merged.namelist()), set(base.namelist()))
                self.assertEqual(merged.read('pairs/0001.jpg'), base.read('pairs/0001.jpg'))
                self.assertNotEqual(merged.read('pairs/0002.jpg'), base.read('pairs/0002.jpg'))

    def test_central_delta_is_merged_for_review_and_followup(self):
        self.check_delta_revision(local=False)

    def test_local_delta_preserves_review_affinity(self):
        self.check_delta_revision(local=True)

    def test_delta_rejects_unmarked_frames(self):
        self.check_delta_revision(local=False, unexpected=True)

    def test_video_queue_exclusive_claim_and_manual_return(self):
        self.setup_profile()
        self.register()
        self.heartbeat()
        other_node, other_token = self.issue('另一台设备')
        other_instance = str(uuid4())
        other_environment = {**self.environment, 'os':'Windows 10.0.26200', 'arch':'x86_64',
                             'codex_model':'different-local-model', 'compatibility':{'legacy':'different'}}
        legacy_capabilities = dict(slots=1, max_frame_width=32, work_kinds=['prepare'],
                                   profile_hashes=['b' * 64], render_backends=['cpu'], environment=other_environment)
        self.machine('POST', '/workers/register', dict(node_key=other_node, instance_id=other_instance,
            capabilities=legacy_capabilities), key='register-other', token=other_token)
        self.machine('POST', f'/workers/{other_node}/heartbeat', dict(instance_id=other_instance,
                     available_slots=1, active_work_ids=[]), token=other_token)
        workers = [(self.node,self.instance,self.token), (other_node,other_instance,other_token)]
        barrier = Barrier(len(workers), timeout=5)

        def claim_as(worker, key):
            node, instance, token = worker
            return self.app.test_client().post('/api/v1/work/claim',base_url=BASE,
                json=dict(worker_id=node,instance_id=instance,wait_seconds=0,work_kinds=['prepare']),
                headers={'Authorization':'Bearer '+token,'Idempotency-Key':key})

        def concurrent_claim(worker):
            barrier.wait()
            return claim_as(worker,'concurrent')

        with ThreadPoolExecutor(max_workers=len(workers)) as pool:
            responses = list(pool.map(concurrent_claim,workers))
        self.assertEqual(sorted(response.status_code for response in responses),[200,204])
        winner = 0 if responses[0].status_code == 200 else 1
        first = responses[winner].json
        self.assertNotIn('profile', first['input_manifest'])
        self.assertEqual(claim_as(workers[winner],'concurrent').json, first)
        self.machine('POST', f'/work/{first["work_id"]}/fail',
                     dict(code='render_failed', message='模拟制作失败', retryable=True),
                     key='fail', lease=first['lease'], token=workers[winner][2])
        self.assertEqual(claim_as(workers[1-winner],'after-failure').status_code,204)
        self.return_work(first,'failed')
        second = claim_as(workers[1-winner],'after-return')
        self.assertEqual(second.status_code,200)
        self.assertEqual(second.json['work_id'],first['work_id'])
        self.assertEqual(second.json['lease']['attempt'],2)
        with closing(sqlite3.connect(self.root/'review.sqlite3')) as conn:
            self.assertEqual(conn.execute('SELECT profile FROM execution_jobs').fetchone()[0], None)
            history = conn.execute('SELECT node,claimed_at,returned_at,return_reason FROM execution_attempts ORDER BY attempt').fetchall()
            self.assertEqual([row[0] for row in history],[workers[winner][0],workers[1-winner][0]])
            self.assertIsNotNone(history[0][1])
            self.assertIsNotNone(history[0][2])
            self.assertEqual(history[0][3],'设备检查后退回')
        page = self.admin.get('/admin/execution',base_url=BASE).text
        self.assertIn('领取记录（2）',page)
        self.assertIn('设备检查后退回',page)
        self.assertNotIn('href="#profiles"',page)
        self.assertNotIn('设备能力与制作档案',page)

    def test_expired_assignment_stays_owned_until_returned(self):
        self.setup_profile()
        self.register()
        self.heartbeat()
        work = self.claim('lease')
        with closing(sqlite3.connect(self.root/'review.sqlite3')) as conn, conn:
            conn.execute('UPDATE execution_work SET expires=0 WHERE id=?', (work['work_id'],))
        self.heartbeat()
        self.claim('lease',204)
        self.claim('after-expiry',204)
        with closing(sqlite3.connect(self.root/'review.sqlite3')) as conn:
            self.assertEqual(conn.execute('SELECT state,node,failure FROM execution_work').fetchone(),
                             ('failed',self.node,'lease_expired'))
        self.return_work(work,'leased')
        second = self.claim('after-return')
        self.assertEqual(second['lease']['attempt'],2)
        self.return_work(second,'leased')
        self.machine('POST',f'/work/{work["work_id"]}/heartbeat',dict(stage='test',completed=0,total=None),
                     lease=second['lease'],status=409)

    def test_device_credential_replay_and_duplicate_name(self):
        route = '/admin/execution/credentials'
        register_request = self.credential_submission(self.admin, 'create')
        create = dict(action='create', name='Mac-Pro-Test', days='7', submission_id=register_request)
        response = self.post(self.admin, route, **create)
        self.assertEqual(response.status_code, 201)
        self.assertIn('已签发 1 枚密钥', response.text)
        self.assertEqual(len(re.findall(r'value="fw_[A-Za-z0-9_-]{43}"', response.text)), 1)
        self.assertIn('no-store', response.headers['Cache-Control'])
        node = re.search(r'value="(node_[a-f0-9]{32})"', response.text)[1]

        repeat = self.post(self.admin, route, **create)
        self.assertEqual(repeat.status_code, 303)
        self.assertTrue(repeat.headers['Location'].endswith('#credentials'))
        page = self.admin.get(repeat.headers['Location'], base_url=BASE)
        self.assertIn('这次提交已处理', page.text)
        self.assertNotRegex(page.text, r'value="fw_[A-Za-z0-9_-]{43}"')

        duplicate = self.post(self.admin, route, action='create', name='  mac-pro-test ', days='7')
        self.assertEqual(duplicate.status_code, 303)
        self.assertTrue(duplicate.headers['Location'].endswith('#devices'))
        page = self.admin.get(duplicate.headers['Location'], base_url=BASE)
        self.assertIn('已登记。如需更换密钥', page.text)

        issue = dict(action='issue', node=node, days='7',
                     submission_id=self.credential_submission(self.admin, 'issue'))
        issued = self.post(self.admin, route, **issue)
        self.assertEqual(issued.status_code, 201)
        self.assertEqual(self.post(self.admin, route, **issue).status_code, 303)
        rotated = self.post(self.admin, route, action='issue', node=node, days='7')
        self.assertEqual(rotated.status_code, 201)
        self.assertNotEqual(re.search(r'value="(fw_[A-Za-z0-9_-]{43})"', issued.text)[1],
                            re.search(r'value="(fw_[A-Za-z0-9_-]{43})"', rotated.text)[1])
        with closing(sqlite3.connect(self.root / 'review.sqlite3')) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM execution_nodes').fetchone()[0], 2)
            self.assertEqual(conn.execute('SELECT count(*) FROM execution_tokens WHERE node=?', (node,)).fetchone()[0], 3)

    def test_legacy_profile_routes_are_removed_and_register_fields_are_ignored(self):
        self.setup_profile()
        response = self.register(key='legacy-fields')
        self.assertEqual(response.headers['X-Film-Worker-Storage'],'1')
        with closing(sqlite3.connect(self.root / 'review.sqlite3')) as conn:
            capabilities = json.loads(conn.execute('SELECT capabilities FROM execution_nodes WHERE id=?', (self.node,)).fetchone()[0])
            self.assertNotIn('profile_hashes', capabilities)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM execution_profiles').fetchone()[0], 0)
        self.assertEqual(self.admin.get('/admin/execution/profiles/' + 'a' * 64, base_url=BASE).status_code, 404)

    def test_admin_can_pin_current_execution_instance(self):
        self.setup_profile()
        self.register()
        route = f'/admin/execution/devices/{self.node}/binding'
        self.assertEqual(self.post(self.reviewer, route, action='bind', instance=self.instance).status_code, 403)
        self.assertEqual(self.post(self.admin, route, action='bind', instance=str(uuid4())).status_code, 409)
        response = self.post(self.admin, route, action='bind', instance=self.instance)
        self.assertEqual(response.status_code, 303)
        self.assertIn('已绑定', self.admin.get('/admin/execution', base_url=BASE).text)
        response = self.register(instance=str(uuid4()), key='replacement', status=403)
        self.assertEqual(response.json['code'], 'instance_bound')
        self.register()
        self.heartbeat()
        self.assertEqual(self.post(self.admin, route, action='unbind', instance=self.instance).status_code, 303)
        self.register(instance=str(uuid4()), key='replacement-3')

    def test_device_and_work_removal_respect_active_leases(self):
        self.setup_profile()
        self.register()
        self.heartbeat()
        work = self.claim('active-work')
        device_path = f'/admin/execution/devices/{self.node}'
        work_path = f'/admin/execution/work/{work["work_id"]}/delete'
        self.assertEqual(self.post(self.reviewer,device_path+'/delete').status_code,403)
        self.assertEqual(self.admin.post(device_path+'/delete',base_url=BASE).status_code,400)
        self.assertEqual(self.post(self.admin,device_path+'/delete').status_code,409)
        self.assertEqual(self.post(self.admin,work_path,state='leased',attempt='1').status_code,409)
        control = f'/admin/execution/jobs/{work["job_id"]}/control'
        self.assertEqual(self.post(self.admin,control,work=work['work_id'],state='leased',attempt='1',action='pause',reason='联调').status_code,303)
        self.assertEqual(self.post(self.admin,work_path,state='paused',attempt='0').status_code,409)
        self.assertEqual(self.post(self.admin,work_path,state='paused',attempt='1').status_code,303)
        self.assertEqual(self.post(self.admin,work_path,state='paused',attempt='1').status_code,404)
        status = self.machine('GET',f'/jobs/{work["job_id"]}/status?work_id={work["work_id"]}').json
        self.assertEqual((status['work_state'],status['directive'],status['terminal']),('deleted','delete',True))
        response = self.machine('POST',f'/work/{work["work_id"]}/heartbeat',
                                dict(stage='test',completed=0,total=None),lease=work['lease'])
        self.assertEqual(response.json['directive'],'delete')
        self.assertEqual(self.post(self.admin,control,work=work['work_id'],state='paused',attempt='1',action='resume',reason='联调').status_code,409)
        self.claim('after-removal',204)
        submission_id = self.credential_submission(self.admin, 'issue')
        self.assertEqual(self.post(self.admin,device_path+'/delete').status_code,303)
        self.register(key='reconnect-after-removal',status=401)
        self.assertEqual(self.post(self.admin,'/admin/execution/credentials',action='issue',node=self.node,days='7',
                                   submission_id=submission_id).status_code,404)
        self.assertEqual(self.post(self.admin,device_path+'/control',action='resume').status_code,404)
        page = self.admin.get('/admin/execution',base_url=BASE).text
        self.assertNotIn('data-search="演练 Mac ',page)
        self.assertNotIn('模拟影片',page)
        self.assertIn('演练 Mac（已删除）',page)
        self.assertIn('已撤销',page)

    def test_pause_resume_and_remove_device_key(self):
        self.setup_profile()
        self.register()
        self.heartbeat()
        first = self.claim('before-key-pause')
        route = '/admin/execution/credentials'
        self.assertEqual(self.post(self.reviewer,route,action='pause',token_id='1').status_code,403)
        self.assertEqual(self.post(self.admin,route,action='delete',token_id='1').status_code,409)
        second_key = self.post(self.admin,route,action='issue',node=self.node,days='7')
        self.assertEqual(second_key.status_code,201)
        alternate = re.search(r'value="(fw_[A-Za-z0-9_-]{43})"',second_key.text)[1]
        self.assertEqual(self.post(self.admin,route,action='pause',token_id='1').status_code,303)
        payload = dict(stage='test',completed=0,total=None)
        work = f'/work/{first["work_id"]}/heartbeat'
        self.machine('POST',work,payload,lease=first['lease'],status=401)
        self.machine('POST',work,payload,lease=first['lease'],token=alternate,status=409)
        self.assertIn('data-status="已暂停"',self.admin.get('/admin/execution',base_url=BASE).text)
        self.assertEqual(self.post(self.admin,route,action='resume',token_id='1').status_code,303)
        self.machine('POST',work,payload,lease=first['lease'],status=409)
        self.heartbeat()
        self.claim('after-key-resume',204)
        self.return_work(first, 'failed')
        second = self.claim('after-return')
        self.assertEqual(second['work_id'],first['work_id'])
        self.assertGreater(second['lease']['fence'],first['lease']['fence'])
        self.assertEqual(self.post(self.admin,route,action='pause',token_id='1').status_code,303)
        self.assertEqual(self.post(self.admin,route,action='delete',token_id='1').status_code,303)
        self.assertEqual(self.post(self.admin,route,action='resume',token_id='1').status_code,404)
        self.assertNotIn(self.token[:12],self.admin.get('/admin/execution',base_url=BASE).text)
        self.machine('POST',work,payload,lease=second['lease'],status=401)
        with closing(sqlite3.connect(self.root/'review.sqlite3')) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM execution_attempts').fetchone()[0],2)
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(),[])

    def test_remove_legacy_revoked_key_without_restoring_device(self):
        device = f'/admin/execution/devices/{self.node}/delete'
        route = '/admin/execution/credentials'
        self.assertEqual(self.post(self.admin,device).status_code,303)
        page = self.admin.get('/admin/execution',base_url=BASE).text
        self.assertIn('演练 Mac（已删除）',page)
        self.assertIn('删除记录',page)
        self.assertEqual(self.post(self.admin,route,action='resume',token_id='1').status_code,409)
        self.assertEqual(self.post(self.admin,route,action='delete',token_id='1').status_code,303)
        page = self.admin.get('/admin/execution',base_url=BASE).text
        self.assertNotIn('演练 Mac',page)
        with closing(sqlite3.connect(self.root/'review.sqlite3')) as conn:
            self.assertIsNotNone(conn.execute('SELECT deleted_at FROM execution_nodes').fetchone()[0])
            self.assertIsNotNone(conn.execute('SELECT revoked FROM execution_tokens').fetchone()[0])

    def test_old_completion_fields_do_not_trigger_profile_validation(self):
        self.setup_profile()
        self.register()
        self.heartbeat()
        work = self.claim('legacy-completion-fields')
        self.assertNotIn('profile', work['input_manifest'])
        self.assertNotIn('profile_hash', work['input_manifest'])
        self.assertEqual(self.admin.get('/admin/execution/profiles',base_url=BASE).status_code,404)

    def test_http_key_management_links_return_to_https_panel(self):
        http = 'http://localhost'
        login = '/login?execution_section=credentials'
        page = self.admin.get('/admin/execution',base_url=http).text
        self.assertIn('href="https://localhost'+login+'"',page)
        self.assertNotIn('name="action" value="issue"',page)
        fresh = self.app.test_client()
        fresh.get(login,base_url=BASE)
        response = self.post(fresh,login,username='admin',password=PASSWORD)
        self.assertEqual(response.headers['Location'],'/admin/execution#credentials')
        fresh.get(response.headers['Location'],base_url=BASE)
        issued = self.post(fresh,'/admin/execution/credentials',action='issue',node=self.node,days='7')
        self.assertEqual(issued.status_code,201)
        self.assertRegex(issued.text,r'value="fw_[A-Za-z0-9_-]{43}"')
        self.assertEqual(self.post(fresh,'/login?execution_section=https://example.invalid',username='admin',
                                   password=PASSWORD).headers['Location'],'/')

    def test_existing_execution_rows_migrate_in_place(self):
        self.setup_profile()
        self.register()
        self.heartbeat()
        work = self.claim('legacy-claim')
        with closing(sqlite3.connect(self.root/'review.sqlite3')) as conn, conn:
            legacy_key = 'legacy-' + 'a' * 64
            conn.execute('INSERT INTO execution_artifacts VALUES(?,?,?,?,?,?)',
                         ('legacy-rules', 'rules_bundle', 'a' * 64, 1, 'application/zip', 'rules.zip'))
            conn.execute('INSERT INTO execution_artifacts VALUES(?,?,?,?,?,?)',
                         ('legacy-workflow', 'workflow_bundle', 'b' * 64, 1, 'application/zip', 'workflow.zip'))
            conn.execute('INSERT INTO execution_profiles(hash,name,profile,rules,workflow) VALUES(?,?,?,?,?)',
                         (legacy_key, 'legacy', '{}', 'legacy-rules', 'legacy-workflow'))
            conn.executescript('''CREATE TABLE execution_jobs_legacy (
                job INTEGER PRIMARY KEY REFERENCES jobs(id), actor INTEGER NOT NULL REFERENCES users(id),
                profile TEXT NOT NULL REFERENCES execution_profiles(hash), source TEXT NOT NULL REFERENCES execution_artifacts(id));''')
            conn.execute('INSERT INTO execution_jobs_legacy SELECT job,actor,?,source FROM execution_jobs',
                         (legacy_key,))
            conn.execute('DROP TABLE execution_jobs')
            conn.execute('ALTER TABLE execution_jobs_legacy RENAME TO execution_jobs')
            conn.execute("UPDATE execution_work SET state='queued',lease=NULL,expires=NULL,failure='lease_expired'")
            manifest = json.loads(conn.execute('SELECT manifest FROM execution_work').fetchone()[0])
            manifest['profile'] = None
            conn.execute('UPDATE execution_work SET manifest=?', (json.dumps(manifest),))
            for table in ('execution_nodes','execution_profiles','execution_work','execution_tokens'):
                conn.execute(f'ALTER TABLE {table} DROP COLUMN deleted_at')
            conn.execute('ALTER TABLE execution_tokens DROP COLUMN paused_at')
            for field in ('claimed_at','returned_at','returned_by','return_reason'):
                conn.execute(f'ALTER TABLE execution_attempts DROP COLUMN {field}')
        for _ in range(2):
            result = self.app.test_cli_runner().invoke(args=['init-db'])
            self.assertEqual(result.exit_code,0,result.output)
        page = self.admin.get('/admin/execution',base_url=BASE).text
        for text in ('演练 Mac','模拟影片','设备续租超时'):
            self.assertIn(text,page)
        with closing(sqlite3.connect(self.root/'review.sqlite3')) as conn:
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(),[])
            self.assertEqual(conn.execute('SELECT state,node FROM execution_work').fetchone(),('failed',self.node))
            self.assertEqual(conn.execute('SELECT profile FROM execution_jobs').fetchone()[0],None)
            cleaned = json.loads(conn.execute('SELECT manifest FROM execution_work').fetchone()[0])
            self.assertNotIn('profile', cleaned)
            self.assertIsNotNone(conn.execute('SELECT claimed_at FROM execution_attempts').fetchone()[0])
        self.return_work(work,'failed')
        result = self.app.test_cli_runner().invoke(args=['init-db'])
        self.assertEqual(result.exit_code,0,result.output)
        self.heartbeat()
        self.assertEqual(self.claim('legacy-return')['lease']['attempt'],2)

    def test_pause_and_revoke_fence_old_attempts(self):
        self.setup_profile()
        self.register()
        self.heartbeat()
        first = self.claim('first')
        work = first['work_id']
        path = f'/admin/execution/jobs/{first["job_id"]}/control'
        self.assertEqual(self.post(self.admin,path,work=work,state='leased',attempt='1',action='pause',reason='演练').status_code,303)
        status = self.machine('POST',f'/work/{work}/heartbeat',dict(stage='test',completed=0,total=None),lease=first['lease']).json
        self.assertEqual(status['directive'],'pause')
        self.assertEqual(self.post(self.admin,path,work=work,state='paused',attempt='1',action='resume',reason='演练').status_code,303)
        self.heartbeat()
        self.claim('after-resume',204)
        self.return_work(first, 'failed')
        second = self.claim('second')
        self.assertEqual(second['lease']['fence'],2)
        self.machine('POST',f'/work/{work}/heartbeat',dict(stage='test',completed=0,total=None),lease=first['lease'],status=409)
        self.assertEqual(self.post(self.admin,'/admin/execution/credentials',action='revoke',token_id='1').status_code,303)
        self.machine('POST',f'/work/{work}/heartbeat',dict(stage='test',completed=0,total=None),lease=second['lease'],status=401)
        self.machine('POST','/workers/register',{},status=403,base='http://localhost')


if __name__ == '__main__':
    unittest.main()
