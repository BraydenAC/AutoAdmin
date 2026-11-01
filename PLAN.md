# AutoAdmin — Plan

An LLM agent that installs, configures, runs, and maintains a single modded Minecraft
server on a Linux or Windows host, with a Discord channel as its human interface.

## Scope

**MVP**
- One server process at a time; multiple **world profiles**, switchable at start-up.
- Loaders: **Fabric** and **NeoForge** (plus vanilla, which comes free).
- Discord is the only human interface.
- World generation: seed, world type (normal/flat/large biomes/amplified), gamemode,
  difficulty, hardcore, and similar standard `server.properties` options.
- Self-healing: crash detection, log analysis, restart, escalation to admins.
- Console command execution while the server is running.
- Hard budget cap ($10 to start) with a no-AI fallback mode.

**Post-MVP**
- In-game chat interface (trigger prefix, replies via `tellraw`).
- Tailnet (Tailscale) management for network exposure.

**Out of scope**
- Scheduled restarts. Start/stop/restart happen only on request or by agent decision.
- World pre-generation, CurseForge integration (maybe later).

## Architecture

```
Discord adapter ──► Router/Sessions ──► Agent loop ──► Tool registry ──► Subsystems
 (discord.py)        (conversations,     (Claude        (permission       (server ctl, install,
                      requester binding)  tool-use)      gate + audit)     mods, worlds, console…)
                                                               ▲
                       Supervisor (crash watcher) ─────────────┘  (system principal)
```

### Components

| Module | Responsibility |
|---|---|
| `adapters/discord.py` | Gateway connection, mention/reply detection, attachment intake, message posting |
| `sessions.py` | Conversation tracking via reply chains; requester binding per turn |
| `agent/loop.py` | Claude tool-use loop, system prompt, context assembly |
| `agent/budget.py` | Token/cost accounting, 80% warning, cap → no-AI mode |
| `auth.py` | Principals, permission store, deterministic checks |
| `tools/` | Typed tools; each declares its required permission |
| `server/process.py` | systemd `--user` unit control, status |
| `server/launcher.py` | Runs the JVM, owns its console; private socket for commands |
| `server/supervisor.py` | Watches process + logs, detects crashes, triggers agent |
| `install/` | Mojang manifest, Fabric/NeoForge installers, Java (Temurin) provisioning |
| `mods/` | Modrinth API, URL download, attachment intake, hash verification |
| `worlds.py` | World profiles, switching, backups |
| `audit.py` | Append-only log of every tool call: principal, args, result |

## Conversations (Discord)

- The bot listens to **one dedicated channel** in **one guild**.
- **@mention** to the bot starts a new conversation.
- **Reply** to any bot message in a conversation continues it. Anyone may reply and join.
- If the bot posts two messages in a row (e.g. a progress update then a result), the second
  one @pings the involved user(s) so they get notified.
- Bot messages are short and conversational. Long output (logs, mod lists) goes in
  an attachment or a compact embed.
- Long operations post a brief "working on it" message, then the result.

## Authorization

The model **never supplies identity**. The harness binds a principal to each agent turn:

- A turn triggered by a Discord message runs as **that message's author** (Discord user ID).
  In a multi-user conversation, each message is checked against its own author.
- A turn triggered by the supervisor runs as the **`system` principal**.

Every tool call is wrapped by the harness: `(principal, tool, args)` → permission check
against `runtime/permissions.json` → allow or refuse → audit log. Refusals are returned
to the model as a tool result so it can explain briefly to the user.

### Roles and bootstrap

1. On first run, the **guild owner** (read from the Discord API) is `owner`. They have every permission,
   including granting/revoking admin.
2. During setup the owner names admins in natural language with @pings. The agent calls
   `grant_admin(user_id)`; the harness extracts IDs only from actual mention entities in
   the owner's message, so the model can't conjure an ID.
