{{- /* Every certificate from the agent provisioner is a user certificate
     for the principal "agent", whatever the request asked for. Guests map
     that principal to the unprivileged agent user only. */ -}}
{{- if ne .Type "user" }}{{ fail "the agent provisioner signs SSH user certificates only" }}{{ end -}}
{
  "type": "user",
  "keyId": "agent",
  "principals": ["agent"],
  "extensions": {"permit-pty": ""}
}
