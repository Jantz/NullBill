"""Print `tofu validate -json` output for one class; exit 1 unless valid."""

import json
import sys

cls, result = sys.argv[1], json.load(sys.stdin)
for d in result["diagnostics"]:
    print(f"  {d['severity']}: {d['summary']}: {d['detail'][:200]}")
print("ok  " if result["valid"] else "FAIL", cls, f"validate, {result['warning_count']} warnings")
sys.exit(0 if result["valid"] else 1)
