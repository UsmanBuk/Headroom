#!/usr/bin/env bash
# Installs ai-limits for the current user: files in ~/.local/share/ai-limits, the claude-auto
# command in ~/.local/bin, and the dashboard as a systemd user service. Safe to re-run.
set -euo pipefail
src=$(cd "$(dirname "$0")" && pwd)
dest="$HOME/.local/share/ai-limits"
conf="$HOME/.config/ai-limits/config.json"

mkdir -p "$dest" "$HOME/.local/bin" "$(dirname "$conf")"
cp "$src"/{server.py,autoswitch.py,config.py,index.html,claude-auto} "$dest/"
ln -sf "$dest/claude-auto" "$HOME/.local/bin/claude-auto"

if [[ $(uname) == Darwin ]]; then
  restart="launchctl kickstart -k gui/$(id -u)/ai-limits"
else
  restart="systemctl --user restart ai-limits"
fi
if [[ ! -f $conf ]]; then
  cp "$src/config.example.json" "$conf"
  echo "Created $conf. Edit it to list your accounts, then: $restart"
fi

if [[ $(uname) == Darwin ]]; then
  # macOS has no systemd: run the dashboard as a launchd agent in the login session,
  # where the Keychain that holds the Claude Code logins is unlocked.
  plist="$HOME/Library/LaunchAgents/ai-limits.plist"
  mkdir -p "$(dirname "$plist")"
  cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>ai-limits</string>
  <key>ProgramArguments</key>
  <array><string>/usr/bin/python3</string><string>$dest/server.py</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>$dest/server.log</string>
</dict>
</plist>
EOF
  launchctl bootout "gui/$(id -u)/ai-limits" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$plist"
else
  mkdir -p "$HOME/.config/systemd/user"
  cp "$src/ai-limits.service" "$HOME/.config/systemd/user/"
  systemctl --user daemon-reload
  systemctl --user enable ai-limits
  systemctl --user restart ai-limits
fi
echo "Dashboard: http://127.0.0.1:8787 (or the port in $conf)"
echo "Next: add the hooks from the README to your Claude Code settings.json."
