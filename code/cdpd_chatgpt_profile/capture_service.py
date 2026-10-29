"""Coordinator-owned S0 -> one-shot Worker -> validated Bundle publication.

This is the L06 synchronous capture slice. It does not run a heartbeat loop,
start a consumer, admit Web operations, replay a capture, approve a Baseline,
or implement L07's Windows Job Object lifecycle. It reads accepted L05 history
without changing current/checkpoint/receipts. Pruning consumers must explicitly
supply protected_event_ids(); that downstream integration remains separate.
"""
from __future__ import annotations
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import threading
import time
from typing import Any, Callable
from uuid import uuid4

from cdpd import CaptureRequest
from cdpd.service import CaptureOutcome, DiagnosticService
from cdpd.collector import CaptureError
from chatgpt_web_api.target_guard import BindingGuard, ParsedBinding, _sample_clock, timestamp, check_continuity
from .spool import canonical_json, strict_json, read_history, stream_key, _FileLock
from .bundle_validation import capture_rule, validate_payload_file, verify_report_bundle
from .evidence_store import (ProfileEvidenceStore, ProfileStorageError, verify_profile_bundle,
                             _safe_directory, _read_regular, _write_new, _sync_directory, _read_blob)
from .structure_script import CATALOG_SHA256, script_sha256
from .worker import WorkerRequest, WorkerInputError, metadata_worker_result, _PermitGate, _validate_structure
from .terminal_failure import (LiveTerminalAuthority, CapturePermitSession, is_terminal, valid_scope,
                               TERMINAL_CODES, valid_receipt)

NORMALIZATION_SHA256 = hashlib.sha256(b"CDPD_L06_STRUCTURE_ONLY_NO_BASELINE_NORMALIZATION_V1").hexdigest()
POLICY_SHA256 = hashlib.sha256(b"CDPD_L06_FIXED_CHATGPT_MAIN_OPEN_SHADOW_NO_TEXT_NO_UI_V1").hexdigest()


class CaptureServiceError(RuntimeError):
    def __init__(self, code: str, *, original_failure_code: str | None = None) -> None:
        self.code = code if code in {"INPUT_INVALID", "STORAGE_FAILED", "CAPTURE_BUSY", "INCOMPLETE_BUNDLE", "S0_INVALID",
                                    "PUBLISH_GUARD_DENIED", "PUBLISH_GUARD_CLEANUP_FAILED", "SPOOL_BACKPRESSURE",
                                    "WINDOWS_STORAGE_POLICY_UNVERIFIED", "BINDING_INVALIDATION_UNCONFIRMED"} else "STORAGE_FAILED"
        self.original_failure_code = original_failure_code if original_failure_code in TERMINAL_CODES else None
        super().__init__(self.code)


def _stamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset().total_seconds() != 0:
        raise CaptureServiceError("INPUT_INVALID")
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _windows_storage_required() -> bool:
    return os.name == "nt"


class L04CapturePermitAuthority:
    """Diagnostic permit and scoped terminal bridge to an accepted L04 guard.

    A stored record or L05 EVENT_DURABILITY receipt cannot instantiate a live
    guard. This adapter never calls preflight/admission, mutation, send, retry,
    or changes the accepted terminal/cached/ledger semantics.
    """
    def __init__(self, guard: BindingGuard, *, supervisor_instance_id: str,
                 binding_record_raw_json: str | None = None) -> None:
        if not isinstance(guard, BindingGuard):
            raise WorkerInputError()
        raw = guard.record.to_bytes() if binding_record_raw_json is None else binding_record_raw_json.encode("utf-8")
        try:
            if len(raw) > 8192 or ParsedBinding.parse(raw) != guard.record:
                raise WorkerInputError()
        except Exception:
            raise WorkerInputError() from None
        self.guard = guard
        self.supervisor_instance_id = supervisor_instance_id
        # The accepted guard owns canonical record semantics. The capture
        # contract separately preserves the exact trusted original raw bytes.
        self.binding_record_sha256 = _hash(raw)
        self._terminal_authority = LiveTerminalAuthority(guard, self.binding_record_sha256, supervisor_instance_id)

    def __call__(self, challenge: dict[str, Any]) -> dict[str, Any]:
        if is_terminal(challenge):
            return self._terminal_authority.notify(challenge)
        response = dict(challenge)
        response.update(schema="cdpd.capture-worker-permit/v1", allowed=False,
                        remaining_lease_seconds=0.0, reason_code="CLOCK_OR_KEEPER_LOST")
        try:
            guard = self.guard
            if not valid_scope(challenge, self._terminal_authority.expected):
                return response
            guard.check(mutation=False)
            try:
                sample = _sample_clock(guard._clock)
                check_continuity(guard._anchor, sample)
            except Exception:
                guard.invalidate("CLOCK_DISCONTINUITY")
                return response
            remaining = min((timestamp(guard.record.expires_at_utc) - sample.utc).total_seconds(),
                            guard._deadline - sample.monotonic)
            if remaining <= 0 or sample.utc < timestamp(guard.record.issued_at_utc):
                guard.invalidate("BINDING_EXPIRED" if remaining <= 0 else "CLOCK_DISCONTINUITY")
                response["reason_code"] = "LEASE_EXPIRED" if remaining <= 0 else "CLOCK_OR_KEEPER_LOST"
                return response
            # A second ordinary guard check catches errors of a sampling Clock
            # in the same accepted shared-generation terminal boundary.
            guard.check(mutation=False)
            if remaining <= 0:
                response["reason_code"] = "LEASE_EXPIRED"
                return response
            if challenge["phase"] in {"START", "COORDINATOR_START"} and not self._terminal_authority.register(challenge):
                return response
            response.update(allowed=True, remaining_lease_seconds=min(14400.0, remaining), reason_code="NONE")
        except Exception:
            # No raw OS/Clock/Provider exception crosses the pipe boundary.
            response["reason_code"] = "CLOCK_OR_KEEPER_LOST"
        return response


