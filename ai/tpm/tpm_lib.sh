#!/usr/bin/env bash
# Shared helpers for TPM-backed binary protection (HW2, requirement 4).
#
# Uses a software TPM 2.0 (swtpm) so the exact seal/unseal workflow of a real
# hardware TPM is demonstrated on a VM that has no physical TPM. On a real edge
# device (e.g. a Raspberry Pi / mini-PC with TPM 2.0) the same tpm2-tools calls
# run unchanged against /dev/tpm0 -- only start_tpm/stop_tpm are swtpm-specific.
#
# The persistent-primary pattern (evict to a handle, flush transients) avoids
# the TPM's small transient-object memory, which otherwise fails tpm2_load.

PRIMARY_HANDLE="0x81010001"

start_tpm() {   # start_tpm <state_dir> <server_port> <ctrl_port>
  local dir="$1" sport="$2" cport="$3"
  mkdir -p "$dir"
  swtpm socket --tpm2 --tpmstate "dir=$dir" \
    --ctrl "type=tcp,port=$cport" --server "type=tcp,port=$sport" \
    --flags not-need-init,startup-clear --daemon
  sleep 1
  export TPM2TOOLS_TCTI="swtpm:host=localhost,port=$sport"
}

stop_tpm() {    # stop_tpm <ctrl_port>
  swtpm_ioctl --tcp ":$1" -s 2>/dev/null || true
}

# Ensure a persistent primary exists at PRIMARY_HANDLE in the current TPM.
ensure_primary() {
  if ! tpm2_readpublic -c "$PRIMARY_HANDLE" >/dev/null 2>&1; then
    local tmp; tmp="$(mktemp -d)"
    tpm2_createprimary -C o -g sha256 -G rsa -c "$tmp/primary.ctx" >/dev/null
    tpm2_evictcontrol -C o -c "$tmp/primary.ctx" "$PRIMARY_HANDLE" >/dev/null
    tpm2_flushcontext -t >/dev/null 2>&1 || true
    rm -rf "$tmp"
  fi
}
