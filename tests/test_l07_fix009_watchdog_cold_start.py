"""Explicit cold-start fail-closed precondition, separate from Blocking Prepare.

This does NOT substitute for authenticated TERMINAL->prepare test: no notice is
expected at all. No deadline, Job/PID or permit acceptance checks are relaxed.
"""
from __future__ import annotations
from copy import deepcopy
import os

from cdpd_chatgpt_profile.watchdog_runner import WatchdogWorkerRunner
from cdpd_chatgpt_profile.worker import WorkerRequest
from test_capture_worker import Permit, request_data
from test_l07_watchdog_runner import FixtureRunner


def test_one_second_cold_start_withheld_notice_remains_fail_closed():
    permit=Permit()
    runner=FixtureRunner('hang', fixed=False)
    result=runner(WorkerRequest.from_dict(request_data(timeout=1.0)),permit=permit)
    m=deepcopy(runner.last_process_metadata)
    assert result['capture_status']=='PARTIAL'
    assert result['structure'] is None
    assert m['timed_out'] is True
    assert 'terminal_notice' not in m and 'terminal_receipt' not in m
    assert m['permit_checks']==0 and len(permit.challenges)==0
    assert m['termination_facts']['confirmed'] is True
    assert m['termination_facts']['active_processes']==0
    assert m['deadline_overrun_ms']<=250
    assert not m['termination_unconfirmed']
    # A 1s cold start cannot be construed as proof that blocked prepare ran.
