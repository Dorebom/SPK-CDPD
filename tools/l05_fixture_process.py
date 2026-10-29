"""Local test process only: no browser, network, or production state access.

This script is evidence infrastructure, not a production Supervisor/Worker.
Exit 73 deliberately demonstrates a process dying with unpublished queue loss.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))
for path in (ROOT / "packages").glob("*/src"):
    sys.path.insert(0, str(path))

from cdpd import DiagnosticEvent
from cdpd_chatgpt_profile.failure_hooks import FailureHooks, HookLimits
from cdpd_chatgpt_profile.spool import FileMailbox, JournalSpool, read_history, read_journal


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["clean", "queue_full", "crash_unflushed", "read"])
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    if args.mode == "read":
        history = read_history(args.root)
        rows = read_journal(args.root)["rows"]
        print(json.dumps({"pid": os.getpid(), "history": history,
                          "journal_row_count": len(rows)}, sort_keys=True))
        return 0

    args.root.mkdir(parents=True, exist_ok=True)
    mailbox = FileMailbox(args.root)
    hooks = FailureHooks(mailbox.publish, producer_id="synthetic-producer",
                         producer_instance_id="synthetic-instance",
                         checkpoint_publisher=mailbox.publish_checkpoint,
                         limits=HookLimits(queue_total=2, queue_critical_reserved=0))
    spool = JournalSpool(args.root)

    def emit(kind, code="NONE"):
        return hooks.try_emit(DiagnosticEvent("synthetic-producer", "synthetic-instance",
                                              " caller-owned-op-01 ", kind,
                                              failure_code=code,
                                              correlation={"trace_id": "opaque-01"}))

    assert emit("ACTION_STARTED")
    if args.mode == "crash_unflushed":
        assert hooks.drain_once() == 1
        receipts = spool.process_pending()
        assert len(receipts) == 1 and receipts[0]["stage"] == "ACK_DURABLE"
        assert emit("ACTION_FINISHED")
        assert emit("ACTION_FAILED", "UNKNOWN")
        assert not emit("OBSERVATION_FAILED", "UNKNOWN")
        print(json.dumps({"pid": os.getpid(), "mode": args.mode,
                          "durable_ack_count": 1, "memory_stats": hooks.stats,
                          "deliberate_exit_code": 73}, sort_keys=True), flush=True)
        # No close/finally: actual process crash loses queued event and final counter.
        os._exit(73)
    assert emit("ACTION_FINISHED")
    if args.mode == "queue_full":
        assert not emit("OBSERVATION_FAILED", "UNKNOWN")
    assert hooks.heartbeat(operation_id=" caller-owned-op-01 ", correlation={"trace_id": "opaque-01"})
    assert hooks.close(timeout=2.0)
    receipts = spool.process_pending()
    assert len(receipts) == 3
    assert all(receipt["stage"] == "ACK_DURABLE" for receipt in receipts)
    spool.close()
    print(json.dumps({"pid": os.getpid(), "mode": args.mode,
                      "durable_ack_count": len(receipts), "memory_stats": hooks.stats,
                      "deliberate_exit_code": 0}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
