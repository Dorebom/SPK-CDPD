"""L02 DESIGN ORACLES (FIX001 origin, FIX002 front-end), never product runtime.

All persistence/process/CDP observations are injected model inputs. Bundle maps
contain synthetic bytes; no browser, filesystem mutation, human approval, or
runtime durability is performed here. L04--L08 must implement these contracts.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any
import copy
import hashlib

from design_model import canonical, strict_json, classify_matrix, transition, capture_result


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def object_digest(value: Any) -> str:
    return digest(canonical(value))


def adapter_envelope(event: dict[str, Any], template: dict[str, Any],
                     mapping: dict[str, Any], matrix: dict[str, Any],
                     *, trusted_reason: str | None = None) -> dict[str, Any]:
    """Preserve generic event; no exception object/text accepted by this boundary."""
    row = [r for r in mapping['entries'] if
           (r['kind'], r['failure_code']) == (event['kind'], event['failure_code'])]
    reason = trusted_reason
    if reason is None:
        if len(row) != 1:
            raise ValueError('UNMAPPED_PROFILE_EVENT')
        reason = row[0]['reason']
    envelope = copy.deepcopy(template)
    envelope['event'] = copy.deepcopy(event)
    envelope['profile']['reason'] = reason
    if event['kind'] == 'ACTION_FAILED' and event['failure_code'] in {'UNKNOWN','DISCONNECTED'}:
        # This event alone cannot prove a provider was never dispatched.
        if envelope['profile']['dispatch_state'] not in {'POSSIBLE','CONFIRMED','UNKNOWN'}:
            envelope['profile']['dispatch_state'] = 'UNKNOWN'
    if classify_matrix(envelope, matrix) is None:
        raise ValueError('UNMAPPED_PROFILE_EVENT')
    return envelope


@dataclass
class IncidentModel:
    """Single-writer linearization model; not an OS/CDP atomicity proof."""
    contract: dict[str, Any]
    state: str = 'RECEIVED'
    failure_reasons: set[str] = field(default_factory=set)
    s0_durable: bool = False
    s3_durable: bool = False
    structure_staged: bool = False
    structure_verified: bool = False
    postcheck_ok: bool = False
    worker_normal: bool = False
    final_guards_ok: bool = False
    stages_complete: bool = False
    coverage_complete: bool = True
    history_complete: bool = True
    policy_allowed: bool = True
    bundle_verified: bool = False
    attempts: int = 0
    committed_bytes: bytes | None = None
    subsequent_metadata: list[str] = field(default_factory=list)

    def apply(self, event: str) -> str:
        if self.state in self.contract['terminal_states']:
            # Never alter either the committed snapshot or the failure latch.
            self.subsequent_metadata.append(event)
            return 'DENY'
        nxt = transition(self.contract, self.state, event)
        if nxt == 'DENY':
            return nxt
        if event in self.contract['failure_latch']['events']:
            self.failure_reasons.add(event)
            self.structure_staged = self.structure_verified = False
            self.s3_durable = self.bundle_verified = False
        if event == 'POLICY_DENIED':
            self.policy_allowed = False
            self.structure_staged = self.structure_verified = False
        if event == 'STORAGE_ERROR':
            self.structure_staged = self.structure_verified = False
        if event == 'S0_COMMITTED':
            self.s0_durable = True
        if event == 'START_WORKER':
            if self.attempts >= self.contract['attempts_per_capture_id_max']:
                return 'DENY'
            self.attempts += 1
        if event == 'POSTCHECK_OK':
            self.postcheck_ok = True
        if event.startswith('COMMIT_'):
            status, publish = capture_result(
                policy=self.policy_allowed, postcheck=self.postcheck_ok,
                worker_normal=self.worker_normal, stages_complete=self.stages_complete,
                precheck=self.final_guards_ok, coverage_complete=self.coverage_complete,
                history_complete=self.history_complete,
                failure_latched=bool(self.failure_reasons))
            expected = {'COMMIT_COMPLETE_VERIFIED':'COMPLETE',
                        'COMMIT_PARTIAL_VERIFIED':'PARTIAL',
                        'COMMIT_REJECTED_VERIFIED':'REJECTED'}[event]
            if not (self.s0_durable and self.s3_durable and self.bundle_verified and status == expected):
                return 'DENY'
            if not publish:
                self.structure_staged = self.structure_verified = False
            if self.structure_staged and not self.structure_verified:
                return 'DENY'
            self.committed_bytes = canonical({'capture_status':status,
                'structure_present':self.structure_staged,
                'failure_reasons':sorted(self.failure_reasons), 'S0':True, 'S3':True})
        self.state = nxt
        return nxt


def journal_row(envelope: dict[str, Any], sequence: int) -> dict[str, Any]:
    if type(sequence) is not int or sequence < 1:
        raise ValueError('JOURNAL_SEQUENCE_INVALID')
    row = {'journal_seq':sequence, 'event_id':envelope['event_id'],
           'payload_sha256':object_digest(envelope), 'envelope':copy.deepcopy(envelope)}
    return dict(row, record_sha256=object_digest(row))


def current_projection(current: dict[str, Any]) -> dict[str, Any]:
    active = current['active_operation_ref']
    return {'supervisor_instance_id':current['supervisor_instance_id'],
            'control_generation':current['control_generation'],
            'binding_id':current['binding_id'], 'binding_sha256':current['record_sha256'],
            'operation_id':active['operation_id'], 'armed_event_id':active['armed_event_id'],
            'current_sha256':object_digest(current)}


def receipt_valid(receipt: dict[str, Any], envelope: dict[str, Any], row: dict[str, Any]) -> bool:
    """After Receipt/Envelope schema validation, prove *record* linkage only."""
    try:
        if set(row) != {'journal_seq','event_id','payload_sha256','envelope','record_sha256'}:
            return False
        base = {k:v for k,v in row.items() if k != 'record_sha256'}
        expected = object_digest(base)
        return (receipt['stage'] in {'ACK_DURABLE','CAPTURE_ACCEPTED'}
                and receipt['failure_code'] is None
                and type(row['journal_seq']) is int and row['journal_seq'] > 0
                and canonical(row['envelope']) == canonical(envelope)
                and receipt['event_id'] == row['event_id'] == envelope['event_id']
                and receipt['payload_sha256'] == row['payload_sha256'] == object_digest(envelope)
                and receipt['journal_seq'] == row['journal_seq']
                and receipt['journal_record_sha256'] == row['record_sha256'] == expected)
    except (KeyError, TypeError, ValueError):
        return False


def admission_allowed(receipt: dict[str, Any], envelope: dict[str, Any],
                      row: dict[str, Any], persisted_current: dict[str, Any],
                      live_current: dict[str, Any], *, io: dict[str, bool],
                      now_monotonic: float, started_monotonic: float,
                      current_supervisor_id: str, live_guard: bool,
                      request_cancelled: bool = False, permit_consumed: bool = False) -> bool:
    """An old durable record is not a fresh dispatch permit. No actual IO here."""
    try:
        if not receipt_valid(receipt,envelope,row): return False
        if receipt['stage'] != 'ACK_DURABLE' or receipt['ack_purpose'] != 'OPERATION_ADMISSION': return False
        required_io = ('journal_written','journal_fsynced','current_written','current_fsynced',
                       'current_published','receipt_fsynced','receipt_published','receipt_hash_matches')
        if not all(io.get(k) is True for k in required_io): return False
        if not live_guard or request_cancelled or permit_consumed: return False
        if not (0 <= now_monotonic-started_monotonic < 2.0): return False
        if envelope['profile']['reason'] != 'OPERATION_ARMED': return False
        if envelope['event']['kind'] != 'ACTION_STARTED' or envelope['event']['failure_code'] != 'NONE': return False
        if canonical(persisted_current) != canonical(live_current): return False
        c=live_current; active=c['active_operation_ref']
        return (receipt['current_projection'] == current_projection(c)
                and c['supervisor_instance_id'] == current_supervisor_id
                and c['control_generation'] == envelope['control_generation']
                and c['binding_id'] == envelope['binding_id']
                and c['record_sha256'] == envelope['binding_sha256']
                and c['state'] == 'BOUND' and c['revoked'] is False and c['overlay'] == 'NONE'
                and active['operation_id'] == envelope['event']['operation_id']
                and active['producer_instance_id'] == envelope['event']['producer_instance_id']
                and active['armed_event_id'] == envelope['event_id']
                and active['journal_seq'] == row['journal_seq'])
    except (KeyError,TypeError,ValueError):
        return False


# ---- F-04: source-byte -> manifest -> probe -> verdict -> declared-output check ----

def verified_bundle(blob: dict[str, bytes]) -> tuple[str, dict[str, Any]]:
    """FIX002: mandatory full typed/semantic front-end on source AND baseline."""
    from bundle_input_model import verify_report_bundle
    return verify_report_bundle(blob)


def checked_probe(p: dict[str, Any]) -> dict[str, Any]:
    if set(p)!={'feature_id','match_count','visible_count','enabled_count','probe_complete'}:
        raise ValueError('PROBE_SHAPE')
    nums=[p[k] for k in ('match_count','visible_count','enabled_count')]
    if not all(type(n) is int and 0<=n<=2000 for n in nums): raise ValueError('PROBE_COUNT')
    if nums[1]>nums[0] or nums[2]>nums[0] or type(p['probe_complete']) is not bool: raise ValueError('PROBE_COUNT')
    return {k:p[k] for k in ('match_count','visible_count','enabled_count','probe_complete')}


def probe_map(payload: dict[str,Any], policy: dict[str,Any]) -> tuple[dict[str,Any],bool]:
    result={};bad=False
    try:
        probes=payload['STRUCTURE.json']['profile']['probes']
        if not isinstance(probes,list) or len(probes)>50: raise ValueError('PROBES')
        for idx,p in enumerate(probes):
            feature=p['feature_id']
            if feature not in policy['baseline_catalog'] or feature in result: raise ValueError('PROBE_ID')
            result[feature]=(idx,checked_probe(p))
    except (KeyError,TypeError,ValueError):
        bad=True
    return result,bad


def expected_probe(policy: dict[str,Any], feature: str, recipe: str):
    spec=policy['probe_contracts'][feature]
    if recipe not in spec['recipes']: return None,False
    expectation=copy.deepcopy(spec['expected'])
    expectation.update(spec.get('recipe_overrides',{}).get(recipe,{}))
    return expectation,spec['required']


def satisfies(obs: dict[str,Any], expected: dict[str,Any]) -> bool:
    return (obs['match_count'] >= expected['match_min']
            and (expected['match_max'] is None or obs['match_count'] <= expected['match_max'])
            and obs['visible_count'] >= expected['visible_min']
            and obs['enabled_count'] >= expected['enabled_min'])


def checked_shape(shape: Any) -> list[dict[str,Any]]:
    if not isinstance(shape,list) or len(shape)>2000: raise ValueError('SHAPE')
    tags={'div','span','input','textarea','button','form','section','article'}
    roles={None,'textbox','button','form','region','group'}
    for i,node in enumerate(shape):
        if not isinstance(node,dict) or set(node)!={'tag','role','parent','shadow'}:raise ValueError('SHAPE')
        if node['tag'] not in tags or node['role'] not in roles or type(node['shadow']) is not bool:raise ValueError('SHAPE')
        parent=node['parent']
        if parent is not None and (type(parent) is not int or not 0<=parent<i):raise ValueError('SHAPE')
    return shape


def stages_verified(profile: dict[str,Any]) -> bool:
    stages=profile.get('stages',{})
    if not isinstance(stages,dict):return False
    for name in ('S0','S1','S2','S3'):
        stage=stages.get(name)
        if not isinstance(stage,dict) or set(stage)!={'status','reason_code','duration_ms'}:return False
        if stage['status']!='COMPLETE' or stage['reason_code'] is not None:return False
        if type(stage['duration_ms']) is not int or stage['duration_ms']<0:return False
    return True


def derive_report(source_blob: dict[str,bytes], baseline_blob: dict[str,bytes] | None,
                  approval: dict[str,Any] | None, policy: dict[str,Any]) -> dict[str,Any]:
    """Derive, never trust a precomputed caller 'overall' or prerequisite bool.

    Approval is a TRUSTED mocked local approval record bound to exact hashes;
    this model cannot authenticate a human. Product L08 must read that record
    through its local approval boundary. Evidence bools are verified file data,
    not proof that the unimplemented Collector really observed those conditions.
    """
    unknown=set(); findings=[]; required_failure=False; drift=False
    sh=None;bh=None;src={};base={}
    try:sh,src=verified_bundle(source_blob)
    except ValueError as exc:unknown.update(getattr(exc,'report_reasons',('INTEGRITY_INVALID',)))
    if baseline_blob is not None:
        try:bh,base=verified_bundle(baseline_blob)
        except ValueError as exc:unknown.update(getattr(exc,'report_reasons',('INTEGRITY_INVALID',)))
    obj=lambda v: v if isinstance(v,dict) else {}
    sr=src.get('RESULT.json',{});sp=obj(sr.get('profile'))
    br=base.get('RESULT.json',{});bp=obj(br.get('profile'))
    sc=obj(sp.get('report_context'));bc=obj(bp.get('report_context'))
    recipe=sc.get('recipe','UNDETERMINED')
    recipes={'IDLE_EMPTY','IDLE_INPUT_PRESENT','GENERATING','RESPONSE_READY'}
    if not isinstance(recipe,str) or recipe not in recipes:recipe='UNDETERMINED'
    capture_status=sr.get('capture_status','NOT_CAPTURED')
    if capture_status not in {'COMPLETE','PARTIAL','REJECTED','NOT_CAPTURED'}:capture_status='NOT_CAPTURED'
    if capture_status!='COMPLETE':unknown.add('CAPTURE_NOT_COMPLETE')
    # Missing evidence yields UNKNOWN, even if the report declares every bool true.
    identity=obj(sp.get('identity_checks'))
    if not all(identity.get(k) is True for k in ['before','after','current','keeper','lease']):unknown.add('BINDING_NOT_VERIFIED')
    if sp.get('policy_allowed') is not True or obj(sp.get('sanitization')).get('passed') is not True:unknown.add('POLICY_NOT_ALLOWED')
    coverage=(sp.get('coverage_complete') is True and obj(sp.get('history_coverage')).get('complete') is True)
    if not coverage:unknown.add('REQUIRED_COVERAGE_MISSING')
    if not stages_verified(sp):unknown.add('REQUIRED_EVIDENCE_MISSING')
    relation=sp.get('observation_relation')
    kind=src.get('TRIGGER.json',{}).get('kind')
    if relation=='CURRENT_OBSERVATION':
        if kind!='MANUAL':unknown.add('DOCUMENT_RELATION_UNKNOWN')
    elif relation!='SAME_DOCUMENT_VERIFIED':unknown.add('DOCUMENT_RELATION_UNKNOWN')
    context_fields=['recipe','locale_class','viewport_class','browser_profile']
    comparable=(recipe in recipes and sc.get('context_verified') is True
        and bc.get('context_verified') is True
        and all(isinstance(sc.get(k),str) and sc.get(k)==bc.get(k) for k in context_fields))
    if not comparable:unknown.add('CONTEXT_NOT_COMPARABLE')
    cat=object_digest(policy['probe_contracts']);norm=object_digest(policy['normalization_contract'])
    policy_hash=object_digest(policy)
    if any(c.get('catalog_sha256')!=cat or c.get('normalization_sha256')!=norm
           or c.get('policy_sha256')!=policy_hash for c in [sc,bc]):unknown.add('CATALOG_NOT_COMPARABLE')
    ap=obj(approval)
    approved=(bh is not None and ap.get('approved') is True
        and ap.get('source_manifest_sha256')==bh
        and all(ap.get(k)==bc.get(k) and bc.get(k) is not None for k in context_fields+['catalog_sha256','normalization_sha256','policy_sha256'])
        and br.get('capture_status')=='COMPLETE' and bp.get('coverage_complete') is True
        and obj(bp.get('history_coverage')).get('complete') is True
        and bp.get('policy_allowed') is True and obj(bp.get('sanitization')).get('passed') is True
        and all(obj(bp.get('identity_checks')).get(k) is True for k in ['before','after','current','keeper','lease'])
        and stages_verified(bp)
        and bp.get('observation_relation') in {'CURRENT_OBSERVATION','SAME_DOCUMENT_VERIFIED'})
    sm,sbad=probe_map(src,policy);bm,bbad=probe_map(base,policy)
    if sbad:unknown.add('REQUIRED_EVIDENCE_MISSING')
    if bbad:approved=False
    for feature in policy['baseline_catalog']:
        expectation,required=expected_probe(policy,feature,recipe)
        si,obs=sm.get(feature,(None,None));bi,bobs=bm.get(feature,(None,None))
        f={'feature_id':feature,'required':required,'verdict':'NOT_APPLICABLE','reason_code':'NOT_APPLICABLE',
           'evidence_pointer':None,'baseline_pointer':None,'coverage_pointer':None,'observed':None,'expected':None}
        if expectation is not None:
            f.update(expected=expectation,observed=obs,
                evidence_pointer=f'STRUCTURE.json#/profile/probes/{si}' if si is not None else None,
                baseline_pointer=f'STRUCTURE.json#/profile/probes/{bi}' if bi is not None else None,
                coverage_pointer='RESULT.json#/profile/coverage_complete' if sh is not None else None)
            if bobs is None or bobs['probe_complete'] is not True or (required and not satisfies(bobs,expectation)):
                approved=False
            if obs is None or obs['probe_complete'] is not True:
                f.update(verdict='UNKNOWN',reason_code='PROBE_INCOMPLETE');unknown.add('REQUIRED_PROBE_INCOMPLETE')
            elif required and not satisfies(obs,expectation):
                f.update(verdict='BREAKING',reason_code='REQUIRED_FUNCTION_VIOLATED');required_failure=True
            elif not required and bobs is not None and obs!=bobs:
                f.update(verdict='REVIEW',reason_code='OPTIONAL_FUNCTION_DRIFT');drift=True
            else:f.update(verdict='PASS',reason_code='CONTRACT_SATISFIED')
        findings.append(f)
    if not approved:unknown.add('BASELINE_NOT_APPROVED')
    # Compare safe normalized structural evidence; never consume a caller's
    # 'significant_drift=True/False' assertion as the verdict oracle.
    for feature in policy['baseline_catalog']:
        exp,_=expected_probe(policy,feature,recipe)
        if exp is None: continue
        try:
            left=checked_shape(src['STRUCTURE.json']['profile']['feature_shapes'][feature])
            right=checked_shape(base['STRUCTURE.json']['profile']['feature_shapes'][feature])
            if left!=right:drift=True
        except (KeyError,TypeError,ValueError):unknown.add('REQUIRED_EVIDENCE_MISSING')
    status='UNKNOWN' if unknown else 'BREAKING' if required_failure else 'REVIEW' if drift else 'PASS'
    reasons=[k for k in policy['unknown_conditions'] if k in unknown]
    if required_failure:reasons.append('REQUIRED_FUNCTION_VIOLATED')
    if drift:reasons.append('SIGNIFICANT_DRIFT')
    actions={'PASS':'NONE','BREAKING':'REVIEW_FEATURE_CONTRACT','REVIEW':'REVIEW_NONBREAKING_DRIFT','UNKNOWN':'COLLECT_OR_VERIFY_EVIDENCE'}
    return {'schema':'cdpd.chatgpt-report/v1','scope':policy['scope'],'assessment_status':status,
        'capture_status':capture_status,'source_manifest_sha256':sh,'baseline_manifest_sha256':bh,
        'baseline_approved':approved,'context_comparable':comparable,'coverage_complete':coverage,
        'reason_codes':reasons,'findings':findings,'automatic_action':'NONE','context_recipe':recipe,
        'catalog_sha256':cat if sc.get('catalog_sha256')==cat else None,
        'normalization_sha256':norm if sc.get('normalization_sha256')==norm else None,
        'not_proven':policy['not_proven'],'hypothesis_code':'NOT_EVALUATED',
        'confidence':'NOT_ESTIMATED','recommended_action':actions[status]}


def report_semantics_valid(declared: dict[str,Any], source_blob: dict[str,bytes],
                           baseline_blob: dict[str,bytes] | None, approval: dict[str,Any] | None,
                           policy: dict[str,Any]) -> bool:
    """Call only after strict parser + Report Schema; rejects forged pointers/bools."""
    try:return canonical(declared)==canonical(derive_report(source_blob,baseline_blob,approval,policy))
    except (KeyError,TypeError,ValueError):return False


def validate_report_bytes(raw: bytes, source_blob: dict[str,bytes],
                          baseline_blob: dict[str,bytes] | None,
                          approval: dict[str,Any] | None, policy: dict[str,Any]) -> bool:
    """Single consumer entry: strict Report parser/schema plus mandatory source checks.

    A valid UNKNOWN diagnostic can describe invalid evidence. A declared PASS
    cannot bypass source validation by setting caller flags or pointers.
    """
    from pathlib import Path
    from jsonschema import Draft202012Validator, FormatChecker
    import json
    try:
        if type(raw) is not bytes:return False
        value=strict_json(raw,262144)
        schema=json.loads((Path(__file__).resolve().parents[1]/'contracts/report_v1.schema.json').read_bytes())
        if not Draft202012Validator(schema,format_checker=FormatChecker()).is_valid(value):return False
        return report_semantics_valid(value,source_blob,baseline_blob,approval,policy)
    except (KeyError,TypeError,ValueError,UnicodeError,RecursionError,OverflowError):
        return False
