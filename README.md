# panos-mcp

An MCP (Model Context Protocol) server for Palo Alto Networks PAN-OS firewalls and
Panorama, built on the PAN-OS XML API. It is a single Python file with no dependencies
outside the standard library.

## Why

AI agents such as Claude Code are good at the tedious parts of firewall work: reading
session tables, digging through traffic logs, checking which rule matches a flow, comparing
the candidate config against running. They should not be trusted with unrestricted write
access or with your admin password. This server gives an agent a narrow, predictable set of
tools:

- it is read-only unless you explicitly start it with writes enabled;
- it has a backup tool and a pending-changes diff meant to be used before any commit;
- it supports partial commits, so the agent only pushes its own changes;
- the API key is obtained from the terminal, never through the chat.

## Tools

| Tool | What it does | Writes |
|------|--------------|--------|
| `panos_list_devices` | List configured PAN-OS/Panorama devices and the server mode (read-only or write-enabled). | no |
| `panos_system_info` | `show system info`: model, serial number, PAN-OS/Apps/Threat versions, uptime. | no |
| `panos_op` | Run an operational command (`type=op`). Accepts XML or a simple CLI string (`show system resources`). Optional `target` serial to forward through Panorama. | partly* |
| `panos_config` | Configuration by xpath (`type=config`). `get` reads the candidate, `show` reads running. `set`/`edit`/`delete`/`rename`/`move` need writes enabled. | set/edit/delete/rename/move |
| `panos_logs` | Query logs: traffic, threat, url, wildfire, data, system, config, hipmatch, globalprotect, iptag, auth, decryption. PAN-OS filter syntax in `query`. Returns key fields only unless `full=true`. | no |
| `panos_log_result` | Fetch a log query result by `job_id` when `panos_logs` gave up waiting. | no |
| `panos_panorama_devices` | Panorama only: `show devices all` - managed firewalls, versions, HA and connection state. | no |
| `panos_backup_config` | Export the running config to a local XML file. | no (local file) |
| `panos_pending_changes` | Unified diff of candidate vs running (computed locally, since `show config diff` is not available over the XML API). | no |
| `panos_commit` | Commit the candidate and wait for the job. `only_admins` makes it a partial commit. | yes |

\* In read-only mode `panos_op` accepts only `show ...` and `test ...` commands. Anything else
(`request ...`, `clear ...`, `debug ...`) needs `PANOS_ALLOW_WRITE=true`. Still give the API
key an admin role that matches what you want the agent to be able to do.

## Safety model

- **Read-only by default.** `panos_config` with a write action and `panos_commit` refuse to
  run unless the server was started with `PANOS_ALLOW_WRITE=true`. `panos_list_devices`
  shows the current mode.
- **Backup first.** `panos_backup_config` saves the running config to
  `~/.panos-mcp/backups/<device>-<timestamp>.xml`. The tool description tells the agent to
  call it before any change.
- **Review before commit.** `panos_pending_changes` shows exactly what a commit would push.
- **Partial commit.** `panos_commit` with `only_admins: ["mcp-api"]` commits only changes
  made by that admin, leaving other admins' uncommitted work untouched. Use a dedicated admin
  account for the agent so this works cleanly.
- **Secrets stay out of the chat.** The API key is generated with
  `python3 panos_mcp.py keygen`, which asks for the password via `getpass` in your terminal.
  The key is stored in the config file (mode 0600) and sent in the POST body, not the URL.
  None of the tools return the key.

## Requirements

Python 3.10 or newer. No packages to install.

## Setup

1. Create a dedicated admin on the firewall (for example `mcp-api`) with an admin role that
   allows the XML API. For read-only use, a read-only role is enough.

2. Get an API key and add the device to the config:

   ```sh
   python3 panos_mcp.py keygen --host 192.0.2.10 --user mcp-api --name fw1 --default
   ```

   You will be prompted for the password. Use `--kind panorama` for Panorama and
   `--verify-ssl` if the device has a certificate your system trusts. A `--password` flag
   exists, but it leaves the password in shell history; prefer the prompt.

3. Check it:

   ```sh
   python3 panos_mcp.py list
   python3 panos_mcp.py test fw1
   ```

### Config file

Default location: `~/.panos-mcp/config.json` (override with `PANOS_MCP_CONFIG`).
See `config.example.json`:

```json
{
  "devices": [
    {"name": "fw1", "host": "192.0.2.10", "api_key": "REPLACE_WITH_API_KEY",
     "kind": "ngfw", "verify_ssl": false, "default": true},
    {"name": "panorama", "host": "192.0.2.20", "api_key": "REPLACE_WITH_API_KEY",
     "kind": "panorama", "verify_ssl": false}
  ]
}
```

### Environment variables

| Variable | Default | Meaning |
|----------|---------|---------|
| `PANOS_MCP_CONFIG` | `~/.panos-mcp/config.json` | Config file path |
| `PANOS_ALLOW_WRITE` | unset (read-only) | `true`/`1`/`yes`/`on` enables config writes and commit |
| `PANOS_BACKUP_DIR` | `<config dir>/backups` | Where `panos_backup_config` writes files |
| `PANOS_MAX_CHARS` | `40000` | Default output truncation limit |
| `PANOS_TIMEOUT` | `60` | HTTP timeout, seconds |
| `PANOS_HOST`, `PANOS_API_KEY` | | Add one device from the environment instead of the config file |
| `PANOS_NAME`, `PANOS_KIND`, `PANOS_VERIFY_SSL` | | Optional extras for the environment device |

## Register in Claude Code

Read-only:

```sh
claude mcp add panos -- python3 /path/to/panos_mcp.py
```

With writes enabled:

```sh
claude mcp add panos -e PANOS_ALLOW_WRITE=true -- python3 /path/to/panos_mcp.py
```

Any other MCP client that speaks stdio works the same way: run `python3 panos_mcp.py`
with no arguments.

## Command line

```
python3 panos_mcp.py                 stdio MCP server
python3 panos_mcp.py keygen ...      get an API key and add the device to the config
python3 panos_mcp.py list            list configured devices
python3 panos_mcp.py test [name]     show system info
python3 panos_mcp.py diff [name]     uncommitted candidate changes
```

## Example prompts

- "Show HA state and interface status on fw1."
- "Why can't 10.0.0.5 reach 198.51.100.7 on tcp/443? Check the security policy match,
  the FIB lookup and the last traffic log entries."
- "List denied traffic from 10.0.0.0/24 in the last hour."
- "Which GlobalProtect users failed to authenticate today?"
- "Back up fw1, add an address object `web-01` 10.0.0.20/32, show me the pending diff and
  wait for my OK before committing only your own changes."

## Limitations

- PAN-OS does not allow `ping` or `traceroute` over the XML API. Check reachability with
  `test routing fib-lookup`, `test security-policy-match`, traffic logs and the session table.
- Only simple CLI strings are converted to XML; commands with arguments should be passed
  as XML.
- `panos_pending_changes` downloads the full running and candidate configs, which can be
  slow on large configs.
- Long log queries are polled for about 150 seconds; after that use `panos_log_result`.
- Panorama commit-all / push to device groups is not implemented; `panos_commit` commits
  locally on the device it targets.
- TLS certificate verification is off unless `verify_ssl` is set, because most firewalls
  run with a self-signed management certificate.
- The API key is stored in plain text in the config file. Protect the file and rotate the
  key if it leaks.

## License

MIT, see `LICENSE`.
