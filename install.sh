#!/usr/bin/env bash
# Installs ai-limits for the current user: files in ~/.local/share/ai-limits, the claude-auto
# command in ~/.local/bin, and the dashboard as a systemd user service. Safe to re-run.
set -euo pipefail
src=$(cd "$(dirname "$0")" && pwd)
dest="$HOME/.local/share/ai-limits"
conf="$HOME/.config/ai-limits/config.json"

mkdir -p "$dest" "$HOME/.local/bin" "$(dirname "$conf")" "$HOME/.config/systemd/user"
cp "$src"/{server.py,autoswitch.py,config.py,index.html,claude-auto} "$dest/"
ln -sf "$dest/claude-auto" "$HOME/.local/bin/claude-auto"
cp "$src/ai-limits.service" "$HOME/.config/systemd/user/"
if [[ ! -f $conf ]]; then
  cp "$src/config.example.json" "$conf"
  echo "Created $conf. Edit it to list your accounts, then: systemctl --user restart ai-limits"
fi

systemctl --user daemon-reload
systemctl --user enable ai-limits
systemctl --user restart ai-limits
echo "Dashboard: http://127.0.0.1:8787 (or the port in $conf)"
echo "Next: add the hooks from the README to your Claude Code settings.json."
