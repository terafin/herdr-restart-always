#!/usr/bin/env python3
"""herdr-restart-always — always restart whatever the pane is running.

Closes the supervision gap herdr leaves open: herdr's native agent session
restore only fires on a SERVER restart; nothing relaunches an agent whose
process dies inside a RUNNING server. This plugin covers both:

  * live-server agent death  -> a per-pane monitor notices agent_status turn
                                `unknown` and re-runs the resume command;
  * server restart           -> if the saved snapshot was written while the
                                agent was dead (no agent_session recorded),
                                herdr restores a bare shell; the plugin's own
                                registry (written while the agent was last
                                alive) still knows what to resume.

Agent-agnostic: the resume command comes from a per-agent table matching
herdr's native restore commands (claude, hermes, codex, pi, opencode, ...),
overridable via config.json.

Conventions
-----------
* The MONITOR is the single relauncher per pane. Hooks (startup/events) only
  ensure a monitor exists and refresh the registry. That makes double-launch
  impossible by construction instead of by timing.
* `[session] resume_agents_on_restore = false` must be set in herdr's
  config.toml so native restore does not race the plugin at server start.
* State lives in the plugin config dir
  (`~/.config/herdr/plugins/config/herdr-restart-always/`):
    registry.json  durable agent_session refs (pane_id -> {agent,kind,value})
    monitors/      one lock file per pane: {pid, socket, heartbeat}
    stop-<pid>     sentinel a monitor checks to exit
    log.txt        restart log
"""

import json
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

# --------------------------------------------------------------------------
# Paths / env
# --------------------------------------------------------------------------

PLUGIN_ID = "herdr-restart-always"

# Resume command per agent kind, matching herdr's native restore commands
# (docs: "Session state and restore"). {value} is the session id/path from the
# pane's agent_session. Override any entry via config.json -> "commands".
DEFAULT_COMMANDS = {
    "claude": "claude --resume {value}",
    "hermes": "hermes --resume {value}",
    "codex": "codex resume {value}",
    "pi": "pi --session {value}",
    "opencode": "opencode --session {value}",
    "agy": "agy --conversation {value}",
    "omp": "omp --resume={value}",
    "cursor-agent": "cursor-agent --resume {value}",
    "grok": "grok --resume {value}",
    "copilot": "copilot --resume={value}",
    "devin": "devin --resume {value}",
    "droid": "droid --resume {value}",
    "kimi": "kimi --session {value}",
    "qodercli": "qodercli --resume {value}",
    "qwen": "qwen --resume {value}",
    "kilo": "kilo --session {value}",
    "mastracode": "mastracode --thread {value}",
}

# agent_status values herdr reports; anything else on a supervised pane means
# "no live agent" -> needs a relaunch.
ALIVE_STATUSES = {"idle", "working", "blocked", "done"}


def env_first(*names):
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def state_dir() -> Path:
    """Plugin state/config dir, honored in the same order herdr does."""
    return Path(
        env_first(
            "HERDR_PLUGIN_CONFIG_DIR",
            "HERDR_PLUGIN_STATE_DIR",
            "HERDR_PLUGIN_ROOT",
        )
        or Path.home() / ".config/herdr/plugins/config" / PLUGIN_ID
    ).resolve()


def herdr_bin() -> str:
    return os.environ.get("HERDR_BIN_PATH", "herdr")


def log_path() -> Path:
    return state_dir() / "log.txt"


# --------------------------------------------------------------------------
# herdr CLI
# --------------------------------------------------------------------------

