"""FIX009 candidate-only synthetic fixture contract and negative controls.

Full baseline is byte-for-byte FIX008, stored under inputs/. Compare both
fixture implementations using the identical product Worker and strict parent.
No browser, network, actual authority or production runtime is exercised.
"""
from __future__ import annotations

import ast
from copy import deepcopy
import os
from pathlib import Path
import subprocess
import sys

import pytest

from cdpd_chatgpt_profile.process_runner import ProcessWorkerRunner
from cdpd_chatgpt_profile.worker import WorkerRequest
from test_capture_worker import Connection as OriginalConnection, Permit, request_data, valid_structure
from l06_synthetic_connection import Connection as LightConnection, valid_structure as light_structure

ROOT = Path(__file__).resolve().parents[1]
ORIGINAL_TEST = ROOT / 'tests/test_capture_worker.py'
LIGHT_TEST = ROOT / 'tests/l06_synthetic_connection.py'
BASELINE = ROOT / 'inputs/fix008_synthetic_fixture_baseline'
COPY_NAMES = ('EPOCH', 'TARGET', 'ORIGIN', 'FEATURES', 'READ_METHODS', 'node', 'valid_structure', 'Connection')


@pytest.fixture(autouse=True)
def synthetic_import_paths(monkeypatch):
    paths = [str(ROOT/'code'), str(ROOT/'tests')]
    paths.extend(str(p) for p in sorted((ROOT/'packages').glob('*/src')))
    paths.extend(p for p in sys.path if p and ('site-packages' in p or 'python_deps' in p))
    if os.environ.get('PYTHONPATH'):
        paths.append(os.environ['PYTHONPATH'])
    monkeypatch.setenv('PYTHONPATH', os.pathsep.join(paths))
    monkeypatch.setenv('PYTHONDONTWRITEBYTECODE', '1')
    monkeypatch.setenv('CDPD_TEST_BROWSER', '0')
    monkeypatch.delenv('CDPD_FIX008_CHILD_PHASE_LOG', raising=False)


def exported_source_nodes(file):
    result = {}
    for item in ast.parse(file.read_text(encoding='utf8')).body:
        name = None
        if isinstance(item, ast.Assign):
            if len(item.targets) == 1 and isinstance(item.targets[0],ast.Name):
                name = item.targets[0].id
        elif isinstance(item, (ast.FunctionDef, ast.ClassDef)):
            name = item.name
        if name in COPY_NAMES:
            result[name] = ast.dump(item, include_attributes=False)
    return result


def test_exact_synthetic_ast_and_forbidden_import_exclusion():
    original = exported_source_nodes(ORIGINAL_TEST)
    light = exported_source_nodes(LIGHT_TEST)
    assert set(original) == set(light) == set(COPY_NAMES)
    assert original == light
    imports = [n for n in ast.walk(ast.parse(LIGHT_TEST.read_text())) if isinstance(n, (ast.Import, ast.ImportFrom))]
    banned = {'pytest','test_capture_worker','subprocess','socket','playwright','selenium','pyppeteer'}
    assert not any((alias.name.split('.')[0] in banned) for n in imports for alias in getattr(n,'names',()))
    assert not any(n.module and n.module.split('.')[0] in banned for n in imports if isinstance(n,ast.ImportFrom))
    for name in ('l06_process_fixture.py','l06_fix001_child_fixture.py'):
        fixed=(ROOT/'tests'/name).read_text()
        baseline=(BASELINE/name).read_text()
        assert fixed.count('from l06_synthetic_connection import ') == 1
        assert fixed.replace('from l06_synthetic_connection import ', 'from test_capture_worker import ') == baseline


@pytest.mark.parametrize('variant', ['default','truncated','exception','wrong_browser','wrong_origin','wrong_target','extra_page','navigation','disconnect','wrong_method','close'])
def test_connection_semantic_equivalence_negative_matrix(variant):
    def prep(cls):
        state = {}
        if variant == 'truncated':state['structure']=valid_structure(truncated=True)
        if variant == 'exception':state['fail_method']='Runtime.evaluate'
        inst=cls(**state)
        if variant == 'wrong_browser':inst.browser_id='0'*32
        if variant == 'wrong_origin':inst.url='https://example.invalid/no-browser'
        if variant == 'wrong_target':inst.target_id='not-selected-target'
        if variant == 'extra_page':inst.extra_targets=[{'targetId':'other','type':'page','url':'https://chatgpt.com/'}]
        if variant == 'navigation':inst.loader_id='SECOND-LOADER'
        if variant == 'disconnect':inst.disconnected=True
        return inst
    def observe(cls):
        conn=prep(cls)
        methods=[('Target.getTargetInfo',{},None),('Target.attachToTarget',{'targetId':'SYNTHETIC-TARGET-A'},None),
                 ('Page.getFrameTree',{},'SYNTHETIC-SESSION-A'),
                 ('Page.createIsolatedWorld',{'frameId':conn.frame_id},'SYNTHETIC-SESSION-A'),
                 ('Runtime.evaluate',{},'SYNTHETIC-SESSION-A')]
        if variant == 'wrong_method':methods.append(('Page.navigate',{},None))
        outcomes=[]
        for f,args,session in methods:
            try:outcomes.append(('OK',deepcopy(conn.send(f,args,session))))
            except Exception as error:outcomes.append(('ERROR',type(error).__name__,getattr(error,'code',None)))
        try:identity=('OK',conn.identity())
        except Exception as error:identity=('ERROR',type(error).__name__,getattr(error,'code',None))
        conn.close('SYNTHETIC-SESSION-A')
        return {'outcomes':outcomes,'identity':identity,'closed':conn.closed,
                'target':conn.target_info(),'audit':conn.audit,'calls':conn.calls,
                'identity_calls':conn.identity_calls}
    assert observe(OriginalConnection) == observe(LightConnection)
    assert valid_structure() == light_structure()


