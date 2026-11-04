# OpenTofu, to validate the rendered configuration against the provider's
# schema without a PVE API.
FROM debian:12-slim@sha256:99c0b25c46d0214d5a2091092e7908907dcaecd6a962cde0ac700acee222b6f3
# extra-ca.crt is empty unless the build runs behind a TLS-intercepting proxy.
COPY extra-ca.crt /tmp/extra-ca.crt
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl python3 \
 && cat /tmp/extra-ca.crt >> /etc/ssl/certs/ca-certificates.crt \
 && curl -fsSLo /tmp/tofu.tar.gz https://github.com/opentofu/opentofu/releases/download/v1.13.2/tofu_1.13.2_linux_amd64.tar.gz \
 && echo "ac05d9952662c7db28dd57e93f0a68018837bfdf9342384ec0987977c16a9645  /tmp/tofu.tar.gz" | sha256sum -c - \
 && tar -xzf /tmp/tofu.tar.gz -C /usr/local/bin tofu \
 && rm -rf /tmp/tofu.tar.gz /var/lib/apt/lists/*
COPY check.sh report.py /usr/local/bin/
ENTRYPOINT ["bash", "/usr/local/bin/check.sh"]
