#!/usr/bin/env bash
# agent-cred installer. Copies the CLI + daemon into ~/.local/bin, creates the run dir,
# and installs a keep-alive service (launchd on macOS, systemd --user on Linux).
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
BIN="${BIN:-$HOME/.local/bin}"
PY="$(command -v python3 || true)"
[ -n "$PY" ] || { echo "error: python3 not found on PATH" >&2; exit 1; }

# The daemon reads run_dir from ~/.config/cred/config.json; the service must log to the
# same place, so honour an existing config instead of assuming ~/.cred.
CFG="${CRED_CONFIG:-$HOME/.config/cred/config.json}"
RUN_DIR_DEFAULT="$("$PY" - "$CFG" <<'PYEOF'
import json, os, sys
try:
    rd = json.load(open(sys.argv[1])).get("run_dir") or "~/.cred"
except Exception:
    rd = "~/.cred"
print(os.path.expanduser(rd))
PYEOF
)"

# launchd/systemd start the daemon with a bare PATH, and the daemon shells out to `bw`
# (config bw_bin). Bake the directory `bw` lives in now into the service so an unlock
# does not fail with "bw: not found" only when started by the service manager.
BW_PATH="$(command -v bw || true)"
SERVICE_PATH="$BIN:/usr/local/bin:/usr/bin:/bin"
[ -n "$BW_PATH" ] && SERVICE_PATH="$(dirname "$BW_PATH"):$SERVICE_PATH"

echo "→ installing cred + cred-brokerd.py to $BIN"
mkdir -p "$BIN"
install -m 0755 "$SRC/cred" "$BIN/cred"
install -m 0755 "$SRC/cred-brokerd.py" "$BIN/cred-brokerd.py"

echo "→ creating run dir $RUN_DIR_DEFAULT (0700)"
mkdir -p "$RUN_DIR_DEFAULT"
chmod 0700 "$RUN_DIR_DEFAULT"

if ! command -v bw >/dev/null 2>&1; then
  echo "! Bitwarden CLI (bw) is not on PATH. Install it and run 'bw login' before using cred." >&2
fi

case "$(uname -s)" in
  Darwin)
    PLIST="$HOME/Library/LaunchAgents/ai.agent-cred.brokerd.plist"
    echo "→ installing launchd agent $PLIST"
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>ai.agent-cred.brokerd</string>
  <key>ProgramArguments</key>
  <array><string>$PY</string><string>$BIN/cred-brokerd.py</string></array>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>$SERVICE_PATH</string></dict>
  <key>KeepAlive</key><true/>
  <key>RunAtLoad</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$RUN_DIR_DEFAULT/stdout.log</string>
  <key>StandardErrorPath</key><string>$RUN_DIR_DEFAULT/stderr.log</string>
</dict>
</plist>
EOF
    LEGACY="$HOME/Library/LaunchAgents/local.cred-broker.plist"
    # A hand-rolled `local.cred-broker` label predates this installer on some machines;
    # two services would fight over the socket, so it is unloaded and kept as .retired.
    if [ -f "$LEGACY" ]; then
      echo "→ retiring legacy service local.cred-broker ($LEGACY → $LEGACY.retired)"
      launchctl unload "$LEGACY" 2>/dev/null || true
      mv "$LEGACY" "$LEGACY.retired"
    fi
    launchctl unload "$PLIST" 2>/dev/null || true
    launchctl load "$PLIST"
    echo "  loaded. (manage with: launchctl kickstart -k gui/\$(id -u)/ai.agent-cred.brokerd)"
    ;;
  Linux)
    UNIT_DIR="$HOME/.config/systemd/user"
    echo "→ installing systemd user service $UNIT_DIR/agent-cred.service"
    mkdir -p "$UNIT_DIR"
    cat > "$UNIT_DIR/agent-cred.service" <<EOF
[Unit]
Description=agent-cred credential broker daemon
[Service]
Environment=PATH=$SERVICE_PATH
ExecStart=$PY $BIN/cred-brokerd.py
Restart=always
[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable --now agent-cred.service
    systemctl --user restart agent-cred.service
    echo "  enabled. (manage with: systemctl --user restart agent-cred)"
    ;;
  *)
    echo "! Unknown OS; start the daemon yourself:  $PY $BIN/cred-brokerd.py &" >&2
    ;;
esac

echo
echo "Done. Make sure $BIN is on your PATH, then:"
echo "  bw login                 # if you haven't"
echo "  cred unlock <item>       # authorize an item"
echo "  cred status              # verify"
