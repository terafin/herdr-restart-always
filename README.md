# herdr-restart-always

Supervise herdr agent panes and **always restart whatever the pane is running**
whenever the agent dies. Closes the gap herdr leaves open: herdr's native agent
session restore only fires on a *server restart*; nothing relaunches an agent
whose process dies inside a *running* server.

Agent-agnostic — any agent herdr can detect (claude, hermes, codex, pi,
opencode, ...) is relaunched into its previous session.

## Why it exists

Herdr natively covers server restart (agent session restore). It has **no**
mechanism for a dead agent in a live server. This plugin is that mechanism:

* a detached **monitor process per pane** polls `herdr pane get`;
* when a supervised pane's `agent_status` turns `unknown` (dead) and the pane
  is back to an idle shell, the monitor re-runs the agent's **resume command**
  in that pane (`herdr pane run`);
* the plugin's own **registry** (written while the agent was last alive)
  survives restarts, so even a server restart that restored the pane as a bare
  shell (snapshot saved while the agent was dead) still knows what to resume.

## Install

```bash
herdr plugin link ~/herdr-restart-always
herdr plugin action invoke herdr-restart-always.supervise-all
```

If the server is already running when you link, the action is required once
(hooks don't fire for a freshly-linked plugin). `startup` runs the same scan on
every server start.

**Required config** — set this in herdr's `config.toml` on every host running
the plugin, so herdr's native restore doesn't race the plugin at server start:

```toml
[session]
resume_agents_on_restore = false
```

## How it works

| Surface | Role |
| --- | --- |
| `[[startup]]` | scan all panes, start a monitor for each supervised pane |
| `[[events]] pane.agent_detected` | ensure a monitor exists for that pane; refresh registry |
| `[[events]] pane.agent_status_changed` | same (death usually surfaces here) |
| `[[events]] pane.exited` | same (pane process tree died) |
| detached monitor per pane | the **single relauncher** — polls, relaunches, never races |

Monitors are detached processes keyed by a pid lock with heartbeat, so hooks
are idempotent and a crashed monitor is re-spawned by the next event or the
next server start. A `pane.exited` death is caught by the monitor's poll within
`poll_seconds`.

Guards against double-launch:
* `agent_status` must be `unknown` (agent not alive);
* the pane's foreground must be back to an idle shell (process-info check —
  a shell running a script is *not* idle);
* a cooldown after each relaunch covers the reconnect window.

## Configuration

`~/.config/herdr/plugins/config/herdr-restart-always/config.json` (see
`herdr plugin config-dir herdr-restart-always`):

```json
{
  "poll_seconds": 5,
  "cooldown_seconds": 15,
  "connect_grace_seconds": 10,
  "commands": {
    "claude": "claude --resume {value}"
  }
}
```

`commands` overrides the built-in resume command per agent kind (`{value}` is
the session id/path). Built-ins match herdr's native restore commands.

## Actions

* `herdr-restart-always.supervise-all` — scan all panes, ensure monitors
* `herdr-restart-always.status` — supervised panes + monitor liveness + recent log
* `herdr-restart-always.stop` — stop all monitors (run `supervise-all` to resume)
* `herdr-restart-always.logs` — recent restart activity

State lives in the plugin config dir: `registry.json` (durable resume refs),
`monitors/<pane>.json` (pid locks/heartbeat), `log.txt`.

The live pipeline — agent dies → monitor sees `agent_status` unknown + idle
shell → `pane run <resume-command>` → agent resumes the same conversation — is
what makes fleet bots self-healing without any manual rescue.
