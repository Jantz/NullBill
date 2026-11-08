# Local AI models

The workload **Local AI models on the GPU** runs one open model on your
NVIDIA graphics card and puts the model gateway in front of it: an
OpenAI-compatible API that your coding agents and your own programs use
with one key. Why it is built this way: [ADR 0008](../adr/0008-local-models.md).

| What | Where |
|---|---|
| Model gateway (LiteLLM) | `https://model-gateway.nullbill.internal:4000/v1` (guest 2021, `10.42.4.21`) |
| Model server (llama.cpp) | guest 2020, `10.42.4.20` port 8080, reached only by the gateway |
| Model | the page's **Services** page, **Local AI model** card; for a 12 GB card, `qwen3.5-9b` |
| Key | the same card, **Show key** (or **Copy**) |

## 1. Turn it on

**You need:** an NVIDIA card in the server, the setup page.

1. In the BIOS, turn **Secure Boot** off if it is on. The card's driver is
   built on the server, and Secure Boot refuses a driver it did not sign.
2. On the **Setup** page, in **What to run**, tick **Local AI models on
   the GPU**, give the card's memory in GB if it asks (12 for an RTX 4070),
   press **Save and plan**, and check that the **Plan** card shows no
   problems.
3. Press **Run setup** at the top of the page. The stage **Set up the
   local AI model and its gateway** takes a while the first time: it
   installs the card's driver on Proxmox, builds llama.cpp (about 15
   minutes) and downloads the model (about 5 GB).
4. If setup stops and says the old nouveau driver holds the card: in the
   Proxmox web UI select the node, press **Reboot**, wait until the page
   answers again, and press **Run setup** again. This happens once.

When setup has finished, **Run checks** shows four checks for it: the host
runs NVIDIA's driver, the model sits in the card's memory, the model
answers through the gateway, and a wrong key is refused.

## 2. Use it

**You need:** the key from the page's **Services** page.

From the workspace (your coding agents), it works as it is:

```sh
curl https://model-gateway.nullbill.internal:4000/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"model": "local", "messages": [{"role": "user", "content": "Hello"}]}'
```

Any OpenAI-compatible client works the same way: base URL
`https://model-gateway.nullbill.internal:4000/v1`, the key as API key, and
`local` (or the model's own name) as the model.

From a device on your tailnet, two things first:

1. Add the line `10.42.4.21 model-gateway.nullbill.internal` to the hosts
   file (`/etc/hosts`, or `C:\Windows\System32\drivers\etc\hosts` edited as
   administrator).
2. The gateway's certificate comes from NullBill's own certificate
   authority. Do not add it to your device's trusted certificates (see
   [everyday-use.md](everyday-use.md)); give it to the one program instead.
   Copy the root from the Proxmox Shell, `pct exec 2012 -- cat
   /etc/step-ca/certs/root_ca.crt`, into a file such as `nullbill-root.crt`,
   then use `curl --cacert nullbill-root.crt ...`, or for Python clients
   `SSL_CERT_FILE=nullbill-root.crt` in that program's environment.

## 3. Choose another model

The models NullBill offers are in `catalog.yaml` under `models`, each with
the card memory it needs; setup picks the largest that fits. To run a
smaller one, add to `config.yaml` in the NullBill container
(`pct enter 2002`, `/var/lib/nullbill/config.yaml`):

```yaml
inference:
  model: qwen3.5-4b
```

then press **Run setup** on the page. A model of your own can be added
under `models` in `config.yaml` in the same form, with the SHA-256 Hugging
Face shows for the file.

## When something is wrong

- **The card is gone after a Proxmox update.** A new kernel needs the
  driver built for it; DKMS does that when the kernel is installed. If the
  checks say the driver is missing, press **Run setup**.
- **Answers are slow.** Check that the model sits in the card's memory
  (the check above). In the Proxmox Shell, `nvidia-smi` shows how much of
  the card's memory is used. `pct exec 2020 -- journalctl -u llama-server`
  shows the model server's log.
