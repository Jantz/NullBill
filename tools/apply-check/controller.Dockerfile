# The controller for the apply check, set up the way install.sh sets up the
# nullbill container: tools/setup-controller on Debian 12, from this tree.
FROM debian:12-slim@sha256:99c0b25c46d0214d5a2091092e7908907dcaecd6a962cde0ac700acee222b6f3
# extra-ca.crt is empty unless the build runs behind a TLS-intercepting proxy.
COPY extra-ca.crt /tmp/extra-ca.crt
RUN apt-get update -qq \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends ca-certificates >/dev/null \
 && if [ -s /tmp/extra-ca.crt ]; then cp /tmp/extra-ca.crt /usr/local/share/ca-certificates/extra-ca.crt && update-ca-certificates >/dev/null; fi
ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt PIP_CERT=/etc/ssl/certs/ca-certificates.crt
COPY tree/ /opt/nullbill/
RUN /opt/nullbill/tools/setup-controller
ENV PATH=/opt/nullbill/venv/bin:$PATH
