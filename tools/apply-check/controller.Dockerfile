# The controller for the apply check: the pinned sops and age, Python with
# requirements-apply.txt and the pinned community.proxmox collection.
FROM golang:1.25-bookworm@sha256:3b4a11519ad929d1e1d261a12cff056f0c85b735253d7d861346b9c6f8b36437 AS tools
# extra-ca.crt is empty unless the build runs behind a TLS-intercepting proxy.
COPY extra-ca.crt /usr/local/share/ca-certificates/extra-ca.crt
RUN update-ca-certificates >/dev/null \
 && go install github.com/getsops/sops/v3/cmd/sops@v3.13.3 \
 && go install filippo.io/age/cmd/age@v1.3.2 filippo.io/age/cmd/age-keygen@v1.3.2

FROM debian:12-slim@sha256:99c0b25c46d0214d5a2091092e7908907dcaecd6a962cde0ac700acee222b6f3
COPY extra-ca.crt /tmp/extra-ca.crt
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      python3 python3-venv openssh-client curl ca-certificates git \
 && cat /tmp/extra-ca.crt >> /etc/ssl/certs/ca-certificates.crt \
 && rm -rf /var/lib/apt/lists/*
COPY --from=tools /go/bin/sops /go/bin/age /go/bin/age-keygen /usr/local/bin/
ENV PATH=/opt/venv/bin:$PATH REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt PIP_CERT=/etc/ssl/certs/ca-certificates.crt
COPY requirements-apply.txt requirements-dev.txt install-apply-deps /opt/deps/
RUN python3 -m venv /opt/venv \
 && pip install -q -r /opt/deps/requirements-dev.txt \
 && cd /opt/deps && COLLECTIONS_DIR=/opt/collections bash install-apply-deps