class CaptureCoordinator:
    def __init__(self, state_dir: str | Path, spool_dir: str | Path, store: ProfileEvidenceStore, *,
                 runner: Callable, permit: Callable[[dict], dict],
                 utcnow: Callable[[], datetime] | None = None,
                 monotonic: Callable[[], float] = time.monotonic,
                 windows_state_authorizer: Callable[[Path], bool] | None = None,
                 publication_gate: Any = None) -> None:
        if not isinstance(store, ProfileEvidenceStore) or not callable(runner) or not callable(permit):
            raise CaptureServiceError("INPUT_INVALID")
        self.state_dir, self.spool_dir = Path(state_dir).absolute(), Path(spool_dir).absolute()
        self.store, self.runner, self.permit = store, runner, permit
        self.utcnow = utcnow or (lambda: datetime.now(timezone.utc))
        self.monotonic = monotonic
        self.publication_gate = publication_gate
        if publication_gate is not None and not callable(getattr(publication_gate, "publish", None)):
            raise CaptureServiceError("INPUT_INVALID")
        if windows_state_authorizer is not None and not callable(windows_state_authorizer):
            raise CaptureServiceError("INPUT_INVALID")
        self._windows_state_authorizer = windows_state_authorizer
        self._lock = threading.Lock()
        self.last_integrity_result: dict[str, Any] | None = None
        self.last_s0_path: str | None = None
        self.last_worker_result: dict[str, Any] | None = None
        self.last_capture_metadata: dict[str, Any] | None = None
        self._terminal_bindings: dict[str, dict] = {}

    def _terminal_key(self, data: dict) -> str:
        # Raw encodings and supervisor restarts cannot bypass a known binding /
        # generation failure. A newly issued binding or generation is separate.
        return _hash(canonical_json({"binding_id": data["trigger_envelope"]["binding_id"],
                                      "control_generation": data["control_generation"]}))

    def _terminal_marker(self, data: dict) -> dict | None:
        key = self._terminal_key(data)
        if key in self._terminal_bindings:
            return self._terminal_bindings[key]
        path = self.state_dir / "terminal-bindings" / (key + ".json")
        _safe_directory(path.parent, allow_missing=True)
        if not path.exists():
            # A failed disk write may have prevented the terminal marker itself
            # from being saved. S0 + attempt necessarily preceded the Worker.
            # An unresolved earlier attempt for this generation is uncertainty,
            # not permission for another Worker after a coordinator restart.
            for prior in (self.state_dir / "incidents").iterdir():
                _safe_directory(prior, allow_missing=False)
                if prior.name == data["capture_id"] or not (prior / "attempt.json").exists():
                    continue
                try:
                    s0 = strict_json(_read_regular(prior / "S0.json", 65536), max_bytes=65536)
                    previous_job = WorkerRequest.from_dict(s0["job"])
                    self._load_s0(prior, previous_job)
                    previous = previous_job.to_dict()
                    if self._terminal_key(previous) != key:
                        continue
                    bundle = self.store.root / previous["capture_id"]
                    verified = verify_profile_bundle(bundle)
                    if verified["integrity_valid"]:
                        _, payload = verify_report_bundle(_read_blob(bundle))
                        # Metadata recovery never proves that an interrupted
                        # Worker did not observe an unreported terminal failure.
                        prior_result = payload["RESULT.json"]
                        unknown_child = (prior_result["failure_code"] == "WORKER_FAILED_OR_DEADLINE"
                                         and prior_result["profile"]["worker_termination"] not in {"NORMAL", "NOT_STARTED"})
                        if prior_result["failure_code"] != "SUPERVISOR_LOST" and not unknown_child:
                            continue
                except CaptureServiceError:
                    raise
                except Exception:
                    pass
                raise CaptureServiceError("BINDING_INVALIDATION_UNCONFIRMED")
            return None
        try:
            value = strict_json(_read_regular(path, 8192), max_bytes=8192)
            notice = value["notice"]
            if (value.get("schema") != "cdpd.capture-terminal-latch/v1"
                    or set(value) != {"schema", "notice", "shared_invalidation", "automatic_recovery"}
                    or value["automatic_recovery"] is not False
                    or not valid_scope(notice, {"binding_id": data["trigger_envelope"]["binding_id"],
                                               "control_generation": data["control_generation"]})
                    or type(notice["binding_sha256"]) is not str
                    or re.fullmatch(r"[0-9a-f]{64}", notice["binding_sha256"]) is None
                    or type(notice["supervisor_instance_id"]) is not str
                    or re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", notice["supervisor_instance_id"]) is None
                    or notice["phase"].removeprefix("TERMINAL:") not in TERMINAL_CODES):
                raise ValueError()
            value["shared_invalidation"] = "UNCONFIRMED"
            receipt_path = self.state_dir / "incidents" / notice["capture_id"] / "TERMINAL_RECEIPT.json"
            _safe_directory(receipt_path.parent, allow_missing=False)
            if receipt_path.exists():
                saved = strict_json(_read_regular(receipt_path, 8192), max_bytes=8192)
                if (set(saved) != {"schema", "receipt", "durable_latch", "original_failure_code", "automatic_recovery"}
                        or saved.get("schema") != "cdpd.capture-terminal-notification/v1"
                        or saved.get("automatic_recovery") is not False
                        or saved.get("original_failure_code") != notice["phase"].removeprefix("TERMINAL:")
                        or not valid_receipt(saved.get("receipt"), notice)
                        or saved["receipt"]["reason_code"] != notice["phase"].removeprefix("TERMINAL:")):
                    raise ValueError()
                # A stored receipt is historical evidence, never fresh live
                # authority on restart. Even a late disk write cannot promote
                # an UNKNOWN capture. Explicit new Binding issuance is required.
        except Exception:
            raise CaptureServiceError("BINDING_INVALIDATION_UNCONFIRMED") from None
        self._terminal_bindings[key] = value
        return value

    def _record_terminal(self, data: dict, incident: Path, notice: dict) -> None:
        value = {"schema": "cdpd.capture-terminal-latch/v1", "notice": notice,
                 "shared_invalidation": "UNCONFIRMED", "automatic_recovery": False}
        key = self._terminal_key(data)
        self._terminal_bindings.setdefault(key, value)
        root = self.state_dir / "terminal-bindings"
        _safe_directory(root, allow_missing=True)
        root.mkdir(mode=0o700, exist_ok=True)
        path = root / (key + ".json")
        if not path.exists():
            _write_new(path, canonical_json(value))
            _sync_directory(root)
        _write_new(incident / "TERMINAL_NOTICE.json", canonical_json(value))
        _sync_directory(incident)

    def _freeze_history(self, data: dict[str, Any], at: datetime) -> tuple[dict, dict, int, int]:
        """Use the accepted v2-aware reader, then account for wrapper bytes."""
        history = read_history(self.spool_dir, stream=stream_key(data["trigger_envelope"]), now_utc=at.timestamp())
        received = _stamp(at)
        events = [{"received_utc": received, "event": envelope["event"], "profile": envelope}
                  for envelope in history["events"]]
        # received_utc means reception by THIS coordinator when freezing S0;
        # original occurred_at_utc remains unchanged in the typed envelope.
        omitted = 0
        while len(canonical_json({"schema": "cdpd.events/v1", "events": events})) > 1048576:
            events.pop(0)
            omitted += 1
        coverage = dict(history["history_coverage"])
        trigger_loss = data["trigger_envelope"]["profile"]
        previous_bound = max(coverage["loss_lower_bound"], trigger_loss["loss_lower_bound"])
        # Spool-budget omissions and wrapper-budget omissions are disjoint
        # event identities; combine only these two locally known populations.
        # Other durable/carrier/gap counters may overlap and stay max-based.
        local_omitted = history["omitted_by_limit"] + omitted
        coverage["loss_lower_bound"] = max(previous_bound, local_omitted)
        coverage["loss_count_exact"] = coverage["loss_count_exact"] and trigger_loss["loss_count_exact"]
        if omitted and previous_bound:
            coverage["loss_count_exact"] = False
        if omitted:
            coverage["sampled"] = True
        coverage["complete"] = (coverage["complete"] and coverage["loss_count_exact"]
                                and coverage["loss_lower_bound"] == 0 and not coverage["sampled"])
        coverage.update(retained_count=len(events), window_seconds=120)
        return {"schema": "cdpd.events/v1", "events": events}, coverage, history["source_cutoff_seq"], omitted

    def _load_s0(self, incident: Path, job: WorkerRequest) -> tuple[dict, dict]:
        try:
            s0 = strict_json(_read_regular(incident / "S0.json", 65536), max_bytes=65536)
            if (type(s0) is not dict or s0.get("schema") != "cdpd.capture-s0/v1"
                    or s0.get("job_sha256") != _hash(job._raw) or s0.get("job") != job.to_dict()):
                raise ValueError()
            history_raw = _read_regular(incident / "HISTORY.json", 1048576)
            if _hash(history_raw) != s0["history_sha256"]:
                raise ValueError()
            history = strict_json(history_raw, max_bytes=1048576)
            validate_payload_file("EVENTS.json", history)
            return s0, history
        except Exception:
            raise CaptureServiceError("S0_INVALID") from None

    def protected_event_ids(self) -> frozenset[str]:
        """Consumer-facing retention pins for active S0, never a pruning action.

        If an active incident is unreadable, raise instead of supplying an empty
        set that would wrongly authorize pruning. Later consumers must forward
        this set to JournalSpool.prune_expired(protected_event_ids=...).
        """
        incident_root = self.state_dir / "incidents"
        if not incident_root.exists():
            return frozenset()
        _safe_directory(incident_root, allow_missing=False)
        protected: set[str] = set()
        for path in incident_root.iterdir():
            _safe_directory(path, allow_missing=False)
            if not (path / "S0.json").exists():
                # A failed pre-S0 reserve cannot contain a live capture.
                if (path / "attempt.json").exists():
                    raise CaptureServiceError("S0_INVALID")
                continue
            try:
                raw = strict_json(_read_regular(path / "S0.json", 65536), max_bytes=65536)
                job = WorkerRequest.from_dict(raw["job"])
                s0, events = self._load_s0(path, job)
                final = path / "FINALIZED.json"
                if final.exists():
                    record = strict_json(_read_regular(final, 4096), max_bytes=4096)
                    checked = verify_profile_bundle(self.store.root / job.to_dict()["capture_id"])
                    if (record.get("job_sha256") == s0["job_sha256"] and checked["integrity_valid"]
                            and record.get("manifest_sha256") == checked["manifest_sha256"]):
                        continue
                protected.add(job.to_dict()["trigger_envelope"]["event_id"])
                protected.update(row["profile"]["event_id"] for row in events["events"])
            except Exception:
                raise CaptureServiceError("S0_INVALID") from None
        return frozenset(protected)

    def _worker_checked(self, job: WorkerRequest, result: Any) -> dict[str, Any]:
        expected = metadata_worker_result(job, "NOT_RUN")
        try:
            detached = strict_json(canonical_json(result), max_bytes=3145728)
            if (type(detached) is not dict or set(detached) != set(expected)
                    or detached["schema"] != expected["schema"] or detached["capture_id"] != expected["capture_id"]
                    or detached["script_sha256"] != expected["script_sha256"]
                    or detached["probe_catalog_sha256"] != expected["probe_catalog_sha256"]
                    or type(detached["pid"]) is not int or detached["pid"] < 0
                    or (detached["pid"] == 0 and detached["worker_termination"] != "NOT_STARTED")):
                raise ValueError()
            from .worker import _FAILURES
            if (detached["capture_status"] not in {"COMPLETE", "PARTIAL", "REJECTED"}
                    or detached["failure_code"] not in _FAILURES | {None, "NONE", "NOT_RUN", "TRUNCATED", "SPOOL_BACKPRESSURE"}
                    or detached["worker_termination"] not in {"NORMAL", "KILLED", "EXITED_ERROR", "UNCONFIRMED", "NOT_STARTED"}
                    or type(detached["sanitization_passed"]) is not bool
                    or type(detached["identity_checks"]) is not dict
                    or set(detached["identity_checks"]) != {"before", "after", "current", "keeper", "lease"}
                    or any(type(v) is not bool for v in detached["identity_checks"].values())
                    or type(detached["stages"]) is not dict or set(detached["stages"]) != {"S1", "S2"}
                    or type(detached["command_audit"]) is not list or len(detached["command_audit"]) > 100):
                raise ValueError()
            commands = {"Target.getTargetInfo", "Target.attachToTarget", "Target.detachFromTarget",
                        "Page.getFrameTree", "Page.createIsolatedWorld", "Runtime.evaluate"}
            if any(type(v) is not str or v not in commands for v in detached["command_audit"]):
                raise ValueError()
            for marker in (detached["before"], detached["after"]):
                if marker is not None and (type(marker) is not dict or set(marker) != {"frame_id", "loader_id"}
                        or any(type(v) is not str or not v or len(v) > 256 or any(ord(c) < 33 or ord(c) > 126 for c in v) for v in marker.values())):
                    raise ValueError()
            for stage in detached["stages"].values():
                if (type(stage) is not dict or set(stage) != {"status", "reason_code", "duration_ms"}
                        or stage["status"] not in {"COMPLETE", "PARTIAL", "FAILED", "NOT_RUN"}
                        or type(stage["duration_ms"]) is not int or not 0 <= stage["duration_ms"] <= 300000
                        or stage["reason_code"] not in _FAILURES | {None, "NONE", "NOT_RUN", "TRUNCATED", "SPOOL_BACKPRESSURE"}
                        or (stage["status"] == "NOT_RUN" and stage["duration_ms"] != 0)
                        or (stage["status"] == "COMPLETE" and stage["reason_code"] is not None)):
                    raise ValueError()
            if detached["structure"] is not None:
                _validate_structure(detached["structure"], maximum=job.to_dict()["capture_request"].get("max_nodes", 1000))
                # A contradictory Worker result must never be repaired by the
                # later HISTORY_INCOMPLETE downgrade. Only these two intrinsic
                # structure-success forms may reach profile assembly.
                truncated = detached["structure"]["truncated"]
                if (detached["worker_termination"] != "NORMAL" or not detached["sanitization_passed"]
                        or not all(detached["identity_checks"].values()) or detached["before"] is None
                        or detached["before"] != detached["after"]
                        or detached["structure"]["command_audit"] != detached["command_audit"]):
                    raise ValueError()
                if truncated:
                    if detached["capture_status"] != "PARTIAL" or detached["failure_code"] != "TRUNCATED":
                        raise ValueError()
                    if any((s["status"], s["reason_code"]) not in {("COMPLETE", None), ("PARTIAL", "TRUNCATED")}
                           for s in detached["stages"].values()):
                        raise ValueError()
                elif (detached["capture_status"] != "COMPLETE" or detached["failure_code"] not in {None, "NONE"}
                      or any(s["status"] != "COMPLETE" or s["reason_code"] is not None for s in detached["stages"].values())):
                    raise ValueError()
            elif detached["capture_status"] == "COMPLETE":
                raise ValueError()
            elif detached["capture_status"] == "REJECTED" and (job.to_dict()["policy_allowed"]
                    or detached["failure_code"] != "POLICY_DENIED" or detached["worker_termination"] != "NOT_STARTED"):
                raise ValueError()
            return detached
        except Exception:
            return metadata_worker_result(job, "WORKER_FAILED_OR_DEADLINE", termination="EXITED_ERROR")

    def _payload(self, job: WorkerRequest, s0: dict, events: dict, result: dict, *, started: str | None, elapsed_ms: int) -> dict:
        data = job.to_dict()
        envelope = data["trigger_envelope"]
        binding = ParsedBinding.parse(data["binding_record_raw_json"])
        finished = _stamp(self.utcnow())
        structure = result["structure"]
        status, code = result["capture_status"], result["failure_code"]
        history_coverage = s0["history_coverage"]
        if structure is not None and not history_coverage["complete"]:
            status = "PARTIAL"
            if not structure["truncated"]:
                code = "HISTORY_INCOMPLETE"
        marker = envelope["profile"]["document_marker"]
        relation = "UNKNOWN"
        if structure is not None:
            if envelope["event"]["kind"] == "MANUAL" and envelope["profile"]["reason"] in {"MANUAL_CAPTURE", "SCHEDULED_CAPTURE"}:
                relation = "CURRENT_OBSERVATION"
            elif marker is not None:
                relation = "SAME_DOCUMENT_VERIFIED" if marker == result["before"] == result["after"] else "DIFFERENT_DOCUMENT"
        stage_ok = lambda ms=0: {"status": "COMPLETE", "reason_code": None, "duration_ms": min(300000, ms)}
        profile = {
            "profile_schema": "cdpd.chatgpt-profile/v1", "capture_id": data["capture_id"],
            "binding_record": binding.to_dict(), "binding_record_raw_json": data["binding_record_raw_json"],
            "binding_record_sha256": data["binding_record_sha256"], "control_generation": data["control_generation"],
            "supervisor_instance_id": data["supervisor_instance_id"], "cause_event_ids": [envelope["event_id"]],
            "initial_trigger_event_id": envelope["event_id"], "trigger_envelope": envelope,
            "source_cutoff_seq": s0["source_cutoff_seq"], "history_coverage": history_coverage,
            "stages": {"S0": stage_ok(s0["duration_ms"]), "S1": result["stages"]["S1"],
                       "S2": result["stages"]["S2"], "S3": stage_ok()},
            "observation_relation": relation,
            "timing": {"failed_at_utc": None if envelope["event"]["kind"] == "MANUAL" else envelope["occurred_at_utc"],
                       "accepted_at_utc": s0["accepted_at_utc"], "capture_started_at_utc": started,
                       "finished_at_utc": finished, "latency_ms": elapsed_ms if started is not None else None},
            "identity_checks": result["identity_checks"],
            "sanitization": {"passed": result["sanitization_passed"], "script_sha256": result["script_sha256"],
                             "probe_catalog_sha256": result["probe_catalog_sha256"], "allowlist_version": "cdpd.chatgpt-safe-structure/v1"},
            "worker_termination": result["worker_termination"],
            "document_markers": {"trigger": marker, "before": result["before"], "after": result["after"]},
            "policy_allowed": data["policy_allowed"], "coverage_complete": structure is not None and not structure["truncated"],
            "report_context": {"recipe": envelope["profile"]["ui_context"], "context_verified": False,
                               "locale_class": "und", "viewport_class": "unknown", "browser_profile": "CDPD_L06_UNQUALIFIED",
                               "catalog_sha256": CATALOG_SHA256, "normalization_sha256": NORMALIZATION_SHA256,
                               "policy_sha256": POLICY_SHA256},
        }
        payload = {"TRIGGER.json": envelope["event"], "EVENTS.json": events,
                   "RESULT.json": {"schema": "cdpd.capture-result/v1", "capture_status": status, "failure_code": code,
                                   "assessment_status": "NOT_PERFORMED", "started_utc": s0["accepted_at_utc"], "finished_utc": finished,
                                   "selected_scope": "MAIN_FRAME_AND_OPEN_SHADOW", "history_dropped_count": history_coverage["loss_lower_bound"],
                                   "target_id": binding.target_id, "profile": profile}}
        if structure is not None:
            payload["STRUCTURE.json"] = structure
        return payload

    def _cached(self, job: WorkerRequest, incident: Path, destination: Path) -> CaptureOutcome | None:
        if not destination.exists():
            return None
        checked = verify_profile_bundle(destination)
        if not checked["integrity_valid"]:
            # A partially published directory is not overwritten or repaired.
            raise CaptureServiceError("INCOMPLETE_BUNDLE")
        self._load_s0(incident, job)
        _, payload = verify_report_bundle(_read_blob(destination))
        p = payload["RESULT.json"]["profile"]
        if p["capture_id"] != job.to_dict()["capture_id"] or p["trigger_envelope"] != job.to_dict()["trigger_envelope"]:
            raise CaptureServiceError("INPUT_INVALID")
        self.last_integrity_result = checked
        r = payload["RESULT.json"]
        return CaptureOutcome(r["capture_status"], str(destination), r["failure_code"])

    def capture(self, job: WorkerRequest) -> CaptureOutcome:
        if not isinstance(job, WorkerRequest):
            raise CaptureServiceError("INPUT_INVALID")
        try:
            job = WorkerRequest.from_dict(job.to_dict())
        except WorkerInputError:
            raise CaptureServiceError("INPUT_INVALID") from None
        if not self._lock.acquire(blocking=False):
            raise CaptureServiceError("CAPTURE_BUSY")
        try:
            _safe_directory(self.state_dir, allow_missing=True)
            if _windows_storage_required():
                authorizer = self._windows_state_authorizer or self.store._windows_storage_authorizer
                try:
                    allowed = authorizer is not None and authorizer(self.state_dir) is True
                except Exception:
                    allowed = False
                if not allowed:
                    raise CaptureServiceError("WINDOWS_STORAGE_POLICY_UNVERIFIED")
            self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            lock_path = self.state_dir / "capture-writer.lock"
            if lock_path.exists():
                _read_regular(lock_path, 4096)
            with _FileLock(lock_path):
                return self._capture_locked(job)
        except CaptureServiceError:
            raise
        except Exception:
            raise CaptureServiceError("STORAGE_FAILED") from None
        finally:
            self._lock.release()

    def _capture_locked(self, job: WorkerRequest) -> CaptureOutcome:
        data = job.to_dict()
        incident_root = self.state_dir / "incidents"
        _safe_directory(incident_root, allow_missing=True)
        incident_root.mkdir(exist_ok=True, mode=0o700)
        incident = incident_root / data["capture_id"]
        destination = self.store.root / data["capture_id"]
        _safe_directory(incident, allow_missing=True)
        cached = self._cached(job, incident, destination)
        if cached is not None:
            return cached
        recovery = incident.exists()
        start_mono = self.monotonic()
        if recovery:
            s0, events = self._load_s0(incident, job)
        else:
            # No deletion/age-pruning in this slice. Safe finite backpressure.
            if len(list(incident_root.iterdir())) >= 250:
                raise CaptureServiceError("SPOOL_BACKPRESSURE")
            accepted = self.utcnow()
            events, coverage, cutoff, omitted = self._freeze_history(data, accepted)
            history_raw = canonical_json(events)
            s0 = {"schema": "cdpd.capture-s0/v1", "job": data, "job_sha256": _hash(job._raw),
                  "accepted_at_utc": _stamp(accepted), "history_sha256": _hash(history_raw),
                  "source_cutoff_seq": cutoff, "history_coverage": coverage, "wrapper_limit_omitted": omitted,
                  "duration_ms": min(300000, max(0, int((self.monotonic() - start_mono) * 1000)))}
            s0_raw = canonical_json(s0)
            if len(s0_raw) > 65536:
                raise CaptureServiceError("INPUT_INVALID")
            incident.mkdir(mode=0o700)
            _write_new(incident / "HISTORY.json", history_raw)
            _write_new(incident / "S0.json", s0_raw)
            _sync_directory(incident)
        self.last_s0_path = str(incident / "S0.json")
        attempt = incident / "attempt.json"
        if not attempt.exists():
            _write_new(attempt, canonical_json({"schema": "cdpd.capture-attempt/v1", "capture_id": data["capture_id"],
                                               "job_sha256": _hash(job._raw), "spawn_may_have_occurred": True}))
            _sync_directory(incident)
        started = None
        prior_terminal = self._terminal_marker(data)
        permit_session = CapturePermitSession(data, self.permit,
            on_detect=lambda notice: self._record_terminal(data, incident, notice))
        parent_gate = _PermitGate(data, permit_session, self.monotonic, data["capture_request"].get("timeout_seconds", 10.0))
        mode = capture_rule(data["trigger_envelope"])["capture_mode"]
        if prior_terminal is not None:
            reason = prior_terminal["notice"]["phase"].removeprefix("TERMINAL:")
            result = metadata_worker_result(job, reason)
            self.last_worker_result = result
            if prior_terminal["shared_invalidation"] != "CONFIRMED":
                raise CaptureServiceError("BINDING_INVALIDATION_UNCONFIRMED", original_failure_code=reason)
        elif recovery:
            result = metadata_worker_result(job, "SUPERVISOR_LOST", termination="NOT_STARTED")
        elif not data["policy_allowed"]:
            result = metadata_worker_result(job, "POLICY_DENIED")
        elif mode in {"NONE", "METADATA_ONLY", "GUARDED_STRUCTURE_IF_IDLE"}:
            # Use worker's exact matrix metadata selection without connection.
            from .worker import run_capture_worker
            result = run_capture_worker(job, permit=self.permit,
                                        connection_factory=lambda *_: (_ for _ in ()).throw(AssertionError()))
        else:
            started = _stamp(self.utcnow())
            try:
                parent_gate.check("COORDINATOR_START")
                result = self._worker_checked(job, self.runner(job, permit=permit_session))
            except Exception:
                result = metadata_worker_result(job, "WORKER_FAILED_OR_DEADLINE", termination="EXITED_ERROR")
            if result["worker_termination"] == "NOT_STARTED":
                started = None
        if (started is not None and result["failure_code"] in TERMINAL_CODES
                and permit_session.failure_code is None):
            # A valid, job-checked result can outlive a failed notification.
            # Preserve that cause locally; do not retry/retarget a callback or
            # infer shared revocation merely from this result's failure code.
            permit_session.prepare({"schema": "cdpd.capture-worker-permit-check/v1",
                "phase": "TERMINAL:" + result["failure_code"], "nonce": uuid4().hex,
                "capture_id": data["capture_id"], "binding_id": data["trigger_envelope"]["binding_id"],
                "binding_sha256": data["binding_record_sha256"], "control_generation": data["control_generation"],
                "supervisor_instance_id": data["supervisor_instance_id"]})
        if permit_session.failure_code is not None:
            # This latch is synchronous memory-only, independent of deadlines
            # or failures in the bounded persistence callback.
            self._terminal_bindings.setdefault(self._terminal_key(data), {
                "schema": "cdpd.capture-terminal-latch/v1", "notice": dict(permit_session.first_notice),
                "shared_invalidation": "UNCONFIRMED", "automatic_recovery": False})
            receipt = permit_session.finish()
            # A callback/child failure cannot replace the earlier authenticated
            # terminal observation, and no contradictory DOM result is repaired.
            if result["failure_code"] != permit_session.failure_code or result["structure"] is not None:
                result = metadata_worker_result(job, permit_session.failure_code,
                                                termination=result["worker_termination"])
            self.last_worker_result = result
            saved = {"schema": "cdpd.capture-terminal-notification/v1", "receipt": receipt,
                     "durable_latch": permit_session.durable, "original_failure_code": permit_session.failure_code,
                     "automatic_recovery": False}
            try:
                from .worker_cli import _bounded_call
                result_raw, saved_raw = canonical_json(result), canonical_json(saved)
                def save_terminal() -> None:
                    _write_new(incident / "TERMINAL_WORKER_RESULT.json", result_raw)
                    _write_new(incident / "TERMINAL_RECEIPT.json", saved_raw)
                    _sync_directory(incident)
                _bounded_call(save_terminal, time.monotonic() + 0.20)
            except Exception:
                permit_session.durable = False
            self.last_capture_metadata = {"terminal_notification": saved, "worker_attempts": 1 if started else 0,
                                          "terminal_evidence_write_complete": permit_session.durable,
                                          "accepted_history_frozen": True, "real_browser_acceptance": "NOT_RUN"}
            if not permit_session.confirmed:
                raise CaptureServiceError("BINDING_INVALIDATION_UNCONFIRMED",
                                          original_failure_code=permit_session.failure_code)
            self._terminal_bindings[self._terminal_key(data)]["shared_invalidation"] = "CONFIRMED"
        if result["structure"] is not None:
            try:
                parent_gate.check("COORDINATOR_FINAL")
            except Exception:
                result = metadata_worker_result(job, "CLOCK_OR_KEEPER_LOST", termination=result["worker_termination"])
        self.last_worker_result = result
        elapsed = max(0, int((self.monotonic() - start_mono) * 1000))
        payload = self._payload(job, s0, events, result, started=started, elapsed_ms=elapsed)
        before_publish = None
        if result["structure"] is not None:
            def before_publish() -> None:
                parent_gate.check("COORDINATOR_COMMIT")
        try:
            from .supervisor_authority import PublicationGate
            if (type(self.publication_gate) is PublicationGate and mode == "METADATA_ONLY"
                    and not recovery and prior_terminal is None and started is None
                    and permit_session.failure_code is None):
                # The original immutable request, not a failed content result,
                # determines eligibility. The view separately rejects all
                # execution/content claims and binds the complete payload.
                view = self.publication_gate.metadata_only_view(
                    job, payload, self.store.root / data["capture_id"])
                bundle = self.store.commit(payload, before_publish=before_publish,
                                           publish_gate=view, metadata_request=job)
            else:
                bundle = self.store.commit(payload, before_publish=before_publish, publish_gate=self.publication_gate)
        except ProfileStorageError as error:
            preserved = {"PUBLISH_GUARD_DENIED", "PUBLISH_GUARD_CLEANUP_FAILED", "WINDOWS_STORAGE_POLICY_UNVERIFIED"}
            raise CaptureServiceError(error.code if error.code in preserved else "STORAGE_FAILED") from None
        except Exception:
            raise CaptureServiceError("STORAGE_FAILED") from None
        verified = verify_profile_bundle(bundle)
        self.last_integrity_result = verified
        if not verified["integrity_valid"]:
            raise CaptureServiceError("STORAGE_FAILED")
        # Retention release is conservative. If this marker cannot be saved,
        # the valid Bundle still exists and active pins remain instead of
        # falsely permitting age pruning.
        final = {"schema": "cdpd.capture-finalized/v1", "capture_id": data["capture_id"],
                 "job_sha256": _hash(job._raw), "manifest_sha256": verified["manifest_sha256"]}
        try:
            _write_new(incident / "FINALIZED.json", canonical_json(final))
            _sync_directory(incident)
        except OSError:
            pass
        self.last_capture_metadata = {"recovery_metadata_only": recovery, "worker_attempts": 0 if recovery or started is None else 1,
                                      "accepted_history_frozen": True, "wrapper_limit_omitted": s0["wrapper_limit_omitted"],
                                      "real_browser_acceptance": "NOT_RUN", "downstream_retention_supplier_accepted": False}
        if permit_session.failure_code is not None:
            self.last_capture_metadata["terminal_notification"] = saved
        permit_session.finish()
        result_payload = payload["RESULT.json"]
        return CaptureOutcome(result_payload["capture_status"], str(bundle), result_payload["failure_code"])


