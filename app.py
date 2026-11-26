import functools
import hashlib
import io
import ipaddress
import json
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import time
import unicodedata

import requests
from contextlib import contextmanager
from pathlib import Path

import click
from flask import (Flask, abort, flash, g, jsonify, redirect, render_template, request,
                   send_file, session, url_for)
from flask.sessions import SecureCookieSessionInterface
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

from packages import MAX_PACKAGE, read_package
from execution import export_options, setup_execution
from execution_artifacts import object_hash
from feedback import annotated_preview, make_feedback_bundle
from oss_storage import Location

ROOT = Path(__file__).resolve().parent
STATES = {"pool": "待领取", "review": "审核中", "changes": "待返修版", "approved": "已通过",
          "paused": "已暂停", "withdrawn": "已撤回", "waiting": "等待原标注员"}
FRAME_STATES = {"pending": "待审", "changes": "有问题", "approved": "已通过"}
EVENTS = {"import": "导入送审版", "claim": "领取作业", "mark": "保存意见", "approve": "通过帧",
          "pending": "恢复待审", "changes": "退回修改", "finish": "全帧通过",
          "pause": "暂停作业", "resume": "恢复作业", "withdraw": "撤回作业", "supplement": "开启补审"}


def valid_username(value):
    if not 1 <= len(value) <= 32:
        return False
    remaining = re.sub(r"[A-Za-z0-9_.-]", "", value)
    if not remaining:
        return len(value) >= 3
    return all(
        char == "〇" or unicodedata.name(char, "").startswith(
            ("CJK UNIFIED IDEOGRAPH-", "CJK COMPATIBILITY IDEOGRAPH-")
        )
        for char in remaining
    )


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class ProxySessionInterface(SecureCookieSessionInterface):
    def get_cookie_name(self, app):
        name = super().get_cookie_name(app)
        return f"{name}_https" if request.is_secure else name

    def get_cookie_secure(self, app):
        return request.is_secure or super().get_cookie_secure(app)


