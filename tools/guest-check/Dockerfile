# The tools that read the router and egress-proxy files, at the versions
# Debian 12 ships, to check rendered files without the guests.
FROM debian:12-slim@sha256:99c0b25c46d0214d5a2091092e7908907dcaecd6a962cde0ac700acee222b6f3
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends nftables dnsmasq-base squid ifupdown \
 && rm -rf /var/lib/apt/lists/*
COPY check.sh /usr/local/bin/check.sh
ENTRYPOINT ["bash", "/usr/local/bin/check.sh"]
