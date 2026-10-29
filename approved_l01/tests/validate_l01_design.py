#!/usr/bin/env python3
"""Offline L01 design checks. No browser calls and no production code imports.

Schema/time/epoch models below are test oracles, NOT the CDPD implementation.
Optional --plan-root verifies the original source ZIPs without extracting them.
This script never writes or updates the Candidate directory.
FIX001: verify format-checker capabilities before running design checks;
install requirements-validation.txt to reproduce the tested environment.
"""
from __future__ import annotations
import argparse
import copy
from datetime import datetime, timedelta, timezone
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit
import zipfile

ROOT = Path(__file__).resolve().parents[1]
RESULTS: list[dict[str, object]] = []


def load(path: Path) -> object:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ValueError('DUPLICATE_JSON_KEY')
            result[key] = value
        return result
    def bad_constant(value: str) -> object:
        raise ValueError('NON_FINITE_JSON')
    return json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=pairs, parse_constant=bad_constant)


def check(name: str, condition: bool) -> None:
    RESULTS.append({'check_id': name, 'status': 'PASS' if condition else 'FAIL'})
    print(('PASS ' if condition else 'FAIL ') + name)


def rejects(callable_, *args: object) -> bool:
    try:
        callable_(*args)
    except (ValueError, TypeError, KeyError):
        return True
    return False


def epoch_model(endpoint: str, ws_url: str) -> str:
    """Specification model: derive epoch only from the permitted raw WS path."""
    if endpoint != 'http://127.0.0.1:9222':
        raise ValueError('ENDPOINT_NOT_ALLOWED')
    # Full-match also rejects credentials, case changes, query, whitespace, and escapes.
    if not re.fullmatch(r'ws://127\.0\.0\.1:9222/devtools/browser/[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}', ws_url):
        raise ValueError('BROWSER_EPOCH_UNVERIFIED')
    return hashlib.sha256(urlsplit(ws_url).path.encode('utf-8')).hexdigest()[:32]


