"""L06 production persistence/frontend controls; all data is synthetic.

These are Execution controls. They do not establish browser observation,
Windows native durability, live permits, Human approval or L08 acceptance.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from cdpd_chatgpt_profile import bundle_validation as bv
from cdpd_chatgpt_profile import evidence_store as es


ROOT = Path(__file__).resolve().parents[1]


def _good() -> dict:
    return json.loads((ROOT / 'tests/fixtures/l06_profile_complete.json').read_bytes())


def _id(files: dict) -> str:
    return files['RESULT.json']['profile']['capture_id']


def _pack_unchecked(files: dict, capture_id: str | None = None) -> dict[str, bytes]:
    """Author deliberately invalid bytes while keeping their hashes honest."""
    raw = {name: bv.canonical(value) for name, value in files.items()}
    raw['MANIFEST.json'] = bv.canonical({
        'schema': 'cdpd.bundle-manifest/v1', 'capture_id': capture_id or _id(files),
        'files': [{'path': name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
                  for name, data in raw.items()],
    })
    raw['COMMIT.json'] = bv.canonical({'schema': 'cdpd.commit/v1',
                                     'manifest_sha256': hashlib.sha256(raw['MANIFEST.json']).hexdigest()})
    return raw


def _rejected(reason=None) -> dict:
    files = _good()
    files.pop('STRUCTURE.json')
    result = files['RESULT.json']
    result.update(capture_status='REJECTED', failure_code='POLICY_DENIED')
    profile = result['profile']
    profile.update(policy_allowed=False, coverage_complete=False, worker_termination='NOT_STARTED')
    profile['timing']['capture_started_at_utc'] = None
    for stage in ('S1', 'S2'):
        profile['stages'][stage] = {'status': 'NOT_RUN', 'reason_code': reason, 'duration_ms': 0}
    return files


def _history_partial() -> dict:
    files = _good()
    result = files['RESULT.json']
    result.update(capture_status='PARTIAL', failure_code='HISTORY_INCOMPLETE')
    profile = result['profile']
    profile['history_coverage'].update(complete=False, loss_count_exact=False)
    profile['trigger_envelope']['profile']['loss_count_exact'] = False
    return files


def test_valid_full_profile_round_trip_and_capture_id(tmp_path):
    files = _good()
    before = copy.deepcopy(files)
    blob = es.encode_profile_bundle(files, _id(files))
    digest, verified = bv.verify_report_bundle(blob)
    path = es.ProfileEvidenceStore(tmp_path / 'bundles').commit(files)
    result = es.verify_profile_bundle(path)
    assert result['integrity_valid'] and result['profile_valid']
    assert result['publication_state'] == 'COMMITTED_VALID'
    assert result['manifest_sha256'] == digest
    assert result['assessment_status'] == 'NOT_PERFORMED'
    assert es.verify_bundle(path)['integrity_valid']
    assert verified == files == before
    assert {p.name for p in path.iterdir()} == set(blob)


@pytest.mark.parametrize('reason', [None, 'NONE', 'POLICY_DENIED', 'NOT_RUN'])
def test_n01_legacy_metadata_reason_compatibility_no_success_inference(tmp_path, reason):
    files = _rejected(reason)
    before = copy.deepcopy(files)
    blob = es.encode_profile_bundle(files, _id(files))
    _, payload = bv.verify_report_bundle(blob)
    assert payload['RESULT.json']['capture_status'] == 'REJECTED'
    assert payload['RESULT.json']['profile']['stages']['S1']['status'] == 'NOT_RUN'
    path = es.ProfileEvidenceStore(tmp_path / 'bundles').commit(files)
    assert es.verify_profile_bundle(path)['capture_status'] == 'REJECTED'
    assert files == before


@pytest.mark.parametrize('stage,status,reason', [
    ('S1', 'NOT_RUN', None), ('S2', 'NOT_RUN', 'NONE'),
    ('S1', 'FAILED', 'COLLECTOR_EXCEPTION'), ('S2', 'FAILED', 'CDP_COMMAND_FAILED'),
])
def test_failed_or_unrun_stage_never_retains_structure(tmp_path, stage, status, reason):
    files = _history_partial()
    files['RESULT.json']['profile']['stages'][stage] = {'status': status, 'reason_code': reason, 'duration_ms': 0}
    with pytest.raises(bv.BundleInputError):
        bv.verify_report_bundle(_pack_unchecked(files))
    destination = tmp_path / 'not-created'
    with pytest.raises(bv.BundleInputError):
        es.ProfileEvidenceStore(destination).commit(files)
    assert not destination.exists()


def test_n02_history_only_partial_keeps_verified_nontruncated_structure(tmp_path):
    files = _history_partial()
    assert not files['STRUCTURE.json']['truncated']
    assert files['RESULT.json']['profile']['coverage_complete']
    path = es.ProfileEvidenceStore(tmp_path / 'bundles').commit(files)
    result = es.verify_profile_bundle(path)
    assert result['integrity_valid'] and result['capture_status'] == 'PARTIAL'
    assert (path / 'STRUCTURE.json').exists()


def test_valid_bounded_partial_and_invalid_forged_truncation(tmp_path):
    files = _good()
    result = files['RESULT.json']
    result.update(capture_status='PARTIAL', failure_code='TRUNCATED')
    result['profile']['coverage_complete'] = False
    result['profile']['stages']['S2'].update(status='PARTIAL', reason_code='TRUNCATED')
    files['STRUCTURE.json']['truncated'] = True
    path = es.ProfileEvidenceStore(tmp_path / 'bundles').commit(files)
    assert es.verify_profile_bundle(path)['profile_valid']
    files['STRUCTURE.json']['truncated'] = False
    with pytest.raises(bv.BundleInputError):
        bv.verify_report_bundle(_pack_unchecked(files))


@pytest.mark.parametrize('mutation', ['nonzero_notrun', 'none_complete', 'float_duration', 'bool_count',
                                      'raw_text', 'extra_field', 'fake_assessment_pass'])
def test_strict_stage_schema_and_privacy_reject_without_repair(mutation):
    files = _rejected() if mutation == 'nonzero_notrun' else _good()
    profile = files['RESULT.json']['profile']
    if mutation == 'nonzero_notrun':
        profile['stages']['S1']['duration_ms'] = 1
    elif mutation == 'none_complete':
        profile['stages']['S1']['reason_code'] = 'NONE'
    elif mutation == 'float_duration':
        profile['stages']['S1']['duration_ms'] = 1.0
    elif mutation == 'bool_count':
        profile['history_coverage']['retained_count'] = False
    elif mutation == 'raw_text':
        files['STRUCTURE.json']['innerHTML'] = 'SYNTHETIC_SECRET_MUST_BE_REJECTED'
    elif mutation == 'extra_field':
        profile['prevalidated'] = True
    else:
        files['RESULT.json']['assessment_status'] = 'PASS'
    before = copy.deepcopy(files)
    with pytest.raises(bv.BundleInputError):
        bv.verify_report_bundle(_pack_unchecked(files))
    assert files == before


def test_single_payload_validation_uses_exact_structure_schema():
    structure = _good()['STRUCTURE.json']
    bv.validate_payload_file('STRUCTURE.json', structure)
    structure['profile']['probes'][0]['match_count'] = 1.0
    with pytest.raises(bv.BundleInputError):
        bv.validate_payload_file('STRUCTURE.json', structure)
    with pytest.raises(bv.BundleInputError):
        bv.validate_payload_file('unknown.json', {})


def test_capture_matrix_classification_is_typed_and_detached():
    envelope = _good()['RESULT.json']['profile']['trigger_envelope']
    rule = bv.capture_rule(envelope)
    assert rule['capture_mode'] == 'GUARDED_STRUCTURE'
    rule['capture_mode'] = 'MUTATED_CALLER_COPY'
    assert bv.capture_rule(envelope)['capture_mode'] == 'GUARDED_STRUCTURE'
    envelope['event'].update(kind='ACTION_STARTED', failure_code='NONE')
    envelope['profile'].update(reason='OPERATION_ARMED', dispatch_state='NOT_DISPATCHED')
    assert bv.capture_rule(envelope)['capture_mode'] == 'NONE'
    envelope['event'].update(kind='ACTION_FAILED', failure_code='DISCONNECTED')
    envelope['profile'].update(reason='PROVIDER_DISCONNECTED', dispatch_state='POSSIBLE')
    assert bv.capture_rule(envelope)['capture_mode'] == 'METADATA_ONLY'
    envelope['producer_seq'] = 1.0
    with pytest.raises(bv.BundleInputError):
        bv.capture_rule(envelope)


def test_duplicate_keys_nonfinite_and_wrong_hash_are_rejected():
    files = _good()
    valid = es.encode_profile_bundle(files, _id(files))
    duplicate = dict(valid)
    duplicate['COMMIT.json'] = b'{"schema":"cdpd.commit/v1","schema":"cdpd.commit/v1","manifest_sha256":"' + b'0' * 64 + b'"}'
    with pytest.raises(bv.BundleInputError, match='BUNDLE_JSON_INVALID'):
        bv.verify_report_bundle(duplicate)
    nonfinite = dict(valid)
    nonfinite['RESULT.json'] = b'{"x":1e9999}'
    with pytest.raises(bv.BundleInputError, match='BUNDLE_JSON_INVALID'):
        bv.verify_report_bundle(nonfinite)
    changed = dict(valid)
    changed['EVENTS.json'] = changed['EVENTS.json'] + b' '
    with pytest.raises(bv.BundleInputError, match='BUNDLE_HASH_INVALID'):
        bv.verify_report_bundle(changed)


def test_total_budget_includes_manifest_and_commit_before_mutation(tmp_path):
    files = _good()
    blob = es.encode_profile_bundle(files, _id(files))
    payload_only = sum(len(raw) for name, raw in blob.items() if name not in ('MANIFEST.json', 'COMMIT.json'))
    full = sum(map(len, blob.values()))
    assert 4096 <= payload_only < full
    destination = tmp_path / 'blocked'
    with pytest.raises(es.ProfileStorageError, match='EVIDENCE_BUDGET_EXCEEDED'):
        es.ProfileEvidenceStore(destination, max_bytes=payload_only).commit(files)
    assert not destination.exists()
    path = es.ProfileEvidenceStore(tmp_path / 'exact', max_bytes=full).commit(files)
    assert es.verify_profile_bundle(path)['integrity_valid']


def test_missing_commit_is_incomplete_and_payload_tampering_fails(tmp_path):
    files = _good()
    path = es.ProfileEvidenceStore(tmp_path / 'bundles').commit(files)
    commit = (path / 'COMMIT.json').read_bytes()
    (path / 'COMMIT.json').unlink()
    result = es.verify_profile_bundle(path)
    assert not result['integrity_valid'] and result['publication_state'] == 'INCOMPLETE'
    assert not es.verify_bundle(path)['integrity_valid']
    (path / 'COMMIT.json').write_bytes(commit)
    (path / 'EVENTS.json').write_bytes((path / 'EVENTS.json').read_bytes() + b' ')
    assert not es.verify_profile_bundle(path)['integrity_valid']


def test_cross_field_tampering_with_honest_new_hashes_is_rejected(tmp_path):
    files = _good()
    files['RESULT.json']['profile']['identity_checks']['after'] = False
    blob = _pack_unchecked(files)
    path = tmp_path / _id(files)
    path.mkdir()
    for name, data in blob.items():
        (path / name).write_bytes(data)
    assert es.verify_bundle(path)['integrity_valid']
    assert not es.verify_profile_bundle(path)['profile_valid']


def test_commit_last_and_failure_before_publication_leaves_no_commit(tmp_path, monkeypatch):
    files = _good()
    writes = []
    original = es._write_new
    def corrupt_manifest(path, raw):
        writes.append(path.name)
        original(path, raw + b' ' if path.name == 'MANIFEST.json' else raw)
    monkeypatch.setattr(es, '_write_new', corrupt_manifest)
    with pytest.raises(es.ProfileStorageError):
        es.ProfileEvidenceStore(tmp_path / 'bundles').commit(files)
    path = tmp_path / 'bundles' / _id(files)
    assert writes[-1] == 'MANIFEST.json' and 'COMMIT.json' not in writes
    assert not (path / 'COMMIT.json').exists()


def test_final_readback_failure_removes_own_commit(tmp_path, monkeypatch):
    """Historical test ID retained; new contract preserves published COMMIT."""
    files = _good()
    original = es.verify_profile_bundle
    observations = []
    def bad_readback(path):
        observations.append((path / 'COMMIT.json').exists())
        return {'integrity_valid': False, 'profile_valid': False}
    monkeypatch.setattr(es, 'verify_profile_bundle', bad_readback)
    with pytest.raises(es.ProfileStorageError):
        es.ProfileEvidenceStore(tmp_path / 'bundles').commit(files)
    path = tmp_path / 'bundles' / _id(files)
    assert observations == [True]
    assert (path / 'COMMIT.json').exists()
    assert original(path)['publication_state'] == 'COMMITTED_VALID'


def test_commit_flush_error_also_removes_partly_created_marker(tmp_path, monkeypatch):
    files = _good()
    original = es._write_new
    def fail_after_write(path, raw):
        original(path, raw)
        if path.name == '.COMMIT.staged':
            raise OSError('SYNTHETIC_FSYNC_FAILURE_BEFORE_ATOMIC_PUBLICATION')
    monkeypatch.setattr(es, '_write_new', fail_after_write)
    with pytest.raises(es.ProfileStorageError):
        es.ProfileEvidenceStore(tmp_path / 'bundles').commit(files)
    path = tmp_path / 'bundles' / _id(files)
    assert not (path / 'COMMIT.json').exists()
    assert not (path / '.COMMIT.staged').exists()
    assert es.verify_profile_bundle(path)['publication_state'] == 'INCOMPLETE'


def test_publish_guard_runs_once_after_staged_verification_before_commit(tmp_path):
    files = _good()
    path = tmp_path / 'bundles' / _id(files)
    called = []
    def permit():
        assert (path / 'STRUCTURE.json').is_file()
        assert (path / 'MANIFEST.json').is_file()
        assert not (path / 'COMMIT.json').exists()
        # Same payload/hash/profile frontend has already accepted these staged
        # files; this callback is solely the final live diagnostic permit seam.
        called.append('PERMIT_CHECKED')
    observed = es.ProfileEvidenceStore(tmp_path / 'bundles').commit(files, before_publish=permit)
    assert observed == path and called == ['PERMIT_CHECKED']
    assert es.verify_profile_bundle(path)['profile_valid']


def test_late_publish_guard_denial_discards_dom_and_leaves_incomplete(tmp_path):
    files = _good()
    path = tmp_path / 'bundles' / _id(files)
    s0 = tmp_path / 'S0.json'
    s0.write_bytes(b'SYNTHETIC_FROZEN_S0')
    called = []
    def revoked():
        assert (path / 'STRUCTURE.json').is_file()
        assert (path / 'MANIFEST.json').is_file()
        called.append('REVOKED_AFTER_STAGES')
        raise RuntimeError('SYNTHETIC_RAW_GUARD_ERROR_MUST_NOT_ESCAPE')
    with pytest.raises(es.ProfileStorageError) as error:
        es.ProfileEvidenceStore(tmp_path / 'bundles').commit(files, before_publish=revoked)
    assert str(error.value) == error.value.code == 'PUBLISH_GUARD_DENIED'
    assert called == ['REVOKED_AFTER_STAGES']
    assert not (path / 'COMMIT.json').exists() and not (path / 'STRUCTURE.json').exists()
    assert s0.read_bytes() == b'SYNTHETIC_FROZEN_S0'
    assert es.verify_profile_bundle(path)['publication_state'] == 'INCOMPLETE'


def test_publish_guard_never_runs_for_invalid_input_or_uses_bool_admission(tmp_path):
    files = _good()
    invalid = copy.deepcopy(files)
    invalid['RESULT.json']['profile']['identity_checks']['after'] = False
    calls = []
    def should_not_run():
        calls.append('UNEXPECTED')
    with pytest.raises(bv.BundleInputError):
        es.ProfileEvidenceStore(tmp_path / 'invalid').commit(invalid, before_publish=should_not_run)
    assert not calls and not (tmp_path / 'invalid').exists()
    for index, returned in enumerate((True, False, 'PERMIT')):
        target = tmp_path / f'non_none_{index}'
        with pytest.raises(es.ProfileStorageError, match='PUBLISH_GUARD_DENIED'):
            es.ProfileEvidenceStore(target).commit(files, before_publish=lambda value=returned: value)
        path = target / _id(files)
        assert not (path / 'COMMIT.json').exists() and not (path / 'STRUCTURE.json').exists()


def test_existing_capture_is_never_overwritten_or_repaired(tmp_path):
    files = _good()
    store = es.ProfileEvidenceStore(tmp_path / 'bundles')
    path = store.commit(files)
    before = {p.name: p.read_bytes() for p in path.iterdir()}
    with pytest.raises(es.ProfileStorageError):
        store.commit(files)
    assert {p.name: p.read_bytes() for p in path.iterdir()} == before


def test_symlink_parent_and_payload_are_rejected(tmp_path):
    files = _good()
    target = tmp_path / 'target'
    target.mkdir()
    alias = tmp_path / 'alias'
    try:
        alias.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
            pytest.skip("WINDOWS_SYMLINK_PRIVILEGE_NOT_GRANTED; no implicit PASS")
        raise
    with pytest.raises(es.ProfileStorageError):
        es.ProfileEvidenceStore(alias / 'bundles').commit(files)
    assert not (target / 'bundles').exists()
    path = es.ProfileEvidenceStore(target / 'good').commit(files)
    extra = tmp_path / 'external-events.json'
    extra.write_bytes((path / 'EVENTS.json').read_bytes())
    (path / 'EVENTS.json').unlink()
    try:
        (path / 'EVENTS.json').symlink_to(extra)
    except OSError as exc:
        if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
            pytest.skip("WINDOWS_SYMLINK_PRIVILEGE_NOT_GRANTED; no implicit PASS")
        raise
    assert not es.verify_profile_bundle(path)['integrity_valid']


def test_sticky_unknown_and_known_loss_cannot_be_declared_complete():
    for change in ({'loss_count_exact': False}, {'loss_lower_bound': 1}):
        files = _good()
        files['RESULT.json']['profile']['trigger_envelope']['profile'].update(change)
        with pytest.raises(bv.BundleInputError):
            bv.verify_report_bundle(_pack_unchecked(files))


def test_exact_original_n01_n02_bytes_source_baseline_and_forged_pass():
    """Use unchanged L02 report oracle as comparator, not production L08."""
    fixture_root = ROOT / 'tests/fixtures/l06_original_issue_cases'
    ledger = json.loads((fixture_root / 'SOURCE_FILE_MAP.json').read_bytes())
    for row in ledger['files']:
        raw = (ROOT / row['path']).read_bytes()
        assert len(raw) == row['bytes'] and hashlib.sha256(raw).hexdigest() == row['sha256']
    script = r'''
import hashlib,json,pathlib,sys
root=pathlib.Path(sys.argv[1])
from cdpd_chatgpt_profile import bundle_validation as production
sys.path.insert(0,str(root/'references/approved_l02/tests'))
# This substitution exists only in an isolated validation process: the
# unchanged report DESIGN oracle exercises the new production input frontend.
sys.modules['bundle_input_model']=production
import fix001_model as oracle
policy=json.loads((root/'references/approved_l02/contracts/report_policy.json').read_bytes())
result=[]
for case in sorted((root/'tests/fixtures/l06_original_issue_cases').iterdir()):
    if not case.is_dir():continue
    source={p.name:p.read_bytes() for p in (case/'source_bundle').iterdir()}
    baseline={p.name:p.read_bytes() for p in (case/'baseline_bundle').iterdir()}
    before=(dict(source),dict(baseline))
    sh,_=production.verify_report_bundle(source)
    bh,_=production.verify_report_bundle(baseline)
    approval=json.loads((case/'TRUSTED_MOCK_APPROVAL_NOT_HUMAN.json').read_bytes())
    report=oracle.derive_report(source,baseline,approval,policy)
    assert report['assessment_status']=='UNKNOWN'
    assert oracle.validate_report_bytes((case/'REPORT.json').read_bytes(),source,baseline,approval,policy)
    forged_valid=oracle.validate_report_bytes((case/'FORGED_PASS_NOT_APPROVED.json').read_bytes(),source,baseline,approval,policy)
    assert not forged_valid
    assert source==before[0] and baseline==before[1]
    result.append({'case':case.name,'status':'PASS','source_manifest_sha256':sh,'baseline_manifest_sha256':bh,
                   'report':'UNKNOWN','forged_PASS_accepted':forged_valid,'original_bytes_unchanged':True,
                   'comparator':'UNCHANGED_L02_DESIGN_ORACLE_NOT_L08_ACCEPTANCE'})
print(json.dumps(result,sort_keys=True))
'''
    result = subprocess.run([sys.executable, '-B', '-c', script, str(ROOT)],
                            text=True, capture_output=True, timeout=20, check=False)
    assert result.returncode == 0, result.stderr
    rows = json.loads(result.stdout)
    assert len(rows) == 4 and all(row['status'] == 'PASS' for row in rows)
    print('ORIGINAL_ISSUE_CONTROLS=' + result.stdout.strip())

