#!/usr/bin/env bash
# tofu init and validate /build/<class>/tofu for each class given. The
# provider is fetched from the OpenTofu registry, which checks its signature.
set -euo pipefail

tofu version | head -1
export TF_PLUGIN_CACHE_DIR=/tmp/plugins
mkdir -p "$TF_PLUGIN_CACHE_DIR"
fail=0
for cls in "$@"; do
  work=/tmp/$cls
  cp -r "/build/$cls/tofu" "$work"
  if ! (cd "$work" && tofu init -backend=false -input=false -no-color >/tmp/init.log 2>&1); then
    cat /tmp/init.log
    echo "FAIL $cls init"
    fail=1
    continue
  fi
  # validate exits 1 on errors; report.py reads the verdict from its JSON.
  result=$(tofu -chdir="$work" validate -json -no-color) || true
  python3 /usr/local/bin/report.py "$cls" <<<"$result" || fail=1
done
exit "$fail"
