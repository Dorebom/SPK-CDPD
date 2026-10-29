"""Internal Execution review regressions; synthetic controls, not Assessment.

The normal fake Worker result is deliberately contradicted before the real
coordinator sees it. History loss cannot repair that Worker outcome or grant
permission to retain its structure.
"""
from pathlib import Path

import pytest

from cdpd_chatgpt_profile.evidence_store import verify_profile_bundle
from cdpd_chatgpt_profile.capture_service import CaptureCoordinator, CaptureServiceError
from test_capture_worker import Permit, observe
from test_l06_capture_service import NOW, Runner, contents, coordinator, snapshot


@pytest.mark.parametrize("status,reason", [
    ("REJECTED", "POLICY_DENIED"),
    ("PARTIAL", "COLLECTOR_EXCEPTION"),
])
def test_incomplete_history_never_masks_contradictory_worker_structure(tmp_path, request, status, reason):
    injected = {}

    def contradict(result):
        assert result["capture_status"] == "COMPLETE" and result["structure"] is not None
        result.update(capture_status=status, failure_code=reason)
        injected.update(status=status, reason=reason, structure_present=True)

    runner = Runner(after=contradict)
    service, job, runner, spool_root, store = coordinator(tmp_path, clean=False, runner=runner)
    outcome = service.capture(job)
    checked = verify_profile_bundle(outcome.bundle_path)
    payload = contents(outcome.bundle_path)["RESULT.json"]
    retained = (Path(outcome.bundle_path) / "STRUCTURE.json").exists()
    assert injected == {"status": status, "reason": reason, "structure_present": True}
    assert outcome.capture_status == "PARTIAL" and outcome.failure_code == "WORKER_FAILED_OR_DEADLINE"
    assert payload["profile"]["history_coverage"]["complete"] is False
    assert payload["profile"]["worker_termination"] == "EXITED_ERROR"
    assert not retained and service.last_worker_result["structure"] is None
    assert checked["integrity_valid"] and checked["profile_valid"]
    assert len(runner.calls) == 1
    observe(request, expected={"worker_contradiction_rejected": True, "structure_retained": False,
                               "failure_code": "WORKER_FAILED_OR_DEADLINE", "runner_calls": 1},
            actual={"worker_contradiction_rejected": service.last_worker_result["structure"] is None,
                    "structure_retained": retained, "failure_code": outcome.failure_code,
                    "runner_calls": len(runner.calls), "injected": injected,
                    "history_complete": payload["profile"]["history_coverage"]["complete"]},
            ac=("AC02", "AC03", "AC04"), cap=("CAP-04", "CAP-05"))


@pytest.mark.parametrize("mode", ["missing", "false", "exception"])
def test_unverified_windows_state_policy_refuses_before_any_state_write_or_worker(tmp_path, request, monkeypatch, mode):
    from cdpd_chatgpt_profile import capture_service
    initial, job, runner, spool_root, store = coordinator(tmp_path)
    checked_paths = []

    def authorizer(path):
        checked_paths.append(path)
        if mode == "exception":
            raise RuntimeError("SYNTHETIC_PRIVATE_STORAGE_POLICY_DETAIL")
        return False

    # FIX004: the explicit unconfigured negative must remove the test-only
    # scoped capability before asserting default fail-closed behaviour.
    if mode == "missing":
        store._windows_storage_authorizer = None
    service = CaptureCoordinator(initial.state_dir, spool_root, store, runner=runner,
                                 permit=Permit(), utcnow=lambda: NOW,
                                 windows_state_authorizer=None if mode == "missing" else authorizer)
    monkeypatch.setattr(capture_service, "_windows_storage_required", lambda: True)
    before = snapshot(spool_root)
    with pytest.raises(CaptureServiceError) as caught:
        service.capture(job)
    assert caught.value.code == "WINDOWS_STORAGE_POLICY_UNVERIFIED"
    assert "SYNTHETIC_PRIVATE" not in str(caught.value)
    assert not service.state_dir.exists() and not store.root.exists() and not runner.calls
    assert before == snapshot(spool_root)
    assert checked_paths == ([] if mode == "missing" else [service.state_dir])
    observe(request, expected={"reason": "WINDOWS_STORAGE_POLICY_UNVERIFIED", "state_created": False,
                               "runner_calls": 0, "spool_unchanged": True},
            actual={"reason": caught.value.code, "state_created": service.state_dir.exists(),
                    "runner_calls": len(runner.calls), "spool_unchanged": before == snapshot(spool_root),
                    "qualification": "SYNTHETIC_BRANCH_ONLY_NATIVE_WINDOWS_NOT_RUN"},
            ac=("AC01", "AC03", "AC04"), cap=("CAP-04",))


