#!/usr/bin/env bash
# Container check for the inference role, run by tools/check-roles: install
# NVIDIA's pinned user-space driver (no GPU here), build llama.cpp's Vulkan
# server from the pinned source, converge twice, then serve a tiny model on
# Mesa's software Vulkan device (lavapipe) the way the unit runs it and
# prove the layers went to the Vulkan device, the gateway's key is needed,
# and an answer comes back.
set -euo pipefail
# shellcheck source=tools/role-check/role-check.sh
source /usr/local/lib/role-check.sh

# A software Vulkan device stands in for the card.
DEBIAN_FRONTEND=noninteractive apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends mesa-vulkan-drivers >/dev/null

key=check-key-0123456789abcdef
# stories260K: a 1 MiB model llama.cpp's own tests use.
vars=/tmp/inference-vars.json
cat >"$vars" <<EOF
{
  "inference_api_key": "$key",
  "inference_model_name": "check-model",
  "inference_model_url": "https://huggingface.co/ggml-org/models-moved/resolve/499bc8821c6b12b4e53c5bffcb21ec206f212d81/tinyllamas/stories260K.gguf",
  "inference_model_sha256": "270cba1bd5109f42d03350f60406024560464db173c0e387d91f0426d3bd256d",
  "inference_context": 256,
  "inference_listen": "127.0.0.1",
  "inference_nvidia_version": "$(sed -n 's/^nvidia_host_version: //p' /ansible/roles/nvidia_host/defaults/main.yml)",
  "inference_nvidia_sha256": "$(sed -n 's/^nvidia_host_sha256: //p' /ansible/roles/nvidia_host/defaults/main.yml)",
  "inference_require_gpu": false,
  "inference_vulkan_devices": "0",
  "inference_build_jobs": $(nproc)
}
EOF

converge inference.yml -e inference_manage_services=false -e "@$vars"

version=$(sed -n 's/^nvidia_host_version: //p' /ansible/roles/nvidia_host/defaults/main.yml)
test -e "/usr/lib/x86_64-linux-gnu/libnvidia-glcore.so.$version" || fail "NVIDIA's user space at $version"
grep -q libGLX_nvidia /etc/vulkan/icd.d/nvidia_icd.json || fail "NVIDIA's Vulkan driver registered"
test ! -e "/lib/modules/$(uname -r)/kernel/drivers/video/nvidia.ko" || fail "kernel modules installed in the container"
pass "NVIDIA's user-space driver $version is installed, with its Vulkan driver and no kernel module"

unit=/etc/systemd/system/llama-server.service
grep -q '^User=llama$' "$unit" || fail "the unit runs as llama"
grep -q '^SupplementaryGroups=video$' "$unit" || fail "the unit joins video, the device nodes' group"
grep -q -- '--api-key-file /etc/llama/api-key' "$unit" || fail "the unit needs the key"
[ "$(stat -c '%U:%G %a' /etc/llama/api-key)" = "root:llama 640" ] || fail "key file root:llama 0640"
pass "the unit runs as llama with the video group and the gateway's key"

# Run the server as the unit does, its cache directory as systemd makes it.
# Verbosity 4 adds llama.cpp's own loading lines, which say where layers went.
install -d -o llama -g llama /var/cache/llama
exec_start=$(sed -n 's/^ExecStart=//p' "$unit")
env_lines=$(sed -n 's/^Environment=//p' "$unit")
# shellcheck disable=SC2086 # the unit's command line, split as systemd does
runuser -u llama -- env $env_lines LLAMA_ARG_LOG_VERBOSITY=4 $exec_start >/tmp/llama.log 2>&1 &
for _ in $(seq 60); do curl -fsS http://127.0.0.1:8080/health >/dev/null 2>&1 && break; sleep 1; done
curl -fsS http://127.0.0.1:8080/health >/dev/null || { tail -30 /tmp/llama.log; fail "llama-server healthy"; }
grep -q 'llvmpipe' /tmp/llama.log || { grep -iE 'vulkan|device|offload' /tmp/llama.log | head -20; fail "the server uses the Vulkan device"; }
! grep -q 'shader cache (Permission denied)' /tmp/llama.log || fail "the shader cache is writable"
grep -Eq 'offloaded ([0-9]+)/\1 layers to GPU' /tmp/llama.log || { grep -i offload /tmp/llama.log; fail "every layer on the Vulkan device"; }
pass "llama-server runs every layer on the Vulkan device"

ask() { # key
  curl -sS -o /tmp/answer.json -w '%{http_code}' http://127.0.0.1:8080/v1/chat/completions \
    -H "Authorization: Bearer $1" -H 'Content-Type: application/json' \
    -d '{"model": "check-model", "messages": [{"role": "user", "content": "Once upon a time"}], "max_tokens": 8}'
}
[ "$(ask wrong-key)" = 401 ] || fail "a wrong key is refused"
[ "$(curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/v1/models)" = 401 ] || fail "no key is refused"
[ "$(ask "$key")" = 200 ] || { cat /tmp/answer.json; fail "the key gets an answer"; }
python3 -c 'import json,sys; c=json.load(open("/tmp/answer.json"))["choices"][0]["message"]["content"]; sys.exit(not c.strip())' \
  || { cat /tmp/answer.json; fail "the answer has text"; }
pass "only the gateway's key gets an answer"
