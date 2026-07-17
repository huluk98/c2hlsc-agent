#!/usr/bin/env bash
# setup_remote_vitis_server.sh — RUN THIS ON THE LINUX VITIS SERVER.
#
# One-shot setup so a remote Mac can drive csim/csynth/cosim over SSH:
#   1. authorizes the Mac's SSH public key (passwordless login for that machine only)
#   2. locates & verifies the Vitis environment (vitis_hls, xsim) + rsync
#   3. creates the run scratch dir
#   4. prints a ready-to-paste config.yaml block for the Mac side
#
# Needs NO sudo (only touches your $HOME). Usage:
#   bash scripts/setup_remote_vitis_server.sh \
#        --pubkey "ssh-ed25519 AAAA... user@mac" \
#        [--vitis-settings /opt/Xilinx/Vitis/2023.2/settings64.sh] \
#        [--runs-dir ~/c2hlsc_runs] [--host <address-the-mac-should-ssh-to>]
set -euo pipefail

PUBKEY=""; VSET=""; RUNS_DIR="${HOME}/c2hlsc_runs"; HOSTADDR=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --pubkey)         PUBKEY="$2"; shift 2;;
    --vitis-settings) VSET="$2";   shift 2;;
    --runs-dir)       RUNS_DIR="$2"; shift 2;;
    --host)           HOSTADDR="$2"; shift 2;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done

fail() { echo "FAIL: $*" >&2; exit 1; }
ok()   { echo "  ok: $*"; }

# --- 1. authorize the Mac's public key (idempotent) ------------------------------------
echo "[1/4] authorizing SSH key"
[[ -n "$PUBKEY" ]] || fail "--pubkey is required (the Mac's public key line)"
[[ "$PUBKEY" == ssh-* ]] || fail "--pubkey does not look like an OpenSSH public key"
mkdir -p "${HOME}/.ssh"; chmod 700 "${HOME}/.ssh"
touch "${HOME}/.ssh/authorized_keys"; chmod 600 "${HOME}/.ssh/authorized_keys"
if grep -qxF "$PUBKEY" "${HOME}/.ssh/authorized_keys"; then
  ok "key already authorized"
else
  echo "$PUBKEY" >> "${HOME}/.ssh/authorized_keys"; ok "key added to authorized_keys"
fi

# --- 2. locate + verify Vitis ----------------------------------------------------------
echo "[2/4] locating Vitis"
if [[ -z "$VSET" ]]; then
  VSET="$(find /opt /tools /usr/local /DATA /data /scratch /apps /mnt "$HOME" \
            -maxdepth 7 -name settings64.sh 2>/dev/null \
          | grep -iE 'vitis' | grep -vi 'vivado' | sort | tail -1 || true)"
fi
[[ -n "$VSET" && -f "$VSET" ]] || fail "could not find Vitis settings64.sh — pass --vitis-settings <path>"
ok "settings64.sh: $VSET"
# shellcheck disable=SC1090
. "$VSET"
command -v rsync     >/dev/null 2>&1 || fail "rsync not on PATH"; ok "rsync: $(command -v rsync)"
command -v vitis_hls >/dev/null 2>&1 || fail "vitis_hls not on PATH after sourcing settings"; ok "vitis_hls: $(command -v vitis_hls)"
# xsim is invoked *internally* by vitis_hls for cosim; it need not be on PATH. Report, don't fail.
if command -v xsim >/dev/null 2>&1; then ok "xsim: $(command -v xsim)"
else echo "  note: 'xsim' not on PATH — fine, Vitis HLS uses its bundled simulator for cosim"; fi

# --- 3. scratch dir --------------------------------------------------------------------
echo "[3/4] run dir"
mkdir -p "$RUNS_DIR"; ok "$RUNS_DIR"

# --- 4. print the config block ---------------------------------------------------------
echo "[4/4] config for the Mac side"
[[ -n "$HOSTADDR" ]] || HOSTADDR="$(whoami)@$(hostname -f 2>/dev/null || hostname)"
cat <<EOF

======================================================================
SETUP OK. Add these keys to a project config.yaml on the Mac:

run_vitis: true
cosim_backend: vitis-ssh
vitis_ssh_host: ${HOSTADDR}
vitis_remote_dir: ${RUNS_DIR}
vitis_setup: ${VSET}

NOTE: if the Mac reaches this box at a different address than
'${HOSTADDR}' (e.g. a LAN IP or a jump-host alias), give the Mac side
that address instead — the rest is correct.
======================================================================
EOF