@pytest.mark.parametrize("source", ["state_authorizer", "store_authorizer_fallback"])
def test_trusted_state_authorizer_checks_actual_state_path_before_write(tmp_path, request, monkeypatch, source):
    from cdpd_chatgpt_profile import capture_service
    initial, job, runner, spool_root, store = coordinator(tmp_path)
    seen = []

    # FIX005: one authoritative state-directory observation and a separate,
    # *test-only* bundle writer scope. The fallback authorizer may be called
    # again for store.root and its descendants on Windows; the original test
    # incorrectly asserted that every callback was the state directory.
    bundle_observations = []

    def authorizer(path):
        candidate = Path(path).absolute()
        if candidate == initial.state_dir.absolute():
            assert not path.exists() and not runner.calls
            seen.append(path)
            return True
        if source != "store_authorizer_fallback":
            return False
        try:
            relative = candidate.relative_to(store.root.absolute())
            assert all(part not in (".", "..") for part in relative.parts)
            if candidate.is_symlink() or any(parent.is_symlink() for parent in candidate.parents
                                              if parent == store.root or store.root in parent.parents):
                return False
        except (ValueError, OSError, RuntimeError):
            return False
        bundle_observations.append(path)
        return True

    if source == "store_authorizer_fallback":
        store._windows_storage_authorizer = authorizer
    service = CaptureCoordinator(initial.state_dir, spool_root, store, runner=runner,
                                 permit=Permit(), utcnow=lambda: NOW,
                                 windows_state_authorizer=authorizer if source == "state_authorizer" else None)
    monkeypatch.setattr(capture_service, "_windows_storage_required", lambda: True)
    outcome = service.capture(job)
    assert seen == [service.state_dir] and len(runner.calls) == 1
    assert all(Path(x).absolute().is_relative_to(store.root.absolute()) for x in bundle_observations)
    assert outcome.capture_status == "COMPLETE" and verify_profile_bundle(outcome.bundle_path)["profile_valid"]
    observe(request, expected={"trusted_callback_calls": 1, "actual_state_path": True, "runner_calls": 1},
            actual={"trusted_callback_calls": len(seen), "actual_state_path": seen == [service.state_dir],
                    "runner_calls": len(runner.calls),
                    "qualification": "SYNTHETIC_BRANCH_ONLY_NATIVE_WINDOWS_NOT_RUN"},
            ac=("AC01", "AC03", "AC04"), cap=("CAP-01",))


def test_late_guard_dom_cleanup_failure_is_not_reported_as_successful_discard(tmp_path, request, monkeypatch):
    def deny_final(response, ordinal):
        if response["phase"] == "COORDINATOR_COMMIT":
            response.update(allowed=False, reason_code="BINDING_REVOKED")
        return response

    service, job, runner, spool_root, store = coordinator(tmp_path, permit=Permit(deny_final))
    destination = store.root / job.to_dict()["capture_id"]
    original_unlink = Path.unlink

    def refuse_dom_remove(path, *args, **kwargs):
        if path == destination / "STRUCTURE.json":
            raise PermissionError("SYNTHETIC_PRIVATE_FILESYSTEM_EXCEPTION")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", refuse_dom_remove)
    with pytest.raises(CaptureServiceError) as caught:
        service.capture(job)
    assert caught.value.code == "PUBLISH_GUARD_CLEANUP_FAILED"
    assert "SYNTHETIC_PRIVATE" not in str(caught.value)
    assert not (destination / "COMMIT.json").exists()
    assert (destination / "STRUCTURE.json").is_file()
    assert verify_profile_bundle(destination)["publication_state"] == "INCOMPLETE"
    assert job.to_dict()["trigger_envelope"]["event_id"] in service.protected_event_ids()
    assert len(runner.calls) == 1
    observe(request, expected={"reason": "PUBLISH_GUARD_CLEANUP_FAILED", "published": False,
                               "dom_removal_claimed": False, "active_s0_protected": True},
            actual={"reason": caught.value.code, "published": (destination / "COMMIT.json").exists(),
                    "dom_removal_claimed": not (destination / "STRUCTURE.json").exists(),
                    "active_s0_protected": job.to_dict()["trigger_envelope"]["event_id"] in service.protected_event_ids()},
            ac=("AC02", "AC03", "AC04"), cap=("CAP-02", "CAP-05"))
