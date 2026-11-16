"""Write Buildx registry credentials without placing passwords in argv or logs.

Required environment: REGISTRY, REGISTRY_USERNAME, REGISTRY_PASSWORD.
DOCKER_CONFIG selects an isolated directory (normally under RUNNER_TEMP).
The build's BuildKit configuration controls registry transport, not this file.
"""
import base64
import json
import os
import tempfile
from pathlib import Path


def main():
    registry, username, password = (os.environ.get(k, '') for k in
                                    ('REGISTRY', 'REGISTRY_USERNAME', 'REGISTRY_PASSWORD'))
    if not all((registry, username, password)):
        raise SystemExit('Registry address and both registry credentials are required.')
    if '/' in registry or any(c.isspace() for c in registry) or ':' in username:
        raise SystemExit('Use a registry host[:port] and a username without colons.')
    directory = Path(os.environ['DOCKER_CONFIG'])
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / 'config.json'
    data = json.loads(path.read_text()) if path.exists() else {}
    data.setdefault('auths', {})[registry] = {
        'auth': base64.b64encode(f'{username}:{password}'.encode()).decode()
    }
    # Never truncate an existing potentially world-readable config in place.
    # mkstemp creates a private file; replacement is atomic and preserves the
    # old config if serialization or writing fails.
    fd, temporary = tempfile.mkstemp(prefix='.registry-auth-', dir=directory)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


if __name__ == '__main__':
    main()
