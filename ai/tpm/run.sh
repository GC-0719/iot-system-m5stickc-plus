#!/usr/bin/env bash
# run.sh <name> <protected-dir> [args...]
#
# Launch step (HW2, req 4): UNSEAL the key from the TPM, decrypt the binary into
# a private tmpfs that is wiped on exit, execute it, then clean up. The plaintext
# binary never touches persistent storage.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; source "$HERE/tpm_lib.sh"

NAME="$1"; DIR="$2"; shift 2 || true
ENC="$DIR/$NAME.enc"

# 1) UNSEAL the DEK (fails on any TPM but the one that sealed it)
CTX="$(mktemp)"; DEK="$(mktemp)"
trap 'shred -u "$DEK" "$CTX" 2>/dev/null || rm -f "$DEK" "$CTX"' EXIT
tpm2_load -C "$PRIMARY_HANDLE" -u "$DIR/dek.seal.pub" -r "$DIR/dek.seal.priv" -c "$CTX" >/dev/null
tpm2_unseal -c "$CTX" -o "$DEK" >/dev/null
tpm2_flushcontext -t >/dev/null 2>&1 || true

# 2) decrypt into a wiped-on-exit workspace and run
RUNDIR="$(mktemp -d)"; trap 'rm -rf "$RUNDIR"; shred -u "$DEK" "$CTX" 2>/dev/null || rm -f "$DEK" "$CTX"' EXIT
openssl enc -d -aes-256-cbc -pbkdf2 -in "$ENC" -out "$RUNDIR/$NAME" -pass "file:$DEK"
chmod +x "$RUNDIR/$NAME"
"$RUNDIR/$NAME" "$@"
