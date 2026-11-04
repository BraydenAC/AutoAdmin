# AutoAdmin

An LLM agent that installs, runs, and maintains a Minecraft server on a Linux
host, controlled from a Discord channel. Ask it in plain language
("example: @AutoAdmin set up a 1.21.1 Fabric superflat server") and it downloads the
right Java, installs the server, configures the world, and keeps it running.

## How it works

- **Claude tool-use loop** with a small set of typed tools. Every tool call is
  checked against a permission store as the Discord user who sent the message;
  the model never supplies identity. Every call is audit-logged.
- **Roles:** the Discord server owner is the owner, who appoints admins in chat(example: @bot, make @username an admin");
  admins have expanded permissions and may grant finer permissions to other users.
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

- Linux with systemd (developed on Arch) or Windows 10/11, Python 3.14,
  [uv](https://docs.astral.sh/uv/). x64, or aarch64 where Temurin has Java builds.
- An Anthropic API key
- A Discord account. Setup walks you through creating your own bot (about a minute); each
  install needs its own, so don't share a bot token.

## Setup

```sh
cd /path/to/where/you/want/it
git clone https://github.com/BraydenAC/AutoAdmin.git
cd autoadmin
uv sync
uv run autoadmin setup
```

Setup walks you through everything and checks each step as it goes:

1. **Anthropic API key**: checked against the API, including access to the
   configured models.
2. **Discord bot**: opens the developer portal and tells you what to click, checks the
   token, then opens the bot's settings page to turn on the Message Content intent.
3. **Server and channel**: opens the invite link, then lets you pick the
   server and channel from a list and checks the bot's permissions there.
4. **Background service**(optional, reversible): a systemd user service plus `loginctl enable-linger`
   on Linux. On Windows, a scheduled task that runs either while you're logged
   in or always (asks for admin once).
5. **Firewall** (optional): opens the game port in ufw, firewalld, or Windows
   Firewall.

Secrets go in `.env`, readable only by you, and the server+channel IDs go in `config.toml`.
Re-run setup at any time to rotate keys or change the channel; existing values
can be kept.

```sh
uv run autoadmin cli        # terminal stand-in for Discord, for testing
uv run autoadmin discord    # run the bot in the foreground
```

Accepting the [Minecraft EULA](https://aka.ms/MinecraftEULA) is always done by
a human in chat; the agent cannot accept it on its own.

## Uninstall

```sh
uv run autoadmin uninstall
```

This stops the bot and the server and removes their background service. It also
removes the bot from your Discord server, and closes the firewall port and turns
off lingering if setup turned them on. Then it deletes all worlds, backups, logs,
your keys and `config.toml`. Before deleting anything it offers to save each
world as a zip in your Downloads folder; if a copy fails, nothing is deleted.
Finally it prints the AutoAdmin code folder's path so you can delete that too.

To bring a saved world back, on this computer or another one, put its zip in
that computer's Downloads folder and give the bot its exact file name, e.g.
"@AutoAdmin import AutoAdmin-survival-20261104-111500.zip". Only the full
name works, and the bot is blind to the rest of the contents of Downloads. Importing is limited
to the owner and admins unless an admin grants `worlds.import`. It installs
the same Minecraft version and loader, then restores the save, settings and
mods. The EULA has to be accepted again on the new install.

## Tests

```sh
uv run pytest
```

Tests use a fake Claude client and fake Discord objects; they make no API calls.

## License

MIT
