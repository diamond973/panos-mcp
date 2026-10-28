#!/usr/bin/env python3
"""MCP server for the PAN-OS / Panorama XML API. Standard library only.

Usage:
  python panos_mcp.py                 - stdio MCP server (this is how Claude Code runs it)
  python panos_mcp.py keygen ...      - obtain an API key and add the device to the config
  python panos_mcp.py list            - list known devices
  python panos_mcp.py test [name]     - check connectivity (show system info)
  python panos_mcp.py diff [name]     - show uncommitted candidate changes

Config: ~/.panos-mcp/config.json (or the path in PANOS_MCP_CONFIG):
  {"devices": [
     {"name": "fw1", "host": "10.0.0.1", "api_key": "...",
      "kind": "ngfw", "verify_ssl": false, "default": true}
  ]}

Changing the device configuration is allowed only with PANOS_ALLOW_WRITE=true.
"""

import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.dom.minidom
import xml.etree.ElementTree as ET
from pathlib import Path

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "panos", "version": "1.0.0"}

# on Windows the standard streams default to cp1251, which breaks non-ASCII text in JSON-RPC
for _stream in (sys.stdin, sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

CONFIG_PATH = Path(
    os.environ.get("PANOS_MCP_CONFIG") or Path.home() / ".panos-mcp" / "config.json"
)
ALLOW_WRITE = (os.environ.get("PANOS_ALLOW_WRITE") or "").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
BACKUP_DIR = Path(
    os.environ.get("PANOS_BACKUP_DIR") or CONFIG_PATH.parent / "backups"
)
MAX_CHARS = int(os.environ.get("PANOS_MAX_CHARS") or 40000)
TIMEOUT = float(os.environ.get("PANOS_TIMEOUT") or 60)


class PanError(Exception):
    pass


# ---------------------------------------------------------------- config


def load_devices():
    devices = []
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception as exc:
            raise PanError(f"cannot read {CONFIG_PATH}: {exc}")
        devices = list(data.get("devices") or [])
    env_host = os.environ.get("PANOS_HOST")
    env_key = os.environ.get("PANOS_API_KEY")
    if env_host and env_key:
        devices.append(
            {
                "name": os.environ.get("PANOS_NAME") or env_host,
                "host": env_host,
                "api_key": env_key,
                "kind": os.environ.get("PANOS_KIND") or "ngfw",
                "verify_ssl": (os.environ.get("PANOS_VERIFY_SSL") or "").lower()
                in ("1", "true", "yes"),
                "default": True,
            }
        )
    return devices


def save_devices(devices):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps({"devices": devices}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    tmp.replace(CONFIG_PATH)
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass


def pick_device(name=None):
    devices = load_devices()
    if not devices:
        raise PanError(
            f"no devices configured: fill in {CONFIG_PATH} "
            "(python panos_mcp.py keygen --host ... --user ...)"
        )
    if name:
        for dev in devices:
            if str(dev.get("name", "")).lower() == name.lower() or dev.get("host") == name:
                return dev
        known = ", ".join(str(d.get("name")) for d in devices)
        raise PanError(f"device '{name}' not found. Known devices: {known}")
    for dev in devices:
        if dev.get("default"):
            return dev
    return devices[0]


# ---------------------------------------------------------------- HTTP/API


def _opener(verify_ssl):
    ctx = ssl.create_default_context()
    if not verify_ssl:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))


