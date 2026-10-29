"""L06 request, fresh-permit and privacy boundaries; synthetic fixtures only."""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

import pytest

from cdpd_chatgpt_profile.worker import WorkerRequest, run_capture_worker
from test_capture_worker import Connection, Permit, node, observe, request_data, run, valid_structure


@pytest.mark.parametrize("change", [
    "raw_hash", "trigger_hash", "generation", "capture_path", "extra", "wrong_origin",
    "request_target", "operation_id", "unbound_trigger", "invalid_record",
])
def test_request_rejects_cross_bound_or_unapproved_inputs(change, request):
    data = request_data()
    if change == "raw_hash": data["binding_record_sha256"] = "0" * 64
    elif change == "trigger_hash": data["trigger_envelope"]["binding_sha256"] = "0" * 64
    elif change == "generation": data["control_generation"] += 1
    elif change == "capture_path": data["capture_id"] = "../arbitrary-path"
    elif change == "extra": data["approved"] = True
    elif change == "wrong_origin": data["capture_request"]["target"]["expected_origin"] = "https://example.invalid"
    elif change == "request_target": data["capture_request"]["target"]["target_id"] = "SYNTHETIC-TARGET-B"
    elif change == "operation_id": data["capture_request"]["event"]["operation_id"] = "different-operation"
    elif change == "unbound_trigger":
        data["trigger_envelope"].update(binding_id=None, binding_sha256=None, control_generation=0)
    else: data["binding_record_raw_json"] = '{"schema":1,"schema":2}'
    with pytest.raises(Exception) as error:
        WorkerRequest.from_dict(data)
    code = getattr(error.value, "code", type(error.value).__name__)
    assert "arbitrary-path" not in str(error.value)
    observe(request, expected={"accepted": False, "browser_operations": 0},
            actual={"accepted": False, "browser_operations": 0, "reason": code},
            ac=("AC01", "AC02"), cap=("CAP-02", "CAP-03"))


def test_detached_request_cannot_change_after_validation(request):
    data = request_data()
    job = WorkerRequest.from_dict(data)
    original_id = data["capture_id"]
    data["capture_id"] = "C-" + "f" * 32
    data["capture_request"]["target"]["target_id"] = "SYNTHETIC-TARGET-B"
    data["trigger_envelope"]["event"]["operation_id"] = "post-validation-change"
    result = run_capture_worker(job, permit=Permit(), connection_factory=lambda endpoint, timeout: Connection())
    assert result["capture_status"] == "COMPLETE" and result["capture_id"] == original_id
    assert result["structure"]["binding"]["target_id"] == "SYNTHETIC-TARGET-A"
    observe(request, expected={"status": "COMPLETE", "capture_id_preserved": True, "target_preserved": True},
            actual={"status": result["capture_status"], "capture_id_preserved": result["capture_id"] == original_id,
                    "target_preserved": result["structure"]["binding"]["target_id"] == "SYNTHETIC-TARGET-A"},
            ac=("AC01", "AC02"), cap=("CAP-01",), result=result)


@pytest.mark.parametrize("change", [
    "schema", "nonce", "capture_id", "binding_id", "binding_sha256", "control_generation",
    "supervisor_instance_id", "phase", "allowed", "lease_zero", "lease_negative", "lease_nan", "extra",
])
def test_stale_or_invalid_permit_cannot_start_capture(change, request):
    def mutate(response, ordinal):
        if change == "schema": response["schema"] = "untrusted/v1"
        elif change in ("nonce", "capture_id", "binding_id", "binding_sha256", "supervisor_instance_id", "phase"):
            response[change] = "wrong-" + str(response[change])
        elif change == "control_generation": response["control_generation"] += 1
        elif change == "allowed": response.update(allowed=False, reason_code="BINDING_REVOKED")
        elif change == "lease_zero": response["remaining_lease_seconds"] = 0
        elif change == "lease_negative": response["remaining_lease_seconds"] = -0.1
        elif change == "lease_nan": response["remaining_lease_seconds"] = float("nan")
        else: response["trust_me"] = True
        return response
    result, connection, permit, creations = run(permit=Permit(mutate))
    assert result["capture_status"] in ("PARTIAL", "REJECTED") and result["structure"] is None
    assert not creations and not connection.calls
    observe(request, expected={"structure_retained": False, "connections": 0},
            actual={"structure_retained": result["structure"] is not None, "connections": len(creations),
                    "reason": result["failure_code"]}, ac=("AC02", "AC03"), cap=("CAP-02",), result=result)


