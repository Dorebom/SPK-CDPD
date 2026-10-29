#!/usr/bin/env python3
"""Run unchanged inherited tests and additive L07 tests with per-case evidence.

Use the exact-pinned virtual environment, for example::

    python -B tools/run_l07_tests.py --evidence-dir evidence/l07_all
    python -B tools/run_l07_tests.py --evidence-dir evidence/l07_selected \
        tests/test_l07_watchdog_runner.py::test_exact_accepted_protocol_success_and_owned_pid

The caller records raw stdout/stderr.  This tool records machine-readable results,
commands, environment and source hashes separately.  It does not enable network,
browser or native Windows acceptance tests, and never edits test sources.

Use a fresh directory inside this Candidate, outside all source directories.
Inherited L05 evidence references are relative to the Candidate root.  To keep
an existing deliverable immutable, run a verified byte-identical execution copy
and place evidence inside that copy; an external evidence directory is rejected
before any tests or fixture processes start.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import inspect
import json
import os
from pathlib import Path
import platform
import sys
import time
import traceback
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRS = (
    "code", "tests", "packages", "accepted_l03", "approved_l01", "contracts",
    "schemas", "references/approved_l02", "tools", "scripts",
)
MANAGED_FILES = (
    "TEST_SUMMARY.json", "TEST_RESULTS.json", "COMMAND.json",
    "ENVIRONMENT.json", "SOURCE_HASHES.json",
)
RAW_DIRS = ("raw_fixtures", "l06_raw_controls", "l07_raw_controls")
EVIDENCE_ATTRIBUTES = ("_l07_evidence", "_l06_evidence", "_l04_evidence")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def json_extra(value: Any) -> str:
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"unsupported evidence type: {type(value).__name__}")


def write_json(path: Path, value: Any) -> None:
    """Replace only a file owned by this invocation, with a complete JSON value."""
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    os.replace(temporary, path)


def snapshot() -> dict[str, dict[str, Any]]:
    paths: set[Path] = set()
    for name in SOURCE_DIRS:
        base = ROOT / name
        if base.is_dir():
            paths.update(path for path in base.rglob("*") if path.is_file())
    paths.update(ROOT.glob("requirements*.txt"))
    paths.update(ROOT / name for name in ("conftest.py", "pytest.ini"))
    paths.update(ROOT / name for name in ("SOURCE_LOCK.json", "INHERITED_FILES.json")
                 if (ROOT / name).is_file())
    result = {}
    for path in sorted(paths):
        if path.suffix in {".pyc", ".pyo"} or any(
            part in {"__pycache__", ".pytest_cache"} for part in path.parts
        ):
            continue
        relative = path.relative_to(ROOT).as_posix()
        result[relative] = {"sha256": sha256(path), "bytes": path.stat().st_size}
    return result


def requirement_pins(path: Path, seen: set[Path] | None = None) -> dict[str, str]:
    """Read the committed recursive exact-pin files without installing anything."""
    seen = set() if seen is None else seen
    path = path.resolve()
    if not path.is_relative_to(ROOT) or path in seen:
        raise ValueError(f"invalid recursive requirement reference: {path}")
    seen.add(path)
    pins: dict[str, str] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("-r "):
            values = requirement_pins(path.parent / line[3:].strip(), seen)
        else:
            parts = line.split("==")
            if len(parts) != 2 or not all(parts) or any(c.isspace() for c in line):
                raise ValueError(f"non-exact requirement at {path.name}:{number}")
            values = {parts[0]: parts[1]}
        for name, version in values.items():
            if name in pins and pins[name] != version:
                raise ValueError(f"conflicting exact requirement for {name}")
            pins[name] = version
    seen.remove(path)
    return pins


def prepare_environment(output: Path) -> tuple[dict[str, Any], list[str]]:
    child_paths = [ROOT / "code", ROOT / "tests"]
    child_paths.extend(sorted((ROOT / "packages").glob("*/src")))
    path_strings = [str(path) for path in child_paths]
    # Set exactly the candidate source paths for fixture subprocesses.  The
    # invoking virtual environment still supplies its own site-packages.
    sys.path[:0] = path_strings
    sys.dont_write_bytecode = True
    child_environment = {
        "PYTHONPATH": os.pathsep.join(path_strings),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "CDPD_TEST_BROWSER": "0",
        "CDPD_L05_EVIDENCE_DIR": str(output / RAW_DIRS[0]),
        "CDPD_L06_EVIDENCE_DIR": str(output / RAW_DIRS[1]),
        "CDPD_L07_EVIDENCE_DIR": str(output / RAW_DIRS[2]),
    }
    os.environ.update(child_environment)
    # Environment-supplied pytest flags/plugins could alter selection, the
    # conftest safety fixtures or the skip set.  Record and remove only those
    # controls; do not dump arbitrary environment variables into evidence.
    removed_controls = {}
    for name in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS"):
        removed_controls[name] = name in os.environ
        os.environ.pop(name, None)
    errors = []
    virtual_environment = sys.prefix != sys.base_prefix
    if not virtual_environment:
        errors.append("exact-pinned virtual environment required")
    if len(child_paths) != 5:
        errors.append("expected candidate code, tests and exactly three package src paths")
    try:
        pins = requirement_pins(ROOT / "requirements-l06-synthetic.txt")
    except (OSError, ValueError) as exc:
        pins = {}
        errors.append(f"requirement file error: {exc}")
    distributions = {}
    for name, expected in sorted(pins.items()):
        try:
            actual = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            actual = None
        matches = actual == expected
        distributions[name] = {"expected": expected, "actual": actual, "matches": matches}
        if not matches:
            errors.append(f"dependency mismatch: {name} expected {expected}, actual {actual}")
    environment = {
        "schema": "cdpd.l07.execution-environment/v1",
        "python": sys.version, "executable": sys.executable,
        "python_implementation": platform.python_implementation(),
        "platform": platform.platform(), "os_name": os.name,
        "virtual_environment": virtual_environment,
        "prefix": sys.prefix, "base_prefix": sys.base_prefix,
        "dependency_pin_file": "requirements-l06-synthetic.txt",
        "distributions": distributions,
        "exact_dependencies_verified": bool(pins) and all(
            row["matches"] for row in distributions.values()
        ),
        "child_environment": child_environment,
        "environment_pytest_controls_removed": removed_controls,
        "prerequisite_errors": errors,
        "network_and_browser_policy": "UNCHANGED_ROOT_CONFTEST",
        "root_conftest_sha256": sha256(ROOT / "conftest.py"),
    }
    return environment, errors


class EvidencePlugin:
    """Observe pytest reports; never change test selection, outcomes or markers."""

    def __init__(self) -> None:
        self.items: dict[str, Any] = {}
        self.reports: dict[str, dict[str, Any]] = {}
        self.collection_errors: list[dict[str, str]] = []
        self.deselected: list[str] = []
        self.recording_errors: list[dict[str, str]] = []
        self.collected = 0

    def pytest_collectreport(self, report: Any) -> None:
        if report.failed:
            self.collection_errors.append({
                "nodeid": report.nodeid, "longrepr": str(report.longrepr),
            })

    def pytest_deselected(self, items: list[Any]) -> None:
        self.deselected.extend(item.nodeid for item in items)

    def pytest_collection_finish(self, session: Any) -> None:
        self.collected = len(session.items)
        for item in session.items:
            if item.nodeid in self.items:
                self.collection_errors.append({
                    "nodeid": item.nodeid,
                    "longrepr": "duplicate nodeid; repeated cases are not additional evidence",
                })
                session.shouldstop = "duplicate test nodeid in explicit selection"
            else:
                self.items[item.nodeid] = item

    def pytest_runtest_logreport(self, report: Any) -> None:
        phase: dict[str, Any] = {
            "outcome": report.outcome, "duration_seconds": report.duration,
            "start_epoch_seconds": report.start, "stop_epoch_seconds": report.stop,
        }
        if report.skipped:
            reason = report.longrepr
            phase["skip_reason"] = str(reason[2] if isinstance(reason, tuple) else reason)
        if report.failed:
            phase["longrepr"] = str(report.longrepr)
        if hasattr(report, "wasxfail"):
            phase["xfail_reason"] = str(report.wasxfail)
        phases = self.reports.setdefault(report.nodeid, {})
        if report.when in phases:
            self.recording_errors.append({
                "nodeid": report.nodeid,
                "error": f"repeated {report.when} report; no additional case counted",
            })
        phases[report.when] = phase

    def rows(self) -> list[dict[str, Any]]:
        rows = []
        for nodeid, item in self.items.items():
            observed_phases = self.reports.get(nodeid, {})
            phases = {
                name: observed_phases.get(name, {
                    "outcome": "not_run", "duration_seconds": None,
                })
                for name in ("setup", "call", "teardown")
            }
            if any(phase["outcome"] == "failed" for phase in phases.values()):
                status = "FAIL"
            elif any(phase["outcome"] == "skipped" for phase in phases.values()):
                status = "NOT_RUN"
            elif all(phase["outcome"] == "passed" for phase in phases.values()):
                status = "PASS"
            else:
                status = "NOT_RUN"
            recorded = {}
            for name in EVIDENCE_ATTRIBUTES:
                if hasattr(item, name):
                    try:
                        recorded[name] = json.loads(json.dumps(
                            getattr(item, name), ensure_ascii=False, allow_nan=False,
                            default=json_extra,
                        ))
                    except (TypeError, ValueError, OverflowError) as exc:
                        self.recording_errors.append({
                            "nodeid": nodeid, "attribute": name,
                            "error": f"non-JSON evidence: {type(exc).__name__}: {exc}",
                        })
            selected_attribute = next((
                name for name in EVIDENCE_ATTRIBUTES
                if isinstance(recorded.get(name), dict)
            ), None)
            selected = recorded.get(selected_attribute, {})
            path = Path(str(item.path)).resolve()
            fixture_ref = path.relative_to(ROOT).as_posix()
            doc = inspect.getdoc(getattr(item, "obj", None))
            row = {
                "test_id": nodeid, "nodeid": nodeid, "status": status,
                "fixture_ref": fixture_ref, "fixture_sha256": sha256(path),
                "phases": phases,
                "duration_seconds": sum(
                    phase["duration_seconds"] or 0.0 for phase in phases.values()
                ),
                "skip_reasons": [
                    phase["skip_reason"] for phase in phases.values() if "skip_reason" in phase
                ],
                "expected": selected.get("expected", doc or f"Assertions in {nodeid}"),
                "actual": selected.get("actual", {
                    "call_executed": "call" in observed_phases,
                    "all_pytest_phases_passed": status == "PASS",
                }),
                "preferred_evidence_attribute": selected_attribute,
                "evidence": recorded,
            }
            rows.append(row)
        return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--evidence-dir", required=True,
                        help="fresh directory inside this Candidate, outside source directories; prior evidence is never overwritten")
    parser.add_argument("selection", nargs="*", metavar="PATH[::NODEID]",
                        help="candidate test paths/nodeids; omitted means the full inherited + L07 suite")
    args = parser.parse_args(argv)
    output = Path(args.evidence_dir)
    output = (ROOT / output).resolve() if not output.is_absolute() else output.resolve()
    if not output.is_relative_to(ROOT):
        parser.error("evidence-dir must be inside this Candidate for inherited ROOT-relative evidence; use a fresh verified execution copy when preserving an existing deliverable")
    if output == ROOT or any(output.is_relative_to(ROOT / name) for name in SOURCE_DIRS):
        parser.error("evidence-dir must be outside candidate source directories")
    for name in (*MANAGED_FILES, *RAW_DIRS):
        if (output / name).exists():
            parser.error(f"prior evidence exists: {output / name}; choose a new evidence-dir")
    selection = args.selection or ["tests", "packages/chatgpt-web-api/tests"]
    for value in selection:
        path_part = value.split("::", 1)[0]
        if not path_part or value.startswith(("-", "@")):
            parser.error("selection must be test paths/nodeids, not pytest options or argument files")
        selected_path = Path(path_part)
        selected_path = (ROOT / selected_path).resolve() if not selected_path.is_absolute() else selected_path.resolve()
        if not selected_path.is_relative_to(ROOT):
            parser.error(f"test selection is outside this candidate: {value}")
        if not selected_path.is_file() and not selected_path.is_dir():
            parser.error(f"test selection path does not exist: {value}")
    output.mkdir(parents=True, exist_ok=True)
    os.chdir(ROOT)
    started = utc_now()
    started_monotonic = time.monotonic()
    before = snapshot()
    environment, prerequisite_errors = prepare_environment(output)
    write_json(output / "ENVIRONMENT.json", environment)
    pytest_args = ["-q", "-p", "no:cacheprovider", "--tb=short", *selection]
    command = {
        "schema": "cdpd.l07.execution-command/v1", "started_utc": started,
        "finished_utc": None, "cwd": str(ROOT),
        "invocation": list(getattr(sys, "orig_argv", [sys.executable, *sys.argv])),
        "pytest_entrypoint": "pytest.main", "pytest_args": pytest_args,
        "equivalent_pytest_command": [sys.executable, "-B", "-m", "pytest", *pytest_args],
        "selection": selection, "selection_kind": "EXPLICIT" if args.selection else "ALL",
        "environment_ref": "ENVIRONMENT.json", "pytest_exit_code": None,
        "effective_exit_code": None,
        "raw_stdout_stderr": "RECORDED_BY_CALLER_OUTSIDE_THIS_RUNNER",
    }
    write_json(output / "COMMAND.json", command)
    write_json(output / "SOURCE_HASHES.json", {"before": before, "after": None, "unchanged": None})
    write_json(output / "TEST_SUMMARY.json", {
        "schema": "cdpd.l07.execution-summary/v1", "ticket_id": "CDPD-L07",
        "state": "RUNNING", "status": "NOT_RUN", "started_utc": started,
        "note": "No completed execution result has been committed yet.",
    })
    plugin = EvidencePlugin()
    pytest_exit_code = None
    runner_error = None
    if not prerequisite_errors:
        try:
            import pytest
            pytest_exit_code = int(pytest.main(pytest_args, plugins=[plugin]))
        except KeyboardInterrupt:
            runner_error = {"type": "KeyboardInterrupt", "detail": "runner interrupted"}
        except Exception as exc:
            runner_error = {"type": type(exc).__name__, "detail": str(exc),
                            "traceback": traceback.format_exc()}
    rows = plugin.rows()
    after = snapshot()
    changed = sorted(name for name in before.keys() | after.keys() if before.get(name) != after.get(name))
    unchanged = not changed
    counts = {status: sum(row["status"] == status for row in rows)
              for status in ("PASS", "FAIL", "NOT_RUN")}
    runner_failures = bool(prerequisite_errors or runner_error or plugin.collection_errors
                           or plugin.recording_errors or not unchanged)
    effective_exit = 1 if runner_failures else (pytest_exit_code if pytest_exit_code is not None else 1)
    if counts["FAIL"] and effective_exit == 0:
        effective_exit = 1
    if effective_exit:
        status = "FAIL"
    elif counts["PASS"]:
        status = "PASS"
    else:
        status = "NOT_RUN"
    finished = utc_now()
    command.update({"finished_utc": finished, "pytest_exit_code": pytest_exit_code,
                    "effective_exit_code": effective_exit})
    write_json(output / "COMMAND.json", command)
    write_json(output / "SOURCE_HASHES.json", {
        "before": before, "after": after, "unchanged": unchanged, "changed_paths": changed,
    })
    results = {
        "schema": "cdpd.l07.execution-cases/v1", "ticket_id": "CDPD-L07",
        "role": "EXECUTION_INTERNAL_QA", "tests": rows,
        "collection_errors": plugin.collection_errors, "deselected_test_ids": plugin.deselected,
        "recording_errors": plugin.recording_errors,
    }
    write_json(output / "TEST_RESULTS.json", results)
    summary = {
        "schema": "cdpd.l07.execution-summary/v1", "ticket_id": "CDPD-L07",
        "state": "COMPLETED", "role": "EXECUTION_INTERNAL_QA", "status": status,
        "started_utc": started, "finished_utc": finished,
        "duration_seconds": time.monotonic() - started_monotonic,
        "collected": plugin.collected, "unique_test_ids": len(rows), "counts": counts,
        "test_ids_by_status": {
            value: [row["test_id"] for row in rows if row["status"] == value]
            for value in counts
        },
        "skip_reason_counts": dict(Counter(
            reason for row in rows for reason in row["skip_reasons"]
        )),
        "pytest_exit_code": pytest_exit_code, "effective_exit_code": effective_exit,
        "inputs_unchanged_during_run": unchanged,
        "prerequisite_errors": prerequisite_errors, "runner_error": runner_error,
        "collection_error_count": len(plugin.collection_errors),
        "recording_error_count": len(plugin.recording_errors),
        "files": {name: {"sha256": sha256(output / name), "bytes": (output / name).stat().st_size}
                  for name in MANAGED_FILES if name != "TEST_SUMMARY.json"},
        "scope": "LOCAL_SYNTHETIC_TESTS_AND_OWNED_PROCESS_CONTROLS",
        "live_browser": "NOT_RUN", "native_windows_acceptance": "NOT_RUN",
        "independent_assessment_performed": False, "l07_accepted": False,
    }
    write_json(output / "TEST_SUMMARY.json", summary)
    print(json.dumps({"status": status, "collected": plugin.collected, "counts": counts,
                      "pytest_exit_code": pytest_exit_code, "effective_exit_code": effective_exit,
                      "inputs_unchanged_during_run": unchanged, "evidence_dir": str(output)},
                     ensure_ascii=False, separators=(",", ":")))
    return effective_exit


if __name__ == "__main__":
    raise SystemExit(main())
