# Local AI models

The workload **Local AI models on the GPU** runs one open model on your
NVIDIA graphics card and puts the model gateway in front of it: an
OpenAI-compatible API that your coding agents and your own programs use
with one key. Why it is built this way: [ADR 0008](../adr/0008-local-models.md).

| What | Where |
|---|---|
| Model gateway (LiteLLM) | `https://model-gateway.nullbill.internal:4000/v1` from the workspace, `https://10.42.4.21:4000/v1` from your devices (guest 2021) |
| Model server (llama.cpp) | guest 2020, `10.42.4.20` port 8080, reached only by the gateway |
| Model | the page's **Services** page, **Local AI model** card; for a 12 GB card, `bonsai-2-27b` |
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
   minutes) and downloads the model (about 8 GB).
4. If setup stops and says the old nouveau driver holds the card: in the
   Proxmox web UI select the node, press **Reboot**, wait until the page
   answers again, and press **Run setup** again. This happens once.

When setup has finished, **Checks** shows four checks for it: the host
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

The model thinks before it answers, and the server already uses the
settings its makers advise (temperature, top-k, penalties). In a chat
program such as Page Assist, leave temperature and the other sampling
settings at their defaults (unset), and do not cap the answer's length
low: the thinking counts against it, so a cap of a few hundred tokens ends
the reply before the answer. Bonsai thinks at medium effort unless a
request sends `"reasoning_effort"`: `"xhigh"` thinks longer, `"none"` not
at all.

From a device on your tailnet, use the address instead of the name:
`https://10.42.4.21:4000/v1`. There the gateway answers with a certificate
from NullBill's browser CA, so once the device trusts that root (the page's
**Access**, **Trust this server**, [trust-this-server.md](trust-this-server.md))
browsers and browser extensions such as Page Assist accept it, with no hosts
line and no exception to renew. The name still gets step-ca's certificate,
which renews daily, so a browser exception for it lasts a day at most.

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
