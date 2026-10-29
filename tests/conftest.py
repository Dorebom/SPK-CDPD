from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / 'packages').glob('*/src'):
    sys.path.insert(0, str(path))
