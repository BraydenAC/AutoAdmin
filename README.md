# AutoAdmin

An LLM agent that installs, runs, and maintains a Minecraft server on a Linux
host, controlled from a Discord channel. Ask it in plain language
("@AutoAdmin set up a 1.21.1 Fabric superflat server") and it downloads the
right Java, installs the server, configures the world, and keeps it running.

**Status:** work in progress. See [PLAN.md](PLAN.md) for the design and phases.

| Phase | |
|---|---|
| Core: permissions, audit log, budget, agent loop | done |
| Server lifecycle: vanilla / Fabric / NeoForge, Java, systemd, private console | done |
| Discord adapter | done |
| Mods (Modrinth) and world profiles, backups | done |
| Crash supervisor | done |

## How it works

- **Claude tool-use loop** with a small set of typed tools. Every tool call is
  checked against a permission store as the Discord user who sent the message;
  the model never supplies identity. Every call is audit-logged.
- **Roles:** the Discord server owner is the owner, who appoints admins in chat;
  admins grant finer permissions to other users.
- **Budget:** AI spend is tracked per user against a cap. Non-admins who use more
  than 5% are blocked until an admin unblocks them. When the cap is reached the
  bot falls back to plain `status` / `start` / `stop` commands.
- **Context discipline:** long operations (installers, server start) block in
  code and return a one-line result or a short failure excerpt. Deeper debugging
  goes to a read-only investigator sub-agent so logs never flood the main
  conversation.
- **Server process:** a `systemd --user` service with a memory cap and lower CPU
  priority. Java runtimes are private per major version; downloads go through a
  checksum-keyed cache. Nothing needs root.

## Requirements

- Linux with systemd (developed on Arch), Python 3.14, [uv](https://docs.astral.sh/uv/).
  Windows support is in progress: setup and the bot service work, the Minecraft
  server backend doesn't yet.
- An Anthropic API key
- A Discord bot application ([developer portal](https://discord.com/developers/applications))

## Setup

```sh
uv sync
uv run autoadmin setup
```

Setup walks you through everything and checks each step as it goes:

1. **Anthropic API key**: checked against the API, including access to the
   configured models.
2. **Discord bot token**: checked, along with the Message Content intent.
3. **Server and channel**: gives you the invite link, then lets you pick the
   server and channel from a list and checks the bot's permissions there.
4. **Background service**: a systemd user service plus `loginctl enable-linger`
   on Linux. On Windows, a scheduled task that runs either while you're logged
   in or always (asks for admin once).
5. **Firewall** (optional): opens the game port in ufw, firewalld, or Windows
   Firewall.

Secrets go in `.env`, readable only by you, and the IDs go in `config.toml`.
Re-run setup at any time to rotate keys or change the channel; existing values
can be kept.

```sh
uv run autoadmin cli        # terminal stand-in for Discord, for testing
uv run autoadmin discord    # run the bot in the foreground
```

Accepting the [Minecraft EULA](https://aka.ms/MinecraftEULA) is always done by
a human in chat; the agent cannot accept it on its own.

## Tests

```sh
uv run pytest
```

Tests use a fake Claude client and fake Discord objects; they make no API calls.

## License

MIT