def test_permit_latency_cannot_extend_remaining_lease(request):
    clock = [100.0]
    def response(value, ordinal):
        clock[0] += 0.25
        value["remaining_lease_seconds"] = 0.1
        return value
    result, connection, permit, creations = run(permit=Permit(response), monotonic=lambda: clock[0])
    assert result["capture_status"] in ("PARTIAL", "REJECTED") and result["structure"] is None
    assert not creations and not connection.calls
    observe(request, expected={"structure_retained": False, "connections": 0, "lease_extended_by_ipc": False},
            actual={"structure_retained": result["structure"] is not None, "connections": len(creations),
                    "lease_extended_by_ipc": bool(creations), "reason": result["failure_code"]},
            ac=("AC02", "AC03"), cap=("CAP-02",), result=result)


def test_total_deadline_not_reset_per_phase(request):
    clock = [100.0]
    connection = Connection()
    def response(value, ordinal):
        clock[0] += 0.03
        return value
    result, connection, permit, creations = run(request_data(timeout=0.1), connection, Permit(response), monotonic=lambda: clock[0])
    assert result["capture_status"] in ("PARTIAL", "REJECTED") and result["structure"] is None
    assert result["failure_code"] in ("CAPTURE_DEADLINE", "LEASE_EXPIRED", "SUPERVISOR_LOST")
    assert len(creations) <= 1 and connection.audit.count("Runtime.evaluate") <= 1
    observe(request, expected={"structure_retained": False, "new_attempts_after_deadline": 0},
            actual={"structure_retained": result["structure"] is not None, "new_attempts_after_deadline": max(0, len(creations)-1),
                    "reason": result["failure_code"], "permit_checks": len(permit.challenges)},
            ac=("AC02", "AC03"), cap=("CAP-04",), result=result)


def test_permit_revocation_after_evaluate_discards_dom(request):
    connection = Connection()
    def response(value, ordinal):
        if "Runtime.evaluate" in connection.audit:
            value.update(allowed=False, reason_code="BINDING_REVOKED")
        return value
    result, connection, permit, creations = run(connection=connection, permit=Permit(response))
    assert result["capture_status"] == "PARTIAL" and result["structure"] is None
    assert connection.audit.count("Runtime.evaluate") == 1
    observe(request, expected={"status": "PARTIAL", "structure_retained": False, "evaluations": 1},
            actual={"status": result["capture_status"], "structure_retained": result["structure"] is not None,
                    "evaluations": connection.audit.count("Runtime.evaluate"), "reason": result["failure_code"]},
            ac=("AC02", "AC03"), cap=("CAP-02",), result=result)


