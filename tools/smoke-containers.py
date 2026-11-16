"""Smoke-test built images without production credentials, networks or published ports.

Run where Docker's Linux engine is available:
python tools/smoke-containers.py --portal-image IMAGE --sync-image IMAGE
"""
import argparse
import subprocess


PORTAL = r'''
import os, secrets, subprocess, tempfile, time, urllib.request, urllib.error
assert os.getuid() == 10001, 'portal must run as UID 10001'
os.environ.update(DJANGO_SECRET_KEY=secrets.token_urlsafe(48),
                  DJANGO_ALLOWED_HOSTS='localhost,127.0.0.1', GUNICORN_WORKERS='2')
with tempfile.TemporaryFile() as log:
    process = subprocess.Popen(['bash', '/app/entrypoint.sh'], stdout=log, stderr=log)
    try:
        for attempt in range(100):
            if process.poll() is not None:
                log.seek(0); raise RuntimeError(log.read().decode())
            try:
                with urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=1) as response:
                    assert response.status == 200
                break
            except (OSError, urllib.error.URLError):
                time.sleep(0.2)
        else:
            log.seek(0); raise RuntimeError('Startup timed out: ' + log.read().decode())
        print('PASS: non-root portal migrations, gunicorn startup and health probe')
    finally:
        process.terminate()
        try: process.wait(timeout=10)
        except subprocess.TimeoutExpired: process.kill(); process.wait()
'''

AUTHORITY = r'''
import os, secrets, subprocess, tempfile, time, ssl, urllib.request, urllib.error
from pathlib import Path
from datetime import datetime, timedelta, timezone
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
assert os.getuid() == 10001
key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, 'localhost')])
now = datetime.now(timezone.utc)
cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now-timedelta(minutes=1)).not_valid_after(now+timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost')]), critical=False)
        .sign(key, hashes.SHA256()))
Path('/etc/hub-tls/tls.crt').write_bytes(cert.public_bytes(serialization.Encoding.PEM))
Path('/etc/hub-tls/tls.key').write_bytes(key.private_bytes(serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
Path('/etc/hub-tls/tls.key').chmod(0o600)
os.environ.update(PORTAL_ROLE='hub-authority', DJANGO_ROOT_URLCONF='hubauthority.control_urls',
                  DJANGO_SECRET_KEY=secrets.token_urlsafe(48), DJANGO_ALLOWED_HOSTS='localhost')
context = ssl.create_default_context(cafile='/etc/hub-tls/tls.crt')
with tempfile.TemporaryFile() as log:
    process = subprocess.Popen(['bash','/app/entrypoint.sh'], stdout=log, stderr=log)
    try:
        for attempt in range(100):
            if process.poll() is not None:
                log.seek(0); raise RuntimeError(log.read().decode())
            try:
                with urllib.request.urlopen('https://localhost:8443/healthz', context=context, timeout=1) as response:
                    assert response.status == 200
                break
            except (OSError, urllib.error.URLError): time.sleep(0.2)
        else:
            log.seek(0); raise RuntimeError('TLS startup timed out: '+log.read().decode())
        print('PASS: non-root authority startup and certificate-verified TLS health probe')
    finally:
        process.terminate()
        try: process.wait(timeout=10)
        except subprocess.TimeoutExpired: process.kill(); process.wait()
'''

SYNC = r'''
import os, subprocess
from identifiers import oracle_identifier
assert os.getuid() == 10001, 'sync must run as UID 10001'
assert oracle_identifier('DJANGO_MIGRATIONS') == '"DJANGO_MIGRATIONS"'
subprocess.run(['python', '/sync.py', '--help'], check=True, stdout=subprocess.DEVNULL)
print('PASS: non-root sync image imports drivers and starts its CLI')
'''


def run(image, code, *options):
    subprocess.run(['docker', 'run', '--rm', '-i', '--network', 'none',
                    '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                    *options, '--entrypoint', 'python', image, '-'],
                   input=code.encode(), check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--portal-image', required=True)
    parser.add_argument('--sync-image', required=True)
    args = parser.parse_args()
    run(args.portal_image, PORTAL)
    run(args.portal_image, AUTHORITY, '--tmpfs', '/etc/hub-tls:rw,mode=1777')
    run(args.sync_image, SYNC, '--read-only', '--tmpfs', '/tmp')
    for command in ('sync_website', 'sync_operations'):
        run(args.portal_image,
            "import subprocess; subprocess.run(['python','manage.py','"+command+"','--help'],check=True,stdout=subprocess.DEVNULL)",
            '--read-only', '--tmpfs', '/tmp')
    print('PASS: worker commands initialize with a read-only root filesystem')


if __name__ == '__main__':
    main()
