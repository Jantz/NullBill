{{- /* The agent needs SSH access only; a TLS certificate for a service name
     from this provisioner would let it impersonate that service. */ -}}
{{ fail "the agent provisioner does not sign X.509 certificates" }}
