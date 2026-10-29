"""Write-once evidence with a final commit marker. No host-workflow state."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + '\n').encode('utf-8')


def _write_new(path: Path, content: bytes) -> None:
    with path.open('xb') as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())


class EvidenceStore:
    def __init__(self, root: str | Path, max_bytes: int = 10 * 1024 * 1024) -> None:
        self.root = Path(root).resolve()
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 4096:
            raise ValueError('max_bytes must be >=4096')
        self.max_bytes = max_bytes

    def commit(self, files: dict[str, Any]) -> Path:
        if not files or set(files) - {'TRIGGER.json', 'RESULT.json', 'STRUCTURE.json', 'EVENTS.json'}:
            raise ValueError('unsupported evidence files')
        encoded = {name: json_bytes(payload) for name, payload in files.items()}
        if sum(map(len, encoded.values())) > self.max_bytes:
            raise ValueError('EVIDENCE_BUDGET_EXCEEDED')
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Exclusive directory reservation; external/correlation IDs never form paths.
        for _ in range(8):
            destination = self.root / ('C-' + uuid4().hex)
            try:
                destination.mkdir(mode=0o700)
                break
            except FileExistsError:
                continue
        else:
            raise FileExistsError('capture ID collision')
        entries = []
        for name, data in encoded.items():
            _write_new(destination / name, data)
            entries.append({'path': name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
        manifest = json_bytes({'schema': 'cdpd.bundle-manifest/v1', 'capture_id': destination.name, 'files': entries})
        _write_new(destination / 'MANIFEST.json', manifest)
        # COMMIT is the only publication point. A directory without it is never valid.
        _write_new(destination / 'COMMIT.json', json_bytes({'schema': 'cdpd.commit/v1', 'manifest_sha256': hashlib.sha256(manifest).hexdigest()}))
        if os.name != 'nt':
            fd = os.open(destination, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        return destination


def verify_bundle(path: str | Path) -> dict[str, Any]:
    root = Path(path)
    try:
        if root.is_symlink() or not root.is_dir():
            raise ValueError('invalid directory')
        for p in root.iterdir():
            if p.is_symlink() or not p.is_file():
                raise ValueError('unexpected entry')
        manifest_raw = (root / 'MANIFEST.json').read_bytes()
        manifest = json.loads(manifest_raw)
        commit = json.loads((root / 'COMMIT.json').read_bytes())
        if commit.get('schema') != 'cdpd.commit/v1' or commit.get('manifest_sha256') != hashlib.sha256(manifest_raw).hexdigest():
            raise ValueError('commit mismatch')
        if manifest.get('schema') != 'cdpd.bundle-manifest/v1' or manifest.get('capture_id') != root.name:
            raise ValueError('manifest mismatch')
        paths = []
        for entry in manifest['files']:
            name = entry['path']
            if name not in {'TRIGGER.json', 'RESULT.json', 'STRUCTURE.json', 'EVENTS.json'} or name in paths:
                raise ValueError('invalid/duplicate file path')
            paths.append(name)
            data = (root / name).read_bytes()
            if len(data) != entry['bytes'] or hashlib.sha256(data).hexdigest() != entry['sha256']:
                raise ValueError('file mismatch')
            json.loads(data)
        if {'TRIGGER.json', 'RESULT.json'} - set(paths):
            raise ValueError('required files missing')
        if {p.name for p in root.iterdir()} != set(paths) | {'MANIFEST.json', 'COMMIT.json'}:
            raise ValueError('unexpected file')
        return {'schema': 'cdpd.integrity-result/v1', 'integrity_valid': True, 'file_count': len(paths), 'assessment_status': 'NOT_PERFORMED'}
    except (OSError, ValueError, KeyError, TypeError):
        return {'schema': 'cdpd.integrity-result/v1', 'integrity_valid': False, 'failure_code': 'BUNDLE_INVALID', 'assessment_status': 'NOT_PERFORMED'}
