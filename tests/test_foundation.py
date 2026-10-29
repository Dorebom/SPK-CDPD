from __future__ import annotations
import ast
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
import jsonschema
from cdpd import *
from cdpd.collector import _local_endpoint
from cdpd_chatgpt_adapter import BoundedEventQueue, InstrumentedFacade
from chatgpt_web_api.api import create_facade
from chatgpt_web_api.facade_contract import validate_facade_contract, FacadeContractViolation

ROOT = Path(__file__).resolve().parents[1]


def event(**kwargs):
    return DiagnosticEvent('app', 'instance', 'operation', 'MANUAL', **kwargs)


def request():
    return CaptureRequest(event(), TargetBinding('http://127.0.0.1:9222', 'ABC', 'http://127.0.0.1:8765'))


@pytest.mark.parametrize('kwargs', [{'kind':'BAD'}, {'producer_id':''}, {'correlation':{'a':3}}, {'schema':'host.event/v1'}, {'failure_code':'free text'}])
def test_event_rejects_invalid(kwargs):
    data=event().to_dict();data.update(kwargs)
    with pytest.raises((ValueError,TypeError)):
        DiagnosticEvent.from_dict(data)


def test_correlation_echo_does_not_normalize():
    correlation={'任意のID':'  e\u0301 / ../run\\x  '}
    e=event(correlation=correlation)
    assert e.to_dict()['correlation']==correlation
    correlation['new']='added'
    assert 'new' not in e.to_dict()['correlation']


def test_schema_roundtrip():
    obj=request().to_dict()
    jsonschema.Draft202012Validator(json.loads((ROOT/'schemas/capture-request.schema.json').read_text())).validate(obj)
    assert CaptureRequest.from_dict(obj).to_dict()==obj


@pytest.mark.parametrize('key', ['prompt','arbitrary_js','output_path','run_id'])
def test_unknown_capture_fields_rejected(key):
    obj=request().to_dict();obj[key]='bad'
    with pytest.raises(ValueError):CaptureRequest.from_dict(obj)


@pytest.mark.parametrize('endpoint',['http://localhost:9222','http://example.org:9222','http://user:pass@127.0.0.1:9222','http://127.0.0.1:9222/?token=x','https://127.0.0.1:9222','http://127.0.0.1:9222/path'])
def test_endpoint_fail_closed(endpoint):
    with pytest.raises(CaptureError):_local_endpoint(endpoint)


@pytest.mark.parametrize('timeout',[0,True,float('nan'),float('inf'),21])
def test_capture_invalid_timeout(timeout):
    with pytest.raises(ValueError):CaptureRequest(event(),request().target,timeout_seconds=timeout)


class FakeCollector:
    def collect(self, request):
        return {'schema':'cdpd.structure/v1','nodes':[], 'truncated':False}


def test_generic_service_without_host_or_browser(tmp_path):
    service=DiagnosticService(FakeCollector(),EvidenceStore(tmp_path))
    service.record(event())
    result=service.capture(request())
    assert result.capture_status=='COMPLETE'
    assert verify_bundle(result.bundle_path)['integrity_valid']
    assert result.assessment_status=='NOT_PERFORMED'


def test_capture_error_yields_metadata_only_bundle(tmp_path):
    class Failed:
        def collect(self, req):raise CaptureError('CDP_CONNECT_FAILED')
    result=DiagnosticService(Failed(),EvidenceStore(tmp_path)).capture(request())
    assert result.capture_status=='PARTIAL'
    assert not (Path(result.bundle_path)/'STRUCTURE.json').exists()
    assert verify_bundle(result.bundle_path)['integrity_valid']


def test_raw_exception_is_not_persisted(tmp_path):
    class Failed:
        def collect(self, req):raise RuntimeError('TOP_SECRET token=abc')
    result=DiagnosticService(Failed(),EvidenceStore(tmp_path)).capture(request())
    assert b'TOP_SECRET' not in b''.join(p.read_bytes() for p in Path(result.bundle_path).iterdir())


def test_disabled_policy_never_connects(tmp_path,monkeypatch):
    import cdpd.collector as module
    def fail(*args,**kwargs):raise AssertionError('should not connect')
    monkeypatch.setattr(module,'_Connection',fail)
    result=DiagnosticService(CDPStructureCollector(CapturePolicy(())),EvidenceStore(tmp_path)).capture(request())
    assert result.capture_status=='REJECTED' and result.failure_code=='POLICY_DENIED'


@pytest.mark.parametrize('alteration',['tamper','extra','missing_commit','traversal'])
def test_bundle_verifier_fails_closed(tmp_path,alteration):
    path=Path(DiagnosticService(FakeCollector(),EvidenceStore(tmp_path)).capture(request()).bundle_path)
    if alteration=='tamper':(path/'TRIGGER.json').write_text('{}')
    if alteration=='extra':(path/'extra.txt').write_text('x')
    if alteration=='missing_commit':(path/'COMMIT.json').unlink()
    if alteration=='traversal':
        (path/'MANIFEST.json').write_text('{"files":[{"path":"../secret"}]}')
    assert not verify_bundle(path)['integrity_valid']


def test_unique_captures_do_not_overwrite(tmp_path):
    service=DiagnosticService(FakeCollector(),EvidenceStore(tmp_path))
    a=service.capture(request());b=service.capture(request())
    assert a.bundle_path!=b.bundle_path
    assert verify_bundle(a.bundle_path)['integrity_valid']


def test_memory_ring_is_bounded(tmp_path):
    service=DiagnosticService(FakeCollector(),EvidenceStore(tmp_path),max_events=2)
    for _ in range(5):service.record(event())
    outcome=service.capture(request())
    saved=json.loads((Path(outcome.bundle_path)/'EVENTS.json').read_text())
    assert len(saved['events'])==2 and service.dropped_events==3


def test_ring_isolated_by_producer_instance(tmp_path):
    service=DiagnosticService(FakeCollector(),EvidenceStore(tmp_path))
    service.record(DiagnosticEvent('other','instance','x','MANUAL'))
    outcome=service.capture(request())
    assert not json.loads((Path(outcome.bundle_path)/'EVENTS.json').read_text())['events']


def test_sdk_core_has_no_provider_imports():
    for path in (ROOT/'packages/cdpd/src/cdpd').glob('*.py'):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node,ast.ImportFrom):
                assert not (node.module or '').startswith(('chatgpt','cdpd_shirabe','tools'))


def test_only_new_public_contract_is_required():
    new = create_facade(session=object())
    assert new.contract_name == 'chatgpt-web.facade'
    assert validate_facade_contract(new).major_version == 2
    assert (ROOT/'packages/cdpd-shirabe-compat').exists() is False
    assert not list((ROOT/'dist').glob('*shirabe_compat*'))


def test_sdk_has_no_old_shirabe_compat_import():
    import ast
    src = ROOT/'packages/cdpd/src/cdpd'
    for f in src.glob('*.py'):
        assert 'cdpd_shirabe_compat' not in f.read_text(encoding='utf-8')