@pytest.mark.parametrize("change", [
    "text", "input_value", "raw_url", "arbitrary_attribute", "nan_box", "unknown_tag", "unknown_role",
    "bad_parent", "duplicate_index", "string_boolean", "extra_probe", "unbounded_match_count", "over_budget",
])
def test_private_or_unbounded_collector_payload_never_crosses_worker_boundary(change, request):
    value = valid_structure()
    if change == "text": value["nodes"][0]["innerText"] = "SYNTHETIC_PRIVATE_TEXT"
    elif change == "input_value": value["nodes"][0]["value"] = "SYNTHETIC_INPUT_VALUE"
    elif change == "raw_url": value["url"] = "https://chatgpt.com/private?token=SYNTHETIC"
    elif change == "arbitrary_attribute": value["nodes"][0]["attributes"] = {"data-secret": "SYNTHETIC"}
    elif change == "nan_box": value["nodes"][0]["box"][0] = float("nan")
    elif change == "unknown_tag": value["nodes"][0]["tag"] = "private-custom-element"
    elif change == "unknown_role": value["nodes"][0]["role"] = "arbitrary-role"
    elif change == "bad_parent": value["nodes"][0]["parent"] = 1
    elif change == "duplicate_index": value["nodes"].append(node())
    elif change == "string_boolean": value["nodes"][0]["disabled"] = "false"
    elif change == "extra_probe": value["profile"]["probes"].append({"feature_id": "arbitrary.field", "match_count": 0, "visible_count": 0, "enabled_count": 0, "probe_complete": True})
    elif change == "unbounded_match_count": value["profile"]["probes"][0]["match_count"] = 2001
    else: value["nodes"] = [node(0, -1), node(1, 0), node(2, 1)]
    data = request_data(max_nodes=2 if change == "over_budget" else 1000)
    result, connection, _, _ = run(data, Connection(structure=value))
    assert result["capture_status"] == "PARTIAL" and result["structure"] is None
    assert result["sanitization_passed"] is False
    encoded = json.dumps(result, allow_nan=False)
    for sentinel in ("SYNTHETIC_PRIVATE_TEXT", "SYNTHETIC_INPUT_VALUE", "private?token", "private-custom-element"):
        assert sentinel not in encoded
    observe(request, expected={"status": "PARTIAL", "structure_retained": False, "sanitization_passed": False},
            actual={"status": result["capture_status"], "structure_retained": result["structure"] is not None,
                    "sanitization_passed": result["sanitization_passed"], "reason": result["failure_code"]},
            ac=("AC02", "AC03"), cap=("CAP-01", "CAP-04"), result=result)


@pytest.mark.parametrize("mode,max_nodes", [
    ("normal", 1000), ("empty_shadow", 1000), ("heavy", 17), ("empty_shadow_budget", 6),
])
def test_exact_fixed_script_traverses_main_open_shadow_and_stays_bounded(mode, max_nodes, request, tmp_path):
    """Run the actual recipe under Node with private DOM accessors that throw."""
    from cdpd_chatgpt_profile.structure_script import capture_script, script_sha256
    executable = shutil.which("node")
    assert executable is not None, "Offline fixed-script control requires installed Node"
    fixture = Path(__file__).with_name("l06_script_dom_fixture.js")
    command = [executable, str(fixture)]
    provided = {"script": capture_script(max_nodes), "mode": mode}
    child = subprocess.run(command, input=json.dumps(provided), capture_output=True,
                           text=True, timeout=5, check=False)
    assert child.returncode == 0 and not child.stderr
    output = json.loads(child.stdout)
    structure, counts = output["structure"], output["observations"]
    assert counts["forbiddenReads"] == []
    assert counts["rectReads"] <= max_nodes
    projected = sum(len(rows) for rows in structure["profile"]["feature_shapes"].values())
    assert len(structure["nodes"]) + projected <= max_nodes
    assert "SYNTHETIC_SECRET" not in json.dumps(structure)
    assert structure["coverage"] == {"scope": "MAIN_FRAME_AND_OPEN_SHADOW", "iframeDocuments": "NOT_CAPTURED",
                                     "closedShadow": "NOT_OBSERVABLE", "accessibilityTree": "NOT_CAPTURED"}
    bounded = mode in {"heavy", "empty_shadow_budget"}
    expected_roots = 0 if bounded else (2 if mode == "empty_shadow" else 1)
    assert structure["openShadowCount"] == expected_roots
    assert structure["openShadowCount"] <= len(structure["nodes"])
    if not bounded:
        assert structure["truncated"] is False
        assert structure["iframeCount"] == 1
        assert len([n for n in structure["nodes"] if n["shadowBoundary"]]) == 2
        assert any(n["tag"] == "#other" for n in structure["nodes"])
        probes = {p["feature_id"]:p for p in structure["profile"]["probes"]}
        assert all(p["match_count"] == 1 and p["probe_complete"] for p in probes.values())
        assert probes["composer.send"]["enabled_count"] == 0
        assert probes["generation.stop"]["visible_count"] == 0
        assert all(n["box"] == [1.1,2.8,100.2,20.3] for n in structure["nodes"])
    else:
        assert structure["truncated"] is True
        assert all(not p["probe_complete"] for p in structure["profile"]["probes"])
        if mode == "heavy":
            assert counts["nodesCreated"] == 5002
        else:
            # Four empty-root hosts were observed but their rows were trimmed
            # to reserve four S1 projections. Only html/body remain in S2.
            assert counts["rectReads"] == 6
            assert projected == 4
            assert [n["tag"] for n in structure["nodes"]] == ["html", "body"]
    # Feed the real script output through the production worker validation.
    result, connection, _, _ = run(request_data(max_nodes=max_nodes), Connection(structure=structure))
    assert result["structure"] is not None
    assert result["capture_status"] == ("PARTIAL" if bounded else "COMPLETE")
    expected = {"forbidden_reads": 0, "shared_node_budget_respected": True,
                "status": "PARTIAL" if bounded else "COMPLETE",
                "retained_open_root_hosts": expected_roots}
    actual = {"forbidden_reads": len(counts["forbiddenReads"]), "shared_node_budget_respected": len(structure["nodes"])+projected <= max_nodes,
              "status": result["capture_status"], "s1_nodes": projected, "s2_nodes":len(structure["nodes"]),
              "retained_open_root_hosts": structure["openShadowCount"],
              "visited_rect_reads":counts["rectReads"], "script_sha256":script_sha256(max_nodes)}
    observe(request, expected=expected, actual=actual, ac=("AC01", "AC02", "AC03"), cap=("CAP-01",), result=result)
    target = os.environ.get("CDPD_L06_EVIDENCE_DIR")
    directory = Path(target) / ("fixed_script_"+mode) if target else tmp_path
    directory.mkdir(parents=True,exist_ok=True)
    (directory / "stdout.txt").write_text(child.stdout)
    (directory / "stderr.txt").write_text(child.stderr)
    (directory / "COMMAND.json").write_text(json.dumps({"argv":command,"stdin":provided,"exit_code":child.returncode,
                                                        "browser_operations":0,"network_operations":0},indent=2)+"\n")


