"""Settings shared by the dashboard and the auto-switch, read from one JSON file."""
import json
from pathlib import Path

CONFIG_FILE = Path.home() / ".config/ai-limits/config.json"
_cfg = json.loads(CONFIG_FILE.read_text())

PORT = _cfg.get("port", 8787)
THRESHOLD = float(_cfg.get("threshold", 95))
# Claude profiles are listed in switch order: the first one under the threshold is used next.
CLAUDE_PROFILES = [(p["name"], Path(p["dir"]).expanduser()) for p in _cfg["claude"]]
CODEX_PROFILES = [(p["name"], Path(p["dir"]).expanduser()) for p in _cfg.get("codex", [])]
CLAUDE_ARGS = _cfg.get("claude_args", [])  # added to every launch, e.g. ["--model", "opus"]
