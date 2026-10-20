#!/usr/bin/env bash
# protect.sh <plaintext-binary> <out-dir>
#
# Provisioning step (HW2, req 4): encrypt a binary with a fresh AES-256 key and
# SEAL that key to the TPM. Afterwards the plaintext key exists nowhere on disk
# -- only the TPM can release it, and only on this machine. Copying the outputs
# to another machine yields files its TPM cannot unseal (see demo.sh).
#
# Env: TPM2TOOLS_TCTI must point at a running TPM (see tpm_lib.sh / demo.sh).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; source "$HERE/tpm_lib.sh"

BIN="$1"; OUT="${2:-$HERE/protected}"
mkdir -p "$OUT"
ensure_primary

# 1) fresh 256-bit data-encryption key, held only in a temp file
DEK="$(mktemp)"; trap 'shred -u "$DEK" 2>/dev/null || rm -f "$DEK"' EXIT
openssl rand -out "$DEK" 32

# 2) encrypt the binary with it (AES-256-CBC, key derived from the DEK)
openssl enc -aes-256-cbc -pbkdf2 -salt -in "$BIN" \
    -out "$OUT/$(basename "$BIN").enc" -pass "file:$DEK"

# 3) SEAL the DEK to the TPM -- the sealed blob is useless off this TPM
tpm2_create -C "$PRIMARY_HANDLE" -i "$DEK" \
    -u "$OUT/dek.seal.pub" -r "$OUT/dek.seal.priv" >/dev/null
tpm2_flushcontext -t >/dev/null 2>&1 || true

echo "protected $(basename "$BIN"):"
echo "  $OUT/$(basename "$BIN").enc   (ciphertext)"
echo "  $OUT/dek.seal.pub / dek.seal.priv   (TPM-sealed key; no plaintext key on disk)"
