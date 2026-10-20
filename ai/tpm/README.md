# TPM-backed binary protection (HW2, requirement 4)

Encrypts the compiled AI binaries (`ai/dist/*.bin`) at rest with an AES-256 key
that is **sealed to the TPM**. The key exists in plaintext nowhere on disk; only
the TPM can release it, and only on the machine that sealed it. Copying the
encrypted binary and its sealed key to another machine is useless — that
machine's TPM cannot unseal the key.

## Why a software TPM here

The development VM (Multipass on macOS) exposes no `/dev/tpm*`, so this uses
**swtpm**, a software TPM 2.0, driven by the same `tpm2-tools` calls a real chip
uses. On a real secure edge device (Raspberry Pi / mini-PC with TPM 2.0) only
`start_tpm`/`stop_tpm` change — the seal/unseal logic is identical against
`/dev/tpm0`.

## Workflow

| Step | Script | What happens |
|---|---|---|
| Provision | `protect.sh <bin> <out>` | fresh AES-256 key → encrypt binary → **seal key to TPM**; plaintext key shredded |
| Launch | `run.sh <name> <dir> [args]` | **unseal key** → decrypt to a wiped-on-exit tmpdir → exec |
| Prove | `demo.sh` | seal on TPM-A, run on A (works), copy to TPM-B (unseal denied) |

```bash
bash ai/tpm/demo.sh          # full copy-protection demonstration
```

## How copy protection works

`tpm2_create` seals the key under a **primary key derived from the TPM's own
seed** (persistent at handle `0x81010001`). A different TPM has a different seed,
so its primary differs, and `tpm2_load`/`tpm2_unseal` of the foreign sealed blob
fails — the key is never released, so the ciphertext cannot be decrypted.

Verified: the 76 MB `main_ai_influx.bin` runs on its origin TPM and is **denied
on a second TPM**, with the protected files byte-identical in both attempts.

## Notes / hardening beyond this demo

- Binding to **PCR values** (`tpm2_create -L <pcr-policy>`) would additionally
  refuse to unseal if the boot/software state changed — defeating an attacker
  who clones the whole disk including TPM state.
- AES-256-CBC is used for simplicity; AES-256-GCM adds integrity. The security
  boundary here is the TPM-sealed key, not the cipher mode.
