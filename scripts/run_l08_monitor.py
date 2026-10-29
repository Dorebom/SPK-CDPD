"""Fixed Task Scheduler action; never registers/enables/starts a task itself."""
from pathlib import Path
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "code", ROOT / "packages/cdpd/src"):
    sys.path.insert(0, str(path))
from cdpd_chatgpt_profile.l08_monitor import main

if __name__ == "__main__":
    raise SystemExit(main())
