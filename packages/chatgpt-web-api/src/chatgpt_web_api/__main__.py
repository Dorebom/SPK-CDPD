from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .api import ChatGPTWebAPI, prompt_sha256


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m chatgpt_web_api")
    sub = parser.add_subparsers(dest="command", required=True)

    smoke = sub.add_parser("smoke", help="Run the M1 Project→Chat→Prompt→Response→ZIP vertical slice.")
    smoke.add_argument("--project-name", required=True)
    smoke.add_argument("--project-key")
    smoke.add_argument("--project-url")
    smoke.add_argument("--prompt-file", required=True)
    smoke.add_argument("--expected-artifact", required=True)
    smoke.add_argument("--output-dir", required=True)
    smoke.add_argument("--cdp-url", default="http://127.0.0.1:9222")
    smoke.add_argument("--client-ref", default="m1-web-005-smoke")
    smoke.add_argument("--project-timeout-seconds", type=float, default=15.0)
    smoke.add_argument("--chat-timeout-seconds", type=float, default=15.0)
    smoke.add_argument("--submit-timeout-seconds", type=float, default=10.0)
    smoke.add_argument("--generation-timeout-seconds", type=float, default=900.0)
    smoke.add_argument("--artifact-timeout-seconds", type=float, default=30.0)
    smoke.add_argument("--poll-interval-seconds", type=float, default=0.5)
    smoke.add_argument("--stable-samples-required", type=int, default=3)
    smoke.add_argument("--print-json", action="store_true")

    observe = sub.add_parser(
        "observe-attachment-transport",
        help="Stage-1 real-Web observation: Project→new Chat→attachment action→picker transport, then stop without upload or Prompt dispatch.",
    )
    observe.add_argument("--project-name", required=True)
    observe.add_argument("--project-key")
    observe.add_argument("--project-url")
    observe.add_argument("--output-dir", required=True)
    observe.add_argument("--cdp-url", default="http://127.0.0.1:9222")
    observe.add_argument("--client-ref", default="m1-webfix-008-stage1")
    observe.add_argument("--project-timeout-seconds", type=float, default=15.0)
    observe.add_argument("--attachment-timeout-seconds", type=float, default=5.0)
    observe.add_argument("--poll-interval-seconds", type=float, default=0.1)
    observe.add_argument("--print-json", action="store_true")

    attachment_smoke = sub.add_parser(
        "smoke-attachment",
        help="Stage-2 real-Web smoke: native picker bridge→real attachment→marker response→response-bound ZIP.",
    )
    attachment_smoke.add_argument("--project-name", required=True)
    attachment_smoke.add_argument("--project-key")
    attachment_smoke.add_argument("--project-url")
    attachment_smoke.add_argument("--attachment-file", required=True)
    attachment_smoke.add_argument("--expected-marker", required=True)
    attachment_smoke.add_argument("--expected-artifact", required=True)
    attachment_smoke.add_argument("--output-dir", required=True)
    attachment_smoke.add_argument("--cdp-url", default="http://127.0.0.1:9222")
    attachment_smoke.add_argument("--client-ref", default="m1-webfix-009-stage2")
    attachment_smoke.add_argument("--project-timeout-seconds", type=float, default=15.0)
    attachment_smoke.add_argument("--chat-timeout-seconds", type=float, default=15.0)
    attachment_smoke.add_argument("--attachment-timeout-seconds", type=float, default=15.0)
    attachment_smoke.add_argument("--submit-timeout-seconds", type=float, default=10.0)
    attachment_smoke.add_argument("--generation-timeout-seconds", type=float, default=900.0)
    attachment_smoke.add_argument("--artifact-timeout-seconds", type=float, default=30.0)
    attachment_smoke.add_argument("--poll-interval-seconds", type=float, default=0.5)
    attachment_smoke.add_argument("--stable-samples-required", type=int, default=3)
    attachment_smoke.add_argument("--print-json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "observe-attachment-transport":
        output_dir = Path(args.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        api = ChatGPTWebAPI(args.cdp_url)
        result = api.run_attachment_transport_observation(
            project_name=args.project_name,
            project_key=args.project_key,
            project_url=args.project_url,
            client_ref=args.client_ref,
            project_timeout_seconds=args.project_timeout_seconds,
            attachment_timeout_seconds=args.attachment_timeout_seconds,
            poll_interval_seconds=args.poll_interval_seconds,
        )
        payload = result.to_dict()
        payload["input"] = {
            "project_name": args.project_name, "project_key": args.project_key,
            "project_url": args.project_url, "cdp_url": args.cdp_url,
            "client_ref": args.client_ref,
        }
        payload["stage1_contract"] = {
            "file_assigned": False, "prompt_dispatched": False,
            "stop_after": "PICKER_TRANSPORT_OBSERVED_NO_UPLOAD",
        }
        report_path = _unique_named_report_path(output_dir, "chatgpt_web_attachment_observation")
        screenshot_path = report_path.with_suffix(".png")
        screenshot_captured = False
        if not result.ok:
            capture = getattr(getattr(api, "session", None), "capture_bound_screenshot", None)
            if callable(capture):
                try:
                    screenshot_captured = bool(capture(screenshot_path))
                except Exception:
                    screenshot_captured = False
        payload["diagnostic_screenshot"] = {
            "captured": screenshot_captured,
            "path": str(screenshot_path) if screenshot_captured else None,
        }
        payload["report_path"] = str(report_path)
        report_path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        if args.print_json:
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        else:
            print(f"attachment transport observation : {payload['result']}")
            print(f"Report                           : {report_path}")
            if payload.get("failure_stage"):
                print(f"Failure Stage                    : {payload['failure_stage']}")
            if payload.get("reason"):
                print(f"Reason                           : {payload['reason']}")
            print("File Assigned                    : NO")
            print("Web Prompt                       : NOT DISPATCHED")
        return 0 if result.ok else 1

    if args.command == "smoke-attachment":
        attachment_path = Path(args.attachment_file)
        if not attachment_path.is_file():
            print(f"ERROR: attachment file does not exist: {attachment_path}", file=sys.stderr)
            return 2
        output_dir = Path(args.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        api = ChatGPTWebAPI(args.cdp_url)
        result = api.run_attachment_vertical_slice_smoke(
            project_name=args.project_name,
            project_key=args.project_key,
            project_url=args.project_url,
            attachment_file=attachment_path,
            expected_marker=args.expected_marker,
            expected_artifact=args.expected_artifact,
            output_dir=output_dir,
            client_ref=args.client_ref,
            project_timeout_seconds=args.project_timeout_seconds,
            chat_timeout_seconds=args.chat_timeout_seconds,
            attachment_timeout_seconds=args.attachment_timeout_seconds,
            submit_timeout_seconds=args.submit_timeout_seconds,
            generation_timeout_seconds=args.generation_timeout_seconds,
            artifact_timeout_seconds=args.artifact_timeout_seconds,
            poll_interval_seconds=args.poll_interval_seconds,
            stable_samples_required=args.stable_samples_required,
        )
        payload = result.to_dict()
        payload["input"] = {
            "project_name": args.project_name,
            "project_key": args.project_key,
            "project_url": args.project_url,
            "attachment_file": str(attachment_path),
            "expected_marker_sha256": __import__("hashlib").sha256(args.expected_marker.encode("utf-8")).hexdigest(),
            "expected_artifact": args.expected_artifact,
            "cdp_url": args.cdp_url,
        }
        report_path = _unique_named_report_path(output_dir, "chatgpt_web_attachment_vertical_slice")
        screenshot_path = report_path.with_suffix(".png")
        screenshot_captured = False
        if not result.ok:
            capture = getattr(getattr(api, "session", None), "capture_bound_screenshot", None)
            if callable(capture):
                try:
                    screenshot_captured = bool(capture(screenshot_path))
                except Exception:
                    screenshot_captured = False
        payload["diagnostic_screenshot"] = {
            "captured": screenshot_captured,
            "path": str(screenshot_path) if screenshot_captured else None,
        }
        payload["report_path"] = str(report_path)
        report_path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        if args.print_json:
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        else:
            print(f"attachment vertical slice : {payload['result']}")
            print(f"Report                    : {report_path}")
            if payload.get("artifact"):
                print(f"Artifact                  : {payload['artifact']['saved_path']}")
            if payload.get("failure_stage"):
                print(f"Failure Stage             : {payload['failure_stage']}")
            if payload.get("reason"):
                print(f"Reason                    : {payload['reason']}")
        return 0 if result.ok else 1

    if args.command != "smoke":
        return 2

    prompt_path = Path(args.prompt_file)
    if not prompt_path.is_file():
        print(f"ERROR: prompt file does not exist: {prompt_path}", file=sys.stderr)
        return 2
    try:
        prompt = prompt_path.read_text(encoding="utf-8")
    except Exception as exc:
        print(f"ERROR: prompt file could not be read as UTF-8: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if not prompt.strip():
        print("ERROR: prompt file is empty.", file=sys.stderr)
        return 2

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    api = ChatGPTWebAPI(args.cdp_url)
    result = api.run_vertical_slice_smoke(
        project_name=args.project_name,
        project_key=args.project_key,
        project_url=args.project_url,
        prompt=prompt,
        expected_artifact=args.expected_artifact,
        output_dir=output_dir,
        client_ref=args.client_ref,
        project_timeout_seconds=args.project_timeout_seconds,
        chat_timeout_seconds=args.chat_timeout_seconds,
        submit_timeout_seconds=args.submit_timeout_seconds,
        generation_timeout_seconds=args.generation_timeout_seconds,
        artifact_timeout_seconds=args.artifact_timeout_seconds,
        poll_interval_seconds=args.poll_interval_seconds,
        stable_samples_required=args.stable_samples_required,
    )

    payload = result.to_dict()
    payload["input"] = {
        "project_name": args.project_name,
        "project_key": args.project_key,
        "project_url": args.project_url,
        "prompt_file": str(prompt_path),
        "prompt_sha256": prompt_sha256(prompt),
        "expected_artifact": args.expected_artifact,
        "cdp_url": args.cdp_url,
    }
    payload["checkpoint_b"] = "CANDIDATE_PASS_REQUIRES_EXTERNAL_REVIEW" if result.ok else "NOT_ACHIEVED"

    report_path = _unique_report_path(output_dir)
    report_path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    payload["report_path"] = str(report_path)

    if args.print_json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(f"chatgpt_web_api smoke : {payload['result']}")
        print(f"Report                : {report_path}")
        if payload.get("artifact"):
            print(f"Artifact              : {payload['artifact']['saved_path']}")
        if payload.get("failure_stage"):
            print(f"Failure Stage         : {payload['failure_stage']}")
        if payload.get("reason"):
            print(f"Reason                : {payload['reason']}")

    return 0 if result.ok else 1


def _unique_named_report_path(output_dir: Path, prefix: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    candidate = output_dir / f"{prefix}_{stamp}.json"
    counter = 1
    while candidate.exists():
        candidate = output_dir / f"{prefix}_{stamp}.{counter}.json"
        counter += 1
    return candidate


def _unique_report_path(output_dir: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    candidate = output_dir / f"chatgpt_web_api_smoke_{stamp}.json"
    counter = 1
    while candidate.exists():
        candidate = output_dir / f"chatgpt_web_api_smoke_{stamp}.{counter}.json"
        counter += 1
    return candidate


if __name__ == "__main__":
    raise SystemExit(main())
