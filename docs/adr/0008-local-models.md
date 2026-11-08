# ADR 0008: Local models on a host-shared NVIDIA card, behind LiteLLM

Status: accepted (decided by Claude under the owner's standing "pick, be
critical and decide", 2026-11-08)

## Context

The `inference` workload made two empty containers: `inference`, which
owns a VRAM budget on a host-shared GPU, and `model-gateway`, whose port
4000 the build zone and the owner may reach. verify reported both gateway
flows DEGRADED. The owner's server has 16 GiB of memory and one RTX 4070
with 12 GiB; local models are an option for him, not the goal.

What was checked before deciding (2026-11-08):

- llama.cpp `b12205` (commit `a691b23`) builds its server with the Vulkan
  backend on Debian 13. On Debian 12 it does not: Debian's Vulkan headers
  (1.3.239) lack `vk::LayerSettingEXT`. Against Khronos' Vulkan-Headers
  `v1.4.309` (Debian 13's version) it builds on Debian 12 and serves a
  model on Mesa's software Vulkan device (lavapipe), once that device is
  listed in `GGML_VK_VISIBLE_DEVICES` (llama.cpp skips CPU devices
  otherwise, `ggml-vulkan.cpp`).
- One file of that backend took 1.8 GiB (maximum resident set, `/usr/bin/time
  -v`) to compile; a one-job build failed in a 1.5 GiB container and passed
  in a 4 GiB one (15 minutes on two cores).
- llama.cpp's Vulkan backend implements the operations Qwen3.5's hybrid
  layers need (`GGML_OP_GATED_DELTA_NET`, `SSM_CONV` in `ggml-vulkan.cpp`).
- NVIDIA's `NVIDIA-Linux-x86_64-595.104.02.run` (the release `latest.txt`
  named; its SHA-256 matches NVIDIA's `.run.sha256sum`) installs its user
  space in a Debian 12 container with `--no-kernel-modules`, including its
  Vulkan driver (`/etc/vulkan/icd.d/nvidia_icd.json`), in about ten seconds.
  It needs `kmod`'s `modprobe` present even then.
- LiteLLM 1.108.3's proxy with its 117 dependencies installs on Debian 12's
  Python 3.11 from wheels alone with `--require-hashes`, starts in seven
  seconds and holds 340 MB. Without a database it refuses requests without
  the master key (with a 500 rather than a 401, from a missing `prisma`
  import in its error path).

Not checked, because no GPU is reachable from here: the host driver build
under PVE 9's kernel, device passthrough into the container, and speed on
the card. verify checks the first two on the real host.

## Decision

**Driver on the host, user space in the container.** The host installs
NVIDIA's driver from its pinned `.run` with DKMS and the open kernel
modules (`ansible/roles/nvidia_host`), blacklists nouveau, and makes the
device nodes at boot before PVE starts guests. apply gives the inference
container `/dev/nvidia<minor>`, `nvidiactl` and `nvidia-uvm(-tools)` as
`devN` entries (group video) as root over SSH, since PVE allows device
passthrough to `root@pam` alone; OpenTofu ignores them. The container
installs the same release's user space. The release is pinned in one
place, the host role's defaults, so the two cannot drift.

**llama.cpp with Vulkan, not CUDA.** CUDA would need NVIDIA's toolkit (gigabytes)
in the container to build, or a prebuilt binary for a different libc.
Vulkan needs a header package and a shader compiler, and the same build
runs in the container check on a software device, so CI exercises the real
code path. The cost is speed: on NVIDIA, llama.cpp's CUDA backend is
usually faster at reading long prompts. A CUDA build can come later behind
the same unit if the owner finds it slow.

**Built in the guest from a pinned source.** The tag must still be the
pinned commit, or the role stops. apply lifts the container's memory limit
to 4 GiB while the role runs and puts it back after; the next tofu run
would reset it in any case.

**Models from an allow-list by VRAM.** `catalog.yaml`'s `models` lists GGUF
files pinned to a Hugging Face commit and by SHA-256, each with the VRAM it
is given. plan picks the largest that fits the inference budget;
`inference.model` pins another. For 12 GiB that is Qwen3.5-9B at Q4_K_M
(Apache-2.0).

**One engine.** vLLM is not built. Rather than plan an engine apply cannot
install, the engine table names llama.cpp for any number of cards; it splits
a model across them by layer.

**LiteLLM as the gateway.** One OpenAI-compatible API at
`https://model-gateway.<domain>:4000/v1`, behind nginx with a step-ca
certificate, one key (kept in sops, shown on the page). The model is
offered under its own name and as `local`, so clients need not change when
the model does. LiteLLM is installed from a hash-pinned lock: in March 2026
two of its PyPI releases (1.82.7 and 1.82.8) carried a credential stealer
(https://docs.litellm.ai/blog/security-update-march-2026). With every
version and file hash fixed, an apply never picks up a newly published
release or a swapped file; a new version is a reviewed change to the lock. The gateway passes a second key
to llama.cpp, which serves only the gateway's address (the firewall) and
only with that key.

## Consequences

- The first apply with local models takes longer: the host driver build,
  a llama.cpp build, and a 5 GiB model download through the proxy.
- If nouveau holds the card, apply stops once and asks for a restart.
  Secure Boot refuses the unsigned module; the runbook says to turn it off.
- A Proxmox kernel upgrade rebuilds the module through DKMS. If the card is
  gone after one, running setup again repairs it.
- Paid cloud models behind the same gateway, as a fallback, are a later
  step: they need the owner's API key and proxy rules for the gateway.
