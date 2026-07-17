#!/usr/bin/env bash
# setup_remote_vitis_mac.sh — RUN THIS ON THE MAC.
#
# Companion to setup_remote_vitis_server.sh. It:
#   1. ensures this Mac has an SSH key and prints the PUBLIC key to hand to the server script
#   2. (optional) if you pass a host, writes a 'vitisbox' SSH alias and tests passwordless login
#
# Key-based auth only — this never handles passwords. Usage:
#   bash scripts/setup_remote_vitis_mac.sh                     # just print my public key
#   bash scripts/setup_remote_vitis_mac.sh user@server         # + create 'vitisbox' alias & test
#   bash scripts/setup_remote_vitis_mac.sh user@server --jump user@gateway
set -euo pipefail

HOST="${1:-}"; JUMP=""
shift || true
while [[ $# -gt 0 ]]; do
  case "$1" in
    --jump) JUMP="$2"; shift 2;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done

KEY="${HOME}/.ssh/id_ed25519"
if [[ ! -f "${KEY}.pub" ]]; then
  echo "[mac] no ed25519 key found — generating one (no passphrase for automation)"
  ssh-keygen -t ed25519 -N "" -f "$KEY" -C "$(whoami)@$(hostname -s)-c2hlsc"
fi
echo
echo "=== MY PUBLIC KEY — paste this into the server script's --pubkey argument ==="
cat "${KEY}.pub"
echo "============================================================================"

if [[ -z "$HOST" ]]; then
  echo
  echo "Next: on the Linux server run"
  echo "  bash scripts/setup_remote_vitis_server.sh --pubkey \"$(cat "${KEY}.pub")\""
  echo "then re-run me with the server address:  bash scripts/setup_remote_vitis_mac.sh user@server"
  exit 0
fi

# --- write a 'vitisbox' alias so configs/tools can use a stable name -------------------
CFG="${HOME}/.ssh/config"; touch "$CFG"; chmod 600 "$CFG"
HOSTUSER="${HOST%@*}"; HOSTNAME_="${HOST#*@}"
if ! grep -qE "^Host vitisbox$" "$CFG"; then
  {
    echo ""
    echo "Host vitisbox"
    echo "    HostName ${HOSTNAME_}"
    echo "    User ${HOSTUSER}"
    echo "    IdentityFile ${KEY}"
    echo "    BatchMode yes"
    [[ -n "$JUMP" ]] && echo "    ProxyJump ${JUMP}"
  } >> "$CFG"
  echo "[mac] added 'vitisbox' alias -> ${HOST}${JUMP:+ (via ${JUMP})}"
else
  echo "[mac] 'vitisbox' alias already present in ~/.ssh/config (leaving as-is)"
fi

echo "[mac] testing passwordless login (ssh -o BatchMode=yes vitisbox true)"
if ssh -o BatchMode=yes -o ConnectTimeout=10 vitisbox true 2>/dev/null; then
  echo "PASS: passwordless SSH to the server works."
else
  echo "FAIL: could not log in without a password."
  echo "  -> confirm the server script ran and added THIS key, and that ${HOST} is reachable."
  exit 1
fi
