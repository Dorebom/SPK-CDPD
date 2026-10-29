"""L06 production Bundle input validation, derived from approved L02 FIX003.

Lineage: references/approved_l02/tests/bundle_input_model.py, SHA-256
9aaefe4bc6deb94cba22a9093eaf76310d4a19012ed50032d91ae14c626ce5c5.
All closed-schema/hash/profile rules are preserved. Production code imports no
model/test module. N-01 makes the existing metadata NOT_RUN/null/NONE semantics
explicit without repairing legacy bytes or allowing unsafe retained structure.
This validator proves internal consistency, not live authority or L08 acceptance.
"""
from __future__ import annotations
import hashlib
import json
import math
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from jsonschema import Draft202012Validator, FormatChecker, validators

ROOT = Path(__file__).resolve().parents[2]

# FIX010: this exact approved schema is checked unconditionally with the same
# extended validator by fix010_validation/test_schema_attestation.py. Its
# immutable bytes do not need the schema's own meta-validation in each new
# worker process. Every envelope/payload still uses the complete validator.
_ATTESTED_SCHEMA_SHA256 = '2bb2483725d3042c3f53e7a44d616c4f949c0f3fe7868e716b6d74ca2eeca996'

def canonical(value: Any) -> bytes:
    """Deterministic UTF-8 JSON; never encode nonfinite floats."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def strict_json(raw: bytes, maximum: int = 32768) -> Any:
    """The approved strict parser, with no test dependency."""
    if type(raw) is not bytes or len(raw) > maximum:
        raise ValueError("INPUT_TOO_LARGE")
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("DUPLICATE_KEY")
            result[key] = value
        return result
    def invalid_constant(_: str) -> None:
        raise ValueError("NONFINITE_JSON")
    def finite_float(text: str) -> float:
        value = float(text)
        if not math.isfinite(value):
            raise ValueError("NONFINITE_JSON")
        return value
    return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                      parse_constant=invalid_constant, parse_float=finite_float)


def classify_matrix(envelope: dict[str, Any], matrix: dict[str, Any]) -> dict[str, Any] | None:
    event = envelope["event"]
    matches = [rule for rule in matrix["rules"]
               if (rule["event_kind"], rule["failure_code"], rule["profile_reason"])
               == (event["kind"], event["failure_code"], envelope["profile"]["reason"])]
    return matches[0] if len(matches) == 1 else None


class BundleInputError(ValueError):
    """Fixed codes only; never expose rejected payloads or exception strings."""
    def __init__(self, code: str, *report_reasons: str) -> None:
        self.code = code
        self.report_reasons = tuple(dict.fromkeys(('INTEGRITY_INVALID',) + report_reasons))
        super().__init__(code)


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _fail(code: str, *reasons: str) -> None:
    raise BundleInputError(code, *reasons)


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


@lru_cache(maxsize=1)
def _contracts():
    raw_schema = (ROOT/'contracts/bundle_payload_v1.schema.json').read_bytes()
    schema = json.loads(raw_schema)
    contract = json.loads((ROOT/'contracts/bundle_input_contract.json').read_bytes())
    matrix = json.loads((ROOT/'contracts/diagnostic_event_matrix.json').read_bytes())
    # JSON Schema's mathematical integer accepts 1.0. This profile explicitly
    # requires JSON integer tokens for counts, sequence and duration (not bool).
    tc = Draft202012Validator.TYPE_CHECKER.redefine('integer', lambda c, v: type(v) is int)
    validator = validators.extend(Draft202012Validator, type_checker=tc)
    if _sha(raw_schema) != _ATTESTED_SCHEMA_SHA256:
        # Different bytes, including otherwise harmless formatting changes,
        # follow the original schema-validation path before any instance use.
        validator.check_schema(schema)
    return validator(schema, format_checker=FormatChecker()), contract, matrix


def _parse(raw: bytes, limit: int, depth_limit: int) -> Any:
    if type(raw) is not bytes or len(raw) > limit:
        _fail('BUNDLE_BYTES_INVALID')
    try:
        value = strict_json(raw, limit)
        todo = [(value, 0)]
        while todo:
            node, depth = todo.pop()
            if depth > depth_limit:
                _fail('BUNDLE_JSON_INVALID')
            if isinstance(node, dict):
                todo.extend((x, depth+1) for x in node.values())
            elif isinstance(node, list):
                todo.extend((x, depth+1) for x in node)
        return value
    except BundleInputError:
        raise
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        _fail('BUNDLE_JSON_INVALID')


def _sequence_domain(envelope: dict[str, Any]) -> str:
    """A liveness sample cannot supply a missing diagnostic event sequence.

    The fixed EVT-24 triple is the only separate sequence domain. Raw heartbeat
    mailbox records are not Bundle inputs. No caller-selected domain is accepted.
    """
    event=envelope['event']
    if (event['kind'],event['failure_code'],envelope['profile']['reason']) == (
            'HEARTBEAT','NONE','HEARTBEAT_SAMPLE'):
        return 'LIVENESS_SAMPLE'
    return 'DIAGNOSTIC_EVENT'


def _history_loss_semantics(history: list[dict[str, Any]],
                            trigger: dict[str, Any], coverage: dict[str, Any]) -> None:
    """FIX003 cumulative declarations plus interior retained diagnostic gaps.

    The enclosing verifier has already bound every history row to the trigger's
    producer/instance/Binding/hash/generation and checked row order per domain.
    All carriers report snapshots of ONE cumulative diagnostic-event loss
    counter per base stream, including HEARTBEAT samples. The domain determines
    which sequence values can establish order; it does not create a separate
    loss population. Liveness coalescing never increments this counter. Known
    loss survives window cropping without explicit scope/reset evidence.
    """
    declarations={}
    for envelope in [trigger]+[row['profile'] for row in history]:
        key=(_sequence_domain(envelope),envelope['producer_seq'])
        if key in declarations and declarations[key]!=envelope:
            _fail('PROFILE_INCONSISTENT','REQUIRED_COVERAGE_MISSING')
        # The same trigger persisted as a history row is one declaration.
        declarations[key]=envelope
    previous={};declared_bound=0;unknown=False
    for (domain,sequence),envelope in sorted(declarations.items()):
        loss=envelope['profile'];bound=loss['loss_lower_bound'];exact=loss['loss_count_exact']
        if domain in previous:
            old_bound,old_exact=previous[domain]
            if bound<old_bound or (not old_exact and exact):
                _fail('PROFILE_INCONSISTENT','REQUIRED_COVERAGE_MISSING')
        previous[domain]=(bound,exact)
        declared_bound=max(declared_bound,bound)
        unknown=unknown or not exact
    diagnostic_sequences=[row['profile']['producer_seq'] for row in history
                          if _sequence_domain(row['profile'])=='DIAGNOSTIC_EVENT']
    # Only holes strictly between retained diagnostic rows count. There is no
    # gap inference before the first row, after the last row, to an out-of-window
    # trigger, from UTC ordering, or from Supervisor journal_seq. Liveness
    # coalescing is a different domain and cannot fill or create these holes.
    gap_bound=sum(right-left-1 for left,right in zip(
        diagnostic_sequences,diagnostic_sequences[1:]))
    # Every carrier snapshots the same diagnostic loss population, and the
    # observed gaps can overlap those snapshots. max is a justified lower bound;
    # summing duplicate snapshots or adding overlapping gap evidence is not.
    minimum_bound=max(declared_bound,gap_bound)
    if (coverage['loss_lower_bound']<minimum_bound
            or (unknown and coverage['loss_count_exact'])):
        _fail('PROFILE_INCONSISTENT','REQUIRED_COVERAGE_MISSING')


def _semantics(data: dict[str, Any], matrix: dict[str, Any],
               contract: dict[str, Any]) -> None:
    """All objects have passed the full closed Schema before this function."""
    r=data['RESULT.json']; p=r['profile']; env=p['trigger_envelope']
    history=data['EVENTS.json']['events']; hc=p['history_coverage']
    no_error=lambda x: x is None or x=='NONE'
    if (p['capture_id']!=data['MANIFEST.json']['capture_id']
        or env['event']!=data['TRIGGER.json']
        or env['event_id']!=p['initial_trigger_event_id']
        or p['cause_event_ids'][0]!=p['initial_trigger_event_id']
        or env['control_generation']!=p['control_generation']):
        _fail('PROFILE_INCONSISTENT')
    rule=classify_matrix(env,matrix)
    if rule is None:
        _fail('PROFILE_INCONSISTENT','DOCUMENT_RELATION_UNKNOWN')
    record=p['binding_record']; raw=p['binding_record_raw_json']; bh=p['binding_record_sha256']
    if record is None:
        if not (raw is None and bh is None and env['binding_id'] is None
                and env['binding_sha256'] is None and p['control_generation']==0
                and r['target_id'] is None and not any(p['identity_checks'].values())):
            _fail('PROFILE_INCONSISTENT','BINDING_NOT_VERIFIED')
    else:
        if raw is None or bh is None:
            _fail('PROFILE_INCONSISTENT','BINDING_NOT_VERIFIED')
        parsed=_parse(raw.encode('utf-8'),8192,16)
        if (parsed!=record or _sha(raw.encode('utf-8'))!=bh or bh!=env['binding_sha256']
            or record['binding_id']!=env['binding_id'] or p['control_generation']<1
            or r['target_id']!=record['target']['target_id']):
            _fail('PROFILE_INCONSISTENT','BINDING_NOT_VERIFIED')
        if (_time(record['expires_at_utc'])-_time(record['issued_at_utc'])).total_seconds()!=14400:
            _fail('PROFILE_INCONSISTENT','BINDING_NOT_VERIFIED')
    if hc['retained_count']!=len(history) or r['history_dropped_count']!=hc['loss_lower_bound']:
        _fail('PROFILE_INCONSISTENT','REQUIRED_COVERAGE_MISSING')
    if hc['complete'] and (hc['loss_lower_bound']!=0 or not hc['loss_count_exact'] or hc['sampled']):
        _fail('PROFILE_INCONSISTENT','REQUIRED_COVERAGE_MISSING')
    seen=set();last={}
    for row in history:
        e=row['profile'];event=e['event']
        key=(event['producer_id'],event['producer_instance_id'],_sequence_domain(e))
        if (row['event']!=event or e['event_id'] in seen
            or e['producer_seq']<=last.get(key,0)
            or (e['binding_id'],e['binding_sha256'],e['control_generation'])
                !=(env['binding_id'],env['binding_sha256'],env['control_generation'])
            or event['producer_id']!=env['event']['producer_id']
            or event['producer_instance_id']!=env['event']['producer_instance_id']
            or classify_matrix(e,matrix) is None):
            _fail('PROFILE_INCONSISTENT','REQUIRED_COVERAGE_MISSING')
        if e['event_id']==env['event_id'] and e!=env:
            _fail('PROFILE_INCONSISTENT','REQUIRED_COVERAGE_MISSING')
        seen.add(e['event_id']);last[key]=e['producer_seq']
    _history_loss_semantics(history,env,hc)
    # source_cutoff_seq is Supervisor journal_seq, NOT producer_seq. Do not
    # compare different clocks/sequences or require UTC event sorting.
    if history and p['source_cutoff_seq']==0:
        _fail('PROFILE_INCONSISTENT','REQUIRED_COVERAGE_MISSING')
    timing=p['timing']
    if (r['started_utc']!=timing['accepted_at_utc']
        or r['finished_utc']!=timing['finished_at_utc']
        or p['sanitization']['probe_catalog_sha256']!=p['report_context']['catalog_sha256']):
        _fail('PROFILE_INCONSISTENT','CATALOG_NOT_COMPARABLE')
    # UTC is corroborating evidence, never the live monotonic lease authority.
    if p['worker_termination']=='NOT_STARTED' and timing['capture_started_at_utc'] is not None:
        _fail('PROFILE_INCONSISTENT')
    for name,stage in p['stages'].items():
        if stage['status']=='COMPLETE' and stage['reason_code'] is not None:
            _fail('PROFILE_INCONSISTENT','REQUIRED_EVIDENCE_MISSING')
        # N-01: null means unspecified stage reason; string NONE means no
        # additional stage reason. Neither denotes execution or success. Both
        # remain valid for metadata-only legacy NOT_RUN, duration exactly zero.
        if stage['status']=='NOT_RUN' and stage['duration_ms']!=0:
            _fail('PROFILE_INCONSISTENT','REQUIRED_EVIDENCE_MISSING')
    if any(p['stages'][s]['status']!='COMPLETE' for s in ('S0','S3')):
        _fail('PROFILE_INCONSISTENT','REQUIRED_EVIDENCE_MISSING')
    structure=data.get('STRUCTURE.json')
    if structure is not None:
        if (not p['policy_allowed'] or not p['sanitization']['passed']
            or not all(p['identity_checks'].values()) or p['worker_termination']!='NORMAL'
            or record is None):
            _fail('PROFILE_INCONSISTENT','BINDING_NOT_VERIFIED','POLICY_NOT_ALLOWED')
        allowed_stage_forms={(form['status'],form['reason_code'])
            for form in contract['structure_eligibility']['allowed_stage_forms']}
        for name in ('S1','S2'):
            stage=p['stages'][name]
            if (stage['status'],stage['reason_code']) not in allowed_stage_forms:
                _fail('PROFILE_INCONSISTENT','REQUIRED_EVIDENCE_MISSING')
            if stage['status']=='PARTIAL' and not (
                    r['capture_status']=='PARTIAL' and structure['truncated']
                    and not p['coverage_complete']):
                _fail('PROFILE_INCONSISTENT','REQUIRED_EVIDENCE_MISSING')
        sb=structure['binding'];target=record['target']
        if (sb['target_id']!=target['target_id'] or sb['browser_id']!=target['expected_browser_id']
            or sb['origin']!=target['expected_origin']):
            _fail('PROFILE_INCONSISTENT','BINDING_NOT_VERIFIED')
        nodes=structure['nodes']
        for index,node in enumerate(nodes):
            if node['index']!=index or not (-1<=node['parent']<index):
                _fail('PROFILE_INCONSISTENT','REQUIRED_EVIDENCE_MISSING')
        if structure['iframeCount']!=sum(n['tag']=='iframe' for n in nodes):
            _fail('PROFILE_INCONSISTENT','REQUIRED_COVERAGE_MISSING')
        if structure['openShadowCount']>len(nodes):
            _fail('PROFILE_INCONSISTENT','REQUIRED_COVERAGE_MISSING')
        features=set()
        for probe in structure['profile']['probes']:
            if (probe['feature_id'] in features or probe['visible_count']>probe['match_count']
                or probe['enabled_count']>probe['match_count']):
                _fail('PROFILE_INCONSISTENT','REQUIRED_EVIDENCE_MISSING')
            features.add(probe['feature_id'])
        for shape in structure['profile']['feature_shapes'].values():
            for index,node in enumerate(shape):
                if node['parent'] is not None and not (0<=node['parent']<index):
                    _fail('PROFILE_INCONSISTENT','REQUIRED_EVIDENCE_MISSING')
    if rule['capture_mode'] in ('METADATA_ONLY','NONE') and structure is not None:
        _fail('PROFILE_INCONSISTENT','CAPTURE_NOT_COMPLETE')
    markers=p['document_markers']
    if markers['trigger']!=env['profile']['document_marker']:
        _fail('PROFILE_INCONSISTENT','DOCUMENT_RELATION_UNKNOWN')
    if structure is not None and (markers['before'] is None
            or markers['before']!=markers['after']
            or structure['binding']['loader_id']!=markers['before']['loader_id']):
        _fail('PROFILE_INCONSISTENT','DOCUMENT_RELATION_UNKNOWN')
    if p['observation_relation']=='SAME_DOCUMENT_VERIFIED' and (
            markers['trigger'] is None or markers['before']!=markers['trigger']
            or markers['after']!=markers['trigger']):
        _fail('PROFILE_INCONSISTENT','DOCUMENT_RELATION_UNKNOWN')
    if structure is not None and structure['truncated'] and p['coverage_complete']:
        _fail('PROFILE_INCONSISTENT','REQUIRED_COVERAGE_MISSING')
    if p['observation_relation']=='CURRENT_OBSERVATION':
        if not (env['event']['kind']=='MANUAL' and env['profile']['reason'] in ('MANUAL_CAPTURE','SCHEDULED_CAPTURE')):
            _fail('PROFILE_INCONSISTENT','DOCUMENT_RELATION_UNKNOWN')
    # Full observed COMPLETE is impossible with a capture failure or unconfirmed
    # worker. Trigger failure is deliberately separate: a failed UI operation
    # may still have successful subsequent evidence collection.
    if r['capture_status']=='COMPLETE':
        if not no_error(r['failure_code']) or p['worker_termination']!='NORMAL':
            _fail('PROFILE_INCONSISTENT','CAPTURE_NOT_COMPLETE')
        if structure is None or any(p['stages'][s]['status']!='COMPLETE' for s in ('S1','S2')):
            _fail('PROFILE_INCONSISTENT','REQUIRED_EVIDENCE_MISSING')
        if not p['policy_allowed'] or not p['sanitization']['passed']:
            _fail('PROFILE_INCONSISTENT','POLICY_NOT_ALLOWED')
        if not all(p['identity_checks'].values()):
            _fail('PROFILE_INCONSISTENT','BINDING_NOT_VERIFIED')
        if not p['coverage_complete'] or not hc['complete'] or structure['truncated']:
            _fail('PROFILE_INCONSISTENT','REQUIRED_COVERAGE_MISSING')
        if timing['capture_started_at_utc'] is None:
            _fail('PROFILE_INCONSISTENT','REQUIRED_EVIDENCE_MISSING')
    elif r['capture_status']=='REJECTED':
        if not (r['failure_code']=='POLICY_DENIED' and not p['policy_allowed']
                and p['worker_termination']=='NOT_STARTED' and structure is None
                and not p['coverage_complete']
                and all(p['stages'][s]['status']=='NOT_RUN' for s in ('S1','S2'))):
            _fail('PROFILE_INCONSISTENT','CAPTURE_NOT_COMPLETE')
    elif structure is not None:
        # PARTIAL can retain only verified bounded truncation/history-loss data,
        # never failed/uncertain process, identity, privacy, or connection data.
        if (r['failure_code'] not in contract['structure_eligibility']['partial_failure_codes']
            or not (structure['truncated'] or not hc['complete'])
            or (r['failure_code']=='TRUNCATED' and not structure['truncated'])
            or (r['failure_code']=='HISTORY_INCOMPLETE' and hc['complete'])):
            _fail('PROFILE_INCONSISTENT','CAPTURE_NOT_COMPLETE')


def verify_report_bundle(blob: dict[str, bytes]) -> tuple[str, dict[str, Any]]:
    """Mandatory all-file front-end. Returns no partially validated payload."""
    validator,contract,matrix=_contracts()
    try:
        if type(blob) is not dict or set(blob)-set(contract['allowed_files']):
            _fail('BUNDLE_BYTES_INVALID')
        if not set(contract['required_files'])<=set(blob):
            _fail('BUNDLE_BYTES_INVALID','REQUIRED_EVIDENCE_MISSING')
        limits=contract['byte_limits']
        if any(type(v) is not bytes for v in blob.values()) or sum(map(len,blob.values()))>limits['total']:
            _fail('BUNDLE_BYTES_INVALID')
        payload={name:_parse(raw,limits.get(name,limits['total']),limits['maximum_json_depth']) for name,raw in blob.items()}
        if not validator.is_valid(payload):
            _fail('BUNDLE_SCHEMA_INVALID')
        manifest=payload['MANIFEST.json'];commit=payload['COMMIT.json']
        if commit['manifest_sha256']!=_sha(blob['MANIFEST.json']):
            _fail('BUNDLE_HASH_INVALID')
        paths=[]
        for row in manifest['files']:
            name=row['path']
            if name in paths or name not in blob or row['bytes']!=len(blob[name]) or row['sha256']!=_sha(blob[name]):
                _fail('BUNDLE_HASH_INVALID')
            paths.append(name)
        if set(blob)!=set(paths)|{'MANIFEST.json','COMMIT.json'}:
            _fail('BUNDLE_HASH_INVALID')
        _semantics(payload,matrix,contract)
        return _sha(blob['MANIFEST.json']),{k:v for k,v in payload.items() if k not in ('MANIFEST.json','COMMIT.json')}
    except BundleInputError:
        raise
    except (KeyError,TypeError,ValueError,UnicodeError,RecursionError,OverflowError):
        _fail('PROFILE_INCONSISTENT')


def validate_payload_file(name: str, value: dict[str, Any]) -> None:
    """Validate one typed file, before the cross-file publication validation.

    This is useful for a Worker return shape. Passing it is not equivalent to
    verify_report_bundle: the coordinator must still validate every file and
    all cross-file/hash/stage/history/identity relationships before COMMIT.
    """
    validator, contract, _ = _contracts()
    if name not in contract['file_definitions']:
        _fail('BUNDLE_BYTES_INVALID')
    try:
        raw = canonical(value)
        limits = contract['byte_limits']
        payload = _parse(raw, limits.get(name, limits['total']), limits['maximum_json_depth'])
        schema = {'$schema': 'https://json-schema.org/draft/2020-12/schema',
                  '$defs': validator.schema['$defs'],
                  '$ref': '#/$defs/' + contract['file_definitions'][name]}
        # Construct the same extended class explicitly: evolve() with a new
        # $schema may select the stock mathematical-integer checker and accept
        # 1.0, while this profile requires an actual JSON integer token.
        if not type(validator)(schema, format_checker=FormatChecker()).is_valid(payload):
            _fail('BUNDLE_SCHEMA_INVALID')
    except BundleInputError:
        raise
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        _fail('BUNDLE_JSON_INVALID')


def capture_rule(envelope: dict[str, Any]) -> dict[str, Any]:
    """Return a detached approved matrix rule for a typed event envelope.

    METADATA_ONLY and NONE never authorize a content connection. Consumers must
    still check live Binding/keeper/lease/policy for modes permitting capture;
    this rule is classification, not an operation or capture admission token.
    """
    validator, _, matrix = _contracts()
    try:
        parsed = _parse(canonical(envelope), 32768, 48)
        schema = {'$schema': 'https://json-schema.org/draft/2020-12/schema',
                  '$defs': validator.schema['$defs'], '$ref': '#/$defs/envelope'}
        if not type(validator)(schema, format_checker=FormatChecker()).is_valid(parsed):
            _fail('BUNDLE_SCHEMA_INVALID')
        rule = classify_matrix(parsed, matrix)
        if rule is None:
            _fail('PROFILE_INCONSISTENT')
        return strict_json(canonical(rule))
    except BundleInputError:
        raise
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        _fail('BUNDLE_JSON_INVALID')
