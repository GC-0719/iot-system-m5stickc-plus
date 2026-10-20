#!/usr/bin/env bash
# Shared helpers for TPM-backed binary protection (HW2, requirement 4).
#
# Implements the scheme from the HW2 slide notes: a TPM-sealed RSA private key
# protects an obfuscated AES key, which protects the software. Uses a software
# TPM 2.0 (swtpm) so the exact tpm2-tools workflow of a real chip runs on a VM
# with no physical TPM; on real edge hardware only start_tpm/stop_tpm change.

RSA_PRIMARY_HANDLE="0x81000001"   # primary (storage root) key
RSA_KEY_HANDLE="0x81000002"       # the RSA decryption key; private part never leaves the TPM

start_tpm() {   # start_tpm <state_dir> <server_port> <ctrl_port>
  mkdir -p "$1"
  swtpm socket --tpm2 --tpmstate "dir=$1" \
    --ctrl "type=tcp,port=$3" --server "type=tcp,port=$2" \
    --flags not-need-init,startup-clear --daemon
  sleep 1
  export TPM2TOOLS_TCTI="swtpm:host=localhost,port=$2"
}

stop_tpm() { swtpm_ioctl --tcp ":$1" -s 2>/dev/null || true; }

# Create the RSA key in the TPM if absent; export its public part to $1 (PEM).
ensure_rsa_key() {   # ensure_rsa_key <rsa.pem out path>
  local pem="$1" tmp
  if ! tpm2_readpublic -c "$RSA_KEY_HANDLE" >/dev/null 2>&1; then
    tmp="$(mktemp -d)"
    tpm2_createprimary -C o -g sha256 -G rsa -c "$tmp/primary.ctx" >/dev/null
    tpm2_evictcontrol -C o -c "$tmp/primary.ctx" "$RSA_PRIMARY_HANDLE" >/dev/null
    tpm2_flushcontext -t >/dev/null 2>&1 || true
    tpm2_create -C "$RSA_PRIMARY_HANDLE" -G rsa2048 \
        -u "$tmp/rsa.pub" -r "$tmp/rsa.priv" >/dev/null
    tpm2_load -C "$RSA_PRIMARY_HANDLE" -u "$tmp/rsa.pub" -r "$tmp/rsa.priv" \
        -c "$tmp/rsa.ctx" >/dev/null
    tpm2_evictcontrol -C o -c "$tmp/rsa.ctx" "$RSA_KEY_HANDLE" >/dev/null
    tpm2_flushcontext -t >/dev/null 2>&1 || true
    rm -rf "$tmp"
  fi
  tpm2_readpublic -c "$RSA_KEY_HANDLE" -o "$pem" -f pem >/dev/null
}

# Obfuscation key = a feature of the ENCRYPTED file (its SHA-256). Binds the AES
# key to this exact ciphertext, per the notes ("a key based on specific features
# of the encrypted file").
obf_key_of() { sha256sum "$1" | cut -d' ' -f1; }