def create_app(config=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config.update(DATA_DIR=os.environ.get("MOVIE_REVIEW_DATA", app.instance_path),
                      SECRET_KEY=os.environ.get("MOVIE_REVIEW_SECRET"),
                      MAX_CONTENT_LENGTH=MAX_PACKAGE + 1024 * 1024,
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                      SESSION_COOKIE_SECURE=os.environ.get("MOVIE_REVIEW_HTTPS") == "1",
                      SESSION_COOKIE_NAME="movie_review",
                      UPLOAD_API_TOKEN_SHA256=os.environ.get("MOVIE_REVIEW_UPLOAD_TOKEN_SHA256", ""),
                      UPLOAD_API_ACTOR=os.environ.get("MOVIE_REVIEW_UPLOAD_ACTOR", "admin"),
                      LOGIN_WINDOW_SECONDS=600, LOGIN_MAX_PER_ACCOUNT=6, LOGIN_MAX_PER_ADDRESS=40)
    if config:
        app.config.update(config)
    if os.environ.get("MOVIE_REVIEW_TRUST_PROXY") == "1":
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
        app.session_interface = ProxySessionInterface()
    data = Path(app.config["DATA_DIR"])
    data = data.resolve()
    data.mkdir(parents=True, exist_ok=True)
    (data / "assets").mkdir(exist_ok=True)
    if not app.secret_key:
        raise RuntimeError("Set MOVIE_REVIEW_SECRET before starting the app")
    dummy_hash = generate_password_hash(secrets.token_urlsafe(24))
    network_setting = os.environ.get("MOVIE_REVIEW_NETWORK")
    allowed_network = ipaddress.ip_network(network_setting) if network_setting else None

    def db():
        if "db" not in g:
            g.db = sqlite3.connect(data / "review.sqlite3", timeout=10, isolation_level=None)
            g.db.row_factory = sqlite3.Row
            g.db.execute("PRAGMA foreign_keys=ON")
        return g.db

    @contextmanager
    def write():
        conn = db()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    @app.teardown_appcontext
    def close_db(_error):
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()

    def init_db():
        conn = db()
        conn.executescript((ROOT / "schema.sql").read_text())
        # SQLite requires a table rebuild to make the legacy profile reference optional.
        columns = {row["name"]: row for row in conn.execute("PRAGMA table_info(execution_jobs)")}
        if columns["profile"]["notnull"]:
            conn.execute("PRAGMA foreign_keys=OFF")
            try:
                with write() as conn:
                    conn.execute("""CREATE TABLE execution_jobs_new (
                        job INTEGER PRIMARY KEY REFERENCES jobs(id), actor INTEGER NOT NULL REFERENCES users(id),
                        profile TEXT REFERENCES execution_profiles(hash), source TEXT NOT NULL REFERENCES execution_artifacts(id)
                    )""")
                    conn.execute("INSERT INTO execution_jobs_new SELECT job,actor,profile,source FROM execution_jobs")
                    conn.execute("DROP TABLE execution_jobs")
                    conn.execute("ALTER TABLE execution_jobs_new RENAME TO execution_jobs")
                    if conn.execute("PRAGMA foreign_key_check").fetchone():
                        raise RuntimeError("工单升级后外键检查失败")
            finally:
                conn.execute("PRAGMA foreign_keys=ON")
        retired_artifacts = []
        with write() as conn:
            migrations = {
                "users": ("deleted_at INTEGER",),
                "execution_nodes": ("deleted_at INTEGER", "oss_delivery TEXT NOT NULL DEFAULT 'manual'"),
                "execution_profiles": ("deleted_at INTEGER",),
                "execution_jobs": ("local_review INTEGER NOT NULL DEFAULT 0",
                                   "review_node TEXT REFERENCES execution_nodes(id)", "review_instance TEXT",
                                   "export_quality TEXT CHECK (export_quality IN ('1080p','4k'))"),
                "execution_work": ("deleted_at INTEGER",),
                "oss_settings": ("direct_upload INTEGER NOT NULL DEFAULT 0",),
                "execution_tokens": ("paused_at INTEGER", "deleted_at INTEGER"),
                "execution_attempts": ("claimed_at INTEGER", "returned_at INTEGER",
                                       "returned_by INTEGER REFERENCES users(id)", "return_reason TEXT"),
            }
            for table, fields in migrations.items():
                columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
                for field in fields:
                    if field.split()[0] not in columns:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {field}")
            # Older versions could leave an expired or failed claim queued with an owner.
            conn.execute("""UPDATE execution_work SET state='failed',lease=NULL,expires=NULL,available_at=0,
                            failure=coalesce(failure,'awaiting_return') WHERE state='queued' AND node IS NOT NULL""")
            if conn.execute("SELECT 1 FROM execution_attempts WHERE claimed_at IS NULL LIMIT 1").fetchone():
                conn.execute("""WITH claims AS (
                    SELECT node, json_extract(response,'$.work_id') AS work,
                           json_extract(response,'$.lease.attempt') AS attempt, created
                    FROM execution_requests WHERE operation='/api/v1/work/claim' AND status=200
                ) UPDATE execution_attempts AS a SET claimed_at=coalesce(a.claimed_at, claims.created)
                  FROM claims WHERE a.work=claims.work AND a.attempt=claims.attempt AND a.node=claims.node
                    AND a.claimed_at IS NULL""")
            # Retire the old server-side production-profile model. Existing work, review,
            # delivery, and claim history stay intact while their dispatch metadata is normalized.
            legacy_artifacts = {
                row[0] for row in conn.execute(
                    "SELECT id FROM execution_artifacts WHERE role IN ('rules_bundle','workflow_bundle')"
                ) if row[0]
            }
            conn.execute("UPDATE execution_jobs SET profile=NULL")
            for row in conn.execute("SELECT id,capabilities FROM execution_nodes WHERE capabilities IS NOT NULL"):
                try:
                    capabilities = json.loads(row[1])
                except (TypeError, json.JSONDecodeError):
                    continue
                if 'profile_hashes' in capabilities:
                    capabilities.pop('profile_hashes', None)
                    conn.execute("UPDATE execution_nodes SET capabilities=? WHERE id=?",
                                 (json.dumps(capabilities, ensure_ascii=False, separators=(',', ':')), row[0]))
            for row in conn.execute("SELECT id,manifest FROM execution_work"):
                try:
                    manifest = json.loads(row[1])
                except (TypeError, json.JSONDecodeError):
                    continue
                changed = 'profile' in manifest
                manifest.pop('profile', None)
                artifacts = manifest.get('artifacts')
                if isinstance(artifacts, list):
                    filtered = [item for item in artifacts if item.get('role') not in {'rules_bundle', 'workflow_bundle'}
                                and item.get('artifact_id') not in legacy_artifacts]
                    changed = changed or filtered != artifacts
                    manifest['artifacts'] = filtered
                if changed:
                    payload = json.dumps(manifest, ensure_ascii=False, separators=(',', ':'))
                    conn.execute("UPDATE execution_work SET manifest=?,manifest_hash=? WHERE id=?",
                                 (payload, object_hash(manifest), row[0]))
            conn.execute("DELETE FROM execution_profiles")
            for artifact_id in legacy_artifacts:
                referenced = conn.execute("""SELECT 1 FROM execution_uploads WHERE artifact=?
                    UNION SELECT 1 FROM execution_jobs WHERE source=?
                    UNION SELECT 1 FROM execution_work WHERE manifest LIKE ?
                    UNION SELECT 1 FROM execution_versions WHERE artifacts LIKE ?
                    UNION SELECT 1 FROM execution_deliveries WHERE artifacts LIKE ? LIMIT 1""",
                    (artifact_id, artifact_id, f'%{artifact_id}%', f'%{artifact_id}%', f'%{artifact_id}%')).fetchone()
                if not referenced:
                    conn.execute("DELETE FROM execution_artifacts WHERE id=?", (artifact_id,))
                    retired_artifacts.append(artifact_id)
        for artifact_id in retired_artifacts:
            (data / "assets" / "execution" / artifact_id).unlink(missing_ok=True)

    def csrf_token():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(24)
        return session["csrf"]

    app.jinja_env.globals.update(csrf_token=csrf_token, state_names=STATES,
                                 frame_states=FRAME_STATES, event_names=EVENTS)

    def session_user():
        if "login" in session:
            return db().execute("""SELECT u.id, u.username, u.name, u.role FROM users u
                JOIN sessions s ON u.id=s.user_id WHERE s.token_hash=? AND s.expires>? AND u.enabled=1
                AND u.deleted_at IS NULL""",
                                (digest(session["login"]), int(time.time()))).fetchone()
        return None

    @app.before_request
    def load_user():
        g.user = None
        if allowed_network and ipaddress.ip_address(request.remote_addr or "0.0.0.0") not in allowed_network:
            abort(403)
        if request.blueprint == "machine" or request.endpoint in {"static", "health", "submit_review_package"}:
            return
        g.user = session_user()
        if request.method == "POST":
            if request.endpoint == 'oss.finish_objects':
                request.max_form_memory_size = 2 * 1024**2
            token = request.form.get("csrf", "")
            if not token or not secrets.compare_digest(token.encode(), session.get("csrf", "").encode()):
                abort(400, "页面会话已变化，请刷新后重试")

    @app.after_request
    def protect(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
            "form-action 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' https://*.aliyuncs.com; connect-src 'self' https://*.aliyuncs.com")
        if request.endpoint != "static":
            response.headers["Cache-Control"] = "private, no-store"
        return response

    def requires(role=None):
        def decorate(fn):
            @functools.wraps(fn)
            def inner(*args, **kwargs):
                if not g.user:
                    return redirect(url_for("sign_in"), code=303)
                if role and g.user["role"] != role:
                    abort(403, "当前账号没有此操作的权限")
                return fn(*args, **kwargs)
            return inner
        return decorate

    def assert_session(role):
        fresh = session_user()
        if not fresh or fresh["role"] != role or fresh["id"] != g.user["id"]:
            abort(403)

    def job_row(job_id):
        row = db().execute("""SELECT j.*, u.name AS owner_name, u.enabled AS owner_enabled, u.deleted_at AS owner_deleted_at,
            COALESCE(c.state, 'active') AS control, c.reason AS control_reason,
            v.id AS version_id, v.number AS version, v.state, v.revision, v.frame_count
            FROM jobs j LEFT JOIN users u ON u.id=j.owner_id LEFT JOIN job_controls c ON c.job_id=j.id
            JOIN versions v ON v.id=(SELECT id FROM versions WHERE job_id=j.id ORDER BY number DESC LIMIT 1)
            WHERE j.id=?""", (job_id,)).fetchone()
        if row is None:
            abort(404)
        return row

    def assigned_job(job_id):
        job = job_row(job_id)
        if g.user["role"] != "admin" and (job["owner_id"] != g.user["id"] or job["control"] == "withdrawn"):
            abort(404)
        return job

    def version_row(job_id, number):
        version = db().execute("SELECT * FROM versions WHERE job_id=? AND number=?", (job_id, number)).fetchone()
        if version is None:
            abort(404)
        return version

    def event(conn, job_id, version_id, actor_id, action, detail=""):
        conn.execute("INSERT INTO events(job_id, version_id, actor_id, action, detail) VALUES(?,?,?,?,?)",
                     (job_id, version_id, actor_id, action, detail))

    def prior_receipt(actor_id, target_key, package_hash):
        return db().execute("""SELECT v.job_id, v.number FROM upload_receipts r JOIN versions v ON v.id=r.version_id
            WHERE r.actor_id=? AND r.target_key=? AND r.package_sha256=?""",
                            (actor_id, target_key, package_hash)).fetchone()

    def import_version(stream, job_id, title, actor_id, base_version=None, allow_duplicate=False, *,
                       channel=None, publish=None, check=None, inherit_only=None, stage_frames=None, package_hash=None):
        if channel in {"web", "api"} and job_id and db().execute("SELECT 1 FROM execution_jobs WHERE job=?", (job_id,)).fetchone():
            abort(409, "此作业的返修版由执行工单提交")
        if package_hash is None:
            package_hash = upload_digest(stream)
        target_key = f"revision:{job_id}:{base_version}" if job_id else f"new:{title}"
        dedupe = channel != "execution" and (not allow_duplicate or job_id is not None)
        if dedupe and (receipt := prior_receipt(actor_id, target_key, package_hash)):
            return receipt["job_id"], receipt["number"], True
        with tempfile.TemporaryDirectory(prefix="incoming-", dir=data) as folder:
            destination = Path(folder)
            source_hash, frames = stage_frames(destination) if stage_frames else read_package(stream, destination)
            asset_key = secrets.token_hex(16)
            asset_dir = data / "assets" / asset_key
            try:
                with write() as conn:
                    if channel == "web":
                        assert_session("admin")
                    if check:
                        check(conn)
                    if dedupe and (receipt := prior_receipt(actor_id, target_key, package_hash)):
                        return receipt["job_id"], receipt["number"], True
                    if base_version is None:
                        if job_id is None:
                            job_id = conn.execute("INSERT INTO jobs(title) VALUES(?)", (title,)).lastrowid
                        number, previous, trusted_source = 1, {}, False
                    else:
                        job = job_row(job_id)
                        if job["control"] != "active" or job["state"] != "changes" or job["version"] != base_version:
                            abort(409, "返修基版或作业状态已变化，请刷新后重试")
                        if job["frame_count"] != len(frames):
                            raise ValueError("返修版帧数应与上一版一致")
                        existing_source = conn.execute("""SELECT vi.source_sha256 FROM version_inputs vi
                            JOIN versions v ON v.id=vi.version_id WHERE v.job_id=? AND vi.source_sha256 IS NOT NULL
                            ORDER BY v.number DESC LIMIT 1""", (job_id,)).fetchone()
                        if source_hash and existing_source and existing_source["source_sha256"] != source_hash:
                            raise ValueError("原片摘要与此作业不一致，请核对原片或另建新作业")
                        parent = conn.execute("SELECT source_sha256 FROM version_inputs WHERE version_id=?",
                                              (job["version_id"],)).fetchone()
                        trusted_source = bool(source_hash and parent and parent["source_sha256"] == source_hash)
                        number = job["version"] + 1
                        previous = {f["number"]: f for f in conn.execute(
                            "SELECT * FROM frames WHERE version_id=?", (job["version_id"],))}
                    version_id = conn.execute("""INSERT INTO versions(job_id, number, state, asset_dir, frame_count)
                        VALUES (?, ?, 'review', ?, ?)""", (job_id, number, asset_key, len(frames))).lastrowid
                    conn.execute("INSERT INTO version_inputs VALUES(?,?,?)", (version_id, source_hash, package_hash))
                    if dedupe:
                        conn.execute("INSERT INTO upload_receipts VALUES(?,?,?,?)",
                                     (actor_id, target_key, package_hash, version_id))
                    records = []
                    inherited_count = 0
                    for frame in frames:
                        old = previous.get(frame.number)
                        inherited = (trusted_source and old and old["state"] == "approved"
                                     and old["approved_from"] is not None and old["time_ms"] is not None
                                     and frame.time_ms is not None and old["signature"] == frame.signature
                                     and (inherit_only is None or frame.number in inherit_only))
                        inherited_count += bool(inherited)
                        records.append((version_id, frame.number, frame.asset_name, frame.mimetype, frame.width,
                                        frame.height, frame.signature, frame.time_ms, frame.duration_ms,
                                        "approved" if inherited else "pending", old["approved_from"] if inherited else None))
                    conn.executemany("""INSERT INTO frames(version_id,number,asset_name,mimetype,width,height,signature,
                        time_ms,duration_ms,state,approved_from) VALUES(?,?,?,?,?,?,?,?,?,?,?)""", records)
                    conn.execute("UPDATE jobs SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (job_id,))
                    source_label = {"api": "客户端上传 · ", "execution": "制作工单 · "}.get(channel, "")
                    event(conn, job_id, version_id, actor_id, "import",
                          f"{source_label}V{number} · {len(frames)} 帧 · 沿用 {inherited_count} 帧")
                    destination.rename(asset_dir)
                    if publish:
                        publish(conn, version_id, number)
            except BaseException:
                if asset_dir.is_dir():
                    shutil.rmtree(asset_dir)
                raise
        return job_id, number, False

    after_execution_review, apply_execution_control, oss_fetch_url = setup_execution(
        app, data, db, write, requires, assert_session, import_version, event)

    @app.cli.command("init-db")
    def cmd_init():
        init_db()

    @app.cli.command("provision")
    @click.argument("credentials", type=click.Path(path_type=Path))
    def provision(credentials):
        """Provision accounts from a private JSON array read once during deployment."""
        init_db()
        for user in json.loads(credentials.read_text(encoding="utf-8")):
            if not valid_username(user["username"]) or len(user["password"]) < 12:
                raise click.ClickException("Invalid account name or password length")
            with write() as conn:
                conn.execute("INSERT OR IGNORE INTO users(username, name, role, password_hash) VALUES(?,?,?,?)",
                             (user["username"], user["name"], user["role"], generate_password_hash(user["password"])))

    @app.cli.command("seed")
    @click.argument("directory", type=click.Path(path_type=Path))
    def seed(directory):
        init_db()
        actor = db().execute("SELECT id FROM users WHERE role='admin' ORDER BY id LIMIT 1").fetchone()
        if actor is None:
            raise click.ClickException("Provision an admin first")
        for name in ("街角转弯", "仓库搬运"):
            title = f"{name} · 模拟送审"
            if db().execute("SELECT 1 FROM jobs WHERE title=?", (title,)).fetchone():
                continue
            with (directory / f"{name}-V1.zip").open("rb") as stream:
                job_id, version, _ = import_version(stream, None, title, actor["id"])
                click.echo(f"{title}: job {job_id}, V{version}")

    @app.get("/health")
    def health():
        db().execute("SELECT 1 FROM version_inputs LIMIT 1").fetchone()
        return jsonify(status="ok")

    @app.route("/login", methods=["GET", "POST"])
    def sign_in():
        execution_section = request.args.get("execution_section")

        def destination(role):
            if request.is_secure and role == "admin" and execution_section in {"devices", "credentials"}:
                return url_for("execution.panel", _anchor=execution_section)
            return url_for("index")

        if request.method == "POST":
            now = int(time.time())
            username = request.form.get("username", "").strip()[:128]
            password = request.form.get("password", "")
            address = request.remote_addr or "unknown"
            throttle_username = username.lower()
            cutoff = now - app.config["LOGIN_WINDOW_SECONDS"]
            with write() as conn:
                conn.execute("DELETE FROM login_attempts WHERE occurred_at <= ?", (cutoff,))
                attempts = conn.execute("""SELECT count(*) AS total, count(CASE WHEN username=? THEN 1 END) AS account
                    FROM login_attempts WHERE address=?""", (throttle_username, address)).fetchone()
                if (attempts["total"] >= app.config["LOGIN_MAX_PER_ADDRESS"]
                        or attempts["account"] >= app.config["LOGIN_MAX_PER_ACCOUNT"]):
                    return render_template("login.html", error="尝试过于频繁，请稍后登录"), 429
                attempt_id = conn.execute("INSERT INTO login_attempts(address,username,occurred_at) VALUES(?,?,?)",
                                          (address, throttle_username, now)).lastrowid
            row = db().execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
            if len(password) <= 128 and check_password_hash(row["password_hash"] if row else dummy_hash, password):
                if row and row["enabled"]:
                    token = secrets.token_urlsafe(32)
                    with write() as conn:
                        current = conn.execute("SELECT enabled,password_hash FROM users WHERE id=?", (row["id"],)).fetchone()
                        if not current["enabled"] or current["password_hash"] != row["password_hash"]:
                            return render_template("login.html", error="账号或密码有误"), 401
                        conn.execute("DELETE FROM login_attempts WHERE id=?", (attempt_id,))
                        conn.execute("DELETE FROM sessions WHERE expires<=?", (now,))
                        if session.get("login"):
                            conn.execute("DELETE FROM sessions WHERE token_hash=?", (digest(session["login"]),))
                        conn.execute("INSERT INTO sessions VALUES(?,?,?)", (digest(token), row["id"], now + 43200))
                    session.clear()
                    session["login"] = token
                    return redirect(destination(row["role"]), code=303)
            return render_template("login.html", error="账号或密码有误"), 401
        return redirect(destination(g.user["role"])) if g.user else render_template("login.html")

    @app.post("/logout")
    @requires()
    def sign_out():
        db().execute("DELETE FROM sessions WHERE token_hash=?", (digest(session["login"]),))
        session.clear()
        return redirect(url_for("sign_in"), code=303)

    @app.get("/")
    @requires()
    def index():
        params = []
        access = ""
        if g.user["role"] == "reviewer":
            params.append(g.user["id"])
            access = """WHERE (j.owner_id=? AND COALESCE(c.state,'active')!='withdrawn')
                OR (j.owner_id IS NULL AND COALESCE(c.state,'active')='active')"""
        owner_options = []
        valid_owners = {"all", "unassigned"}
        if g.user["role"] == "admin":
            owner_options = db().execute("""SELECT id, name, username, enabled, deleted_at FROM users
                WHERE role='reviewer' ORDER BY deleted_at IS NOT NULL, name COLLATE NOCASE, id""").fetchall()
            valid_owners.update(str(row["id"]) for row in owner_options)
        else:
            valid_owners.add(str(g.user["id"]))
        owner_filter = request.args.get("owner", "all")
        if owner_filter not in valid_owners:
            owner_filter = "all"
        rows = db().execute(f"""SELECT j.id, j.title, j.owner_id, j.updated_at, u.name AS owner_name,
            v.number AS version, v.frame_count, CASE
              WHEN c.state IN ('paused','withdrawn') THEN c.state
              WHEN v.state='review' AND u.enabled=0 THEN 'waiting'
              WHEN j.owner_id IS NULL THEN 'pool' ELSE v.state END AS state,
            (SELECT count(*) FROM frames f WHERE f.version_id=v.id AND f.state='approved') AS accepted
            FROM jobs j LEFT JOIN users u ON u.id=j.owner_id LEFT JOIN job_controls c ON c.job_id=j.id
            JOIN versions v ON v.id=(
                SELECT id FROM versions WHERE job_id=j.id ORDER BY number DESC LIMIT 1)
            {access} ORDER BY j.updated_at DESC, j.id DESC""", params).fetchall()
        status = request.args.get("state", "all")
        query = request.args.get("q", "").strip()[:120]
        number = query.removeprefix("#").lstrip("0")
        rows = [r for r in rows if (not query or query.casefold() in r["title"].casefold() or number == str(r["id"]))
                and (owner_filter == "all"
                     or (owner_filter == "unassigned" and r["owner_id"] is None)
                     or owner_filter == str(r["owner_id"]))]
        counts = {key: sum(r["state"] == key for r in rows) for key in STATES}
        total = len(rows)
        if status in STATES:
            rows = [r for r in rows if r["state"] == status]
        return render_template("index.html", rows=rows, total=total, counts=counts, status=status, query=query,
                               owner_filter=owner_filter, owner_options=owner_options)

    @app.get("/session/identity")
    def identity():
        if not g.user:
            return jsonify(user_id=None), 401
        return jsonify(user_id=g.user["id"])

    def upload_problem(status, code, message):
        response = jsonify(error={"code": code, "message": message})
        response.status_code = status
        if status == 401:
            response.headers["WWW-Authenticate"] = 'Bearer realm="movie-review-upload"'
        return response

    @app.post("/api/v1/review-packages")
    def submit_review_package():
        if not request.is_secure:
            return upload_problem(403, "https_required", "请使用 HTTPS 上传")
        expected = app.config["UPLOAD_API_TOKEN_SHA256"]
        if not re.fullmatch(r"[a-f0-9]{64}", expected):
            return upload_problem(503, "upload_unavailable", "客户端上传尚未配置")
        auth = request.authorization
        if (not auth or auth.type != "bearer" or not auth.token
                or not secrets.compare_digest(digest(auth.token), expected)):
            return upload_problem(401, "invalid_token", "需要有效的上传 Token")
        actor = db().execute("SELECT id FROM users WHERE username=? AND role='admin' AND enabled=1",
                             (app.config["UPLOAD_API_ACTOR"],)).fetchone()
        if actor is None:
            return upload_problem(503, "upload_unavailable", "客户端上传的服务账号不可用")
        if request.mimetype != "multipart/form-data":
            return upload_problem(415, "unsupported_media_type", "请使用 multipart/form-data 上传 ZIP")
        try:
            file = request.files.get("package")
            if not file or not file.filename.lower().endswith(".zip"):
                raise ValueError("请在 package 字段上传 ZIP 文件")
            title = request.form.get("title", "").strip()
            job = request.form.get("job_id", "").strip()
            base = request.form.get("base_version", "").strip()
            if job or base:
                if not re.fullmatch(r"[1-9][0-9]{0,17}:[1-9][0-9]{0,17}", f"{job}:{base}"):
                    raise ValueError("返修上传需要提供正整数 job_id 和 base_version")
            elif not 1 <= len(title) <= 120:
                raise ValueError("首版上传需要提供 1 至 120 字的 title")
            job_id, version, reused = import_version(file.stream, int(job) if job else None, title,
                                                     actor["id"], int(base) if base else None, channel="api")
        except ValueError as error:
            return upload_problem(422, "invalid_package", str(error))
        except HTTPException as error:
            codes = {400: "bad_request", 404: "job_not_found", 409: "version_conflict", 413: "request_too_large"}
            if error.code not in codes:
                raise
            return upload_problem(error.code, codes[error.code], error.description)
        path = url_for("review", job_id=job_id, v=version)
        return jsonify(job_id=job_id, version=version, reused=reused, review_path=path), 200 if reused else 201

    @app.route("/admin/import", methods=["GET", "POST"])
    @requires("admin")
    def upload():
        retry_jobs = db().execute("""SELECT j.id, j.title, v.number FROM jobs j
            LEFT JOIN job_controls c ON c.job_id=j.id JOIN versions v ON v.id=(
            SELECT id FROM versions WHERE job_id=j.id ORDER BY number DESC LIMIT 1)
            WHERE v.state='changes' AND COALESCE(c.state,'active')='active'
              AND NOT EXISTS(SELECT 1 FROM execution_jobs e WHERE e.job=j.id) ORDER BY j.updated_at DESC""").fetchall()
        if request.method == "POST":
            file = request.files.get("package")
            title = request.form.get("title", "").strip()
            existing = request.form.get("job_id", "").strip()
            try:
                if not file or not file.filename.lower().endswith(".zip"):
                    raise ValueError("请选择送审 ZIP 压缩包")
                match = re.fullmatch(r"([1-9][0-9]{0,17}):([1-9][0-9]{0,17})", existing) if existing else None
                if existing and not match:
                    abort(409, "请刷新后重新选择返修作业")
                if not existing and not 1 <= len(title) <= 120:
                    raise ValueError("请输入 1 至 120 字的作业名称")
                job_id, number, reused = import_version(file.stream, int(match[1]) if match else None, title,
                                                         g.user["id"], int(match[2]) if match else None,
                                                         request.form.get("allow_duplicate") == "1", channel="web")
            except ValueError as error:
                return render_template("import.html", jobs=retry_jobs, error=str(error), selected=existing,
                                       draft=title), 422
            flash(f"V{number} 已存在，已打开原作业" if reused else f"V{number} 已进入审核队列", "success")
            return redirect(url_for("review", job_id=job_id, v=number), code=303)
        selected_id = request.args.get("job_id", type=int)
        selected = next((f"{job['id']}:{job['number']}" for job in retry_jobs if job["id"] == selected_id), "")
        return render_template("import.html", jobs=retry_jobs, selected=selected)

    @app.post("/jobs/<int:job_id>/claim")
    @requires("reviewer")
    def claim(job_id):
        with write() as conn:
            assert_session("reviewer")
            job = job_row(job_id)
            if job["control"] != "active":
                abort(409, "作业已暂停或撤回，请刷新队列")
            if job["owner_id"] == g.user["id"]:
                return redirect(url_for("review", job_id=job_id), code=303)
            if job["owner_id"] is not None or job["state"] != "review":
                abort(409, "这条作业已被领取，请返回列表刷新")
            conn.execute("UPDATE jobs SET owner_id=?, updated_at=CURRENT_TIMESTAMP WHERE id=? AND owner_id IS NULL",
                         (g.user["id"], job_id))
            event(conn, job_id, job["version_id"], g.user["id"], "claim")
        return redirect(url_for("review", job_id=job_id), code=303)

    @app.route("/admin/accounts", methods=["GET", "POST"])
    @requires("admin")
    def accounts():
        if request.method == "POST":
            action = request.form.get("action")
            password = request.form.get("password", "")
            if action in {"create", "reset"} and not 12 <= len(password) <= 128:
                abort(422, "密码须为 12 至 128 个字符")
            password_hash = generate_password_hash(password) if action in {"create", "reset"} else None
            with write() as conn:
                assert_session("admin")
                if action == "create":
                    name = request.form.get("name", "").strip()
                    username = request.form.get("username", "").strip()
                    if not 1 <= len(name) <= 60:
                        abort(422, "显示名称须为 1 至 60 个字符")
                    if not valid_username(username):
                        abort(422, "账号可用汉字、英文字母、数字、点、下划线和连字符。"
                              "含汉字时为 1 至 32 个字符，其他账号为 3 至 32 个字符。")
                    role = request.form.get("role", "reviewer")
                    if role not in {"admin", "reviewer"}:
                        abort(422, "请选择管理员或标注员")
                    if conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
                        abort(409, "账号已存在")
                    target_id = conn.execute("""INSERT INTO users(username, name, role, password_hash)
                                                VALUES (?,?,?,?)""", (username, name, role, password_hash)).lastrowid
                else:
                    target_id = request.form.get("user_id", type=int)
                    target = conn.execute("SELECT id, username, role, enabled, deleted_at FROM users WHERE id=?", (target_id,)).fetchone()
                    if not target:
                        abort(404, "账号不存在")
                    if target["deleted_at"] is not None:
                        abort(409, "账号已经删除")
                    if action == "delete":
                        if target_id == g.user["id"]:
                            abort(422, "不能删除当前登录账号")
                        if target["role"] == "admin":
                            admin_count = conn.execute("""SELECT count(*) FROM users
                                WHERE role='admin' AND deleted_at IS NULL""").fetchone()[0]
                            if admin_count <= 1:
                                abort(409, "至少保留一个管理员账号")
                            if (app.config["UPLOAD_API_TOKEN_SHA256"]
                                    and target["username"].lower() == app.config["UPLOAD_API_ACTOR"].lower()):
                                abort(409, "此账号用于客户端送审上传，请先切换上传服务账号")
                        conn.execute("""UPDATE users SET enabled=0, deleted_at=?, password_hash=''
                                        WHERE id=?""", (int(time.time()), target_id))
                    else:
                        if target["role"] != "reviewer":
                            abort(403, "密码、启停操作仅适用于标注员账号")
                        if action == "reset":
                            conn.execute("UPDATE users SET password_hash=? WHERE id=?", (password_hash, target_id))
                        elif action in {"enable", "disable"}:
                            conn.execute("UPDATE users SET enabled=? WHERE id=?", (action == "enable", target_id))
                        else:
                            abort(422, "请选择有效的账号操作")
                    conn.execute("DELETE FROM sessions WHERE user_id=?", (target_id,))
                conn.execute("INSERT INTO account_events(actor_id,target_id,action) VALUES(?,?,?)",
                             (g.user["id"], target_id, action))
            flash("账号已删除，登录会话已注销" if action == "delete" else "账号已更新", "success")
            return redirect(url_for("accounts"), code=303)
        users = db().execute("""SELECT u.id, u.name, u.username, u.role, u.enabled, count(j.id) AS owned
            FROM users u LEFT JOIN jobs j ON j.owner_id=u.id
            WHERE u.deleted_at IS NULL
            GROUP BY u.id ORDER BY u.role, u.id""").fetchall()
        recent = db().execute("""SELECT e.action,e.created_at,t.name AS target,a.name AS actor FROM account_events e
            JOIN users t ON t.id=e.target_id JOIN users a ON a.id=e.actor_id ORDER BY e.id DESC LIMIT 15""").fetchall()
        upload_actor = app.config["UPLOAD_API_ACTOR"].lower() if app.config["UPLOAD_API_TOKEN_SHA256"] else None
        return render_template("accounts.html", users=users, recent=recent, upload_actor=upload_actor)

    @app.post("/admin/jobs/<int:job_id>/control")
    @requires("admin")
    def job_control(job_id):
        action = request.form.get("action")
        reason = request.form.get("reason", "").strip()
        if action not in {"pause", "resume", "withdraw"} or not 1 <= len(reason) <= 250:
            abort(422, "请选择作业操作并填写原因")
        with write() as conn:
            assert_session("admin")
            job = job_row(job_id)
            expected = f"{job['version_id']}:{job['revision']}"
            if request.form.get("revision") != expected:
                abort(409, "作业已更新，请刷新详情")
            allowed = {"active": {"pause", "withdraw"}, "paused": {"resume", "withdraw"}, "withdrawn": set()}
            if action not in allowed[job["control"]] or (action == "pause" and job["state"] == "approved"):
                abort(409, "该作业目前无法执行此操作")
            state = {"pause": "paused", "resume": "active", "withdraw": "withdrawn"}[action]
            conn.execute("""INSERT INTO job_controls(job_id,state,reason) VALUES(?,?,?)
                            ON CONFLICT(job_id) DO UPDATE SET state=excluded.state,reason=excluded.reason""",
                         (job_id, state, reason))
            conn.execute("UPDATE versions SET revision=revision+1 WHERE id=?", (job["version_id"],))
            conn.execute("UPDATE jobs SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (job_id,))
            event(conn, job_id, job["version_id"], g.user["id"], action, reason)
            apply_execution_control(conn, job_id, action)
        flash(f"{EVENTS[action]}成功", "success")
        return redirect(url_for("review", job_id=job_id), code=303)

    def is_execution_job(job_id):
        return db().execute("SELECT 1 FROM execution_jobs WHERE job=?", (job_id,)).fetchone() is not None

    def can_open_supplement(job_id, version_id, execution_job):
        if not execution_job:
            return True
        return db().execute("""WITH RECURSIVE lineage(id) AS (
            SELECT ? UNION ALL SELECT s.base_id FROM supplements s JOIN lineage l ON l.id=s.version_id
            ) SELECT 1 FROM lineage l JOIN versions v ON v.id=l.id JOIN execution_deliveries d
            ON d.job=v.job_id AND d.version=v.number WHERE d.job=? LIMIT 1""", (version_id, job_id)).fetchone() is not None

    def review_frame(job_id, version, number):
        frame = db().execute("""SELECT f.*, v.number AS origin FROM frames f
            LEFT JOIN versions v ON v.id=f.approved_from WHERE f.version_id=? AND f.number=?""",
                             (version["id"], number)).fetchone()
        if frame is None:
            abort(404)
        prior = db().execute("""SELECT f.note, f.regions FROM frames f JOIN versions v ON v.id=f.version_id
            WHERE v.job_id=? AND v.number=? AND f.number=? AND f.state='changes'""",
                             (job_id, version["number"] - 1, number)).fetchone()
        return frame, prior

    def review_writable(job, version):
        return (g.user["role"] == "reviewer" and job["owner_id"] == g.user["id"]
                and job["control"] == "active" and job["version_id"] == version["id"] and version["state"] == "review")

    @app.get("/jobs/<int:job_id>/v/<int:number>/frame-data/<int:frame_no>")
    @requires()
    def frame_data(job_id, number, frame_no):
        db().execute("BEGIN")
        job = assigned_job(job_id)
        version = version_row(job_id, number)
        if request.args.get("revision", type=int) != version["revision"]:
            abort(409, "审核已更新，请刷新作业后继续")
        frame, prior = review_frame(job_id, version, frame_no)
        return jsonify(user_id=g.user["id"], revision=version["revision"], writable=review_writable(job, version),
                       frame=dict(number=frame_no, state=frame["state"], width=frame["width"], height=frame["height"],
                                  time_ms=frame["time_ms"], origin=frame["origin"], note=frame["note"],
                                  regions=json.loads(frame["regions"])),
                       prior=dict(note=prior["note"], regions=json.loads(prior["regions"]),
                                  marked_url=url_for("marked_image", job_id=job_id, number=number-1, frame_no=frame_no))
                       if prior else None)

    @app.get("/jobs/<int:job_id>")
    @requires()
    def review(job_id):
        db().execute("BEGIN")
        job = assigned_job(job_id)
        versions = db().execute("SELECT * FROM versions WHERE job_id=? ORDER BY number DESC", (job_id,)).fetchall()
        version = version_row(job_id, request.args.get("v", job["version"], type=int))
        frames = db().execute("""SELECT number, state FROM frames
                                 WHERE version_id=? ORDER BY number""", (version["id"],)).fetchall()
        selected_filter = request.args.get("frames", "all")
        if selected_filter not in {"all", "pending", "changes", "approved"}:
            selected_filter = "all"
        visible = [row for row in frames if selected_filter == "all" or row["state"] == selected_filter]
        current = request.args.get("frame", type=int)
        if current is None:
            current = (next((f["number"] for f in visible if f["state"] == "pending"), visible[0]["number"])
                       if visible else 1)
        if not 1 <= current <= len(frames):
            abort(404)
        previous_frame = next((row["number"] for row in reversed(visible) if row["number"] < current), None)
        next_frame = next((row["number"] for row in visible if row["number"] > current), None)
        frame, prior = review_frame(job_id, version, current)
        strip_offset = max(0, next((i for i, row in enumerate(visible) if row["number"] >= current),
                                   len(visible)) - 16)
        history = db().execute("""SELECT e.*, u.name, v.number AS version FROM events e JOIN users u ON e.actor_id=u.id
            JOIN versions v ON v.id=e.version_id WHERE e.job_id=? AND e.action IN ('import','claim','changes','finish','pause','resume','withdraw','supplement')
            ORDER BY e.id DESC LIMIT 15""", (job_id,)).fetchall()
        writable = review_writable(job, version)
        supplement = db().execute("""SELECT parent.number FROM supplements s JOIN versions parent ON parent.id=s.base_id
                                      WHERE s.version_id=?""", (version["id"],)).fetchone()
        execution_job = is_execution_job(job_id)
        export = export_options(db(), data / "assets" / "execution", job_id) if execution_job and g.user["role"] == "admin" else None
        delivery = execution_job and g.user["role"] == "admin" and db().execute(
            "SELECT 1 FROM execution_deliveries WHERE job=? AND version=?", (job_id, version["number"])
        ).fetchone() is not None
        return render_template("review.html", job=job, versions=versions, version=version, frames=frames,
                               supplement=supplement, execution_job=execution_job, delivery=delivery, export=export,
                               supplement_allowed=can_open_supplement(job_id, version["id"], execution_job),
                               frame=frame, writable=writable, regions=json.loads(frame["regions"]),
                               prior=prior, prior_regions=json.loads(prior["regions"]) if prior else [],
                               history=history, visible=visible[strip_offset:strip_offset + 33], selected_filter=selected_filter,
                               previous_frame=previous_frame, next_frame=next_frame, approved=sum(f["state"] == "approved" for f in frames),
                               changes=sum(f["state"] == "changes" for f in frames))

    @app.post("/jobs/<int:job_id>/v/<int:number>/supplement")
    @requires("reviewer")
    def start_supplement(job_id, number):
        asset_key = secrets.token_hex(16)
        target = data / "assets" / asset_key
        try:
            with write() as conn:
                assert_session("reviewer")
                job = assigned_job(job_id)
                base = version_row(job_id, number)
                if job["control"] != "active" or base["state"] != "approved":
                    abort(409, "仅可对审核通过且处于正常状态的作业开启补审")
                if job["version_id"] != base["id"]:
                    duplicate = conn.execute("SELECT 1 FROM supplements WHERE version_id=? AND base_id=?",
                                             (job["version_id"], base["id"])).fetchone()
                    if duplicate and job["state"] == "review":
                        return redirect(url_for("review", job_id=job_id, v=job["version"]), code=303)
                    abort(409, "作业已有更新的送审版，请刷新后操作")
                if not can_open_supplement(job_id, base["id"], is_execution_job(job_id)):
                    abort(409, "成片交付后才可开启补审")
                if request.form.get("revision") != str(base["revision"]):
                    abort(409, "作业已更新，请刷新后操作")
                try:
                    shutil.copytree(data / "assets" / base["asset_dir"], target, copy_function=os.link)
                except OSError:
                    abort(409, "原审核图片不可用，请先恢复图片")
                number += 1
                version_id = conn.execute("""INSERT INTO versions(job_id,number,state,asset_dir,frame_count)
                    VALUES (?,?,'review',?,?)""", (job_id, number, asset_key, base["frame_count"])).lastrowid
                conn.execute("INSERT INTO supplements(version_id,base_id) VALUES(?,?)", (version_id, base["id"]))
                conn.execute("""INSERT INTO version_inputs(version_id,source_sha256,package_sha256)
                    SELECT ?,source_sha256,package_sha256 FROM version_inputs WHERE version_id=?""", (version_id, base["id"]))
                conn.execute("""INSERT INTO frames(version_id,number,asset_name,mimetype,width,height,signature,
                    time_ms,duration_ms,state,approved_from)
                    SELECT ?,number,asset_name,mimetype,width,height,signature,time_ms,duration_ms,state,approved_from
                    FROM frames WHERE version_id=?""", (version_id, base["id"]))
                conn.execute("""INSERT INTO oss_frames(version_id,number,location)
                    SELECT ?,number,location FROM oss_frames WHERE version_id=?""", (version_id,base["id"]))
                conn.execute("UPDATE jobs SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (job_id,))
                event(conn, job_id, version_id, g.user["id"], "supplement", f"基于 V{base['number']}")
        except BaseException:
            if target.is_dir():
                shutil.rmtree(target)
            raise
        flash("补审已开启，可以记录新发现的问题。", "success")
        return redirect(url_for("review", job_id=job_id, v=number), code=303)

    @app.post("/jobs/<int:job_id>/v/<int:number>/close-supplement")
    @requires("reviewer")
    def close_supplement(job_id, number):
        with write() as conn:
            version = assert_editable(job_id, number)
            if (not conn.execute("SELECT 1 FROM supplements WHERE version_id=?", (version["id"],)).fetchone()
                    or conn.execute("SELECT 1 FROM frames WHERE version_id=? AND state!='approved'", (version["id"],)).fetchone()):
                abort(409, "仍有待审或有问题的帧，请先完成审核")
            mark_finished(conn, job_id, version["id"], g.user["id"])
        return redirect(url_for("review", job_id=job_id, v=number), code=303)

    def frame_location(version_id, number):
        row = db().execute('SELECT location FROM oss_frames WHERE version_id=? AND number=?',
                           (version_id,number)).fetchone()
        return Location(**json.loads(row['location'])) if row else None

    def frame_source(version, number, asset_name):
        location = frame_location(version['id'], number)
        if location is None:
            return data / 'assets' / version['asset_dir'] / asset_name
        try:
            with requests.get(oss_fetch_url(location), timeout=(8,20), allow_redirects=False) as response:
                response.raise_for_status()
                return io.BytesIO(response.content)
        except requests.RequestException as error:
            raise OSError('OSS 审核图暂不可用') from error

    @app.get("/jobs/<int:job_id>/v/<int:number>/image/<int:frame_no>")
    @requires()
    def image(job_id, number, frame_no):
        assigned_job(job_id)
        version = version_row(job_id, number)
        frame = db().execute("SELECT asset_name,mimetype FROM frames WHERE version_id=? AND number=?",
                             (version["id"], frame_no)).fetchone()
        if not frame:
            abort(404)
        location = frame_location(version['id'], frame_no)
        if location:
            return redirect(oss_fetch_url(location), code=302)
        path = data / "assets" / version["asset_dir"] / frame["asset_name"]
        if not path.is_file():
            abort(404)
        return send_file(path, mimetype=frame["mimetype"], conditional=False, etag=False)

    def mark_finished(conn, job_id, version_id, actor_id):
        conn.execute("UPDATE versions SET state='approved',closed_at=CURRENT_TIMESTAMP,revision=revision+1 WHERE id=?",
                     (version_id,))
        conn.execute("UPDATE jobs SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (job_id,))
        event(conn, job_id, version_id, actor_id, "finish")
        after_execution_review(conn, job_id, version_id, "finalize")
        flash("全部帧已通过，审核结束", "success")

    def assert_editable(job_id, number):
        assert_session("reviewer")
        job = assigned_job(job_id)
        version = version_row(job_id, number)
        if (job["control"] != "active" or job["version_id"] != version["id"] or version["state"] != "review"
                or str(version["revision"]) != request.form.get("revision")):
            abort(409, "审核记录已更新，请刷新作业后继续")
        return version

    @app.post("/jobs/<int:job_id>/v/<int:number>/frames/<int:frame_no>")
    @requires("reviewer")
    def edit_frame(job_id, number, frame_no):
        action = request.form.get("action")
        with write() as conn:
            version = assert_editable(job_id, number)
            v_id, last = version["id"], version["frame_count"]
            if not 1 <= frame_no <= last:
                abort(404)
            targets = [(frame_no, "", [], None)]
            next_frame = frame_no
            if action == "approve":
                asset = conn.execute("SELECT asset_name FROM frames WHERE version_id=? AND number=?",
                                     (v_id, frame_no)).fetchone()
                if not asset or (not frame_location(v_id, frame_no)
                                 and not (data / "assets" / version["asset_dir"] / asset["asset_name"]).is_file()):
                    abort(409, "当前帧图片不可用，无法通过")
                targets = [(frame_no, "", [], v_id)]
                state, next_frame, logged = "approved", min(last, frame_no + 1), "approve"
            elif action == "pending":
                state, logged = "pending", "pending"
            elif action in {"save", "previous"}:
                state, logged = "changes", "mark"
                try:
                    note, regions = load_feedback(request.form.get("note", ""), request.form.get("regions", "[]"))
                    end = int(request.form.get("range_end") or frame_no) if action == "save" else frame_no
                    if not frame_no <= end <= last:
                        raise ValueError("区间末帧需在当前帧与片尾之间")
                    if action == "previous":
                        previous = conn.execute("SELECT * FROM frames WHERE version_id=? AND number=?",
                                                (v_id, frame_no - 1)).fetchone()
                        if not previous or previous["state"] != "changes":
                            raise ValueError("上一帧没有已保存的问题")
                        note, regions = merge_feedback(note, regions, previous["note"], json.loads(previous["regions"]))
                    if not note and not regions:
                        raise ValueError("请填写整帧意见或添加带说明的圈选")
                    targets = [(frame_no, note, regions, None)]
                    for dest in conn.execute("SELECT * FROM frames WHERE version_id=? AND number BETWEEN ? AND ?",
                                             (v_id, frame_no + 1, end)):
                        merged = merge_feedback(dest["note"], json.loads(dest["regions"]), note, regions)
                        targets.append((dest["number"], *merged, None))
                    next_frame = min(last, end + 1)
                except ValueError as error:
                    abort(422, str(error))
            else:
                abort(422, "请选择有效的审核操作")
            conn.executemany("""UPDATE frames SET state=?,note=?,regions=?,approved_from=?
                WHERE version_id=? AND number=?""", ((state, note, json.dumps(regions, ensure_ascii=False),
                origin, v_id, target) for target, note, regions, origin in targets))
            conn.execute("UPDATE versions SET revision=revision+1 WHERE id=?", (v_id,))
            conn.execute("UPDATE jobs SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (job_id,))
            event(conn, job_id, v_id, g.user["id"], logged, ", ".join(str(t[0]) for t in targets))
            finished = not conn.execute("SELECT 1 FROM frames WHERE version_id=? AND state!='approved'", (v_id,)).fetchone()
            if finished:
                mark_finished(conn, job_id, v_id, g.user["id"])
            elif action == "approve":
                pending = conn.execute("""SELECT number FROM frames WHERE version_id=? AND state='pending'
                    ORDER BY number<=?, number LIMIT 1""", (v_id, frame_no)).fetchone()
                if pending:
                    next_frame = pending["number"]
        selected_filter = request.form.get("frames", "all")
        location = url_for("review", job_id=job_id, v=number, frame=next_frame, frames=selected_filter)
        if request.accept_mimetypes.best == "application/json":
            return jsonify(location=location, frame=next_frame, revision=version["revision"] + 1 + int(finished),
                           reload=finished, frames=[target[0] for target in targets], state=state)
        return redirect(location, code=303)

    @app.post("/jobs/<int:job_id>/v/<int:number>/return")
    @requires("reviewer")
    def return_job(job_id, number):
        with write() as conn:
            version = assert_editable(job_id, number)
            if not conn.execute("SELECT 1 FROM frames WHERE version_id=? AND state='changes'", (version["id"],)).fetchone():
                abort(422, "先保存至少一帧的修改意见")
            conn.execute("UPDATE versions SET state='changes',revision=revision+1,closed_at=CURRENT_TIMESTAMP WHERE id=?",
                         (version["id"],))
            conn.execute("UPDATE jobs SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (job_id,))
            event(conn, job_id, version["id"], g.user["id"], "changes")
            after_execution_review(conn, job_id, version["id"], "revise")
        flash("返修意见已冻结，可下载意见单", "success")
        return redirect(url_for("review", job_id=job_id, v=number), code=303)

    def feedback_payload(job_id, number):
        job = assigned_job(job_id)
        version = version_row(job_id, number)
        if version["state"] != "changes":
            abort(404)
        rows = db().execute("""SELECT number,time_ms,note,regions,asset_name FROM frames
                              WHERE version_id=? AND state='changes' ORDER BY number""", (version["id"],)).fetchall()
        payload = {"job_id": job_id, "title": job["title"], "version": number, "reviewer": job["owner_name"],
                   "frames": [{**dict(row), "regions": json.loads(row["regions"])} for row in rows]}
        return version, payload

    @app.get("/jobs/<int:job_id>/v/<int:number>/feedback.json")
    @requires()
    def download_feedback(job_id, number):
        _, payload = feedback_payload(job_id, number)
        for row in payload["frames"]:
            del row["asset_name"]
        content = io.BytesIO(json.dumps(payload, ensure_ascii=False, indent=2).encode())
        return send_file(content, mimetype="application/json", as_attachment=True,
                         download_name=f"review-{job_id}-V{number}-feedback.json")

    @app.get("/jobs/<int:job_id>/v/<int:number>/feedback.zip")
    @requires()
    def download_feedback_bundle(job_id, number):
        version, payload = feedback_payload(job_id, number)
        try:
            content, length = make_feedback_bundle(payload, lambda row: frame_source(version, row['number'], row['asset_name']))
        except OSError:
            abort(409, "这个版本的部分图片不可用，请联系管理员")
        response = send_file(content, mimetype="application/zip", as_attachment=True,
                             download_name=f"review-{job_id}-V{number}-feedback.zip", etag=False)
        response.content_length = length
        return response

    @app.get("/jobs/<int:job_id>/v/<int:number>/marked/<int:frame_no>")
    @requires()
    def marked_image(job_id, number, frame_no):
        assigned_job(job_id)
        version = version_row(job_id, number)
        row = db().execute("""SELECT asset_name,regions FROM frames
            WHERE version_id=? AND number=? AND state='changes'""", (version["id"], frame_no)).fetchone()
        if not row or version["state"] != "changes":
            abort(404)
        try:
            preview = annotated_preview(frame_source(version, frame_no, row['asset_name']), json.loads(row["regions"]))
        except OSError:
            abort(404)
        return send_file(io.BytesIO(preview), mimetype="image/jpeg", etag=False)

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(409)
    @app.errorhandler(413)
    @app.errorhandler(422)
    def readable_error(error):
        messages = {403: "当前账号没有此操作的权限", 404: "找不到该记录，或当前账号没有读取权限",
                    413: "文件超过 256 MiB 的上传上限"}
        message = messages.get(error.code, error.description)
        if request.endpoint in {"frame_data", "edit_frame"} and request.accept_mimetypes.best == "application/json":
            return jsonify(message=message), error.code
        return render_template("error.html", code=error.code, message=message), error.code

    return app


def upload_digest(stream):
    checksum = hashlib.sha256()
    size = 0
    while chunk := stream.read(1024 * 1024):
        size += len(chunk)
        if size > MAX_PACKAGE:
            raise ValueError("上传包超过 256 MiB")
        checksum.update(chunk)
    stream.seek(0)
    return checksum.hexdigest()


def load_feedback(note, raw):
    if len(note) > 4000 or len(raw) > 64000:
        raise ValueError("整帧意见和圈选文字超过长度上限")
    try:
        regions = json.loads(raw)
    except ValueError as error:
        raise ValueError("圈选标注格式无效") from error
    if not isinstance(regions, list) or len(regions) > 24:
        raise ValueError("一帧最多 24 个圈选")
    normalized = []
    for region in regions:
        if not isinstance(region, dict):
            raise ValueError("圈选标注格式无效")
        x, y, w, h = (region.get(key) for key in ("x", "y", "w", "h"))
        if (any(type(value) not in (int, float) or not 0 <= value <= 1 for value in (x, y, w, h))
                or x >= 1 or y >= 1 or w <= 0 or h <= 0 or x + w > 1 + 1e-9 or y + h > 1 + 1e-9):
            raise ValueError("圈选须在画面内并具有宽度和高度")
        text = region.get("note")
        if not isinstance(text, str) or not text.strip() or len(text) > 500:
            raise ValueError("每个圈选需要 1 至 500 字的说明")
        normalized.append(dict(x=x, y=y, w=min(w, 1-x), h=min(h, 1-y), note=text.strip()))
    return note.strip(), normalized


def merge_feedback(note, regions, other_note, other_regions):
    paragraphs = list(dict.fromkeys(filter(None, (note + "\n\n" + other_note).split("\n\n"))))
    unique = {(r["x"], r["y"], r["w"], r["h"], r["note"]): r for r in [*regions, *other_regions]}
    return load_feedback("\n\n".join(paragraphs), json.dumps(list(unique.values())))
