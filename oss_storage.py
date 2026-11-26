"""OSS location, credentials and signed requests shared by the platform and processor."""
import base64
import hashlib
import os
import re
import threading
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from dataclasses import dataclass
from urllib.parse import quote

import oss2
import requests
from cryptography.fernet import Fernet

PREFIX = 'movie-review'
PART_BYTES = 32 * 1024**2
URL_SECONDS = 900
REGION = re.compile(r'cn-[a-z0-9]+(?:-[a-z0-9]+)*\Z')
BUCKET = re.compile(r'[a-z0-9][a-z0-9-]{1,61}[a-z0-9]\Z')


@dataclass(frozen=True)
class Location:
    region: str
    bucket: str
    key: str

    def __post_init__(self):
        if (not REGION.fullmatch(self.region) or not BUCKET.fullmatch(self.bucket)
                or not re.fullmatch(r'movie-review/(incoming|published)/[A-Za-z0-9_./-]+', self.key)
                or any(part in ('', '.', '..') for part in self.key.split('/'))):
            raise ValueError('OSS 对象位置无效')

    @property
    def url(self):
        return f'https://{self.bucket}.oss-{self.region}.aliyuncs.com/{quote(self.key, safe="/")}'

    def public(self):
        return dict(region=self.region, bucket=self.bucket, key=self.key)


def cipher(app_secret):
    secret = app_secret.encode() if isinstance(app_secret, str) else app_secret
    key = hashlib.sha256(b'movie-review:oss-settings:v1\x00' + secret).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def settings(conn):
    return conn.execute('SELECT * FROM oss_settings WHERE id=1').fetchone()


def bucket_for(location, auth, *, internal=False):
    suffix = '-internal' if internal else ''
    return oss2.Bucket(auth, f'https://oss-{location.region}{suffix}.aliyuncs.com',
                       location.bucket, region=location.region, connect_timeout=10)


def platform_bucket(location, config, app_secret):
    secret = cipher(app_secret).decrypt(config['encrypted_secret'].encode()).decode()
    return bucket_for(location, oss2.AuthV4(config['access_key_id'], secret))


class InstanceCredentials(oss2.credentials.CredentialsProvider):
    """Refresh the processor identity through ECS IMDSv2."""
    def __init__(self, role):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', role):
            raise ValueError('Invalid ECS RAM role name')
        self.role = role
        self.credentials = None
        self.expires = datetime.min.replace(tzinfo=UTC)
        self.lock = threading.Lock()

    def get_credentials(self):
        with self.lock:
            if self.expires > datetime.now(UTC) + timedelta(minutes=3):
                return self.credentials
            endpoint = 'http://100.100.100.200/latest'
            with requests.Session() as session:
                session.trust_env = False
                token = session.put(endpoint + '/api/token',
                    headers={'X-aliyun-ecs-metadata-token-ttl-seconds': '90'}, timeout=3, allow_redirects=False)
                token.raise_for_status()
                result = session.get(endpoint + '/meta-data/ram/security-credentials/' + self.role,
                    headers={'X-aliyun-ecs-metadata-token': token.text}, timeout=3, allow_redirects=False)
                result.raise_for_status()
                value = result.json()
            self.expires = datetime.fromisoformat(value['Expiration'].replace('Z', '+00:00'))
            self.credentials = oss2.credentials.Credentials(value['AccessKeyId'], value['AccessKeySecret'],
                                                              value['SecurityToken'])
            return self.credentials


@lru_cache(maxsize=1)
def processor_auth(role):
    provider = InstanceCredentials(role) if role else oss2.credentials.EnvironmentVariableCredentialsProvider()
    return oss2.ProviderAuthV4(provider)


@lru_cache(maxsize=16)
def cloud_bucket_for(region, bucket, role, internal):
    example = Location(region, bucket, f'{PREFIX}/incoming/connection')
    return bucket_for(example, processor_auth(role), internal=internal)


def cloud_bucket(location):
    return cloud_bucket_for(location.region, location.bucket, os.environ.get('OSS_ECS_ROLE'),
                            os.environ.get('OSS_INTERNAL', '1') == '1')
