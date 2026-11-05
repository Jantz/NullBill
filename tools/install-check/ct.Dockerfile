# What Proxmox's Debian 12 container template gives the nullbill container,
# plus a fake systemctl (no init here) that starts the page as the unit does.
FROM debian:12-slim@sha256:99c0b25c46d0214d5a2091092e7908907dcaecd6a962cde0ac700acee222b6f3
COPY extra-ca.crt /tmp/extra-ca.crt
RUN apt-get update -qq \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends ca-certificates iproute2 procps >/dev/null \
 && if [ -s /tmp/extra-ca.crt ]; then cp /tmp/extra-ca.crt /usr/local/share/ca-certificates/extra-ca.crt && update-ca-certificates >/dev/null; fi \
 && rm -rf /var/lib/apt/lists/* \
 && git_safe='[safe]\n\tdirectory = *\n' && printf "$git_safe" > /etc/gitconfig
ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt PIP_CERT=/etc/ssl/certs/ca-certificates.crt
COPY fakes/systemctl /usr/local/bin/systemctl
RUN chmod 0755 /usr/local/bin/systemctl
CMD ["sleep", "infinity"]
