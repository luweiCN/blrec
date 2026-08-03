#!/usr/bin/env python3
from __future__ import annotations

import argparse
import binascii
import hashlib
import logging
import os
import re
import ssl
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Protocol, Sequence


LOGGER = logging.getLogger('cdn-certificate-sync')
CERTIFICATE_PATTERN = re.compile(
    r'-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----', re.DOTALL
)


class CertificateSyncError(RuntimeError):
    pass


@dataclass(frozen=True)
class LocalCertificate:
    public_pem: str
    private_key_pem: str
    fingerprint: str


@dataclass(frozen=True)
class RemoteCertificate:
    public_pem: Optional[str]
    enabled: bool


class CdnCertificateClient(Protocol):
    def describe(self, domain: str) -> RemoteCertificate:
        pass

    def upload(
        self, domain: str, certificate_name: str, public_pem: str, private_key_pem: str
    ) -> None:
        pass


CommandRunner = Callable[[Sequence[str]], bytes]
Sleeper = Callable[[float], None]


def _run_command(command: Sequence[str]) -> bytes:
    try:
        completed = subprocess.run(
            command,
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
    except FileNotFoundError as exc:
        raise CertificateSyncError('找不到 openssl，无法校验证书') from exc
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        operation = command[1] if len(command) > 1 else 'unknown'
        raise CertificateSyncError(
            '本地证书校验失败（openssl {}）'.format(operation)
        ) from exc
    return completed.stdout


def _first_certificate_pem(public_pem: str) -> str:
    match = CERTIFICATE_PATTERN.search(public_pem)
    if match is None:
        raise CertificateSyncError('证书文件不包含有效的 PEM 证书块')
    return '{}\n'.format(match.group(0))


def certificate_fingerprint(public_pem: str) -> str:
    try:
        certificate_der = ssl.PEM_cert_to_DER_cert(_first_certificate_pem(public_pem))
    except (ValueError, binascii.Error) as exc:
        raise CertificateSyncError('证书 PEM 内容无法解析') from exc
    return hashlib.sha256(certificate_der).hexdigest()


def _read_text(path: Path, label: str) -> str:
    if not path.is_file():
        raise CertificateSyncError('{}文件不存在：{}'.format(label, path))
    try:
        return path.read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError) as exc:
        raise CertificateSyncError('{}文件无法读取：{}'.format(label, path)) from exc


def load_local_certificate(
    certificate_path: Path,
    private_key_path: Path,
    domain: str,
    minimum_valid_days: int,
    command_runner: CommandRunner = _run_command,
) -> LocalCertificate:
    public_pem = _read_text(certificate_path, '证书')
    private_key_pem = _read_text(private_key_path, '私钥')
    minimum_valid_seconds = minimum_valid_days * 24 * 60 * 60

    command_runner(
        (
            'openssl',
            'x509',
            '-in',
            str(certificate_path),
            '-noout',
            '-checkhost',
            domain,
        )
    )
    command_runner(
        (
            'openssl',
            'x509',
            '-in',
            str(certificate_path),
            '-noout',
            '-checkend',
            str(minimum_valid_seconds),
        )
    )
    certificate_public_key = command_runner(
        ('openssl', 'x509', '-in', str(certificate_path), '-pubkey', '-noout')
    )
    private_public_key = command_runner(
        ('openssl', 'pkey', '-in', str(private_key_path), '-pubout', '-passin', 'pass:')
    )
    if (
        hashlib.sha256(certificate_public_key.strip()).digest()
        != hashlib.sha256(private_public_key.strip()).digest()
    ):
        raise CertificateSyncError('证书和私钥不匹配')

    return LocalCertificate(
        public_pem=public_pem,
        private_key_pem=private_key_pem,
        fingerprint=certificate_fingerprint(public_pem),
    )


def remote_certificate_matches(
    remote: RemoteCertificate, local_fingerprint: str
) -> bool:
    if not remote.enabled or not remote.public_pem:
        return False
    try:
        return certificate_fingerprint(remote.public_pem) == local_fingerprint
    except CertificateSyncError:
        return False


def certificate_name(domain: str, fingerprint: str) -> str:
    safe_domain = re.sub(r'[^a-z0-9]+', '-', domain.casefold()).strip('-')
    return '{}-{}'.format(safe_domain, fingerprint[:12])