3. **Admins** have all permissions except `admins.manage` and `budget.manage`
   (the owner may grant `budget.manage` to specific admins). They grant
   granular permissions to other users case by case.
4. Everyone else: `chat` only, by default.

### Permission catalog (draft)

| Permission | Covers |
|---|---|
| `chat` | Talk to the bot, read-only info (status, online players, mod list, world list) |
| `server.control` | Start, stop, restart |
| `server.console` | Run arbitrary console commands |
| `players.manage` | Whitelist, op/deop, kick, ban |
| `mods.manage` | Install, remove, update mods (incl. uploading jars) |
| `config.edit` | `server.properties` and mod configs |
| `worlds.manage` | Create and switch world profiles |
| `backups.create` | Make backups |
| `backups.restore` | Restore a backup (destructive, needs confirmation) |
| `worlds.delete` | Delete a world (destructive, needs confirmation) |
| `perms.grant` | Grant/revoke the above to non-admins (admin+) |
| `budget.manage` | Raise/lower the budget cap via chat. Owner by default; owner can grant to admins |
| `admins.manage` | Owner only |

**Console commands that duplicate a permission are checked against that permission.**
For example, `op`/`whitelist`/`ban` need `players.manage` and `stop` needs `server.control`,
so `server.console` can't be used to bypass finer-grained grants.

**Destructive actions** (`backups.restore`, `worlds.delete`, world reset) need an
explicit confirmation reply from a human holding the permission. The model can't
confirm on its own.

### `system` principal

This principal is used for autonomous crash handling. It is allowed to read logs, restart the server,
disable (move aside) a single mod, create backups, run read-only console commands, and notify
admins. It is never allowed to restore, delete, or change permissions.

## Server lifecycle

### Install flow (from the example)
1. The user requests a version, loader, mods, and world options.
2. Resolve the version via Mojang's version manifest; read the required Java major version.
3. Provision Temurin JDK into `runtime/java/<major>/` if missing (no sudo).
4. Run the Fabric or NeoForge server installer for that version.
5. **EULA**: post the link and ask the requester to confirm. Record who accepted and when, then
   write `eula.txt`.
6. Write `server.properties` from the world options. RCON and query stay off (forced on every start).
7. Install mods (see below).
8. Write and enable the systemd user unit, then start. Watch the log for `Done (` to confirm startup.
9. Report back.

### Process management
- Server runs as `systemd --user` unit `autoadmin-mc.service` (needs `loginctl enable-linger`).
- The bot runs as its own unit, `autoadmin-bot.service`, so either can restart independently.
- Console access goes through the **launcher**, which owns the server's stdin; the bot sends
  commands over a private Unix socket in `$XDG_RUNTIME_DIR` (no network port). A command's
  output is the console lines that follow it; saves wait for `Saved the game`. `systemctl
  stop` (KillMode=mixed) makes the launcher send `stop`, so the world is saved; Java's exit
  status passes through, so a killed JVM still looks like a crash.

### Crash handling
1. The supervisor detects an unexpected exit, or a startup that fails or times out.
2. Code pre-filters `logs/latest.log` and the newest `crash-reports/` file into an incident
   (exception lines, `Caused by` chains, implicated mod ids, a few lines of context).
3. The orchestrator (as `system`) hands the incident to the **investigator sub-agent**
   (see Context discipline). The investigator returns a report with a verdict: `understood`
   (with a cause) or `unknown`.
4. The orchestrator decides what to do with the report. If `understood`, it applies a
   reversible fix (e.g. disable the mod, revert the config).
5. It **always restarts** after analysis, whatever the verdict.
6. On the next crash, the supervisor checks the **previous** verdict:
   - Previous was `unknown`: @ping all admins with a short summary and a log excerpt.
     Auto-restart stops until a human responds.
   - Previous was `understood`: go back to step 2 and analyze again, then restart.