def api_raw(host, params, verify_ssl=False, timeout=TIMEOUT):
    """POST to /api/ so the key and cmd stay out of the URL and are not limited by its length."""
    url = f"https://{host}/api/"
    body = urllib.parse.urlencode(
        {k: v for k, v in params.items() if v is not None}
    ).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with _opener(verify_ssl).open(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", "replace")
        raise PanError(f"HTTP {exc.code} from {host}: {text[:800]}")
    except urllib.error.URLError as exc:
        raise PanError(f"cannot reach {host}: {exc.reason}")


def api(device, params, timeout=TIMEOUT):
    params = dict(params)
    params["key"] = device["api_key"]
    text = api_raw(device["host"], params, device.get("verify_ssl", False), timeout)
    return parse_response(text)


def parse_response(text):
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise PanError(f"unparseable response from device: {exc}\n{text[:800]}")
    status = root.attrib.get("status")
    if status and status != "success":
        code = root.attrib.get("code", "")
        raise PanError(f"device returned status={status} code={code}\n{pretty(root)}")
    return root


def pretty(elem):
    raw = ET.tostring(elem, encoding="unicode")
    try:
        out = xml.dom.minidom.parseString(raw).toprettyxml(indent="  ")
        return "\n".join(line for line in out.splitlines()[1:] if line.strip())
    except Exception:
        return raw


def clip(text, limit=None):
    limit = limit or MAX_CHARS
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... truncated, {len(text)} characters in total"


def result_of(root):
    res = root.find("result")
    return pretty(res) if res is not None else pretty(root)


def cli_to_xml(cmd):
    """'show system info' -> '<show><system><info/></system></show>'."""
    parts = cmd.split()
    if not parts:
        raise PanError("empty command")
    xml_str = ""
    for part in reversed(parts):
        tag = part
        if not tag.replace("-", "").replace("_", "").isalnum():
            raise PanError(
                f"'{part}' does not look like a CLI element - pass the command as XML instead"
            )
        xml_str = f"<{tag}>{xml_str}</{tag}>" if xml_str else f"<{tag}/>"
    return xml_str


def as_cmd(cmd):
    cmd = cmd.strip()
    return cmd if cmd.startswith("<") else cli_to_xml(cmd)


def wait_job(device, job_id, kind="log", tries=40, delay=1.5):
    """Wait for a job (log query or commit) and return the finished result."""
    for _ in range(tries):
        if kind == "log":
            root = api(device, {"type": "log", "action": "get", "job-id": job_id})
            status = root.findtext("./result/job/status") or ""
        else:
            root = api(
                device,
                {"type": "op", "cmd": f"<show><jobs><id>{job_id}</id></jobs></show>"},
            )
            status = root.findtext("./result/job/status") or ""
        if status.upper() in ("FIN", "FINISHED"):
            return root
        time.sleep(delay)
    raise PanError(f"job {job_id} did not finish within {int(tries * delay)} s")


# ---------------------------------------------------------------- tools

READ_ACTIONS = {"get", "show"}
WRITE_ACTIONS = {"set", "edit", "delete", "rename", "move", "override"}

DEVICE_ARG = {
    "type": "string",
    "description": "Device name from the config. Defaults to the one marked default.",
}


def t_list_devices(_args):
    devices = load_devices()
    if not devices:
        return f"No devices. Config: {CONFIG_PATH}"
    lines = [f"Config: {CONFIG_PATH}", f"Configuration writes: {'allowed' if ALLOW_WRITE else 'DISABLED (read-only)'}", ""]
    for dev in devices:
        mark = " (default)" if dev.get("default") else ""
        lines.append(
            f"- {dev.get('name')}{mark}: {dev.get('host')}, kind {dev.get('kind', 'ngfw')}, "
            f"TLS verification {'on' if dev.get('verify_ssl') else 'off'}"
        )
    return "\n".join(lines)


def t_system_info(args):
    dev = pick_device(args.get("device"))
    root = api(dev, {"type": "op", "cmd": "<show><system><info/></system></show>"})
    info = root.find("./result/system")
    if info is None:
        return result_of(root)
    keys = [
        "hostname",
        "ip-address",
        "serial",
        "model",
        "sw-version",
        "app-version",
        "threat-version",
        "av-version",
        "wildfire-version",
        "global-protect-client-package-version",
        "uptime",
        "multi-vsys",
        "operational-mode",
    ]
    lines = [f"{dev.get('name')} ({dev.get('host')})"]
    for key in keys:
        val = info.findtext(key)
        if val:
            lines.append(f"  {key}: {val}")
    return "\n".join(lines)


def t_op(args):
    dev = pick_device(args.get("device"))
    cmd = as_cmd(args["cmd"])
    params = {"type": "op", "cmd": cmd}
    if args.get("target"):
        params["target"] = args["target"]
    root = api(dev, params)
    return clip(result_of(root), args.get("max_chars"))


def t_config(args):
    dev = pick_device(args.get("device"))
    action = (args.get("action") or "get").lower()
    if action in WRITE_ACTIONS and not ALLOW_WRITE:
        raise PanError(
            f"action={action} changes the configuration, but the server is running read-only. "
            "Enable it with PANOS_ALLOW_WRITE=true."
        )
    if action not in READ_ACTIONS | WRITE_ACTIONS:
        raise PanError(f"unknown action={action}")
    params = {"type": "config", "action": action, "xpath": args["xpath"]}
    if args.get("element"):
        params["element"] = args["element"]
    if args.get("newname"):
        params["newname"] = args["newname"]
    root = api(dev, params)
    return clip(result_of(root), args.get("max_chars"))


# Fields that actually matter when troubleshooting. The rest only with full=true:
# PAN-OS returns ~90 fields per entry, and the full output floods the context for no benefit.
KEY_FIELDS = [
    "time_generated",
    "src",
    "sport",
    "dst",
    "dport",
    "proto",
    "app",
    "from",
    "to",
    "rule",
    "action",
    "srcuser",
    "inbound_if",
    "outbound_if",
    "bytes",
    "packets",
    "session_end_reason",
    "threatid",
    "severity",
    "category",
    "subtype",
    "eventid",
    "status",
    "stage",
    "error_code",
    "error",
    "auth_method",
    "portal",
    "object",
    "public_ip",
    "machinename",
    "client_os",
    "opaque",
    "admin",
    "cmd",
    "result",
    "description",
]


def format_logs(done, log_type, args):
    logs = done.find("./result/log/logs")
    if logs is None:
        return clip(result_of(done), args.get("max_chars"))
    entries = logs.findall("entry")
    if not entries:
        return f"{log_type} logs: no entries match the query."
    full = bool(args.get("full"))
    lines = [f"{log_type} logs: {len(entries)} entries (count={logs.attrib.get('count')})"]
    for entry in entries:
        values = {
            child.tag: child.text.strip()
            for child in entry
            if child.text and child.text.strip()
        }
        lines.append("---")
        if full:
            for tag, val in values.items():
                lines.append(f"  {tag}: {val}")
        else:
            for tag in KEY_FIELDS:
                if tag in values:
                    lines.append(f"  {tag}: {values[tag]}")
    return clip("\n".join(lines), args.get("max_chars"))


def t_log_result(args):
    """Fetch the result of a previously started log query by job-id."""
    dev = pick_device(args.get("device"))
    job_id = str(args["job_id"]).strip()
    root = api(dev, {"type": "log", "action": "get", "job-id": job_id})
    status = (root.findtext("./result/job/status") or "").upper()
    if status not in ("FIN", "FINISHED"):
        return f"Log query {job_id} is not ready yet (status={status or 'unknown'})."
    return format_logs(root, args.get("log_type") or "query", args)


def t_logs(args):
    dev = pick_device(args.get("device"))
    log_type = (args.get("log_type") or "traffic").lower()
    params = {
        "type": "log",
        "log-type": log_type,
        "nlogs": str(args.get("nlogs") or 20),
    }
    if args.get("query"):
        params["query"] = args["query"]
    if args.get("dir"):
        params["dir"] = args["dir"]
    root = api(dev, params)
    job_id = root.findtext("./result/job")
    if not job_id:
        return clip(result_of(root), args.get("max_chars"))
    job_id = job_id.strip()
    try:
        done = wait_job(dev, job_id, kind="log", tries=75, delay=2)
    except PanError:
        raise PanError(
            f"log query {job_id} is still running (queries over a wide range can take a while). "
            f"Fetch the result later with panos_log_result job_id={job_id}, "
            "or narrow the query by time/subnet."
        )
    return format_logs(done, log_type, args)


def t_panorama_devices(args):
    dev = pick_device(args.get("device"))
    root = api(dev, {"type": "op", "cmd": "<show><devices><all/></devices></show>"})
    entries = root.findall("./result/devices/entry")
    if not entries:
        return result_of(root)
    lines = [f"Devices managed by {dev.get('name')}: {len(entries)}"]
    for entry in entries:
        lines.append(
            "- {name} / {serial}: {model} {sw}, connected={conn}, ha={ha}".format(
                name=entry.findtext("hostname") or "?",
                serial=entry.attrib.get("name") or entry.findtext("serial") or "?",
                model=entry.findtext("model") or "?",
                sw=entry.findtext("sw-version") or "?",
                conn=entry.findtext("connected") or "?",
                ha=entry.findtext("./ha/state") or "-",
            )
        )
    return clip("\n".join(lines), args.get("max_chars"))


def t_backup_config(args):
    """Export the running config to a file as a safety net before making changes."""
    dev = pick_device(args.get("device"))
    text = api_raw(
        dev["host"],
        {"type": "export", "category": "configuration", "key": dev["api_key"]},
        dev.get("verify_ssl", False),
    )
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = BACKUP_DIR / f"{dev.get('name')}-{stamp}.xml"
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return f"Config of {dev.get('name')} saved: {path} ({len(text)} characters)"


def t_pending_changes(args):
    """Uncommitted candidate changes compared to running.

    `show config diff` is not available over the XML API ('invalid client cli'),
    so we fetch both configs and diff them ourselves.
    """
    import difflib

    dev = pick_device(args.get("device"))

    def fetch(which):
        root = api(dev, {"type": "op", "cmd": f"<show><config><{which}/></config></show>"})
        res = root.find("result")
        return pretty(res if res is not None else root).splitlines()

    running = fetch("running")
    candidate = fetch("candidate")
    diff = [
        line
        for line in difflib.unified_diff(
            running, candidate, fromfile="running", tofile="candidate", lineterm="", n=3
        )
    ]
    if not diff:
        return "Candidate matches running - no uncommitted changes."
    added = sum(1 for l in diff if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in diff if l.startswith("-") and not l.startswith("---"))
    header = f"Uncommitted changes: +{added} lines, -{removed} lines\n"
    return clip(header + "\n".join(diff), args.get("max_chars"))


def t_commit(args):
    dev = pick_device(args.get("device"))
    if not ALLOW_WRITE:
        raise PanError("commit refused: the server is read-only (PANOS_ALLOW_WRITE is not set).")
    body = ""
    if args.get("description"):
        body += f"<description>{args['description']}</description>"
    admins = args.get("only_admins")
    if admins:
        members = "".join(f"<member>{a}</member>" for a in admins)
        body += f"<partial><admin>{members}</admin></partial>"
    cmd = f"<commit>{body}</commit>"
    root = api(dev, {"type": "commit", "cmd": cmd})
    job_id = (root.findtext("./result/job") or "").strip()
    if not job_id:
        return result_of(root)
    if not args.get("wait", True):
        return f"commit started, job {job_id}"
    done = wait_job(dev, job_id, kind="op", tries=120, delay=3)
    return clip(result_of(done), args.get("max_chars"))


TOOLS = [
    {
        "name": "panos_list_devices",
        "description": "List configured PAN-OS/Panorama devices and the server mode (read-only or write-enabled).",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": t_list_devices,
    },
    {
        "name": "panos_system_info",
        "description": "show system info: model, serial number, PAN-OS/Apps/Threat versions, uptime.",
        "inputSchema": {"type": "object", "properties": {"device": DEVICE_ARG}},
        "handler": t_system_info,
    },
    {
        "name": "panos_op",
        "description": (
            "Run an operational command (type=op). cmd can be XML "
            "(<show><interface>all</interface></show>) or a CLI string ('show system resources'). "
            "Simple commands are converted automatically; for commands with arguments pass XML directly. "
            "Useful: show interface all, show session all filter ..., show routing route, "
            "show high-availability state, show arp all, show counter global, "
            "show running security-policy, test security-policy-match ..., "
            "test routing fib-lookup ..., show jobs all, request license info. "
            "On Panorama you can set target=<serial>. "
            "IMPORTANT: PAN-OS does not expose ping and traceroute over the XML API "
            "('not available to xmlapi client') - check reachability with "
            "test routing fib-lookup, traffic logs and the session table."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "cmd": {"type": "string", "description": "XML or a CLI string."},
                "device": DEVICE_ARG,
                "target": {
                    "type": "string",
                    "description": "Device serial number - forwards the command through Panorama.",
                },
                "max_chars": {"type": "integer"},
            },
            "required": ["cmd"],
        },
        "handler": t_op,
    },
    {
        "name": "panos_config",
        "description": (
            "Work with the configuration by xpath (type=config). action=get reads the candidate, show reads running. "
            "Example xpath: /config/devices/entry/vsys/entry[@name='vsys1']/rulebase/security/rules. "
            "set/edit/delete only work when the server is started with PANOS_ALLOW_WRITE=true."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "xpath": {"type": "string"},
                "action": {
                    "type": "string",
                    "enum": ["get", "show", "set", "edit", "delete", "rename", "move"],
                },
                "element": {"type": "string", "description": "XML element for set/edit."},
                "newname": {"type": "string", "description": "For rename."},
                "device": DEVICE_ARG,
                "max_chars": {"type": "integer"},
            },
            "required": ["xpath"],
        },
        "handler": t_config,
    },
    {
        "name": "panos_logs",
        "description": (
            "Query logs (type=log): traffic, threat, url, wildfire, data, system, config, hipmatch, "
            "globalprotect, iptag, auth, decryption. query uses PAN-OS filter syntax, for example "
            "\"(addr.src in 10.0.0.5) and (action eq deny)\"."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "log_type": {"type": "string"},
                "query": {"type": "string"},
                "nlogs": {"type": "integer", "description": "Number of entries, default 20."},
                "dir": {"type": "string", "enum": ["backward", "forward"]},
                "full": {
                    "type": "boolean",
                    "description": "All ~90 fields per entry instead of the key ones. Default false.",
                },
                "device": DEVICE_ARG,
                "max_chars": {"type": "integer"},
            },
        },
        "handler": t_logs,
    },
    {
        "name": "panos_log_result",
        "description": "Fetch a log query result by job_id when panos_logs gave up waiting for it.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
                "log_type": {"type": "string"},
                "full": {"type": "boolean"},
                "device": DEVICE_ARG,
                "max_chars": {"type": "integer"},
            },
            "required": ["job_id"],
        },
        "handler": t_log_result,
    },
    {
        "name": "panos_panorama_devices",
        "description": "Panorama only: show devices all - managed firewalls, versions, HA state and connection status.",
        "inputSchema": {
            "type": "object",
            "properties": {"device": DEVICE_ARG, "max_chars": {"type": "integer"}},
        },
        "handler": t_panorama_devices,
    },
    {
        "name": "panos_backup_config",
        "description": (
            "Export the device running configuration to a local XML file "
            f"({BACKUP_DIR}). Do this BEFORE any change - it is your rollback."
        ),
        "inputSchema": {"type": "object", "properties": {"device": DEVICE_ARG}},
        "handler": t_backup_config,
    },
    {
        "name": "panos_pending_changes",
        "description": "Uncommitted candidate changes versus running. Review before commit.",
        "inputSchema": {
            "type": "object",
            "properties": {"device": DEVICE_ARG, "max_chars": {"type": "integer"}},
        },
        "handler": t_pending_changes,
    },
    {
        "name": "panos_commit",
        "description": (
            "Commit the candidate configuration and wait for the job. Only with PANOS_ALLOW_WRITE=true. "
            "only_admins makes it a partial commit: push only these admins' changes without touching "
            "other admins' uncommitted changes."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "description": {"type": "string"},
                "only_admins": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Admin usernames for a partial commit, for example ['mcp-api'].",
                },
                "wait": {"type": "boolean"},
                "device": DEVICE_ARG,
                "max_chars": {"type": "integer"},
            },
        },
        "handler": t_commit,
    },
]