class ProfileDiagnosticService(DiagnosticService):
    """Existing capture(request) signature, always delegated to guarded L06.

    context_provider is trusted composition code supplying the actual Trigger,
    immutable Binding and live coordinator identity; no implicit binding or
    preflight. This class does not change the accepted generic SDK bytes.
    """
    def __init__(self, coordinator: CaptureCoordinator, *, context_provider: Callable[[], dict]) -> None:
        self.coordinator, self.context_provider = coordinator, context_provider

    def capture(self, request: CaptureRequest) -> CaptureOutcome:
        if not isinstance(request, CaptureRequest):
            raise WorkerInputError()
        context = dict(self.context_provider())
        context.update(schema="cdpd.capture-worker-request/v1", capture_request=request.to_dict())
        context.setdefault("capture_id", "C-" + uuid4().hex)
        return self.coordinator.capture(WorkerRequest.from_dict(context))

    def record(self, event):
        # The profile uses accepted L05 hooks/spool; do not create a parallel
        # weak in-memory history path through the inherited generic method.
        raise CaptureServiceError("INPUT_INVALID")

    @staticmethod
    def capabilities() -> dict[str, Any]:
        return {"contract": "cdpd.diagnostics", "major_version": 1, "implementation_version": "0.3.0-l06-candidate",
                "implemented": ["capture.structure.one-shot-profile", "bundle.verify.profile", "history.consume.accepted-l05"],
                "not_implemented": ["watchdog.windows-job", "scheduler", "baseline.diff", "operation.admission"],
                "production_ready": False, "authority": "CANDIDATE_ONLY"}
