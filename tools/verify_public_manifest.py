#!/usr/bin/env python3
"""Verify public-export source manifest. Not a STATE007 source-admission gate."""
from pathlib import Path
import argparse
import hashlib
import re
import sys

def verify(root:Path):
    manifest=root/'MANIFEST_SHA256.txt'
    if not manifest.is_file():raise ValueError('Missing MANIFEST_SHA256.txt')
    recorded={}
    for line in manifest.read_text(encoding='utf-8').splitlines():
        found=re.fullmatch(r'([0-9a-f]{64})  (.+)',line)
        if not found:raise ValueError('Invalid manifest row')
        digest,name=found.groups()
        if name in recorded or '\\' in name or name.startswith('/') or any(x in ('','.', '..') for x in name.split('/')):
            raise ValueError('Unsafe or duplicate name: '+name)
        recorded[name]=digest
    observed=set()
    for file in root.rglob('*'):
        rel=file.relative_to(root).as_posix()
        if any(part in {'.git','__pycache__','.pytest_cache','.venv'} for part in file.relative_to(root).parts):continue
        if file.is_symlink():raise ValueError('Symlink not permitted: '+rel)
        if not file.is_file():continue
        if rel=='MANIFEST_SHA256.txt':continue
        observed.add(rel)
        if rel not in recorded:raise ValueError('Unexpected file (not original export): '+rel)
        digest=hashlib.sha256(file.read_bytes()).hexdigest()
        if digest!=recorded[rel]:raise ValueError('Digest mismatch: '+rel)
    if observed!=set(recorded):raise ValueError('Missing manifest files: '+str(sorted(set(recorded)-observed)[:10]))
    return len(observed)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parent.parent)
    opts=parser.parse_args()
    try:
        count=verify(opts.root.resolve())
    except (OSError,ValueError) as e:
        print('PUBLIC_MANIFEST_FAIL:',e,file=sys.stderr)
        return 1
    print('PUBLIC_MANIFEST_PASS',count,'payload files')
    return 0

if __name__=='__main__':raise SystemExit(main())