def lease_model(record: dict, now: datetime, active_id: str, revoked: bool, monotonic_expired: bool = False, clock_jump: bool = False, keeper_available: bool = True) -> None:
    """Specification model, after JSON Schema validation. No wall-clock reads."""
    issued = datetime.strptime(record['issued_at_utc'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
    expires = datetime.strptime(record['expires_at_utc'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
    if expires - issued != timedelta(seconds=14400):
        raise ValueError('INVALID_LEASE_DURATION')
    if not issued <= now < expires or monotonic_expired:
        raise ValueError('BINDING_EXPIRED_OR_NOT_YET_VALID')
    if revoked or active_id != record['binding_id']:
        raise ValueError('BINDING_REVOKED')
    if clock_jump:
        raise ValueError('CLOCK_DISCONTINUITY')
    if not keeper_available:
        raise ValueError('BINDING_STATE_UNVERIFIED')


def blocked_environment(reason_code: str, message: str) -> int:
    """Stop before design checks when required validation cannot be performed."""
    print('BLOCKED: ' + message, file=sys.stderr)
    print(json.dumps({
        'scope': 'DESIGN_STATIC_SELF_VERIFICATION_NOT_RUNTIME',
        'status': 'BLOCKED', 'reason_code': reason_code,
        'checks': 0, 'passed': 0, 'failed': 0,
        'runtime_tests_executed': 0, 'gate_approved': False,
    }, ensure_ascii=False))
    return 2


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--plan-root', type=Path, help='Original CDPD plan root containing SOURCE_LOCK.json and sources/.')
    args = ap.parse_args()
    RESULTS.clear()
    try:
        from jsonschema import Draft202012Validator, FormatChecker
    except ImportError:
        return blocked_environment(
            'JSONSCHEMA_IMPORT_UNAVAILABLE',
            'jsonschema could not be imported. Install requirements-validation.txt '
            'with the same Python interpreter.',
        )
    checker = FormatChecker()
    required_formats = ('date-time', 'uuid')
    available = {name: name in checker.checkers for name in required_formats}
    packages = {}
    for name in ('jsonschema', 'rfc3339-validator'):
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = None
    print('ENV ' + json.dumps({
        'python': sys.version.split()[0], 'platform': sys.platform,
        'packages': packages, 'format_checkers': available,
    }, ensure_ascii=True))
    missing = [name for name, present in available.items() if not present]
    if missing:
        return blocked_environment(
            'REQUIRED_FORMAT_CHECKER_UNAVAILABLE',
            'Required format checker(s) unavailable: ' + ', '.join(missing)
            + '. Install requirements-validation.txt with the same Python '
            'interpreter. A missing checker must not be treated as validation.',
        )
    # These are environment probes, not replacements for the negative Schema tests.
    probes = (
        ('date-time', '2026-02-28T12:00:00Z', True),
        ('date-time', '2026-02-30T12:00:00Z', False),
        ('uuid', '11111111-1111-4111-8111-111111111111', True),
        ('uuid', 'not-a-uuid', False),
    )
    for name, value, expected in probes:
        if checker.conforms(value, name) is not expected:
            return blocked_environment(
                'REQUIRED_FORMAT_CHECKER_SELFTEST_FAILED',
                'Installed format checker failed its positive/negative probe: '
                + name + '. Reinstall requirements-validation.txt; do not skip '
                'SCHEMA-19 or weaken the schema.',
            )
    schema = load(ROOT / 'contracts/chatgpt_target_binding_v1.schema.json')
    Draft202012Validator.check_schema(schema)
    check('SCHEMA-00-Draft202012-valid', True)
    validator = Draft202012Validator(schema, format_checker=checker)
    sample = load(ROOT / 'tests/fixtures/binding_valid.json')
    check('SCHEMA-01-positive-example', validator.is_valid(sample))

    def variant(name: str, mutate) -> None:
        value = copy.deepcopy(sample)
        mutate(value)
        check(name, not validator.is_valid(value))

    variant('SCHEMA-02-no-epoch', lambda x: x['target'].pop('expected_browser_id'))
    variant('SCHEMA-03-null-epoch', lambda x: x['target'].__setitem__('expected_browser_id', None))
    variant('SCHEMA-04-uppercase-epoch', lambda x: x['target'].__setitem__('expected_browser_id', 'A'*32))
    variant('SCHEMA-05-short-epoch', lambda x: x['target'].__setitem__('expected_browser_id', 'a'*31))
    variant('SCHEMA-06-extra-enveloped-field', lambda x: x.__setitem__('approved', True))
    variant('SCHEMA-07-extra-target-field', lambda x: x['target'].__setitem__('fallback', 'last'))
    variant('SCHEMA-08-empty-target', lambda x: x['target'].__setitem__('target_id', ''))
    variant('SCHEMA-09-target-control', lambda x: x['target'].__setitem__('target_id', 'A\n'))
    variant('SCHEMA-10-target-space', lambda x: x['target'].__setitem__('target_id', ' A'))
    variant('SCHEMA-11-target-too-long', lambda x: x['target'].__setitem__('target_id', 'a'*257))
    variant('SCHEMA-12-other-origin', lambda x: x['target'].__setitem__('expected_origin', 'https://example.test'))
    variant('SCHEMA-13-www-origin', lambda x: x['target'].__setitem__('expected_origin', 'https://www.chatgpt.com'))
    variant('SCHEMA-14-localhost', lambda x: x['target'].__setitem__('endpoint', 'http://localhost:9222'))
    variant('SCHEMA-15-other-port', lambda x: x['target'].__setitem__('endpoint', 'http://127.0.0.1:9223'))
    variant('SCHEMA-16-future-schema', lambda x: x.__setitem__('schema', 'cdpd.chatgpt-target-binding/v2'))
    variant('SCHEMA-17-unknown-algorithm', lambda x: x.__setitem__('epoch_algorithm', 'pid/v1'))
    variant('SCHEMA-18-invalid-uuid', lambda x: x.__setitem__('binding_id', 'caller-run-id'))
    variant('SCHEMA-19-invalid-date', lambda x: x.__setitem__('issued_at_utc', '2026-02-30T12:00:00Z'))
    variant('SCHEMA-20-timezone-not-Z', lambda x: x.__setitem__('issued_at_utc', '2026-10-17T12:00:00+00:00'))
    variant('SCHEMA-21-test-only-not-allowed', lambda x: x.__setitem__('test_only', True))
    variant('SCHEMA-22-epoch-newline', lambda x: x['target'].__setitem__('expected_browser_id', 'a'*32+'\n'))

    now = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
    check('LEASE-01-start-inclusive', not rejects(lease_model, sample, now, sample['binding_id'], False))
    check('LEASE-02-before-expiry', not rejects(lease_model, sample, now+timedelta(seconds=14399), sample['binding_id'], False))
    check('LEASE-03-expiry-exclusive', rejects(lease_model, sample, now+timedelta(seconds=14400), sample['binding_id'], False))
    check('LEASE-04-before-issued', rejects(lease_model, sample, now-timedelta(seconds=1), sample['binding_id'], False))
    check('LEASE-05-revoked', rejects(lease_model, sample, now, sample['binding_id'], True))
    check('LEASE-06-superseded', rejects(lease_model, sample, now, 'different-id', False))
    check('LEASE-07-monotonic-expired', rejects(lease_model, sample, now, sample['binding_id'], False, True))
    check('LEASE-08-clock-discontinuity', rejects(lease_model, sample, now, sample['binding_id'], False, False, True))
    check('LEASE-10-no-clock-keeper', rejects(lease_model, sample, now, sample['binding_id'], False, False, False, False))
    bad_duration = copy.deepcopy(sample)
    bad_duration['expires_at_utc'] = '2026-10-17T17:00:00Z'
    check('LEASE-09-schema-cannot-prove-duration', validator.is_valid(bad_duration) and rejects(lease_model, bad_duration, now, sample['binding_id'], False))

    endpoint = 'http://127.0.0.1:9222'
    prefix = 'ws://127.0.0.1:9222/devtools/browser/'
    wsa = prefix+'11111111-1111-4111-8111-111111111111'
    wsb = prefix+'33333333-3333-4333-8333-333333333333'
    check('EPOCH-01-golden-vector', epoch_model(endpoint, wsa) == sample['target']['expected_browser_id'])
    check('EPOCH-02-independent-same-path', epoch_model(endpoint, wsa) == epoch_model(endpoint, wsa))
    check('EPOCH-03-new-guid-different', epoch_model(endpoint, wsa) != epoch_model(endpoint, wsb))
    invalid_ws = [prefix[:-1], prefix, prefix+'not-a-uuid', wsa+'?token=x', wsa+'#x', wsa+'/', wsa.replace('9222','9223'), wsa.replace('127.0.0.1','localhost'), wsa.replace('ws://','wss://'), wsa.replace('/browser/','/page/'), wsa.replace('ws://','ws://user@'), wsa.replace('/devtools/','/%64evtools/'), wsa+'\n']
    for i, value in enumerate(invalid_ws, 1):
        check(f'EPOCH-NEG-{i:02d}', rejects(epoch_model, endpoint, value))
    check('EPOCH-04-endpoint-checked-before-hash', rejects(epoch_model, 'http://localhost:9222', wsa))

    contract = load(ROOT / 'tests/fixtures/state_contract.json')
    states = set(contract['states'])
    transitions = contract['transitions']
    keys = [(x['from'], x['event']) for x in transitions]
    check('STATE-01-unique-state-event-pairs', len(keys) == len(set(keys)))
    check('STATE-02-all-states-resolve', all(x['from'] in states and x['to'] in states for x in transitions))
    check('STATE-03-preflight-is-only-new-binding-path', all(x['event']=='EXPLICIT_PREFLIGHT' for x in transitions if x['to']=='PREFLIGHT'))
    check('STATE-04-invalidated-has-no-auto-rebind', all(x['event']=='EXPLICIT_PREFLIGHT' for x in transitions if x['from'] in {'INVALIDATED','EXPIRED','RELEASED'}))
    check('STATE-05-guard-default-deny', contract['default_transition']=='DENY' and all(x['guard'] and x['effect'] for x in transitions))
    check('STATE-06-owner-crash-no-new-identity', any(x['event']=='OWNER_CRASH' and x['from']==x['to']=='BOUND' and 'DIAGNOSTIC_ONLY' in x['effect'] for x in transitions))
    check('STATE-07-cdp-disconnect-invalidates', any(x['event']=='CDP_DISCONNECTED' and x['to']=='INVALIDATED' for x in transitions))

    priority = contract['priority_rules']
    check('STATE-08-priority-scopes-resolve', all(set(x['from']) <= states and x['to'] in states for x in priority))
    check('STATE-09-navigation-interrupts-defined', any(set(x['from'])=={'BOUND','NAVIGATING'} and x['to']=='INVALIDATED' and {'CDP_DISCONNECTED','BROWSER_EPOCH_CHANGED','ORIGIN_CHANGED'} <= set(x['events']) for x in priority))

    cases = load(ROOT / 'tests/fixtures/runtime_test_cases.json')['tests']
    ids = [x['test_id'] for x in cases]
    check('TEST-01-unique-runtime-definition-ids', len(ids)==len(set(ids))==36)
    check('TEST-02-required-TGT01-05-preserved', {f'TGT-{i:02d}' for i in range(1,6)} <= set(ids))
    check('TEST-03-all-runtime-tests-explicitly-NOT_RUN', all(x['runtime_status']=='NOT_RUN' and x['actual'] is None and x['command'] is None and x['exit_code'] is None and x['log_ref'] is None for x in cases))
    check('TEST-04-all-definitions-have-oracles-and-owners', all(x['preconditions'] and x['stimulus'] and x['expected'] and x['implementation_owners'] for x in cases))
    acs = set().union(*(set(x['acceptance_criteria']) for x in cases))
    check('TEST-05-all-ACs-covered-in-test-definitions', acs=={'AC01','AC02','AC03','AC04'})

    inspections = load(ROOT / 'evidence/source_inspection.json')
    check('SOURCE-STATIC-01-callsite-counts', {k:len(v) for k,v in inspections['call_sites'].items()} == {'bind_chatgpt_page(':14, '._bound_page(':23, 'bound.ops.':26})
    check('SOURCE-STATIC-02-8-implicit-bind-modules', len({x['path'] for x in inspections['call_sites']['bind_chatgpt_page(']})==8)
    f = next(x for x in inspections['files'] if x['source_id']=='api')
    fac = next(x for x in f['symbols'] if x['name']=='create_facade')
    check('SOURCE-STATIC-03-factory-signature-recorded', 'session: ChatGPTSession | None' in fac['signature'] and fac['returns']=='ChatGPTWebFacadeV2')
    check('SOURCE-STATIC-04-all-source-hashes-recorded', all(re.fullmatch('[0-9a-f]{64}', x['sha256']) and x['lines']>0 for x in inspections['files']))

    broken = []
    for path in ROOT.rglob('*.md'):
        for m in re.finditer(r'\[[^\]\n]*\]\(([^)]+)\)', path.read_text(encoding='utf-8')):
            target = m.group(1).split('#')[0]
            if target and '://' not in target and not (path.parent/target).is_file():
                broken.append(str(path.relative_to(ROOT))+':'+target)
    check('DOC-01-all-local-links-resolve', not broken)
    if broken:
        print('\n'.join(broken))
    for path in ['design/01_Target_Binding_Detail.md','design/01_Target_State_Machine.md','tests/01_Target_Test_Cases.md']:
        text = (ROOT/path).read_text(encoding='utf-8')
        check('DOC-frontmatter-'+Path(path).stem, text.startswith('---\n') and 'ticket: CDPD-L01\n' in text and 'status: CANDIDATE\n' in text and 'role: EXECUTION\n' in text)
    check('AUTHORITY-01-no-production-packages', not (ROOT/'packages').exists())
    check('AUTHORITY-02-no-gate-decision-file', not (ROOT/'gates').exists())

    if args.plan_root:
        root = args.plan_root.resolve()
        original_lock_path = root/'SOURCE_LOCK.json'
        if not original_lock_path.is_file():
            print('BLOCKED: supplied plan root has no SOURCE_LOCK.json', file=sys.stderr)
            return 2
        check('LIVE-SOURCE-01-lock-bytes', original_lock_path.read_bytes()==(ROOT/'evidence/SOURCE_LOCK.input.json').read_bytes())
        lock = load(original_lock_path)
        for key, meta in lock['sources'].items():
            path = root/'sources'/meta['filename']
            if not path.is_file():
                print(f'BLOCKED: missing source ZIP: {path}', file=sys.stderr)
                return 2
            check('LIVE-SOURCE-hash-'+key, path.stat().st_size==meta['bytes'] and hashlib.sha256(path.read_bytes()).hexdigest()==meta['sha256'])
            with zipfile.ZipFile(path) as archive:
                check('LIVE-SOURCE-ZIP-CRC-'+key, archive.testzip() is None)
                if key=='cdpd_v021':
                    for entry in inspections['files']:
                        name = 'CDPD_v0.2.1/'+entry['path']
                        check('LIVE-SOURCE-file-'+entry['source_id'], hashlib.sha256(archive.read(name)).hexdigest()==entry['sha256'])
    else:
        print('NOT_RUN optional source ZIP recheck: --plan-root not supplied (recorded source checks are evidence only).')
    manifest = ROOT/'MANIFEST_SHA256.txt'
    if manifest.is_file():
        entries = {}
        for line in manifest.read_text(encoding='utf-8').splitlines():
            digest, name = line.split('  ',1)
            if name in entries or not re.fullmatch('[0-9a-f]{64}',digest):
                raise ValueError('INVALID_MANIFEST')
            entries[name]=digest
        actual_paths={str(p.relative_to(ROOT)).replace('\\','/') for p in ROOT.rglob('*') if p.is_file() and p!=manifest and '__pycache__' not in p.parts}
        check('PACKAGE-01-manifest-complete', set(entries)==actual_paths)
        check('PACKAGE-02-all-hashes-match', all((ROOT/name).is_file() and hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==digest for name,digest in entries.items()))
    else:
        print('NOT_RUN package manifest check: manifest not yet generated.')
    fail = sum(x['status']=='FAIL' for x in RESULTS)
    print(json.dumps({'scope':'DESIGN_STATIC_SELF_VERIFICATION_NOT_RUNTIME','status':'PASS' if not fail else 'FAIL','checks':len(RESULTS),'passed':len(RESULTS)-fail,'failed':fail,'runtime_tests_executed':0,'gate_approved':False}, ensure_ascii=False))
    return 1 if fail else 0


if __name__=='__main__':
    try:
        raise SystemExit(main())
    except (ValueError, TypeError, KeyError, OSError, zipfile.BadZipFile) as exc:
        print(f'FAIL: design validation stopped: {type(exc).__name__}: {exc}', file=sys.stderr)
        raise SystemExit(1)
