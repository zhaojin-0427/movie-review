import hashlib
import io
import json
import re
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from zipfile import ZipFile

from PIL import Image

from app import create_app


SOURCE = "a" * 64
PASSWORD = "temporary-test-password"


def package(timing=None, changed=(), source=SOURCE):
    data = io.BytesIO()
    with ZipFile(data, "w") as archive:
        manifest = {"source_sha256": source} if source else {}
        if timing is not None:
            manifest.update(timing)
        archive.writestr("manifest.json", json.dumps(manifest))
        for number in range(1, 5):
            image = Image.new("RGB", (32, 36), "tan" if number in changed else "gray")
            payload = io.BytesIO()
            image.save(payload, "PNG")
            archive.writestr(f"pairs/{number:04}.png", payload.getvalue())
    return data.getvalue()


TIMING = {"time_base": "1/30000", "pts": [0, 1001, 2302, 3303], "end_pts": 4304}


class ReviewTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.data = Path(self.folder.name)
        self.app = create_app({"TESTING": True, "DATA_DIR": self.data, "SECRET_KEY": "test-only-key",
                               "LOGIN_MAX_PER_ACCOUNT": 3})
        credentials = self.data / "accounts.json"
        credentials.write_text(json.dumps([
            {"username": name, "name": name, "password": PASSWORD, "role": role}
            for name, role in (("admin", "admin"), ("reviewer-a", "reviewer"), ("reviewer-b", "reviewer"))
        ]))
        setup = self.app.test_cli_runner().invoke(args=["provision", str(credentials)])
        self.assertEqual(setup.exit_code, 0, setup.output)
        self.admin, self.user, self.other = (self.login(name) for name in ("admin", "reviewer-a", "reviewer-b"))

    def login(self, name, password=PASSWORD, client=None):
        client = client or self.app.test_client()
        self.assertEqual(self.login_as(client, name, password).status_code, 303)
        return client

    def login_as(self, client, name, password):
        client.get("/login")
        response = self.post(client, "/login", username=name, password=password)
        if response.status_code == 303:
            client.get("/")
        return response

    def post(self, client, route, **fields):
        with client.session_transaction() as session:
            fields["csrf"] = session["csrf"]
        return client.post(route, data=fields)

    def upload(self, payload, **fields):
        defaults = {"title": "测试短片", "job_id": "", "package": (io.BytesIO(payload), "sample.zip")}
        return self.post(self.admin, "/admin/import", **(defaults | fields))

    def begin(self, payload):
        uploaded = self.upload(payload)
        self.assertEqual(uploaded.headers.get("Location"), "/jobs/1?v=1")
        self.assertEqual(self.post(self.user, "/jobs/1/claim").status_code, 303)

    def edit(self, number, action, revision, **values):
        return self.post(self.user, f"/jobs/1/v/1/frames/{number}", action=action, revision=str(revision), **values)

    def states(self, version):
        with closing(sqlite3.connect(self.data / "review.sqlite3")) as db:
            return db.execute("""SELECT f.state, f.approved_from FROM frames f JOIN versions v ON v.id=f.version_id
                                 WHERE v.job_id=1 AND v.number=? ORDER BY f.number""", (version,)).fetchall()

    def control(self, action, reason="联调验证"):
        detail = self.admin.get("/jobs/1").text
        revision = re.search(r'name="revision" value="([^\"]+)"', detail)[1]
        return self.post(self.admin, "/admin/jobs/1/control", action=action, reason=reason, revision=revision)

    def test_upload_replay_and_intentional_duplicate(self):
        payload = package(TIMING)
        first = self.upload(payload)
        repeat = self.upload(payload)
        self.assertEqual(first.headers['Location'], repeat.headers['Location'])
        self.assertIn('V1 已存在', self.admin.get(repeat.location).text)
        another = self.upload(payload, allow_duplicate="1")
        self.assertNotEqual(first.location, another.location)

    def test_client_upload_authentication_and_replay(self):
        token = "test-upload-only-token"
        self.app.config["UPLOAD_API_TOKEN_SHA256"] = hashlib.sha256(token.encode()).hexdigest()
        client = self.app.test_client()
        route = "/api/v1/review-packages"
        original = package(TIMING)

        def send(payload=original, auth=token, secure=True, **fields):
            return client.post(route, base_url="https://localhost" if secure else "http://localhost",
                               headers={"Authorization": f"Bearer {auth}"},
                               data={"title": "客户端测试", "package": (io.BytesIO(payload), "sample.zip")} | fields)

        self.assertEqual(send(secure=False).json["error"]["code"], "https_required")
        unauthenticated = self.admin.post(route, base_url="https://localhost")
        self.assertEqual(unauthenticated.status_code, 401)
        self.assertEqual(send(auth="wrong").status_code, 401)
        self.assertEqual(send(payload=b"invalid zip").status_code, 422)
        self.assertEqual(send(job_id="1").status_code, 422)
        first = send()
        self.assertEqual(first.status_code, 201)
        self.assertEqual(first.json, {"job_id": 1, "version": 1, "review_path": "/jobs/1?v=1", "reused": False})
        replay = send()
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json, first.json | {"reused": True})
        self.assertEqual(send(job_id="1", base_version="1").status_code, 409)

        self.assertEqual(self.post(self.user, "/jobs/1/claim").status_code, 303)
        self.assertEqual(self.edit(1, "save", 0, note="返修").status_code, 303)
        self.assertEqual(self.post(self.user, "/jobs/1/v/1/return", revision="1").status_code, 303)
        revision = package(TIMING, changed=(1,))
        revised = send(revision, job_id="1", base_version="1")
        self.assertEqual(revised.status_code, 201)
        self.assertEqual(revised.json["version"], 2)
        self.assertEqual(send(revision, job_id="1", base_version="1").status_code, 200)
        self.assertEqual(send(original, job_id="1", base_version="1").status_code, 409)
        self.assertIn("客户端上传", self.admin.get("/jobs/1?v=2").text)
        self.assertEqual(client.get("/jobs/1?v=2", headers={"Authorization": f"Bearer {token}"}).status_code, 303)

    def test_inheritance_requires_source_and_exact_time(self):
        self.begin(package(TIMING))
        self.assertEqual(self.edit(1, "approve", 0).status_code, 303)
        self.assertEqual(self.edit(2, "approve", 1).status_code, 303)
        self.assertEqual(self.edit(3, "save", 2, note="眼神偏移").status_code, 303)
        self.assertEqual(self.post(self.user, "/jobs/1/v/1/return", revision="3").status_code, 303)
        revised = package({**TIMING, "pts": [0, 1001, 2303, 3303]})
        self.assertEqual(self.upload(package(TIMING, source="b" * 64), job_id="1:1").status_code, 422)
        self.assertEqual(self.upload(revised, job_id="1:1").status_code, 303)
        self.assertEqual(self.upload(revised, job_id="1:1").status_code, 303)
        self.assertEqual(self.upload(package(TIMING, (4,)), job_id="1:1").status_code, 409)
        self.assertEqual(self.states(2), [('approved', 1), ('pending', None), ('pending', None), ('pending', None)])
        self.assertEqual(self.other.get('/jobs/1/v/2/image/1').status_code, 404)

    def test_missing_time_or_source_does_not_inherit(self):
        original = package()
        self.begin(original)
        self.assertEqual(self.edit(1, "approve", 0).status_code, 303)
        self.assertEqual(self.edit(2, "save", 1, note="返修").status_code, 303)
        self.assertEqual(self.post(self.user, "/jobs/1/v/1/return", revision="2").status_code, 303)
        self.assertEqual(self.upload(original, job_id="1:1").status_code, 303)
        self.assertTrue(all(row == ('pending', None) for row in self.states(2)))

    def test_pause_revoke_sessions_and_withdraw(self):
        self.begin(package(TIMING))
        self.assertEqual(self.control('pause').status_code, 303)
        self.assertEqual(self.edit(1, 'approve', 1).status_code, 409)
        self.assertEqual(self.control('resume').status_code, 303)
        self.assertEqual(self.edit(1, 'approve', 0).status_code, 409)
        self.assertEqual(self.post(self.admin, '/admin/accounts', action='disable', user_id='2').status_code, 303)
        self.assertEqual(self.user.get('/session/identity').status_code, 401)
        self.assertIn('等待原标注员', self.admin.get('/jobs/1').text)
        self.assertEqual(self.other.get('/jobs/1').status_code, 404)
        self.assertEqual(self.post(self.admin, '/admin/accounts', action='enable', user_id='2').status_code, 303)
        self.user = self.login('reviewer-a')
        self.assertEqual(self.edit(1, 'approve', 2).status_code, 303)
        self.assertEqual(self.control('withdraw').status_code, 303)
        self.assertEqual(self.user.get('/jobs/1/v/1/image/1').status_code, 404)
        response = self.admin.get('/jobs/1/v/1/image/1')
        self.addCleanup(response.close)
        self.assertEqual(response.status_code, 200)

    def test_feedback_zip_and_missing_image(self):
        self.begin(package(TIMING))
        original = next((self.data / 'assets').glob('*/000001.png'))
        original.unlink()
        self.assertEqual(self.edit(1, 'approve', 0).status_code, 409)
        regions = json.dumps([{"x": .15, "y": .57, "w": .18, "h": .23, "note": "调整"}])
        self.assertEqual(self.edit(2, 'save', 0, note='参考上图', regions=regions).status_code, 303)
        self.assertEqual(self.post(self.user, '/jobs/1/v/1/return', revision='1').status_code, 303)
        response = self.user.get('/jobs/1/v/1/feedback.zip')
        self.addCleanup(response.close)
        self.assertEqual(response.status_code, 200)
        with ZipFile(io.BytesIO(response.data)) as archive:
            self.assertEqual(json.loads(archive.read('feedback.json'))['frames'][0]['preview'], 'marked/000002.jpg')
            self.assertEqual(archive.read('marked/000002.jpg')[:2], b'\xff\xd8')
        self.assertEqual(self.other.get('/jobs/1/v/1/feedback.zip').status_code, 404)

    def test_supplement_is_separate_and_owner_only(self):
        self.begin(package(TIMING))
        for number in range(1, 5):
            self.assertEqual(self.edit(number, 'approve', number-1).status_code, 303)
        path = '/jobs/1/v/1/supplement'
        self.assertEqual(self.post(self.other, path, revision='5').status_code, 404)
        self.assertEqual(self.post(self.admin, path, revision='5').status_code, 403)
        self.assertEqual(self.post(self.user, path, revision='5').location, '/jobs/1?v=2')
        self.assertEqual(self.post(self.user, path, revision='5').location, '/jobs/1?v=2')
        self.assertEqual(self.post(self.user, '/jobs/1/v/2/frames/1', action='save', revision='0', note='需要补改').status_code, 303)
        self.assertEqual(self.post(self.user, '/jobs/1/v/2/close-supplement', revision='1').status_code, 409)
        self.assertEqual(self.post(self.user, '/jobs/1/v/2/return', revision='1').status_code, 303)
        self.assertEqual(self.states(1), [('approved', 1)] * 4)
        self.assertEqual(self.states(2), [('changes', None)] + [('approved', 1)] * 3)
        self.assertEqual(self.upload(package(TIMING, changed=(1,)), job_id='1:2').location, '/jobs/1?v=3')
        self.assertEqual(self.states(3), [('pending', None)] + [('approved', 1)] * 3)

    def test_admin_can_filter_queue_by_owner_and_unassigned(self):
        self.begin(package(TIMING))
        self.assertEqual(self.upload(package(TIMING), title="第二个作业").status_code, 303)
        self.assertEqual(self.post(self.other, "/jobs/2/claim").status_code, 303)
        self.assertEqual(self.upload(package(TIMING), title="第三个作业").status_code, 303)

        reviewer_page = self.admin.get("/?owner=2")
        self.assertEqual(reviewer_page.status_code, 200)
        self.assertIn("测试短片", reviewer_page.text)
        self.assertNotIn("第二个作业", reviewer_page.text)
        self.assertNotIn("第三个作业", reviewer_page.text)
        self.assertIn('option value="2" selected', reviewer_page.text)

        unassigned_page = self.admin.get("/?owner=unassigned")
        self.assertEqual(unassigned_page.status_code, 200)
        self.assertNotIn("测试短片", unassigned_page.text)
        self.assertNotIn("第二个作业", unassigned_page.text)
        self.assertIn("第三个作业", unassigned_page.text)
        self.assertIn('option value="unassigned" selected', unassigned_page.text)
        combined = self.admin.get("/", query_string={"owner": "2", "state": "review", "q": "#0001"})
        self.assertIn("测试短片", combined.text)
        self.assertIn("state=review&amp;q=%230001&amp;owner=2", combined.text)
        mine = self.user.get("/?owner=2").text
        self.assertIn("测试短片", mine)
        self.assertNotIn("第三个作业", mine)
        visible = self.user.get("/?owner=3").text
        self.assertIn("第三个作业", visible)
        self.assertNotIn("第二个作业", visible)
        self.assertNotIn("reviewer-b", visible)

    def test_admin_can_delete_accounts_and_preserve_audit(self):
        route = "/admin/accounts"
        self.begin(package(TIMING))
        self.assertEqual(self.edit(1, "approve", 0).status_code, 303)
        self.assertEqual(self.user.post(route, data={"action": "delete", "user_id": 2}).status_code, 400)
        self.assertEqual(self.post(self.user, route, action="delete", user_id=2).status_code, 403)
        self.assertEqual(self.post(self.admin, route, action="delete", user_id=1).status_code, 422)
        self.assertEqual(self.post(self.admin, route, action="delete", user_id=2).status_code, 303)
        page = self.admin.get(route).text
        self.assertNotIn('<span class="mono">reviewer-a</span>', page)
        self.assertIn("删除 · reviewer-a", page)
        self.assertEqual(self.user.get("/session/identity").status_code, 401)
        self.assertEqual(self.login_as(self.app.test_client(), "reviewer-a", PASSWORD).status_code, 401)
        self.assertEqual(self.post(self.admin, route, action="enable", user_id=2).status_code, 409)
        self.assertEqual(self.states(1)[0][0], "approved")
        self.assertIn("原标注员账号已删除", self.admin.get("/jobs/1").text)
        self.assertEqual(self.post(self.other, "/jobs/1/claim").status_code, 409)
        self.assertEqual(self.post(self.admin, route, action="create", name="备用管理员", username="backup-admin",
                                   role="admin", password=PASSWORD).status_code, 303)
        backup = self.login("backup-admin")
        self.app.config["UPLOAD_API_TOKEN_SHA256"] = "a" * 64
        self.assertEqual(self.post(backup, route, action="delete", user_id=1).status_code, 409)
        self.assertEqual(self.post(self.admin, route, action="delete", user_id=4).status_code, 303)
        self.assertEqual(backup.get("/session/identity").status_code, 401)

    def test_create_admin_account_requires_admin_session_and_csrf(self):
        path = "/admin/accounts"
        fields = {"action": "create", "name": "新管理员", "username": "new-admin",
                  "password": PASSWORD, "role": "admin"}
        self.assertEqual(self.post(self.user, path, **fields).status_code, 403)
        self.assertEqual(self.admin.post(path, data=fields).status_code, 400)
        self.assertEqual(self.post(self.admin, path, **(fields | {"role": "unknown"})).status_code, 422)
        self.assertEqual(self.post(self.admin, path, **fields).status_code, 303)
        duplicate = self.post(self.admin, path, **(fields | {"username": "NEW-ADMIN"}))
        self.assertEqual(duplicate.status_code, 409)

        new_admin = self.login("new-admin")
        accounts = new_admin.get(path)
        self.assertEqual(accounts.status_code, 200)
        self.assertRegex(accounts.text, r'新管理员[\s\S]*?new-admin[\s\S]*?badge admin">管理员[\s\S]*?badge enabled">启用')
        self.assertEqual(new_admin.get("/admin/import").status_code, 200)
        self.assertEqual(self.post(new_admin, path, action="disable", user_id="1").status_code, 403)

        default_fields = {"action": "create", "name": "新标注员", "username": "new-reviewer", "password": PASSWORD}
        self.assertEqual(self.post(new_admin, path, **default_fields).status_code, 303)
        self.assertEqual(self.login("new-reviewer").get(path).status_code, 403)

    def test_chinese_account_creation_and_login(self):
        route = "/admin/accounts"
        fields = {"action": "create", "name": "中文账号测试", "password": PASSWORD}
        for username in ("张三", "审核-Team_01", "𠮷" * 32):
            with self.subTest(username=username):
                self.assertEqual(self.post(self.admin, route, username=username, **fields).status_code, 303)
                self.assertEqual(self.login(username).get("/session/identity").status_code, 200)
        self.assertIn('<span class="mono">张三</span>', self.admin.get(route).text)
        for duplicate in ("张三", "审核-TEAM_01"):
            self.assertEqual(self.post(self.admin, route, username=duplicate, **fields).status_code, 409)
        self.login("审核-team_01")
        for bad_name in ("ab", "张 三", "审核🙂", "审" * 33):
            with self.subTest(bad_name=bad_name):
                self.assertEqual(self.post(self.admin, route, username=bad_name, **fields).status_code, 422)

    def test_provision_chinese_admin(self):
        credentials = self.data / "chinese-accounts.json"
        credentials.write_text(json.dumps([{"username": "李四", "name": "李四", "password": PASSWORD,
                                            "role": "admin"}], ensure_ascii=False), encoding="utf-8")
        result = self.app.test_cli_runner().invoke(args=["provision", str(credentials)])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(self.login("李四").get("/admin/accounts").status_code, 200)

    def test_login_throttle_and_password_reset(self):
        fresh = self.app.test_client()
        for _ in range(3):
            self.assertEqual(self.login_as(fresh, 'unknown', 'invalid').status_code, 401)
        self.assertEqual(self.login_as(fresh, 'UNKNOWN', 'invalid').status_code, 429)
        self.assertEqual(self.post(self.admin, '/admin/accounts', action='reset', user_id='3',
                                   password='test-new-password').status_code, 303)
        self.assertEqual(self.other.get('/session/identity').status_code, 401)
        self.login('reviewer-b', 'test-new-password')


if __name__ == '__main__':
    unittest.main()