class WireSocket:
    def __init__(self, response):
        self.response=response
        self.sent=[]
    def settimeout(self,value):
        assert value>0
    def send(self,value):
        self.sent.append(json.loads(value))
    def recv(self):
        return json.dumps(self.response)


def wire_connection(response):
    from cdpd_chatgpt_profile.capture_transport import ReadOnlyCaptureConnection
    connection=ReadOnlyCaptureConnection.__new__(ReadOnlyCaptureConnection)
    connection.endpoint="http://127.0.0.1:9222"
    connection.deadline=time.monotonic()+10
    connection.sequence=0
    connection.audit=[]
    connection.metadata_audit=[]
    connection.socket=WireSocket(response)
    return connection


@pytest.mark.parametrize("case",["different_session","missing_session","session_on_browser_reply"])
def test_same_id_response_must_belong_to_exact_sent_session(case,request):
    from cdpd.collector import CaptureError
    response={"id":1,"result":{"synthetic":True}}
    method="Page.getFrameTree"
    session="BOUND_SESSION"
    if case=="different_session": response["sessionId"]="OTHER_SESSION"
    elif case=="session_on_browser_reply":
        response["sessionId"]="OTHER_SESSION"
        method="Target.getTargetInfo"
        session=None
    connection=wire_connection(response)
    with pytest.raises(CaptureError) as error:
        connection.send(method,{},session)
    assert len(connection.socket.sent)==1
    observe(request,expected={"mismatched_response_accepted":False,"commands_sent":1},
            actual={"mismatched_response_accepted":False,"commands_sent":len(connection.socket.sent),"reason":error.value.code},
            ac=("AC02",),cap=("CAP-02",))


@pytest.mark.parametrize("session",[None,"BOUND_SESSION"])
def test_exact_response_session_normal_control(session,request):
    response={"id":1,"result":{"synthetic":True}}
    if session is not None: response["sessionId"]=session
    connection=wire_connection(response)
    method="Target.getTargetInfo" if session is None else "Page.getFrameTree"
    result=connection.send(method,{},session)
    assert result=={"synthetic":True} and len(connection.socket.sent)==1
    observe(request,expected={"matched_response_accepted":True,"commands_sent":1},
            actual={"matched_response_accepted":result=={"synthetic":True},"commands_sent":len(connection.socket.sent)},
            ac=("AC02",),cap=("CAP-01",))