HANDLERS = {tool["name"]: tool.pop("handler") for tool in TOOLS}


# ---------------------------------------------------------------- MCP stdio


def send(msg):
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def log(msg):
    sys.stderr.write(f"[panos-mcp] {msg}\n")
    sys.stderr.flush()


def handle(req):
    method = req.get("method")
    req_id = req.get("id")
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            },
        }
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = req.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        handler = HANDLERS.get(name)
        if not handler:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"no such tool: {name}"},
            }
        try:
            text = handler(args)
            is_error = False
        except PanError as exc:
            text, is_error = str(exc), True
        except Exception as exc:  # keep the whole server from crashing
            text, is_error = f"{type(exc).__name__}: {exc}", True
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"content": [{"type": "text", "text": text}], "isError": is_error},
        }
    if req_id is None:
        return None
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"method not supported: {method}"},
    }


def serve():
    log(f"started, config {CONFIG_PATH}, writes {'allowed' if ALLOW_WRITE else 'disabled'}")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as exc:
            log(f"bad JSON: {exc}")
            continue
        resp = handle(req)
        if resp is not None:
            send(resp)


# ---------------------------------------------------------------- CLI


def cli_keygen(argv):
    import argparse
    import getpass

    ap = argparse.ArgumentParser(prog="panos_mcp.py keygen")
    ap.add_argument("--host", required=True)
    ap.add_argument("--user", required=True)
    ap.add_argument("--password")
    ap.add_argument("--name")
    ap.add_argument("--kind", default="ngfw", choices=["ngfw", "panorama"])
    ap.add_argument("--verify-ssl", action="store_true")
    ap.add_argument("--default", action="store_true")
    args = ap.parse_args(argv)

    password = args.password or getpass.getpass(f"Password for {args.user}@{args.host}: ")
    text = api_raw(
        args.host,
        {"type": "keygen", "user": args.user, "password": password},
        verify_ssl=args.verify_ssl,
    )
    key = parse_response(text).findtext("./result/key")
    if not key:
        raise PanError(f"no key in the response:\n{text[:800]}")

    name = args.name or args.host
    devices = [d for d in load_devices() if d.get("name") != name]
    if args.default:
        for dev in devices:
            dev.pop("default", None)
    entry = {
        "name": name,
        "host": args.host,
        "api_key": key,
        "kind": args.kind,
        "verify_ssl": bool(args.verify_ssl),
    }
    if args.default or not devices:
        entry["default"] = True
    devices.append(entry)
    save_devices(devices)
    print(f"Key received, device '{name}' saved to {CONFIG_PATH}")


def cli_test(argv):
    name = argv[0] if argv else None
    print(t_system_info({"device": name}))


def main():
    argv = sys.argv[1:]
    if not argv:
        serve()
        return
    cmd, rest = argv[0], argv[1:]
    try:
        if cmd == "keygen":
            cli_keygen(rest)
        elif cmd == "list":
            print(t_list_devices({}))
        elif cmd == "test":
            cli_test(rest)
        elif cmd == "diff":
            print(t_pending_changes({"device": rest[0] if rest else None, "max_chars": 200000}))
        else:
            print(__doc__)
            sys.exit(2)
    except PanError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
