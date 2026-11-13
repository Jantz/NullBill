# What Proxmox's Debian 13 container template gives the nullbill container,
# plus a fake systemctl (no init here) that starts the page as the unit does.
FROM debian:13-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2
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
