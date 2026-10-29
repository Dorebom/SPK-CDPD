"""L08-specific synthetic validation recorder; not the FIX011 REV005 toolkit.

Run only after the delivered archive/source gate. Outputs must be outside this
Candidate so its Manifest and accepted tests are not rewritten by validation.
No native Windows, Scheduler registration, real browser or Human adoption is
claimed by a successful synthetic run.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
TESTS = ["tests/test_l08_report_engine.py", "tests/test_baseline_and_report.py",
         "tests/test_l08_scheduler_script.py", "tests/test_l06_bundle_contract.py"]
EXPECTED_CASES = 201


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def validation_output(value):
    output, source = Path(value).resolve(strict=False), ROOT.resolve(strict=True)
    if output == source or source in output.parents:
        raise ValueError("VALIDATION_OUTPUT_INSIDE_CANDIDATE")
    return output


def test_environment():
    environment = dict(os.environ)
    # Ambient options must not silently narrow the fixed declared suite or
    # inject an unrelated plugin into a validated source/evidence process.
    for key in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTEST_CURRENT_TEST"):
        environment.pop(key, None)
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONPATH"] = os.pathsep.join(str(ROOT / p) for p in (
        "code", "packages/cdpd/src", "packages/chatgpt-web-api/src", "packages/cdpd-chatgpt-adapter/src"))
    return environment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        output = validation_output(args.output_dir)
    except ValueError:
        parser.error("validation output must be outside the immutable Candidate")
    output.mkdir(parents=True, exist_ok=False)
    command = [sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider",
               "--junitxml", str(output / "pytest.xml"), "--basetemp", str(output / "tmp"), *TESTS]
    environment = test_environment()
    started = datetime.now(timezone.utc).isoformat()
    result = subprocess.run(command, cwd=ROOT, env=environment, capture_output=True, text=True)
    (output / "pytest.stdout.txt").write_text(result.stdout, encoding="utf-8")
    (output / "pytest.stderr.txt").write_text(result.stderr, encoding="utf-8")
    finished = datetime.now(timezone.utc).isoformat()
    rows = []
    if (output / "pytest.xml").exists():
        xml = ET.parse(output / "pytest.xml")
        for case in xml.iter("testcase"):
            filename = case.get("classname", "").replace(".", "/") + ".py"
            name = case.get("name", "")
            source = ROOT / filename
            status = "FAIL" if case.find("failure") is not None or case.find("error") is not None else (
                "NOT_RUN" if case.find("skipped") is not None else "PASS")
            rows.append({"test_id": filename + "::" + name, "status": status,
                "expected": {"contract": name.split("[")[0].removeprefix("test_").replace("_", " "),
                             "executable_assertions_ref": filename},
                "actual": {"pytest_status": status, "assertion_failures": 1 if status == "FAIL" else 0,
                           "duration_seconds": float(case.get("time", "0"))},
                "command": command, "exit_code": result.returncode,
                "log_ref": "pytest.stdout.txt", "xml_ref": "pytest.xml",
                "source_ref": filename, "source_sha": digest(source) if source.is_file() else None})
    counts = {name: sum(row["status"] == name for row in rows) for name in ("PASS", "FAIL", "BLOCKED", "NOT_RUN")}
    versions = {}
    for name in ("pytest", "jsonschema", "websocket-client", "attrs", "jsonschema-specifications",
                 "referencing", "rpds-py", "typing-extensions", "pluggy", "packaging", "iniconfig"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    passed = result.returncode == 0 and counts["FAIL"] == 0 and len(rows) == EXPECTED_CASES
    recorder_exit = result.returncode if result.returncode != 0 else (0 if passed else 1)
    summary = {"schema": "cdpd.l08-synthetic-validation/v1", "ticket_id": "CDPD-L08", "role": "EXECUTION",
        "authority": "CANDIDATE_ONLY", "toolkit_id": "L08_NEW_SYNTHETIC_RECORDER_NOT_FIX011_REV005",
        "recorder_sha256": digest(Path(__file__)),
        "status": "PASS" if passed else "FAIL",
        "expected_case_count": EXPECTED_CASES, "observed_case_count": len(rows), "recorder_exit_code": recorder_exit,
        "ambient_pytest_options_removed": True, "uncontrolled_plugin_autoload": False,
        "counts": counts, "command": command, "cwd": str(ROOT), "exit_code": result.returncode,
        "started_utc": started, "finished_utc": finished, "tests": rows,
        "environment": {"python": sys.version, "executable": sys.executable, "platform": platform.platform(),
                        "os_name": os.name, "distributions": versions},
        "source_pins": {p: digest(ROOT / p) for p in TESTS},
        "implementation_pins": {p.relative_to(ROOT).as_posix(): digest(p)
            for p in sorted((ROOT / "code/cdpd_chatgpt_profile").glob("*.py"))},
        "limits": {"native_windows_acceptance": False, "scheduler_registration": "NOT_RUN",
                   "powershell_dynamic_execution": "NOT_RUN", "real_browser_or_web": "NOT_RUN",
                   "human_baseline_adoption": "NOT_RUN", "independent_assessment": "NOT_RUN",
                   "inherited_l07_suite_rerun": False, "original_test_files_edited": False}}
    dump(output / "TEST_RESULTS.json", summary)
    dump(output / "ENVIRONMENT.json", summary["environment"])
    print(json.dumps({"status": summary["status"], "counts": counts, "exit_code": result.returncode,
                      "result": str(output / "TEST_RESULTS.json")}))
    return recorder_exit


if __name__ == "__main__":
    raise SystemExit(main())
