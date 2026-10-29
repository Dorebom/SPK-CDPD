"""Linux synthetic leader lifetime adapter; no protocol output or extra child.

Run in a new interpreter, never as a Popen preexec_fn. PDEATHSIG survives an
ordinary execve, but is not inherited by forked descendants. Linux associates
parent death with the creating thread, so that thread must remain alive while
its Worker is needed. This is not whole-tree Windows Job Object equivalence.
"""
from __future__ import annotations

import ctypes
import os
import signal
import sys


def _arm_parent_death():
    prctl = ctypes.CDLL(None, use_errno=True).prctl
    prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong,
                     ctypes.c_ulong, ctypes.c_ulong]
    prctl.restype = ctypes.c_int
    if prctl(1, int(signal.SIGKILL), 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        raise OSError(ctypes.get_errno(), 'PARENT_DEATH_SIGNAL_FAILED')


def main(argv=None):
    values = sys.argv[1:] if argv is None else argv
    try:
        if (not sys.platform.startswith('linux') or len(values) < 3
                or not values[0].isdecimal() or int(values[0]) <= 0
                or values[1] != '--' or not os.path.isabs(values[2])):
            return 125
        expected_parent = int(values[0])
        target = values[2:]
        _arm_parent_death()
        # Parent death before prctl would otherwise be missed. Both parent and
        # this child use the namespace checked before OwnedWorker launched us.
        if os.getppid() != expected_parent:
            return 125
        os.execve(target[0], target, os.environ)
    except BaseException:
        # stdout belongs exclusively to the unchanged Worker pipe protocol.
        return 125
    return 125  # execve must replace this interpreter.


if __name__ == '__main__':
    raise SystemExit(main())
