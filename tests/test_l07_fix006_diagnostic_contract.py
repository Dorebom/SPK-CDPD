"""FIX006 tests: the diagnostic cannot authorize a rejected strict RESULT.

Only in-process synthetic browser, no worker launch, Chrome, network or file store.
"""
from __future__ import annotations
from copy import deepcopy
import os
import pytest
from cdpd_chatgpt_profile.worker import WorkerRequest, run_capture_worker
from cdpd_chatgpt_profile.process_runner import _valid_result
from l07_fix006_protocol_diagnostic import result_reason, RESULT_CODES, bounded_enum_histogram
from test_capture_worker import Connection,Permit,request_data


def good():
    data=WorkerRequest.from_dict(request_data()).to_dict()
    result=run_capture_worker(WorkerRequest.from_dict(data),permit=Permit(),
        connection_factory=lambda endpoint,timeout: Connection())
    assert _valid_result(result,data,os.getpid())
    return data,result


def test_fix006_strict_probe_accepts_only_original_validated_result():
    data,result=good()
    assert result_reason(result,data,os.getpid())=='VALID'
    assert _valid_result(result,data,os.getpid())

@pytest.mark.parametrize('field,corrupt,reason',[
    ('schema','UNKNOWN','SCHEMA'),
    ('pid',lambda old:old+1,'PID'),
    ('capture_id','C-invalid','CAPTURE_ID'),
    ('script_sha256','0'*64,'SCRIPT_DIGEST'),
    ('probe_catalog_sha256','0'*64,'CATALOG_DIGEST'),
    ('identity_checks',{'before':True},'IDENTITY_SHAPE'),
    ('structure',None,'NO_STRUCTURE_CONSISTENCY'),
    ('extra',True,'ENVELOPE_KEYS'),
])
def test_fix006_strict_rejections_remain_rejected(field,corrupt,reason):
    data,result=good()
    rejected=deepcopy(result)
    rejected[field]=corrupt(result[field]) if callable(corrupt) else corrupt
    assert _valid_result(rejected,data,os.getpid()) is False
    assert result_reason(rejected,data,os.getpid())==reason


def test_fix006_histogram_cannot_store_untrusted_child_data():
    got=bounded_enum_histogram(['PID','PID','SECRET/path/raw','VALIDATOR_OTHER'], RESULT_CODES)
    assert got=={'PID':2,'VALIDATOR_OTHER':2}
    assert not any('SECRET' in key or '/' in key for key in got)