@pytest.mark.parametrize("method",["Page.reload","Page.navigate","Input.dispatchKeyEvent","Browser.close","Target.createTarget","Target.activateTarget"])
def test_transport_has_no_web_mutation_or_tab_switch_capability(method,request):
    from cdpd.collector import CaptureError
    connection=wire_connection({"id":1,"result":{}})
    with pytest.raises(CaptureError) as error:
        connection.send(method,{},"BOUND_SESSION")
    assert error.value.code=="COMMAND_NOT_ALLOWED" and connection.socket.sent==[]
    observe(request,expected={"reason":"COMMAND_NOT_ALLOWED","commands_sent":0},
            actual={"reason":error.value.code,"commands_sent":len(connection.socket.sent)},
            ac=("AC02",),cap=("CAP-02",))


def test_transport_rejects_arbitrary_javascript_before_send(request):
    from cdpd.collector import CaptureError
    connection=wire_connection({"id":1,"sessionId":"BOUND_SESSION","result":{}})
    with pytest.raises(CaptureError) as error:
        connection.send("Runtime.evaluate",{"expression":"document.body.innerText","returnByValue":True,"awaitPromise":False},"BOUND_SESSION")
    assert error.value.code=="SCRIPT_NOT_ALLOWED" and connection.socket.sent==[]
    observe(request,expected={"reason":"SCRIPT_NOT_ALLOWED","commands_sent":0},
            actual={"reason":error.value.code,"commands_sent":len(connection.socket.sent)},
            ac=("AC02",),cap=("CAP-03",))


_MATRIX = json.loads((Path(__file__).resolve().parents[1]/"references/approved_l02/contracts/diagnostic_event_matrix.json").read_text())["rules"]
_NO_DOM_RULES = [rule for rule in _MATRIX if rule["capture_mode"] in {"NONE","METADATA_ONLY","GUARDED_STRUCTURE_IF_IDLE"}]


@pytest.mark.parametrize("rule",_NO_DOM_RULES,ids=[rule["case_id"] for rule in _NO_DOM_RULES])
def test_fixed_event_matrix_does_not_recursively_capture_or_infer_idle(rule,request):
    data=request_data()
    data["trigger_envelope"]["event"].update(kind=rule["event_kind"],failure_code=rule["failure_code"])
    data["trigger_envelope"]["profile"]["reason"]=rule["profile_reason"]
    data["capture_request"]["event"]=deepcopy(data["trigger_envelope"]["event"])
    result,connection,permit,creations=run(data)
    assert result["capture_status"] in ("PARTIAL","REJECTED") and result["structure"] is None
    assert creations==[] and connection.calls==[] and permit.challenges==[]
    observe(request,expected={"matrix_rule":rule["case_id"],"capture_mode":rule["capture_mode"],"connections":0,"structure_retained":False},
            actual={"matrix_rule":rule["case_id"],"capture_mode":rule["capture_mode"],"connections":len(creations),
                    "structure_retained":result["structure"] is not None,"reason":result["failure_code"]},
            ac=("AC02","AC03"),cap=("CAP-04",),result=result)


def test_boolean_response_id_cannot_match_integer_command_id(request):
    connection=wire_connection({"id":1,"result":{"synthetic":"CORRECT"}})
    answers=[json.dumps({"id":True,"result":{"synthetic":"WRONG_BOOL_ID"}}),
             json.dumps({"id":1,"result":{"synthetic":"CORRECT"}})]
    connection.socket.recv=lambda:answers.pop(0)
    result=connection.send("Target.getTargetInfo",{})
    assert result=={"synthetic":"CORRECT"} and len(answers)==0
    observe(request,expected={"wrong_bool_id_accepted":False,"commands_sent":1},
            actual={"wrong_bool_id_accepted":result!={"synthetic":"CORRECT"},"commands_sent":len(connection.socket.sent)},
            ac=("AC02",),cap=("CAP-02",))
