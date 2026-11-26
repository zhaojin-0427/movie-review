"""Verify database integrity and media references in an extracted backup."""
import hashlib
import sqlite3
import sys
from contextlib import closing
from pathlib import Path


def verify(state):
    if not (state / 'review.sqlite3').is_file():
        raise ValueError('Review database is missing')
    with closing(sqlite3.connect(state.joinpath('review.sqlite3').as_uri() + '?mode=ro', uri=True)) as db:
        if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise ValueError('SQLite integrity check failed')
        if db.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise ValueError('SQLite foreign key check failed')
        assets = state / 'assets'
        tables = {name for name, in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        frames = 0
        frame_query = 'SELECT v.asset_dir,f.asset_name FROM frames f JOIN versions v ON v.id=f.version_id'
        if 'oss_frames' in tables:
            frame_query += ' WHERE NOT EXISTS(SELECT 1 FROM oss_frames o WHERE o.version_id=f.version_id AND o.number=f.number)'
        for folder, file in db.execute(frame_query):
            path = assets / folder / file
            if not path.is_file() or path.stat().st_size == 0:
                raise ValueError(f'Frame asset missing: {folder}/{file}')
            frames += 1
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='execution_artifacts'").fetchone():
            artifact_query = 'SELECT id,size_bytes,sha256 FROM execution_artifacts a'
            if 'oss_artifacts' in tables:
                artifact_query += " WHERE a.role='version_manifest' OR NOT EXISTS(SELECT 1 FROM oss_artifacts o WHERE o.artifact=a.id)"
            for key, size, checksum in db.execute(artifact_query):
                path = assets / 'execution' / key
                if not path.is_file() or path.stat().st_size != size:
                    raise ValueError(f'Execution artifact missing or truncated: {key}')
                with path.open('rb') as file:
                    if hashlib.file_digest(file,'sha256').hexdigest() != checksum:
                        raise ValueError(f'Execution artifact SHA-256 mismatch: {key}')
            for upload, number, checksum in db.execute('''SELECT p.upload,p.part_no,p.sha256 FROM execution_parts p
                JOIN execution_uploads u ON u.id=p.upload WHERE u.artifact IS NULL'''):
                path = state / 'execution-incoming' / upload / str(number)
                if not path.is_file():
                    raise ValueError(f'Pending execution upload part missing: {upload}/{number}')
                with path.open('rb') as file:
                    if hashlib.file_digest(file,'sha256').hexdigest() != checksum:
                        raise ValueError(f'Pending execution upload part SHA-256 mismatch: {upload}/{number}')
        jobs = db.execute('SELECT count(*) FROM jobs').fetchone()[0]
        versions = db.execute('SELECT count(*) FROM versions').fetchone()[0]
        print(f'Backup restored and checked: {jobs} jobs, {versions} versions, {frames} frame files.')


if __name__ == '__main__':
    verify(Path(sys.argv[1]).resolve(strict=True))
