#!/usr/bin/env python3
"""Offline FIX001 regression. No browser, production imports, or source writes.

Uses child-process import/registry fault injection to simulate absent or broken
optional format support. This is NOT a Windows or clean-install test.
"""
from __future__ import annotations
import argparse
import copy
import json
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
PLAN_ROOT: Path | None = None
BOOTSTRAP = r'''
import importlib.abc, runpy, sys
mode, script, *args = sys.argv[1:]
class MissingModule(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        missing = {'missing_rfc3339': 'rfc3339_validator', 'missing_jsonschema': 'jsonschema'}.get(mode)
        if missing and (fullname == missing or fullname.startswith(missing + '.')):
            raise ImportError('FIX001_SIMULATED_MODULE_UNAVAILABLE:' + missing)
sys.meta_path.insert(0, MissingModule())
if mode in {'missing_uuid', 'date_accept_all', 'date_reject_all'}:
    from jsonschema import FormatChecker
    if mode == 'missing_uuid':
        FormatChecker.checkers.pop('uuid', None)
    else:
        expected = mode == 'date_accept_all'
        FormatChecker.checkers['date-time'] = (lambda value: expected, ())
sys.argv = [script, *args]
runpy.run_path(script, run_name='__main__')
'''


class ValidationEnvironmentRegression(unittest.TestCase):
    def run_validator(self, mode: str):
        args = [sys.executable, '-B', '-c', BOOTSTRAP, mode,
                str(ROOT / 'tests/validate_l01_design.py'),
                '--plan-root', str(PLAN_ROOT)]
        proc = subprocess.run(args, capture_output=True, text=True,
                              encoding='utf-8', timeout=45, cwd=ROOT)
        lines = [line for line in proc.stdout.splitlines() if line.startswith('{')]
        self.assertTrue(lines, proc.stdout + proc.stderr)
        return proc, json.loads(lines[-1])

    def assert_blocked(self, mode: str, code: str):
        proc, result = self.run_validator(mode)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(result['reason_code'], code)
        self.assertEqual(result['checks'], 0)
        self.assertEqual(result['passed'], 0)
        self.assertEqual(result['runtime_tests_executed'], 0)
        self.assertFalse(result['gate_approved'])
        self.assertNotIn('PASS SCHEMA-', proc.stdout)

    def test_01_full_design_validation(self):
        proc, result = self.run_validator('normal')
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual((result['checks'], result['passed'], result['failed']), (92, 92, 0))
        self.assertIn('PASS SCHEMA-19-invalid-date', proc.stdout)
        self.assertIn('PASS PACKAGE-02-all-hashes-match', proc.stdout)
        self.assertEqual(result['runtime_tests_executed'], 0)
        self.assertFalse(result['gate_approved'])

    def test_02_missing_rfc3339_blocks_before_design_checks(self):
        self.assert_blocked('missing_rfc3339', 'REQUIRED_FORMAT_CHECKER_UNAVAILABLE')

    def test_03_missing_jsonschema_blocks_before_design_checks(self):
        self.assert_blocked('missing_jsonschema', 'JSONSCHEMA_IMPORT_UNAVAILABLE')

    def test_04_missing_uuid_checker_blocks(self):
        self.assert_blocked('missing_uuid', 'REQUIRED_FORMAT_CHECKER_UNAVAILABLE')

    def test_05_accept_all_checker_is_not_usable(self):
        self.assert_blocked('date_accept_all', 'REQUIRED_FORMAT_CHECKER_SELFTEST_FAILED')

    def test_06_reject_all_checker_is_not_usable(self):
        self.assert_blocked('date_reject_all', 'REQUIRED_FORMAT_CHECKER_SELFTEST_FAILED')

    def test_07_both_timestamp_fields_check_calendar_dates(self):
        from jsonschema import Draft202012Validator, FormatChecker
        schema = json.loads((ROOT / 'contracts/chatgpt_target_binding_v1.schema.json').read_text(encoding='utf-8'))
        sample = json.loads((ROOT / 'tests/fixtures/binding_valid.json').read_text(encoding='utf-8'))
        checker = FormatChecker()
        self.assertIn('date-time', checker.checkers)
        validator = Draft202012Validator(schema, format_checker=checker)
        cases = [('2026-02-30T12:00:00Z', False), ('2026-02-29T12:00:00Z', False),
                 ('2024-02-29T12:00:00Z', True), ('2026-04-31T12:00:00Z', False),
                 ('2026-13-01T12:00:00Z', False), ('2026-10-17T24:00:00Z', False),
                 ('2026-10-17T12:60:00Z', False), ('2026-10-17T12:00:00Z', True)]
        for field in ('issued_at_utc', 'expires_at_utc'):
            for value, expected in cases:
                with self.subTest(field=field, value=value):
                    record = copy.deepcopy(sample)
                    record[field] = value
                    self.assertEqual(validator.is_valid(record), expected)

    def test_08_dependency_file_declares_format_support(self):
        lines = (ROOT / 'requirements-validation.txt').read_text(encoding='utf-8').splitlines()
        dependencies = [line.strip() for line in lines if line.strip() and not line.startswith('#')]
        self.assertEqual(dependencies, ['jsonschema[format-nongpl]==4.26.0'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan-root', required=True, type=Path)
    args = parser.parse_args()
    PLAN_ROOT = args.plan_root.resolve()
    if not (PLAN_ROOT / 'SOURCE_LOCK.json').is_file():
        parser.error('--plan-root must contain SOURCE_LOCK.json')
    unittest.main(argv=[sys.argv[0]], verbosity=2)
