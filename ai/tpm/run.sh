#!/usr/bin/env bash
# run.sh <name> <protected-dir> [args...]
#
# Launch (HW2 req 4, per the slide notes):
#   5. TPM RSA-decrypts pseudo_aes.enc  (fails on any TPM but the sealing one)
#   6. de-obfuscate -> AES key
#   7. AES-decrypt the software, run it from a wiped-on-exit dir
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; source "$HERE/tpm_lib.sh"
NAME="$1"; DIR="$2"; shift 2 || true

PSEUDO="$(mktemp)"; AES="$(mktemp)"; RUNDIR="$(mktemp -d)"
trap 'rm -rf "$RUNDIR"; shred -u "$PSEUDO" "$AES" 2>/dev/null || rm -f "$PSEUDO" "$AES"' EXIT

tpm2_rsadecrypt -c "$RSA_KEY_HANDLE" -s oaep \
    "$DIR/pseudo_aes.enc" -o "$PSEUDO"                     # step 5 (TPM-gated)
OBF="$(obf_key_of "$DIR/$NAME.enc")"                       # step 6: recompute feature
openssl enc -d -aes-256-cbc -pbkdf2 -in "$PSEUDO" -out "$AES" -pass "pass:$OBF"
openssl enc -d -aes-256-cbc -pbkdf2 -in "$DIR/$NAME.enc" \
    -out "$RUNDIR/$NAME" -pass "file:$AES"                 # step 7
chmod +x "$RUNDIR/$NAME"
"$RUNDIR/$NAME" "$@"
