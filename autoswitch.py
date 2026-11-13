#!/usr/bin/env python3
"""Account auto-switch decisions for the claude-auto launcher.

  autoswitch.py start <profile>   print the profile to start on (stderr: why, if changed)
  autoswitch.py launch <profile>  print its config dir, then the extra claude args, one per line
  autoswitch.py hook              Stop / StopFailure hook; reads hook JSON on stdin

The hook only acts inside claude-auto (CLAUDE_AUTO_PID set). When the current account
is at the threshold, or a reply died on a rate limit, it writes a marker for the
launcher and ends this Claude process; the launcher reopens the chat on the next
account. Usage comes from the local dashboard; if that is down, nothing switches.
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from config import CLAUDE_ARGS, CLAUDE_PROFILES, PORT, THRESHOLD

ORDER = [name for name, _ in CLAUDE_PROFILES]
DASHBOARD = f"http://127.0.0.1:{PORT}/api/usage"


def accounts():
    with urllib.request.urlopen(DASHBOARD, timeout=20) as resp:
        data = json.load(resp)
    return {a["name"]: a for a in data["accounts"] if a["tool"] == "claude"}


def used(account, model):
    """Highest percentage among the limits that would block this model.

    A limit either covers every model or names one ("Fable"); a named limit counts
    when that name appears in the model in use ("claude-fable-5-2", "--model fable")."""
    if account is None or account["status"] != "ok":
        return 100.0
    relevant = [
        l
        for l in account["limits"]
        if l["model"] is None or l["model"].lower() in model.lower()
    ]
    return max([l["percent"] or 0 for l in relevant], default=0)


def next_profile(accs, current, model):
    for name in ORDER:
        if name != current and used(accs.get(name), model) < THRESHOLD:
            return name
    return None


def transcript_model(path):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 400_000))
            tail = f.read().decode("utf-8", "ignore")
    except OSError:
        return ""
    # The reply line lands after the Stop hook runs; the request line before it already names the model.
    found = re.findall(r'"model":"(claude-[^"]+)"', tail)
    return found[-1] if found else ""


def claude_pid():
    """The Claude process is the ancestor whose parent is the launcher."""
    launcher, pid = os.environ["CLAUDE_AUTO_PID"], os.getppid()
    while pid > 1:
        with open(f"/proc/{pid}/stat") as f:
            ppid = f.read().rsplit(")", 1)[1].split()[1]
        if ppid == launcher:
            return pid
        pid = int(ppid)
    return None


def background_work(pid, transcript_path):
    """What closing Claude now would kill, or None. Hooks fire after the turn's own
    commands finish, so a shell-snapshot bash child left over is a background job;
    a subagent transcript written in the last 5 minutes means a subagent may be live."""
    for stat in Path("/proc").glob("[0-9]*/stat"):
        try:
            if stat.read_text().rsplit(")", 1)[1].split()[1] != str(pid):
                continue
            if b"shell-snapshots/snapshot-" in (stat.parent / "cmdline").read_bytes():
                return "a background command"
        except (OSError, IndexError):
            continue
    subagents = Path(transcript_path).with_suffix("") / "subagents"
    if any(time.time() - f.stat().st_mtime < 300 for f in subagents.glob("*.jsonl")):
        return "a subagent"
    return None


def launch(profile):
    print(dict(CLAUDE_PROFILES)[profile])
    for arg in CLAUDE_ARGS:
        print(arg)


def start(profile, *claude_args):
    if profile not in ORDER:
        sys.exit(f"claude-auto: unknown profile '{profile}' (config has: {', '.join(ORDER)})")
    model = " ".join([*CLAUDE_ARGS, *claude_args])  # e.g. --model fable
    try:
        accs = accounts()
    except Exception:
        print(profile)
        return
    pct = used(accs.get(profile), model)
    if pct >= THRESHOLD:
        nxt = next_profile(accs, profile, model)
        if nxt:
            print(
                f"↻ {profile} is at {pct:.0f}% — starting on {nxt} instead",
                file=sys.stderr,
            )
            profile = nxt
    print(profile)


def hook():
    if "CLAUDE_AUTO_PID" not in os.environ:
        return
    event = json.load(sys.stdin)
    current = os.environ["CLAUDE_AUTO_PROFILE"]
    model = transcript_model(event.get("transcript_path", ""))
    try:
        accs = accounts()
    except Exception:
        return
    if event.get("hook_event_name") == "StopFailure":
        if event.get("error") != "rate_limit":
            return
        reason = f"{current} hit its usage limit"
    else:
        if (accs.get(current) or {}).get("status") != "ok":
            return  # unknown is not the same as full
        pct = used(accs.get(current), model)
        if pct < THRESHOLD and not os.environ.get(
            "CLAUDE_AUTO_FORCE"
        ):  # FORCE: testing only
            return
        reason = f"{current} at {pct:.0f}%"
    nxt = next_profile(accs, current, model)
    if nxt is None:
        print(
            json.dumps(
                {
                    "systemMessage": f"{reason}, and every other account is at {THRESHOLD:.0f}%+ — staying put"
                }
            )
        )
        return
    pid = claude_pid()
    if pid is None:
        return
    busy = background_work(pid, event.get("transcript_path", ""))
    if busy:
        msg = f"{reason} — not switching to {nxt} yet: {busy} is still running. It will switch at the first reply after that finishes."
        print(json.dumps({"systemMessage": msg}))
        return
    with open(os.path.join(os.environ["CLAUDE_AUTO_DIR"], "switch"), "w") as f:
        cut = "cut" if event.get("hook_event_name") == "StopFailure" else "done"
        f.write(f"{event['session_id']} {nxt} {model or '-'} {cut} {reason}\n")
    # Kill after this hook returns so Claude finishes the turn bookkeeping first.
    subprocess.Popen(
        ["sh", "-c", f"sleep 2; kill -TERM {pid}"],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print(json.dumps({"systemMessage": f"↻ {reason} — reopening this chat on {nxt}…"}))


if __name__ == "__main__":
    {"start": start, "launch": launch, "hook": hook}[sys.argv[1]](*sys.argv[2:])
