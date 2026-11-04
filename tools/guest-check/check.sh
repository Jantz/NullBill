#!/usr/bin/env bash
# Check /build/<class>/{router,egress-proxy} for each class given.
set -euo pipefail

dpkg-query -W -f '${Package} ${Version}\n' nftables dnsmasq-base squid ifupdown
fail=0
run() {
  local label=$1
  shift
  local log
  if log=$("$@" 2>&1); then
    # squid -k parse reports problems as WARNING/ERROR lines but exits 0.
    if grep -E 'WARNING|ERROR|FATAL' <<<"$log"; then
      echo "FAIL $label"
      fail=1
    else
      echo "ok   $label"
    fi
  else
    printf '%s\n' "$log"
    echo "FAIL $label"
    fail=1
  fi
}

for cls in "$@"; do
  r=/build/$cls/router
  run "$cls nftables.conf" nft -c -f "$r/etc/nftables.conf"
  for conf in "$r"/etc/nullbill/dnsmasq-*.conf; do
    run "$cls $(basename "$conf")" dnsmasq --test --conf-file="$conf"
  done
  run "$cls interfaces" ifquery --interfaces="$r/etc/network/interfaces" --list --all
  run "$cls squid.conf" squid -k parse -f "/build/$cls/egress-proxy/etc/squid/squid.conf"
done
exit "$fail"
