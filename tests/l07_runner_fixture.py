"""Local lifetime fixture; production Worker has no fixture flags.

The extra child has no sockets, browser or output. It belongs to the same
owned process group/Job as the fixture parent, so its existence tests the
difference between a zero leader exit and an entirely finished worker tree.
"""
from pathlib import Path
import runpy
import subprocess
import sys

mode, pid_path = sys.argv[1:]
if mode != "descendant_after_result":
    raise SystemExit(64)
child = subprocess.Popen([sys.executable, "-B", "-c", "import time; time.sleep(30)"],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True)
Path(pid_path).write_text(str(child.pid), encoding="ascii")
sys.argv = [str(Path(__file__).with_name("l06_process_fixture.py")), "normal"]
runpy.run_path(sys.argv[0], run_name="__main__")
