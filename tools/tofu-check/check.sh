#!/usr/bin/env bash
# tofu init and validate /build/<class>/tofu for each class given. The
# provider is fetched from the OpenTofu registry, which checks its signature.
set -euo pipefail

# sed reads to the end: head would close the pipe while tofu still writes
# its update notice, and pipefail would end the check on SIGPIPE.
tofu version | sed -n 1p
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
# apply's full run: a saved plan, encrypted too, read back by tofu show -json
# (apply.guest_removals reads type, name, address and actions), then applied.
python3 - "$work/main.tf.json" <<'PY'
import json, sys
config = json.load(open(sys.argv[1]))
config["resource"]["terraform_data"] = {"other": {"input": "kept"}}
json.dump(config, open(sys.argv[1], "w"))
PY
if ! (cd "$work" && tofu plan -input=false -no-color -out=nullbill.tfplan >/tmp/plan.log 2>&1); then
  cat /tmp/plan.log
  echo "FAIL saved plan"
  fail=1
elif grep -q nullbill-state-marker "$work/nullbill.tfplan"; then
  echo "FAIL plan file holds plaintext"
  fail=1
elif ! tofu -chdir="$work" show -json nullbill.tfplan | python3 -c '
import json, sys
changes = {c["address"]: (c["type"], c["name"], c["change"]["actions"]) for c in json.load(sys.stdin)["resource_changes"]}
assert changes["terraform_data.marker"] == ("terraform_data", "marker", ["delete"]), changes
'; then
  echo "FAIL tofu show -json of the saved plan"
  fail=1
elif ! (cd "$work" && tofu apply -input=false -no-color nullbill.tfplan >/tmp/apply.log 2>&1); then
  cat /tmp/apply.log
  echo "FAIL applying the saved plan"
  fail=1
else
  echo "ok   a saved plan is encrypted, tofu show -json names what it deletes, and it applies"
fi
exit "$fail"