7. Loop guards. These two cases also escalate instead of restarting:
   - The same cause is diagnosed twice in a row, which means the fix didn't work.
   - More than N crashes happen within a time window (configurable, e.g. 5 per hour).
8. The crash chain resets after a stable uptime period (configurable, e.g. 30 min).
9. Every autonomous action is posted to the channel briefly.

Implementation notes (phase 5):
- The server records a desired state: `running` after a successful start, `stopped` before
  any stop. A crash is "desired running, unit not running, no start/stop in progress".
- A clean exit (status 0) with desired `running` means someone ran `/stop` in game: the
  supervisor marks it stopped and doesn't restart.
- systemd `oom-kill` results are named in the incident ("killed for exceeding its memory cap").
- The investigator returns a `cause_key` (e.g. `mod-crash:create`, `oom`) so "same cause
  twice" is an exact comparison.
- The fix turn runs as `system`, which may only toggle mods and change settings
  (`system.crash_recovery`); it can't install, delete, or restore.
- A failed restart is treated as the next crash in the chain, so the guards still apply.
- Escalation pauses auto-restart; replying to the escalation message starts a conversation
  that already contains the incident and report. Supervision resumes once the server is
  started again.
- Auto-start: on bot startup the server is started if its desired state is `running`.
- State (crash chain, escalation) persists in `runtime/supervisor.json`.

## World profiles

Implemented as one complete server folder per world: `runtime/worlds/<name>/` holds the
install, `mods/`, configs, `server.properties`, the save, and `autoadmin.json` (version,
loader, pinned loader version, Java, heap, installed mods). `runtime/active_world` names the
world the service runs. EULA acceptance is recorded once (`runtime/eula.json`) and applied to
every world. Destructive actions (delete world, restore, delete backup) use a two-step
confirmation: same person, later message.

```
runtime/worlds/<profile>/
  profile.toml   # mc_version, loader, loader_version, mods (modrinth ids + hashes), properties
  world/         # save data
```

- Switching: stop server → back up current → swap active profile (version, loader, and
  mods installed as needed; mods cached in `runtime/cache/`) → start.
- Selecting a world at start-up: "start the server with the `skyblock` world".

## Mods

