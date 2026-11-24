#!/usr/bin/env python3
"""Account auto-switch decisions for the claude-auto launcher.

  autoswitch.py start <profile>   print the profile to start on (stderr: why, if changed)
  autoswitch.py launch <profile>  print its config dir, then the extra claude args, one per line
  autoswitch.py hook              Stop / StopFailure hook; reads hook JSON on stdin
  autoswitch.py watch <pid> <transcript>   finishes a switch the hook had to put off

The hook only acts inside claude-auto (CLAUDE_AUTO_PID set). When the current account
is at the threshold, or a reply died on a rate limit, it writes a marker for the
launcher and ends this Claude process; the launcher reopens the chat on the next
account. Usage comes from the local dashboard; if that is down, nothing switches.
"""
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from config import CLAUDE_ARGS, CLAUDE_PROFILES, PORT, THRESHOLD

ORDER = [name for name, _ in CLAUDE_PROFILES]
DASHBOARD = f"http://127.0.0.1:{PORT}/api/usage"
LAST_GOOD = Path(__file__).resolve().parent / "last_good.json"


def accounts():
    with urllib.request.urlopen(DASHBOARD, timeout=20) as resp:
        data = json.load(resp)
    return {a["name"]: a for a in data["accounts"] if a["tool"] == "claude"}


def last_known(name):
    """The dashboard's last reading of an account. An idle account's login token
    expires after about 8 hours, so its live usage can't be read; the last reading is
    a fair guess (only use of that account raises it, and starting Claude there
    renews the token). A limit whose reset time has passed since counts as 0."""
    try:
        entry = json.loads(LAST_GOOD.read_text()).get(name)
    except (OSError, ValueError):
        return None
    if not entry:
        return None
    now = datetime.now(timezone.utc)
    limits = [
        {**l, "percent": 0}
        if l.get("resets_at") and datetime.fromisoformat(l["resets_at"]) < now
        else l
        for l in entry["limits"]
    ]
    return {**entry, "limits": limits}


def used(account, model):
    """Highest percentage among the limits that would block this model.

    A limit either covers every model or names one ("Fable"); a named limit counts
    when that name appears in the model in use ("claude-fable-5-2", "--model fable").
    An account the dashboard can't read right now falls back to its last reading."""
    if account is not None and account["status"] != "ok":
        account = last_known(account["name"])
    if account is None:
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


def processes():
    """pid -> (parent pid, command line) for every process; ps works on Linux and macOS."""
    out = subprocess.run(
        ["ps", "-eo", "pid=,ppid=,args="], capture_output=True, text=True
    ).stdout
    procs = {}
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) >= 2:
            procs[int(parts[0])] = (int(parts[1]), parts[2] if len(parts) > 2 else "")
    return procs


def ancestors(procs):
    """This hook's own chain of parents, nearest first."""
    chain, pid = [], os.getppid()
    while pid > 1 and pid in procs and pid not in chain:
        chain.append(pid)
        pid = procs[pid][0]
    return chain


def claude_pid(procs):
    """The Claude process is the ancestor whose parent is the launcher."""
    launcher = int(os.environ["CLAUDE_AUTO_PID"])
    for pid in ancestors(procs):
        if procs[pid][0] == launcher:
            return pid
    return None


def background_work(procs, pid, transcript_path):
    """What closing Claude now would kill, or None. Hooks fire after the turn's own
    commands finish, so a shell-snapshot bash child left over is a background job;
    a subagent transcript written in the last 5 minutes means a subagent may be live."""
    own = ancestors(procs)  # the shell running this hook is not background work
    for child, (ppid, command) in procs.items():
        if ppid == pid and child not in own and "shell-snapshots/snapshot-" in command:
            return "a background command"
    subagents = Path(transcript_path).with_suffix("") / "subagents"
    if any(time.time() - f.stat().st_mtime < 300 for f in subagents.glob("*.jsonl")):
        return "a subagent"
    return None


def alive(pid):
    """Whether a process exists; works where there is no /proc (macOS)."""
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def watch(pid, transcript_path):
    """Finish a switch the hook put off. Background work can end without another
    reply (e.g. a subagent dying on the same limit), so no hook would fire again."""
    pid = int(pid)
    adir = Path(os.environ["CLAUDE_AUTO_DIR"])
    pending, marker = adir / "pending", adir / "switch"
    config = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
    status_file = config / "sessions" / f"{pid}.json"
    (adir / "watcher").write_text(str(os.getpid()))
    while alive(pid) and pending.exists() and not marker.exists():
        time.sleep(15)
        try:
            idle = json.loads(status_file.read_text()).get("status") == "idle"
        except (OSError, ValueError):
            idle = False
        if idle and pending.exists() and not background_work(processes(), pid, transcript_path):
            pending.rename(marker)
            os.kill(pid, signal.SIGTERM)
            return


def watcher_running(adir):
    try:
        return alive((Path(adir) / "watcher").read_text().strip())
    except (OSError, ValueError):
        return False


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
            Path(os.environ["CLAUDE_AUTO_DIR"], "pending").unlink(missing_ok=True)
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
    procs = processes()
    pid = claude_pid(procs)
    if pid is None:
        return
    adir = os.environ["CLAUDE_AUTO_DIR"]
    cut = "cut" if event.get("hook_event_name") == "StopFailure" else "done"
    line = f"{event['session_id']} {nxt} {model or '-'} {cut} {reason}\n"
    transcript = event.get("transcript_path", "")
    busy = background_work(procs, pid, transcript)
    if busy:
        Path(adir, "pending").write_text(line)
        if not watcher_running(adir):
            subprocess.Popen(
                [sys.executable, __file__, "watch", str(pid), transcript],
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        msg = f"{reason} — not switching to {nxt} yet: {busy} is still running. It will switch once that finishes and the chat is idle."
        print(json.dumps({"systemMessage": msg}))
        return
    Path(adir, "pending").unlink(missing_ok=True)
    Path(adir, "switch").write_text(line)
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
    {"start": start, "launch": launch, "hook": hook, "watch": watch}[sys.argv[1]](*sys.argv[2:])
