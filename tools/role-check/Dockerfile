# A Debian container standing in for a service guest, with Ansible to
# apply a role to itself. Each role's tests/check.sh runs here, on the
# release its guest runs (tools/check-roles passes BASE). Ansible is the
# one apply runs (requirements-apply.txt), not the release's own: a role
# passed here under Debian 12's 2.14 and failed on the server under 2.19.
ARG BASE=debian:12-slim@sha256:99c0b25c46d0214d5a2091092e7908907dcaecd6a962cde0ac700acee222b6f3
FROM ${BASE}
# extra-ca.crt is empty unless the build runs behind a TLS-intercepting proxy.
COPY extra-ca.crt /tmp/extra-ca.crt
# No init in the container: package scripts must not try to start services.
RUN printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d && chmod +x /usr/sbin/policy-rc.d \
 && apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      python3-venv python3-apt python3-yaml openssh-client ca-certificates curl openssl gnupg systemd e2fsprogs \
 && cat /tmp/extra-ca.crt >> /etc/ssl/certs/ca-certificates.crt \
 # Also where update-ca-certificates keeps it, for roles that rebuild the store.
 && if [ -s /tmp/extra-ca.crt ]; then cp /tmp/extra-ca.crt /usr/local/share/ca-certificates/extra-ca.crt; fi \
 && rm -rf /var/lib/apt/lists/* \
 # Empty lists with a fresh update stamp, the state that hid two missing
 # cache refreshes from local runs: roles must not trust cache_valid_time.
 && mkdir -p /var/lib/apt/periodic && touch /var/lib/apt/periodic/update-success-stamp
COPY requirements-apply.txt /tmp/requirements-apply.txt
RUN python3 -m venv /opt/ansible \
 && PIP_CERT=/etc/ssl/certs/ca-certificates.crt /opt/ansible/bin/pip install -q --no-cache-dir --disable-pip-version-check \
      -r /tmp/requirements-apply.txt \
 && ln -s /opt/ansible/bin/ansible /opt/ansible/bin/ansible-playbook /usr/local/bin/
# Modules run under the guest's own Python, as apply's inventory has it.
ENV ANSIBLE_PYTHON_INTERPRETER=/usr/bin/python3
COPY role-check.sh /usr/local/lib/role-check.sh
