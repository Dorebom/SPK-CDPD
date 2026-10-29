"""Test-only, read-only rejection classifier for the *unchanged* strict Worker protocol.

This module grants no authority and never returns replacement permit / RESULT values.
No IPC bodies, paths, PID, DOM, stderr or hashes of user data are serialized.
It is not imported by any production CDPD module.
"""
from __future__ import annotations

from collections import Counter
from typing import Any

# These are fixed reason categories, NOT excerpts from child-controlled data.
RESULT_CODES = frozenset({
    'VALID', 'ENVELOPE_KEYS', 'SCHEMA', 'CAPTURE_ID', 'PID',
    'SCRIPT_DIGEST', 'CATALOG_DIGEST', 'IDENTITY_SHAPE', 'STATUS_OR_MARKER',
    'AUDIT_SHAPE', 'STAGES_SHAPE', 'STRUCTURE_SCHEMA', 'BINDING_RECORD',
    'STRUCTURE_BINDING', 'STRUCTURE_CONSISTENCY', 'NO_STRUCTURE_CONSISTENCY',
    'POLICY_REJECT_CONSISTENCY', 'POLICY_ALLOWED', 'VALIDATOR_OTHER',
})


def result_reason(value: Any, data: dict, owned_pid: int, *, validator=None) -> str:
    """Observes the exact production validator first; cannot turn reject into accept.

    The reason is a best-effort stage classification, with VALIDATOR_OTHER for
    anything this intentionally conservative classifier cannot explain.
    """
    from cdpd_chatgpt_profile import process_runner as pr
    from cdpd_chatgpt_profile.structure_script import CATALOG_SHA256, script_sha256
    from cdpd_chatgpt_profile.worker import _validate_structure
    if validator is None:
        validator = pr._valid_result
    try:
        valid = validator(value, data, owned_pid)
    except Exception:
        return 'VALIDATOR_OTHER'
    if valid is True:
        return 'VALID'
    try:
        if type(value) is not dict or set(value) != pr._RESULT_KEYS:
            return 'ENVELOPE_KEYS'
        if value['schema'] != 'cdpd.capture-worker-result/v1':
            return 'SCHEMA'
        if value['capture_id'] != data['capture_id']:
            return 'CAPTURE_ID'
        if type(value['pid']) is not int or value['pid'] != owned_pid:
            return 'PID'
        if value['script_sha256'] != script_sha256(data['capture_request']['max_nodes']):
            return 'SCRIPT_DIGEST'
        if value['probe_catalog_sha256'] != CATALOG_SHA256:
            return 'CATALOG_DIGEST'
        identity = value['identity_checks']
        if (type(identity) is not dict or set(identity) != {'before','after','current','keeper','lease'}
                or any(type(x) is not bool for x in identity.values())):
            return 'IDENTITY_SHAPE'
        if (value['capture_status'] not in {'COMPLETE','PARTIAL','REJECTED'}
                or value['failure_code'] not in pr._STAGE_CODES
                or type(value['sanitization_passed']) is not bool
                or value['worker_termination'] not in {'NORMAL','NOT_STARTED'}
                or not pr._marker(value['before']) or not pr._marker(value['after'])):
            return 'STATUS_OR_MARKER'
        audit=value['command_audit']
        if (type(audit) is not list or len(audit)>64 or
            any(type(m) is not str or m not in pr._METHODS for m in audit) or
            audit.count('Runtime.evaluate')>1):
            return 'AUDIT_SHAPE'
        stages=value['stages']
        if type(stages) is not dict or set(stages) != {'S1','S2'}:
            return 'STAGES_SHAPE'
        for stage in stages.values():
            if (type(stage) is not dict or set(stage) != {'status','reason_code','duration_ms'}
                or stage['status'] not in {'NOT_RUN','COMPLETE','PARTIAL','FAILED'}
                or stage['reason_code'] not in pr._STAGE_CODES or type(stage['duration_ms']) is not int
                or not 0 <= stage['duration_ms'] <= 300000):
                return 'STAGES_SHAPE'
        structure=value['structure']
        if structure is not None:
            try:
                _validate_structure(structure, maximum=data['capture_request']['max_nodes'])
            except Exception:
                return 'STRUCTURE_SCHEMA'
            try:
                from chatgpt_web_api.target_guard import ParsedBinding
                binding=ParsedBinding.parse(data['binding_record_raw_json'])
            except Exception:
                return 'BINDING_RECORD'
            expected={'target_id':binding.target_id,'browser_id':binding.expected_browser_id,
                      'origin':data['capture_request']['target']['expected_origin'],
                      'loader_id':value['before']['loader_id'] if type(value['before']) is dict else None,
                      'binding_strength':'PINNED_BROWSER'}
            if structure['binding'] != expected:
                return 'STRUCTURE_BINDING'
            truncated=structure['truncated']
            status='PARTIAL' if truncated else 'COMPLETE'
            reason='TRUNCATED' if truncated else None
            if (not all(identity.values()) or not value['sanitization_passed']
                or value['worker_termination'] != 'NORMAL' or value['before'] is None
                or value['after'] != value['before'] or structure['command_audit'] != audit
                or audit.count('Runtime.evaluate') != 1 or value['capture_status'] != status
                or value['failure_code'] != reason
                or any(s['status'] != status or s['reason_code'] != reason for s in stages.values())):
                return 'STRUCTURE_CONSISTENCY'
        elif (value['capture_status'] == 'COMPLETE' or value['failure_code'] is None
              or any(s['status'] not in {'NOT_RUN','FAILED'} for s in stages.values())):
            return 'NO_STRUCTURE_CONSISTENCY'
        if value['capture_status'] == 'REJECTED':
            if not (data['policy_allowed'] is False and structure is None
                    and value['failure_code']=='POLICY_DENIED' and value['worker_termination']=='NOT_STARTED'
                    and not any(identity.values()) and not audit and value['before'] is None
                    and value['after'] is None and value['sanitization_passed'] is False
                    and all(s['status']=='NOT_RUN' and s['reason_code']=='POLICY_DENIED' for s in stages.values())):
                return 'POLICY_REJECT_CONSISTENCY'
        elif (structure is None and (value['failure_code'] not in pr._FAILURES
                or value['worker_termination'] != 'NORMAL'
                or any(s['reason_code'] != value['failure_code'] for s in stages.values()))):
            return 'NO_STRUCTURE_CONSISTENCY'
        if data['policy_allowed'] is not True and value['capture_status'] != 'REJECTED':
            return 'POLICY_ALLOWED'
    except Exception:
        return 'VALIDATOR_OTHER'
    return 'VALIDATOR_OTHER'


def bounded_enum_histogram(values, allow: set[str] | frozenset[str], limit: int = 64) -> dict[str,int]:
    """No free-form diagnostic payload. Only allowlisted reason enums."""
    result = Counter(x if type(x) is str and x in allow else 'VALIDATOR_OTHER' for x in values[:limit])
    return {key:result[key] for key in sorted(result)}