def invoke(args, timeout=15):
    """Run a herdr CLI command; return the parsed envelope result or None."""
    try:
        proc = subprocess.run(
            [herdr_bin(), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    for chunk in (proc.stdout or "", proc.stderr or ""):
        text = chunk.strip()
        if not text:
            continue
        try:
            env = json.loads(text)
            if "result" in env:
                return env["result"]
            return None
        except json.JSONDecodeError:
            for line in reversed(text.splitlines()):
                if not line.strip().startswith("{"):
                    continue
                try:
                    env = json.loads(line)
                    if "result" in env:
                        return env["result"]
                except json.JSONDecodeError:
                    continue
    return None


def pane_list():
    result = invoke(["pane", "list"]) or {}
    return result.get("panes") or []


def pane_get(pane_id):
    result = invoke(["pane", "get", pane_id])
    if not result:
        return None
    pane = result.get("pane")
    return pane if isinstance(pane, dict) else None


def pane_run(pane_id, argv):
    return invoke(["pane", "run", pane_id, *argv])


SHELL_NAMES = {"zsh", "bash", "sh", "dash", "ksh", "fish", "nu", "pwsh", "ash"}


def pane_has_running_process(pane_id):
    """True if the pane is running something besides its idle shell.

    pane run sends input to the pane's shell even while a foreground process
    is active, so this is the anti-double-launch guard: only relaunch into a
    pane that has returned to a bare shell. An interactive shell is a single
    argv (e.g. ["/usr/bin/zsh"]); a shell running a script (["/bin/sh",
    "myscript.sh"]) is NOT idle — treat it as running.
    """
    result = invoke(["pane", "process-info", "--pane", pane_id])
    if not result:
        return True  # unknown -> be conservative, don't relaunch
    info = result.get("process_info") or {}
    for proc in info.get("foreground_processes") or []:
        argv = proc.get("argv") or []
        if not argv:
            continue
        if os.path.basename(str(argv[0])) in SHELL_NAMES and len(argv) == 1:
            continue
        return True
    return False


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def load_config():
    config = {}
    cfg_path = state_dir() / "config.json"
    try:
        with open(cfg_path, encoding="utf-8") as handle:
            loaded = json.load(handle)
        if isinstance(loaded, dict):
            config = loaded
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {
        "poll_seconds": int(config.get("poll_seconds", 5)),
        "cooldown_seconds": int(config.get("cooldown_seconds", 15)),
        "connect_grace_seconds": int(config.get("connect_grace_seconds", 10)),
        "sweep_seconds": int(config.get("sweep_seconds", 60)),
        "pane_gone_grace_seconds": int(config.get("pane_gone_grace_seconds", 120)),
        "commands": {**DEFAULT_COMMANDS, **config.get("commands", {})},
    }


def resume_argv(pane, config, registry=None):
    """Resume argv for a pane, or None if it isn't a supervised agent pane.

    Uses the LIVE agent_session first (preferred — freshest reference); falls
    back to the registry entry so a pane that died and lost its live session
    (or was restored as a bare shell) still knows what to resume.
    """
    pane_id = pane.get("pane_id")
    session = pane.get("agent_session") or (registry or {}).get(pane_id, {})
    agent = session.get("agent") or pane.get("agent")
    value = session.get("value")
    if not agent or not value:
        return None
    template = config["commands"].get(agent)
    if not template:
        return None
    try:
        return shlex.split(template.format(value=str(value)))
    except (ValueError, KeyError):
        return None


# --------------------------------------------------------------------------
# registry (durable resume refs)
# --------------------------------------------------------------------------

def load_registry():
    path = state_dir() / "registry.json"
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_registry(registry):
    state_dir().mkdir(parents=True, exist_ok=True)
    tmp = state_dir() / "registry.json.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(registry, handle, indent=2)
    tmp.replace(state_dir() / "registry.json")


def remember_session(pane):
    """Record a live agent_session so we can resume after the agent dies."""
    session = pane.get("agent_session") or {}
    if not session.get("agent") or not session.get("value"):
        return False
    registry = load_registry()
    pane_id = pane["pane_id"]
    registry[pane_id] = {
        "agent": session["agent"],
        "kind": session.get("kind"),
        "value": session["value"],
        "source": session.get("source"),
        "updated_at": int(time.time()),
    }
    save_registry(registry)
    return True


# --------------------------------------------------------------------------
# monitor lifecycle (one detached process per pane)
# --------------------------------------------------------------------------

def monitor_lock_path(pane_id):
    return state_dir() / "monitors" / f"{pane_id.replace(':', '_')}.json"


def monitor_pid(pane_id):
    """PID of a live monitor for this pane, or None."""
    try:
        with open(monitor_lock_path(pane_id), encoding="utf-8") as handle:
            lock = json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    pid = lock.get("pid")
    if not pid:
        return None
    try:
        os.kill(pid, 0)
        return pid
    except (ProcessLookupError, PermissionError):
        return None


def spawn_monitor(pane_id):
    """Spawn a detached monitor for a pane if one isn't already live."""
    if monitor_pid(pane_id):
        return False
    state_dir().mkdir(parents=True, exist_ok=True)
    here = Path(__file__).resolve()
    proc = subprocess.Popen(
        [sys.executable, str(here), "monitor", pane_id],
        cwd=str(Path.cwd()),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return proc.pid is not None


def is_stop_requested(pane_id):
    return (state_dir() / "stop-all").exists() or monitor_stop_path(pane_id).exists()


def monitor_stop_path(pane_id):
    return state_dir() / "monitors" / f"{pane_id.replace(':', '_')}.stop"


def write_lock(pane_id, heartbeat):
    lock = {"pid": os.getpid(), "heartbeat": int(time.time()), "pane_id": pane_id}
    lock_path = monitor_lock_path(pane_id)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = lock_path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(lock, handle)
    tmp.replace(lock_path)


def run_monitor(pane_id, config):
    """Detached per-pane watchdog: relaunch the agent whenever it's dead.

    Spawned detached (start_new_session), so no os.setsid() here. This is the
    SINGLE relauncher for its pane — hooks only spawn it, never relaunch.
    """
    write_lock(pane_id, time.time())
    last_relaunch = 0.0
    pane_missing_since = None
    first_seen = None
    saw_alive = False
    log(f"monitor started pane={pane_id} pid={os.getpid()}")

    while True:
        if is_stop_requested(pane_id):
            log(f"monitor stopping pane={pane_id} (stop requested)")
            break

        pane = pane_get(pane_id)
        now = time.time()
        if first_seen is None:
            first_seen = now
        if pane is None:
            if pane_missing_since is None:
                pane_missing_since = now
            elif now - pane_missing_since > config["pane_gone_grace_seconds"]:
                log(f"monitor giving up pane={pane_id} (pane gone {now - pane_missing_since:.0f}s)")
                break
            time.sleep(config["poll_seconds"])
            continue
        pane_missing_since = None

        remember_session(pane)
        registry = load_registry()
        argv = resume_argv(pane, config, registry)

        status = pane.get("agent_status")
        if status in ALIVE_STATUSES:
            saw_alive = True
        # Grace: on first contact give an agent that is mid-connect a chance to
        # report alive before we declare it dead (avoids double-launch on
        # monitor spawn during a connect window).
        ready_at = first_seen + (0 if saw_alive else config["connect_grace_seconds"])
        if (
            argv
            and status not in ALIVE_STATUSES
            and now >= ready_at
            and now - last_relaunch >= config["cooldown_seconds"]
            and not pane_has_running_process(pane_id)
        ):
            log(f"relaunch pane={pane_id} status={status} agent={pane.get('agent')} argv={' '.join(argv)}")
            pane_run(pane_id, argv)
            last_relaunch = now

        write_lock(pane_id, now)
        time.sleep(config["poll_seconds"])

    cleanup_monitor(pane_id)


def cleanup_monitor(pane_id):
    for path in (monitor_lock_path(pane_id), monitor_stop_path(pane_id)):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


# --------------------------------------------------------------------------
# hooks / scans
# --------------------------------------------------------------------------

def event_pane_id():
    """Pane id from the event the hook was fired for."""
    direct = os.environ.get("HERDR_PANE_ID")
    if direct:
        return direct
    raw = os.environ.get("HERDR_PLUGIN_EVENT_JSON")
    if not raw:
        return None
    try:
        event = json.loads(raw)
    except json.JSONDecodeError:
        return None

    def search(value):
        if not isinstance(value, dict):
            return None
        if isinstance(value.get("pane_id"), str):
            return value["pane_id"]
        for nested in value.values():
            found = search(nested)
            if found:
                return found
        return None

    return search(event)


def ensure_monitor(pane):
    """Remember the pane's session and make sure a monitor covers it."""
    remember_session(pane)
    spawn_monitor(pane["pane_id"])


def resume_monitoring():
    try:
        (state_dir() / "stop-all").unlink()
    except FileNotFoundError:
        pass


def scan_all(config):
    """Scan every pane; start monitors for supervised ones."""
    resume_monitoring()
    started = 0
    for pane in pane_list():
        if pane.get("agent_session"):
            ensure_monitor(pane)
            started += 1
    # Also cover supervised panes that currently show no live agent_session
    # (server restart restored them as bare shells): the registry knows them.
    registry = load_registry()
    for pane_id in registry:
        if not monitor_pid(pane_id):
            spawn_monitor(pane_id)
            started += 1
    return started


# --------------------------------------------------------------------------
# actions
# --------------------------------------------------------------------------

def action_status(config):
    lines = []
    registry = load_registry()
    lines.append(f"Poll interval: {config['poll_seconds']}s   cooldown: {config['cooldown_seconds']}s")
    lines.append(f"Registry entries: {len(registry)}")
    for pane_id, entry in sorted(registry.items()):
        pid = monitor_pid(pane_id)
        state = "monitored" if pid else "NO MONITOR"
        lines.append(f"  {pane_id}: {entry.get('agent')} ({entry.get('kind')}) {state}")
    log_lines = tail_log(12)
    if log_lines:
        lines.append("\nRecent activity:")
        lines.extend("  " + line for line in log_lines)
    print("\n".join(lines))


def action_stop():
    (state_dir() / "stop-all").touch()
    stopped = 0
    for lock in (state_dir() / "monitors").glob("*.json"):
        try:
            with open(lock, encoding="utf-8") as handle:
                pid = json.load(handle).get("pid")
            if pid:
                os.kill(pid, signal.SIGTERM)
                stopped += 1
        except (FileNotFoundError, json.JSONDecodeError, ProcessLookupError):
            continue
    print(f"Stopping {stopped} monitor(s).")


def action_logs(config):
    print("\n".join(tail_log(80) or ["(no log yet)"]))
    if (state_dir() / "stop-all").exists():
        print("(monitoring globally stopped; run supervise-all to resume)")


# --------------------------------------------------------------------------
# logging
# --------------------------------------------------------------------------

def log(message):
    try:
        state_dir().mkdir(parents=True, exist_ok=True)
        with open(log_path(), "a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {message}\n")
    except OSError:
        pass


def tail_log(limit):
    try:
        lines = log_path().read_text(encoding="utf-8").splitlines()
        return lines[-limit:]
    except OSError:
        return []


# --------------------------------------------------------------------------
# entrypoint
# --------------------------------------------------------------------------

def main():
    config = load_config()
    command = sys.argv[1] if len(sys.argv) > 1 else ""

    if command == "startup":
        scan_all(config)
    elif command == "hook-pane":
        pane_id = event_pane_id()
        if pane_id:
            pane = pane_get(pane_id)
            if pane:
                ensure_monitor(pane)
        # Throttled full re-scan keeps supervision self-healing even if a
        # monitor process dies: any pane event is a chance to respawn it.
        sweep = state_dir() / "last-sweep"
        try:
            due = time.time() - float(sweep.read_text()) >= config["sweep_seconds"]
        except (OSError, ValueError):
            due = True
        if due:
            sweep.parent.mkdir(parents=True, exist_ok=True)
            sweep.write_text(str(time.time()))
            scan_all(config)
    elif command == "supervise-all":
        started = scan_all(config)
        print(f"Scanned panes; monitors ensured for {started} supervised pane(s).")
    elif command == "monitor" and len(sys.argv) >= 3:
        run_monitor(sys.argv[2], config)
    elif command == "status":
        action_status(config)
    elif command == "stop":
        action_stop()
    elif command == "logs":
        action_logs(config)
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
