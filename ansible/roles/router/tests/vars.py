"""Print router_files for the router check from a directory of rendered
files laid out as on the router (tests/files/etc/...)."""

import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
files = {"/" + str(p.relative_to(root)): p.read_text() for p in sorted(root.rglob("*")) if p.is_file()}
print(json.dumps({"router_files": files}))