class _ChildRunner(ProcessWorkerRunner):
    def __init__(self, path:Path, mode:str):
        super().__init__()
        self._script=path
        self._mode=mode
    def _command(self):
        return [sys.executable,'-B',str(self._script),self._mode]


def _run_variant(variant, group, mode, *, timeout=8.0):
    name='l06_process_fixture.py' if group=='process' else 'l06_fix001_child_fixture.py'
    path=(BASELINE/name) if variant=='original' else ROOT/'tests'/name
    runner=_ChildRunner(path,mode)
    permit=Permit()
    result=runner(WorkerRequest.from_dict(request_data(timeout=timeout)),permit=permit)
    meta=runner.last_process_metadata
    return {'status':result['capture_status'], 'failure_code':result['failure_code'],
            'has_structure':result['structure'] is not None,
            'worker_termination':result['worker_termination'],
            'identity_checks':result['identity_checks'],
            'command_audit':result['command_audit'],
            'protocol_failure':meta.get('failure_reason'),
            'termination':meta.get('termination'),
            'terminal_phase':(meta.get('terminal_notice') or {}).get('phase'),
            'permit_check_count':meta.get('permit_checks'),
            'challenge_count':len(permit.challenges),
            'no_unconfirmed_termination':not meta.get('termination_unconfirmed',False)}


PROCESS_MODES=(
    'normal','truncated','capture_exception','epoch_after',
    'wrong_pid','wrong_capture_id','extra_field','unsafe_structure',
    'wrong_challenge','duplicate_nonce','duplicate_result','oversized',
    'early_exit','result_then_nonzero',
)
FIX001_MODES=(
    'normal','normal_detach','preconnect_failure','collector_exception',
    'pages_before','pages_after','origin_before','origin_after',
    'epoch_before','epoch_after','target_before','target_after',
    'navigation_after','disconnect_after',
    'epoch_after+terminal_wrong_capture','epoch_after+terminal_wrong_binding',
    'epoch_after+terminal_wrong_hash','epoch_after+terminal_wrong_generation',
    'epoch_after+terminal_generation_bool','epoch_after+terminal_wrong_supervisor',
    'epoch_after+terminal_duplicate_nonce','epoch_after+terminal_duplicate_notice',
    'epoch_after+no_notice','epoch_after+result_wrong_capture',
    'epoch_after+no_notice_abrupt_death','epoch_after+result_then_nonzero',
)


@pytest.mark.parametrize('mode',PROCESS_MODES)
def test_process_fixture_baseline_light_strict_parent_equivalence(mode):
    a=_run_variant('original','process',mode)
    b=_run_variant('light','process',mode)
    assert b==a, {'mode':mode, 'original':a,'light':b}


@pytest.mark.parametrize('mode',FIX001_MODES)
def test_fix001_fixture_baseline_light_strict_parent_equivalence(mode):
    a=_run_variant('original','fix001',mode)
    b=_run_variant('light','fix001',mode)
    assert b==a, {'mode':mode,'original':a,'light':b}


@pytest.mark.parametrize('group,mode,timeout',[
    ('process','hang',0.4),('process','result_then_hang',1.7),
    ('fix001','epoch_after+no_notice_hang',1.7),('fix001','epoch_after+result_then_hang',1.7),
])
def test_hung_and_late_result_never_accepted_by_either_fixture(group,mode,timeout):
    for variant in ('original','light'):
        result=_run_variant(variant,group,mode,timeout=timeout)
        assert result['has_structure'] is False, (variant,mode,result)
        assert result['no_unconfirmed_termination'] is True, (variant,mode,result)
        assert result['termination'] in {'KILLED','EXITED_ERROR'},(variant,mode,result)
