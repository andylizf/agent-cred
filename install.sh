#!/usr/bin/env bash
# agent-cred installer. Copies the CLI + daemon into ~/.local/bin, creates the run dir,
# and installs a keep-alive service (launchd on macOS, systemd --user on Linux).
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
BIN="${BIN:-$HOME/.local/bin}"
RUN_DIR_DEFAULT="$HOME/.cred"
PY="$(command -v python3 || true)"
[ -n "$PY" ] || { echo "error: python3 not found on PATH" >&2; exit 1; }

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
  <key>KeepAlive</key><true/>
  <key>RunAtLoad</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$RUN_DIR_DEFAULT/stdout.log</string>
  <key>StandardErrorPath</key><string>$RUN_DIR_DEFAULT/stderr.log</string>
</dict>
</plist>
EOF
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
ExecStart=$PY $BIN/cred-brokerd.py
Restart=always
[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable --now agent-cred.service
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
