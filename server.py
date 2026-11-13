#!/usr/bin/env python3
"""Local dashboard of Claude + Codex usage limits across accounts.

Reads each profile's existing login token and asks the provider's usage endpoint.
Tokens are only read, never refreshed: refresh tokens are single-use, so refreshing
here could race a running session and log that profile out.
"""
import json
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from config import CLAUDE_PROFILES, CODEX_PROFILES, PORT

HERE = Path(__file__).resolve().parent
# Running Claude sessions poll the same per-account usage endpoint, so ask rarely
# and back off on 429 instead of competing with them.
CACHE_SECONDS = 180
BACKOFF_SECONDS = 600

_cache = {"at": 0.0, "data": None}
LAST_GOOD_FILE = (
    HERE / "last_good.json"
)  # survives restarts, so a 429 never blanks a card
_last_good = json.loads(LAST_GOOD_FILE.read_text()) if LAST_GOOD_FILE.exists() else {}
_backoff_until = {}  # name -> epoch seconds


def get_json(url, headers):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp)


def window_label(seconds):
    hours = round((seconds or 0) / 3600)
    return {5: "5-hour", 168: "Weekly"}.get(hours, f"{hours}h window")


def claude_account(name, config_dir):
    oauth = json.loads((config_dir / ".credentials.json").read_text())["claudeAiOauth"]
    usage = get_json(
        "https://api.anthropic.com/api/oauth/usage",
        {
            "Authorization": f"Bearer {oauth['accessToken']}",
            "anthropic-beta": "oauth-2025-04-20",
        },
    )
    limits = []
    for lim in usage.get("limits") or []:
        model = ((lim.get("scope") or {}).get("model") or {}).get("display_name")
        label = {"session": "5-hour", "weekly_all": "Weekly"}.get(
            lim["kind"], lim["kind"]
        )
        if model:
            label = f"Weekly · {model}"
        limits.append(
            {
                "label": label,
                "model": model,
                "percent": lim.get("percent"),
                "resets_at": lim.get("resets_at"),
            }
        )
    sub = oauth.get("subscriptionType") or "?"
    plan = {"max": "Max", "team": "Team", "pro": "Pro"}.get(sub, sub)
    if "20x" in (oauth.get("rateLimitTier") or ""):
        plan += " 20x"
    return {"plan": plan, "limits": limits}


def codex_account(name, codex_home):
    tokens = json.loads((codex_home / "auth.json").read_text())["tokens"]
    usage = get_json(
        "https://chatgpt.com/backend-api/wham/usage",
        {
            "Authorization": f"Bearer {tokens['access_token']}",
            "ChatGPT-Account-Id": tokens["account_id"],
            "User-Agent": "codex_cli_rs",
        },
    )
    limits = []
    rate = usage.get("rate_limit") or {}
    for key in ("primary_window", "secondary_window"):
        win = rate.get(key)
        if win:
            limits.append(
                {
                    "label": window_label(win.get("limit_window_seconds")),
                    "model": None,
                    "percent": win.get("used_percent"),
                    "resets_at": datetime.fromtimestamp(
                        win["reset_at"], timezone.utc
                    ).isoformat(),
                }
            )
    credits = usage.get("credits") or {}
    note = None
    if credits.get("has_credits") and credits.get("balance"):
        note = f"{float(credits['balance']):,.0f} credits"
    return {
        "plan": (usage.get("plan_type") or "").capitalize(),
        "limits": limits,
        "note": note,
    }


def collect(tool, name, path, fetch):
    base = {
        "tool": tool,
        "name": name,
        "plan": None,
        "limits": [],
        "note": None,
        "status": "ok",
        "error": None,
        "as_of": None,
    }
    if time.time() < _backoff_until.get(name, 0) and name in _last_good:
        return _last_good[name]
    try:
        base.update(fetch(name, path))
        base["as_of"] = datetime.now(timezone.utc).isoformat()
        _last_good[name] = base
        LAST_GOOD_FILE.write_text(json.dumps(_last_good))
        return base
    except urllib.error.HTTPError as e:
        if e.code == 429:
            wait = max(BACKOFF_SECONDS, int(e.headers.get("Retry-After") or 0))
            _backoff_until[name] = time.time() + wait
            if name in _last_good:
                return _last_good[name]
        base["status"] = "stale" if e.code in (401, 403) else "error"
        base["error"] = (
            f"Login token expired — open {name} once to refresh it"
            if base["status"] == "stale"
            else f"HTTP {e.code}"
        )
    except Exception as e:  # one broken account must not blank the whole page
        base["status"] = "error"
        base["error"] = f"{type(e).__name__}: {e}"
    return base


def snapshot():
    if _cache["data"] and time.time() - _cache["at"] < CACHE_SECONDS:
        return _cache["data"]
    jobs = [("claude", n, p, claude_account) for n, p in CLAUDE_PROFILES]
    jobs += [("codex", n, p, codex_account) for n, p in CODEX_PROFILES]
    with ThreadPoolExecutor(len(jobs)) as pool:
        accounts = list(pool.map(lambda j: collect(*j), jobs))
    _cache.update(
        at=time.time(),
        data={
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "accounts": accounts,
        },
    )
    return _cache["data"]


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        # A web page can point its own domain at 127.0.0.1 (DNS rebinding), so answer local names only.
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        if host not in ("127.0.0.1", "localhost"):
            self.send_error(403)
            return
        if self.path == "/api/usage":
            body, ctype = json.dumps(snapshot()).encode(), "application/json"
        elif self.path in ("/", "/index.html"):
            body, ctype = (HERE / "index.html").read_bytes(), "text/html; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
