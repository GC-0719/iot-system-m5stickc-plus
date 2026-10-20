# TPM-backed binary protection (HW2, requirement 4)

Encrypts the compiled AI binaries (`ai/dist/*.bin`) so they only run on the machine that protected
them. Implements the scheme from the HW2 slide notes.

## Why a software TPM here

The development VM (Multipass on macOS) exposes no `/dev/tpm*`, so this uses **swtpm**, a software
TPM 2.0, driven by the same `tpm2-tools` calls a real chip uses. On a real secure edge device
(Raspberry Pi / mini-PC with TPM 2.0) only `start_tpm`/`stop_tpm` change — the key operations run
against `/dev/tpm0` unchanged.

## The scheme (exactly as the notes specify)

**Provision — `protect.sh <binary> <out>`**

1. Generate an **RSA key pair in the TPM**; the private half is sealed inside it. Export the public
   key (`rsa.pem`).
2. Generate an **AES key**, encrypt the binary with it (`<name>.enc`).
3. **Obfuscate** the AES key: derive a key from a *feature of the encrypted file* (its SHA-256) and
   encrypt the AES key with it → the "pseudo AES key". This binds the key to that exact ciphertext.
4. **RSA-encrypt** the pseudo AES key with `rsa.pem` (`pseudo_aes.enc`).

What remains on disk: `<name>.enc`, `pseudo_aes.enc`, `rsa.pem`. The plaintext AES key and pseudo key
are shredded; the RSA private key never leaves the TPM.

**Launch — `run.sh <name> <dir> [args]`**

5. **RSA-decrypt** the pseudo AES key using the TPM (`tpm2_rsadecrypt`) — the step that only the
   sealing TPM can perform.
6. **De-obfuscate**: recompute the ciphertext's SHA-256 and decrypt the pseudo key → the AES key.
7. **AES-decrypt** the binary into a wiped-on-exit directory and run it.

```bash
bash ai/tpm/demo.sh          # full copy-protection demonstration
```

## How copy protection works

The RSA private key is sealed to the TPM's seed. A different machine's TPM has a different seed, so
`tpm2_rsadecrypt` of the foreign `pseudo_aes.enc` fails — the AES key is never recovered, so the
ciphertext cannot be decrypted. The obfuscation layer additionally ties the AES key to that specific
encrypted file.

Verified: `main_ai_influx.bin` runs on its origin TPM and is **denied on a second TPM** at the
RSA-decrypt step.

## Hardening beyond this demo

- Bind the RSA/seal to **PCR values** so a whole-disk clone (including TPM state) still fails to
  unseal after any boot/software change.
- AES-256-CBC is used for simplicity; AES-256-GCM adds integrity. The security boundary here is the
  TPM-sealed RSA key, not the cipher mode.