Client-only mods are refused: Modrinth's `server_side: unsupported`, or `"environment":
"client"` in a Fabric jar (NeoForge jars have no reliable marker). Releases are preferred over
betas. Required dependencies are installed and tracked (`required_by`), and removing a mod
something else needs is refused. Disabling moves the jar to `mods/disabled/`.

Resolution order:
1. **Modrinth API**, split between the agent and code:
   - The **agent** searches (`search_mods`, compact top results) and picks the exact slug.
   - **`install_mods(slugs)`** does the rest in code: picks the version matching the game version
     and loader, resolves required dependencies, downloads, and verifies SHA-512. It returns a
     per-mod report saying what was installed (with version and dependencies) or exactly why it failed.
2. **Direct URL** given by the user (curl, then check that it's a valid jar with `fabric.mod.json` /
   `META-INF/neoforge.mods.toml`).
3. **Ask for upload**. The user attaches the jar in the channel. Only accepted from users
   with `mods.manage`.

Each install is recorded in the profile, with a backup taken first.

## Context discipline

Only what's needed for a decision enters the main agent's context.

- **Large data never enters the context.** Version manifests, Modrinth listings, and raw logs
  are handled in code. Tools return compact results, with a hard cap of about 2k tokens.
- **Long operations block in code until a definite result.** For installers, that's the process
  exit code, plus the success or failure line where one exists (exact strings to be checked
  against the real Fabric/NeoForge installers). For server start, it's the `Done (` line in
  `latest.log`, or process exit, or a timeout.
  - On success: a one-line "continue" result.
  - On failure: the error or stack trace plus about 10 surrounding lines, and an incident id.
- **Investigation happens in a sub-agent.** `investigate(incident_id, question)` starts a
  separate Claude conversation with read-only tools: read or grep log slices, list mods,
  read mod metadata and configs. It returns a short structured report (cause, confidence,
  evidence, suggested actions, `understood` yes/no), and its working context is then thrown
  away. The investigator only reports; the orchestrating agent decides what to act on.
  Its cost is charged to whoever triggered the turn. Model: `claude-haiku-4-5` with a
  4k-token thinking budget (Haiku has no effort setting); raise to 8k if reports are shallow.
- **Agent-side mod search.** The agent resolves names to slugs; everything after that is code.
- **Human waits (T1):** not mitigated; the cost is low.
- **Machine resources** (decided in phase 2):
  - Java: private Temurin **JRE** per major in `runtime/java/<major>/` (~52 MB download, ~160 MB disk).
  - Heap: 6 GB default, adjustable via `set_server_memory`; refused if it would leave the host
    under 4 GB.
  - systemd guards: `MemoryMax` = heap + 1.5 GB, `Nice=5`.
  - Downloads: shared cache in `runtime/cache/` keyed by checksum, 3 retries with backoff.
  - Backups (phase 4): world data, mods, config, server.properties and the profile, as
    `.tar.zst` in `runtime/backups/<world>/`. Server jars/libraries are excluded and rebuilt
    from the profile when restoring a deleted world. Automatic backups (before mod changes and
    restores) keep the newest **3** per world; the owner can change this from chat. Manual
    backups are kept until deleted. Live backups pause world saving (`save-off`/`save-all
    flush`/`save-on`).
- **Verified signals** (from real runs): NeoForge installer exits 0 and prints
  `The server installed successfully`; on failure it exits 1 and prints
  `There was an error during installation` after the stack trace. Fabric needs no installer
  (meta API serves a launcher jar). Server ready = `]: Done (N.NNNs)!`. EULA stop =
  `You need to agree to the EULA` (exit code 0, so the exit code alone is not success).
- **Tool limits:** at most 20 tools may be `strict`. Only tools with arguments are strict,
  and a test enforces the limit.

## Budget

- Track input/output/cache tokens per API call. Prices are kept in config, not hardcoded.
- Spend persists in `state.db`.
- At **80%** of the cap, ping the admins with a warning.
- At **100%**, switch to **no-AI mode**: the bot answers only `status`, `start`, `stop`
  (permission-checked, deterministic parsing). The supervisor can't analyze crashes in this mode, so every
  crash counts as `unknown`: it restarts once, and the next crash pings the admins. Raising the cap (in config, or via chat by a holder of `budget.manage`) restores full mode.

### Per-user spend limit
- The cost of each agent turn is attributed to that turn's principal. Supervisor turns are
  charged to `system`.
- **Non-admin users** who spend more than **5% of the current cap** are **blocked**: all
  their permissions are suspended, including `chat`. The bot tells them once, then ignores them.
- The block is sticky. It lasts until an admin (`perms.grant`) tells the agent to unblock
  them. Unblocking resets that user's spend counter.
- Owner and admins are exempt.
- Model: `claude-sonnet-5-5` at `medium` effort (configurable).
  Server-side refusal fallback (`fallbacks: "default"`) is on; fallback attempts are charged at
  their own model's price.

## Platforms

Linux (systemd) and Windows (x64; arm64 where Temurin has builds). The OS is detected and
configured for during first-time setup.

**Launcher.** A small Python process, shipped with AutoAdmin, sits between the service
manager and Java:
- It starts Java and owns its stdin. The bot sends console commands to it over a private
  local channel (Unix socket on Linux, named pipe on Windows), which replaces RCON and
  closes the `0.0.0.0:25575` exposure. A command's output is the console lines that
  follow it within a short window.
- It writes the server's exit code to a file, so the supervisor can classify an exit even
  after a bot restart.

**`ProcessBackend`** interface (start, stop, state, stats, send command):
- **Linux:** systemd `--user` unit runs the launcher; `MemoryMax` and `Nice` as before.
- **Windows:** the bot spawns the launcher detached so it outlives the bot; memory cap via a
  Job Object, below-normal priority class.

**Other platform-specific pieces:** Temurin download by OS/arch (`.zip` + `java.exe` on
Windows), host RAM/CPU via `psutil` instead of `/proc`. Backups (stdlib zstd), Discord, the
agent, and mod/NeoForge installs are already portable.

**Bot startup on Windows** (setup asks which; a Task Scheduler task either way, with
restart-on-failure and no run-time limit):
- *Logged-in only*: runs at the user's logon with their session. No admin needed.
- *Always on*: runs at boot whether or not anyone is logged in, as the user via S4U. Needs
  admin once to register; no password is stored. S4U can reach the internet but not network
  shares. The Linux equivalent is `loginctl enable-linger`.
- The bot runs under `pythonw` (no console window) and logs to `runtime/logs/bot.log`.

**Firewall** (setup offers it, opt-in): detect ufw / firewalld / Windows Defender Firewall
and open only the game port (25565/tcp). Needs sudo or admin for that one step; if declined,
setup prints the command to run by hand.

## First-time setup (`autoadmin setup`)

Interactive and re-runnable. Every existing value can be kept, and kept values are re-checked.

1. **Host**: detect the OS, CPU architecture, and systemd. Stop on unsupported hosts.
   Create `config.toml` from the example if missing.
2. **Anthropic key** (hidden input): validated with free model lookups, which also confirm
   the configured agent and investigator models are available. If the API is unreachable,
   offer to save without checking.
3. **Discord token** (hidden input; portal instructions shown first time): validated via
   `/users/@me`. Then the Message Content intent is read from the application flags;
   if it's off, wait while the user turns it on.
4. **Invite**: the link is pre-filled with the bot's client id and permissions 101376. Wait
   until the bot is in a server.
5. **Server and channel**: pick from lists, with no IDs to copy. The bot's effective channel
   permissions (roles + overwrites) are computed, and missing ones are named.
6. **Store**: secrets go in `.env`, created owner-only (`chmod 600`, or an ACL for the user
   alone on Windows) and saved after each validated secret. IDs go in `config.toml`.
7. **Background service**, **linger** (Linux), **firewall** (opt-in), as above.

## Directory layout

```
AutoAdmin/
  autoadmin/            # package source
  config.toml           # guild/channel IDs, model, budget cap, paths (token via env/secret file)
  pyproject.toml        # managed with uv
  runtime/              # the jail; all agent file ops are confined here
    java/ server/ worlds/ backups/ cache/ downloads/
    permissions.json  state.db  audit.log
```

## Phases

1. ✅ **Core**: config, auth + permission store, tool registry w/ permission gate + audit,
   agent loop, budget tracking, CLI harness (fake Discord) for testing.
2. ✅ **Server lifecycle**: manifest/Java provisioning, Fabric + NeoForge install, EULA,
   properties, systemd unit, RCON, start/stop/status.
3. ✅ **Discord adapter**: ready message, mention/reply conversations, double-message ping,
   attachments, owner bootstrap + admin grants.
4. ✅ **Mods + worlds**: Modrinth, fallback chain, profiles, switching, backups.
5. ✅ **Supervisor**: crash detection, analysis turn, escalation, no-AI mode.
6. **Hardening**: rate limits, destructive-action confirmations, end-to-end test on a real server.
   ✅ **Console exposure**: launcher process + `ProcessBackend` (systemd backend), RCON removed.
   **Windows backend**: `ProcessBackend` for Windows on the same launcher (see Platforms).
   ✅ **Installer**: `autoadmin setup` (see First-time setup).
7. *(post-MVP)* In-game chat adapter; Tailnet management.

## Open items
- Conversations are in memory only; a bot restart forgets them (phase 6).
