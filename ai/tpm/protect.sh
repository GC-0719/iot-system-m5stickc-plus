#!/usr/bin/env bash
# protect.sh <plaintext-binary> <out-dir>
#
# Provisioning (HW2 req 4, per the slide notes):
#   1. RSA key pair generated in the TPM (private part sealed inside it); rsa.pem
#   2. fresh AES key encrypts the software
#   3. AES key obfuscated with a key derived from the encrypted file -> pseudo key
#   4. pseudo key RSA-encrypted with rsa.pem
# Only software.enc + pseudo_aes.enc + rsa.pem remain; plaintext keys shredded.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; source "$HERE/tpm_lib.sh"
BIN="$1"; OUT="${2:-$HERE/protected}"; NAME="$(basename "$BIN")"
mkdir -p "$OUT"
ensure_rsa_key "$OUT/rsa.pem"                              # step 1

AES="$(mktemp)"; PSEUDO="$(mktemp)"
trap 'shred -u "$AES" "$PSEUDO" 2>/dev/null || rm -f "$AES" "$PSEUDO"' EXIT
openssl rand -out "$AES" 32                                # step 2a
openssl enc -aes-256-cbc -pbkdf2 -salt -in "$BIN" \
    -out "$OUT/$NAME.enc" -pass "file:$AES"                # step 2b

OBF="$(obf_key_of "$OUT/$NAME.enc")"                       # step 3: feature of ciphertext
openssl enc -aes-256-cbc -pbkdf2 -salt -in "$AES" \
    -out "$PSEUDO" -pass "pass:$OBF"                       #   -> pseudo AES key
openssl pkeyutl -encrypt -pubin -inkey "$OUT/rsa.pem" \
    -pkeyopt rsa_padding_mode:oaep -pkeyopt rsa_oaep_md:sha256 \
    -in "$PSEUDO" -out "$OUT/pseudo_aes.enc"               # step 4

echo "protected $NAME:"
echo "  $OUT/$NAME.enc          (software, AES-encrypted)"
echo "  $OUT/pseudo_aes.enc     (obfuscated AES key, RSA-encrypted to the TPM)"
echo "  $OUT/rsa.pem            (public key; private part sealed in the TPM)"