def synchronize_certificate(
    client: CdnCertificateClient,
    domain: str,
    local: LocalCertificate,
    poll_attempts: int,
    poll_interval: float,
    sleeper: Sleeper = time.sleep,
) -> bool:
    remote = client.describe(domain)
    if remote_certificate_matches(remote, local.fingerprint):
        return False

    client.upload(
        domain,
        certificate_name(domain, local.fingerprint),
        local.public_pem,
        local.private_key_pem,
    )
    for attempt in range(poll_attempts):
        remote = client.describe(domain)
        if remote_certificate_matches(remote, local.fingerprint):
            return True
        if attempt + 1 < poll_attempts:
            sleeper(poll_interval)

    raise CertificateSyncError('阿里云 CDN 未在等待时间内返回新证书状态')


class AliyunCdnCertificateClient:
    def __init__(
        self, access_key_id: str, access_key_secret: str, endpoint: str, region_id: str
    ) -> None:
        try:
            from alibabacloud_cdn20180510.client import Client
            from alibabacloud_tea_openapi.models import Config
        except ImportError as exc:
            raise CertificateSyncError(
                '缺少阿里云 CDN SDK，请先安装 deploy/aliyun/requirements.txt'
            ) from exc

        config = Config(
            access_key_id=access_key_id,
            access_key_secret=access_key_secret,
            endpoint=endpoint,
            region_id=region_id,
        )
        self._client = Client(config)

    def describe(self, domain: str) -> RemoteCertificate:
        from alibabacloud_cdn20180510 import models

        request = models.DescribeDomainCertificateInfoRequest(domain_name=domain)
        response = self._client.describe_domain_certificate_info(request)
        cert_infos = getattr(
            getattr(response.body, 'cert_infos', None), 'cert_info', None
        )
        infos = cert_infos or ()
        selected = next(
            (item for item in infos if getattr(item, 'domain_name', None) == domain),
            infos[0] if infos else None,
        )
        if selected is None:
            return RemoteCertificate(public_pem=None, enabled=False)
        return RemoteCertificate(
            public_pem=getattr(selected, 'server_certificate', None),
            enabled=(
                str(getattr(selected, 'server_certificate_status', '')).casefold()
                == 'on'
            ),
        )

    def upload(
        self, domain: str, certificate_name: str, public_pem: str, private_key_pem: str
    ) -> None:
        from alibabacloud_cdn20180510 import models

        request = models.SetCdnDomainSSLCertificateRequest(
            cert_name=certificate_name,
            cert_type='upload',
            domain_name=domain,
            sslpri=private_key_pem,
            sslprotocol='on',
            sslpub=public_pem,
        )
        self._client.set_cdn_domain_sslcertificate(request)


def _positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError('必须大于 0')
    return parsed


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='将 Nginx UI 续签证书幂等同步到阿里云 CDN'
    )
    parser.add_argument('--domain', required=True)
    parser.add_argument('--certificate', required=True, type=Path)
    parser.add_argument('--private-key', required=True, type=Path)
    parser.add_argument('--minimum-valid-days', type=_positive_integer, default=14)
    parser.add_argument('--poll-attempts', type=_positive_integer, default=18)
    parser.add_argument('--poll-interval', type=_positive_integer, default=10)
    parser.add_argument('--endpoint', default='cdn.aliyuncs.com')
    parser.add_argument('--region-id', default='cn-hangzhou')
    return parser.parse_args()


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise CertificateSyncError('缺少环境变量 {}'.format(name))
    return value


def _safe_cloud_error(exc: Exception) -> str:
    code = getattr(exc, 'code', None) or type(exc).__name__
    request_id = getattr(exc, 'request_id', None)
    if request_id:
        return '阿里云 CDN 请求失败：code={} request_id={}'.format(code, request_id)
    return '阿里云 CDN 请求失败：code={}'.format(code)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    args = _parse_args()
    try:
        local = load_local_certificate(
            args.certificate, args.private_key, args.domain, args.minimum_valid_days
        )
        client = AliyunCdnCertificateClient(
            _required_environment('ALIBABA_CLOUD_ACCESS_KEY_ID'),
            _required_environment('ALIBABA_CLOUD_ACCESS_KEY_SECRET'),
            args.endpoint,
            args.region_id,
        )
        changed = synchronize_certificate(
            client, args.domain, local, args.poll_attempts, args.poll_interval
        )
    except CertificateSyncError as exc:
        LOGGER.error('%s', exc)
        return 1
    except Exception as exc:
        LOGGER.error('%s', _safe_cloud_error(exc))
        return 1

    action = 'updated' if changed else 'already-current'
    LOGGER.info(
        'domain=%s certificate=%s fingerprint=%s',
        args.domain,
        action,
        local.fingerprint[:12],
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
