#!/usr/bin/env bash
# demo.sh -- proof the notes' TPM scheme prevents copy/cloning (HW2 req 4).
#   TPM-A seals an RSA private key, encrypts the binary, RSA-encrypts the
#   obfuscated AES key.  * On TPM-A: decrypt+run works.  * Copied to a machine
#   with a DIFFERENT TPM: tpm2_rsadecrypt fails, so nothing decrypts.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; source "$HERE/tpm_lib.sh"
BIN="${1:-$HERE/../dist/main_ai_influx.bin}"
ROOT=/tmp/tpm_demo; rm -rf "$ROOT"; mkdir -p "$ROOT"; pkill -f swtpm 2>/dev/null; sleep 1

echo "=============================================================="
echo " Protecting $(basename "$BIN") with the TPM RSA+obfuscation scheme"
echo "=============================================================="
start_tpm "$ROOT/tpmA" 2320 2321
echo; echo "[TPM-A] provision (RSA key in TPM, encrypt, obfuscate, RSA-wrap)"
bash "$HERE/protect.sh" "$BIN" "$ROOT/protected" | sed 's/^/   /'
echo; echo "[TPM-A] decrypt + run   (expect: runs)"
if bash "$HERE/run.sh" "$(basename "$BIN")" "$ROOT/protected" --help >/dev/null 2>&1; then
  echo "   RESULT: ran on the origin TPM  OK"; A_OK=1
else echo "   RESULT: failed on origin TPM (unexpected)"; A_OK=0; fi
stop_tpm 2321; sleep 1

echo; echo "[copy] protected files copied to a second machine..."
cp -r "$ROOT/protected" "$ROOT/stolen"
start_tpm "$ROOT/tpmB" 2330 2331         # a DIFFERENT TPM (fresh seed)
ensure_rsa_key "$ROOT/stolen/rsaB.pem"   # B has its own RSA key at the same handle
echo "[TPM-B] attempt decrypt + run   (expect: DENIED at RSA-decrypt)"
if bash "$HERE/run.sh" "$(basename "$BIN")" "$ROOT/stolen" --help >/dev/null 2>&1; then
  echo "   RESULT: ran on a foreign TPM (copy NOT prevented)"; B_DENIED=0
else echo "   RESULT: RSA-decrypt denied on foreign TPM  OK (copy prevented)"; B_DENIED=1; fi
stop_tpm 2331; pkill -f swtpm 2>/dev/null

echo; echo "=============================================================="
if [ "${A_OK:-0}" = 1 ] && [ "${B_DENIED:-0}" = 1 ]; then
  echo " PASS: runs on its origin TPM; a copy is useless on a different TPM."
else echo " INCOMPLETE: A_OK=${A_OK:-?} B_DENIED=${B_DENIED:-?}"; fi
echo "=============================================================="
