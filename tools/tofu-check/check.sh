#!/usr/bin/env bash
# tofu init and validate /build/<class>/tofu for each class given. The
# provider is fetched from the OpenTofu registry, which checks its signature.
set -euo pipefail

tofu version | head -1
# A throwaway passphrase: init and validate evaluate the encryption block.
export TF_VAR_state_passphrase=throwaway-check-passphrase
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
# The rendered encryption block, applied for real with one resource that
# holds a marker: the state must not contain it, and a wrong passphrase must
# not read the state back.
work=/tmp/encryption
mkdir -p "$work"
python3 - "/build/$1/tofu/main.tf.json" "$work/main.tf.json" <<'PY'
import json, sys
config = json.load(open(sys.argv[1]))
test = {
    "terraform": {"encryption": config["terraform"]["encryption"]},
    "variable": {"state_passphrase": config["variable"]["state_passphrase"]},
    "resource": {"terraform_data": {"marker": {"input": "nullbill-state-marker"}}},
}
json.dump(test, open(sys.argv[2], "w"))
PY
if ! (cd "$work" && tofu init -input=false -no-color >/dev/null && tofu apply -auto-approve -input=false -no-color >/tmp/apply.log 2>&1); then
  cat /tmp/apply.log
  echo "FAIL state encryption apply"
  fail=1
elif grep -q nullbill-state-marker "$work/terraform.tfstate"; then
  echo "FAIL state holds plaintext"
  fail=1
elif TF_VAR_state_passphrase=a-different-passphrase tofu -chdir="$work" plan -input=false -no-color >/dev/null 2>&1; then
  echo "FAIL a wrong passphrase read the state"
  fail=1
else
  echo "ok   state is encrypted and a wrong passphrase cannot read it"
fi
exit "$fail"
