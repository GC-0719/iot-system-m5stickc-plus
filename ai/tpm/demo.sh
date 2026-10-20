#!/usr/bin/env bash
# demo.sh -- end-to-end proof that TPM sealing prevents copy/cloning (HW2 req 4).
#
#   TPM-A seals the key and encrypts the binary.
#   * On TPM-A: unseal -> decrypt -> run  == SUCCESS
#   * Copied to a machine with a DIFFERENT TPM (TPM-B): unseal FAILS, so the
#     binary cannot be decrypted or run == COPY DEFEATED
#
# Run on the VM: bash ai/tpm/demo.sh
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; source "$HERE/tpm_lib.sh"

BIN="${1:-$HERE/../dist/main_ai_influx.bin}"
ROOT=/tmp/tpm_demo; rm -rf "$ROOT"; mkdir -p "$ROOT"
pkill -f swtpm 2>/dev/null; sleep 1

echo "=============================================================="
echo " Protecting: $(basename "$BIN")  ($(du -h "$BIN" | cut -f1))"
echo "=============================================================="

# ---- origin machine: TPM-A ----
start_tpm "$ROOT/tpmA" 2321 2322
echo; echo "[TPM-A] seal key + encrypt binary"
bash "$HERE/protect.sh" "$BIN" "$ROOT/protected" | sed 's/^/   /'

echo; echo "[TPM-A] unseal -> decrypt -> run  (expect: runs)"
if bash "$HERE/run.sh" "$(basename "$BIN")" "$ROOT/protected" --help >/dev/null 2>&1; then
  echo "   RESULT: ran successfully on the origin TPM  ✔"; A_OK=1
else
  echo "   RESULT: failed on origin TPM  �‼ (unexpected)"; A_OK=0
fi
stop_tpm 2322; sleep 1

# ---- attacker copies the protected files to another machine (TPM-B) ----
echo; echo "[copy] protected files copied to a second machine..."
cp -r "$ROOT/protected" "$ROOT/stolen"

start_tpm "$ROOT/tpmB" 2331 2332     # a DIFFERENT TPM (fresh seed)
ensure_primary                        # B makes its own primary at the same handle
echo "[TPM-B] attempt unseal -> decrypt -> run  (expect: DENIED)"
if bash "$HERE/run.sh" "$(basename "$BIN")" "$ROOT/stolen" --help >/dev/null 2>&1; then
  echo "   RESULT: ran on a foreign TPM  ✘ (copy NOT prevented)"; B_DENIED=0
else
  echo "   RESULT: unseal/decrypt denied on foreign TPM  ✔ (copy prevented)"; B_DENIED=1
fi
stop_tpm 2332; pkill -f swtpm 2>/dev/null

echo; echo "=============================================================="
if [ "${A_OK:-0}" = 1 ] && [ "${B_DENIED:-0}" = 1 ]; then
  echo " PASS: binary runs on its origin TPM, and a copy is useless elsewhere."
else
  echo " INCOMPLETE: A_OK=$A_OK B_DENIED=$B_DENIED"
fi
echo "=============================================================="
