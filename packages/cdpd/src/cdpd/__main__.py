from __future__ import annotations
import argparse
import json
from dataclasses import asdict
from pathlib import Path
from . import CaptureRequest, CapturePolicy, CDPStructureCollector, DiagnosticService, EvidenceStore, verify_bundle


def _read(path: str) -> dict:
    raw = Path(path).read_bytes()
    if len(raw) > 65536:
        raise ValueError('input exceeds 64KiB')
    return json.loads(raw)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='cdpd')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('capabilities')
    capture = commands.add_parser('capture')
    capture.add_argument('--request-file', required=True)
    capture.add_argument('--policy-file', required=True)
    capture.add_argument('--output-root', required=True)
    verify = commands.add_parser('verify')
    verify.add_argument('--bundle', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'capabilities':
            result = DiagnosticService.capabilities()
            code = 0
        elif args.command == 'verify':
            result = verify_bundle(args.bundle)
            code = 0 if result['integrity_valid'] else 1
        else:
            policy_data = _read(args.policy_file)
            if set(policy_data) != {'enabled', 'allowed_origins'} or not isinstance(policy_data['allowed_origins'], list):
                raise ValueError('invalid policy')
            policy = CapturePolicy(allowed_origins=tuple(policy_data['allowed_origins']), enabled=policy_data['enabled'])
            service = DiagnosticService(CDPStructureCollector(policy), EvidenceStore(args.output_root))
            request = CaptureRequest.from_dict(_read(args.request_file))
            service.record(request.event)
            outcome = service.capture(request)
            result = asdict(outcome)
            code = 0 if outcome.capture_status == 'COMPLETE' else 1
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return code
    except Exception:
        # Never echo raw exception messages, input files, endpoints or auth-like values.
        print(json.dumps({'capture_status': 'ERROR', 'failure_code': 'LOCAL_INPUT_OR_STORAGE_ERROR', 'assessment_status': 'NOT_PERFORMED'}))
        return 2

if __name__ == '__main__':
    raise SystemExit(main())
