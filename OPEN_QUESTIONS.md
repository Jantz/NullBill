# Open questions

Each question has a recommendation. Answer in chat or edit this file;
answered items move to the decisions list at the bottom.

1. **Test target.** What can NullBill test on: a nested PVE VM, or a spare box
   reset by snapshot? Recommendation: a nested PVE VM on a machine you name,
   with a clean snapshot to roll back to.
2. **Hardware class of the real server.** small (no GPU), mid (1 GPU) or
   large (several)? Which GPU and VRAM? Recommendation: tell me the class and
   GPU model only, no serials.
3. **Controller.** Which machine runs `nullbill`? Recommendation: your PC
   (Linux, or WSL on Windows); never the PVE host.
4. **NULLBILL_HOME.** Does the directory exist on the controller, in your
   backup set, with a `denylist.txt` of your names, hostnames, domains and
   serials? Recommendation: `~/nullbill-home`, included in backups, created
   before your first push from that machine.
5. **Tailscale Personal.** Its free plan is for personal, non-commercial use.
   Is this server personal and non-commercial? Recommendation: confirm, or I
   draft an ADR for Headscale instead.
6. **CI.** GitHub documents standard runners as free for public repositories,
   so this repo's checks may not draw on your exhausted minutes.
   Recommendation: keep `.github/workflows/check.yml` and see whether it runs
   on the first PR; `tools/check` runs the same checks locally either way.
   Cloudflare runners only if it does not.
7. **Guest floors.** The sizes in `catalog.yaml` are starting values I chose,
   not measurements. Recommendation: accept them for M1; replace them with
   measured values once a test target exists.

## Defaults picked for cosmetic choices

- Zone subnets: `10.42.<index>.0/24`, pinnable through `network.base`.
- Zone bridges: `vmbr<100 + index>`.
- Inference engine: llama.cpp for one GPU, vLLM for several; pinnable.
- Plan output: YAML with a comment above each decision.

## Decisions

- Licence: Apache-2.0 (owner, 2026-11-04).
- Commits: owner's GitHub noreply author, no Claude attribution (owner, 2026-11-04).
