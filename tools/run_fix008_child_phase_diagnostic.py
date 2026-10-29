#!/usr/bin/env python3
"""Local synthetic only FIX008 first-message phase survey.

No real Chrome, no production Worker instrumentation, no raw log capture.
The output is sanitized phases+relative timings from opt-in test fixture files.
NOT a substitute for the pinned 1,462-ID acceptance suite.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
CASES = {
    'SLOW_PERMIT': 'tests/test_l06_process_worker.py::test_slow_permit_callback_wait_is_bounded_and_late_result_discarded',
    'TERMINAL_BINDING': 'tests/test_l06_fix001_e2e.py::test_authenticated_terminal_notice_survives_later_owned_child_failure[result_then_hang-DEADLINE]',
    'WATCHDOG_PREPARE': 'tests/test_l07_watchdog_runner.py::test_blocked_terminal_prepare_preserves_authenticated_cause_and_kill',
}
ALLOWED = {'fixture_start','product_import_begin','product_import_end','stdin_request_read','first_challenge_write','fixture_serve_begin','fixture_serve_end'}


def probe(out: Path, python: str) -> int:
    if not (ROOT/'SOURCE_LOCK.json').exists() or json.loads((ROOT/'SOURCE_LOCK.json').read_text())['candidate_id'] != 'CDPD-L07_CANDIDATE_v0.3.0_IMPL001_FIX008':
        raise RuntimeError('FIX008_SOURCE_REQUIRED')
    if out.resolve().is_relative_to(ROOT.resolve()):
        raise RuntimeError('OUTPUT_MUST_BE_EXTERNAL_TO_CANDIDATE')
    out.mkdir(parents=True,exist_ok=False)
    phase_dir=out/'phases';phase_dir.mkdir()
    watched = [ROOT / x for x in (
        'code/cdpd_chatgpt_profile/process_runner.py',
        'tests/l06_process_fixture.py',
        'tests/l06_fix001_child_fixture.py',
        'tests/fix008_child_phase.py',
    )]
    before = {x.relative_to(ROOT).as_posix():hashlib.sha256(x.read_bytes()).hexdigest() for x in watched}
    rows=[]
    for name,nodeid in CASES.items():
        log=phase_dir/(name+'.jsonl')
        env=os.environ.copy()
        env.update(PYTHONDONTWRITEBYTECODE='1', PYTEST_DISABLE_PLUGIN_AUTOLOAD='1', CDPD_TEST_BROWSER='0', CDPD_FIX008_CHILD_PHASE_LOG=str(log))
        env['PYTHONPATH']=os.pathsep.join([str(ROOT/'code'),str(ROOT/'tests'),*(str(p) for p in sorted((ROOT/'packages').glob('*/src')))])
        env.pop('PYTEST_ADDOPTS',None);env.pop('PYTEST_PLUGINS',None)
        started=time.monotonic()
        cp=subprocess.run([python,'-B','-m','pytest','-q','-p','no:cacheprovider',nodeid],cwd=ROOT,env=env,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=60)
        phases=[]
        if log.is_file():
            for line in log.read_text(encoding='utf8').splitlines()[:40]:
                try:
                    row=json.loads(line)
                    if set(row)=={'phase','elapsed_ms'} and row['phase'] in ALLOWED and type(row['elapsed_ms']) in (int,float) and 0<=row['elapsed_ms']<=60000:
                        phases.append(row)
                except Exception:pass
        # No raw pytest stdout/stderr is persisted; only status and sanitized
        # fixture phase-relative timing. This is timing-invasive diagnostics.
        rows.append({'case':name,'test_id':nodeid,'returncode':cp.returncode,
            'pytest_wall_ms':round((time.monotonic()-started)*1000,2),
            'child_phases':phases,'phase_count':len(phases),
            'raw_stdout_or_stderr_saved':False})
    after = {x.relative_to(ROOT).as_posix():hashlib.sha256(x.read_bytes()).hexdigest() for x in watched}
    unchanged = before == after
    content={'schema':'cdpd.l07.fix008-child-side-phase-investigation/v1',
             'created_utc':datetime.now(timezone.utc).isoformat(),
             'candidate_only':True,'diagnostic_not_formal_timing':True,
             'product_code_modified':False,'expected_cases':list(CASES),
             'cases':rows,'watched_sources_unchanged':unchanged,
             'full_pinned_suite':'NOT_RUN','native_product_acceptance':'NOT_RUN'}
    (out/'CHILD_PHASE_EVIDENCE.json').write_text(json.dumps(content,indent=2,ensure_ascii=False)+'\n',encoding='utf8')
    print(json.dumps({'cases':[{k:r[k] for k in ('case','returncode','phase_count')} for r in rows],
        'output_file':str(out/'CHILD_PHASE_EVIDENCE.json'),'source_unchanged':unchanged},ensure_ascii=False))
    return 0 if unchanged else 2  # test failure is diagnostic; source mutation is not

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--python',default=sys.executable)
    args=parser.parse_args()
    try:raise SystemExit(probe(args.output,args.python))
    except (OSError,RuntimeError,subprocess.TimeoutExpired) as e:
        print('DIAGNOSTIC_ERROR_'+type(e).__name__,file=sys.stderr)
        raise SystemExit(2)
