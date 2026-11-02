# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Set up the infrastructure tools (Proxmox, SSH) from the command line, for whoever deploys and operates the instance (docs/infra.md). It does exactly what
Settings -> Infrastructure does, through the same code (credentials stored encrypted, the same validation, the same audit log), so there is never a hand-edited
database row or a plaintext secret on disk. Run it INSIDE the container, which is where the data directory and the encryption key are:

    docker compose exec nori python scripts/infra_provision.py status
    docker compose exec -e PROXMOX_TOKEN_SECRET=... nori python scripts/infra_provision.py proxmox-set --hosts 10.0.0.7,10.0.0.93 --token-id iron@pve!agent --ca-file /tmp/ca.pem
    docker compose exec nori python scripts/infra_provision.py proxmox-test
    docker compose exec nori python scripts/infra_provision.py ssh-set-hosts --hosts pve1=root@10.0.0.7,pve2=root@10.0.0.93
    docker compose exec nori python scripts/infra_provision.py ssh-genkey          # prints the PUBLIC key to put in authorized_keys
    docker compose exec nori python scripts/infra_provision.py ssh-pin --expect pve1=SHA256:abc... --expect pve2=SHA256:def...
    docker compose exec nori python scripts/infra_provision.py ssh-test
    docker compose exec -e CLOUDFLARE_API_TOKEN=... nori python scripts/infra_provision.py cloudflare-set [--account-id <32 hex>]
    docker compose exec nori python scripts/infra_provision.py cloudflare-test
    docker compose exec -e GITHUB_API_TOKEN=... nori python scripts/infra_provision.py github-set [--owner <user or organisation>]
    docker compose exec nori python scripts/infra_provision.py github-test
    docker compose exec nori python scripts/infra_provision.py enable proxmox   # and: enable ssh / cloudflare / github, disable <service>
    docker compose exec nori python scripts/infra_provision.py policy proxmox.power ask

Secrets: the Proxmox token secret is read from the PROXMOX_TOKEN_SECRET environment variable, the Cloudflare API token from CLOUDFLARE_API_TOKEN and the GitHub
token from GITHUB_API_TOKEN (the Cloudflare and GitHub ones, with --secret-stdin, from standard input instead); never from the command line (it would show in process
lists and shell history). The CA certificate comes from --ca-file or --ca-stdin. Nothing secret is ever printed: not a token, not the SSH private key. Every command
prints one JSON object and exits 0 on success, 1 on failure.

ssh-pin pins a host's key ONLY where the server presents a key whose fingerprint equals the one given with --expect for it (read from the server itself:
`ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`); on any mismatch nothing is pinned. --trust-scan pins whatever the servers offer without checking, which is only as
safe as the network between here and them; use --expect.

Changes (power, snapshots, shell commands...) are still asked of the owner in the chat; this tool only sets up access and policies and never makes one."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import accounts  # noqa: E402
import config  # noqa: E402
import infra  # noqa: E402
import infra_proxmox  # noqa: E402,F401
import infra_proxmox_write  # noqa: E402,F401
import infra_cloudflare  # noqa: E402
import infra_cloudflare_write  # noqa: E402,F401
import infra_github  # noqa: E402
import infra_github_write  # noqa: E402,F401
import infra_ssh  # noqa: E402
import store  # noqa: E402


def _out(ok: bool, **kw) -> int:
    print(json.dumps({"ok": ok, **kw}, indent=2, default=str))
    return 0 if ok else 1


def _ws() -> int:
    ws = accounts.the_workspace_id()
    if ws is None:
        raise SystemExit(json.dumps({"ok": False, "error": "this instance has no workspace yet (first-run setup has not been completed)"}))
    return ws


def cmd_status(a, ws) -> int:
    services = {}
    for key, spec in infra.SERVICES.items():
        services[key] = {"label": spec["label"], "switch_on": bool(config.get("workspace", ws, infra.switch_key(key))), "configured": infra.configured(ws, key),
                         "enabled": infra.enabled(key, ws),
                         "fields": {f["key"]: ("set" if f["set"] else "not set") if f["secret"] else (f["value"] or "not set") for f in infra.credential_status(ws, key)}}
    creds = infra.credentials(ws, "ssh")
    try:
        hosts = infra_ssh.parse_hosts(creds.get("hosts", ""))
        services["ssh"]["host_keys"] = {n: ("pinned" if infra_ssh._pinned(creds, h) else "NOT pinned") for n, h in hosts.items()}
    except infra_ssh.SshError:
        pass
    services["ssh"]["has_key"] = bool(creds.get("private_key"))
    if creds.get("public_key"):
        services["ssh"]["public_key"] = creds["public_key"]
    return _out(True, services=services, policies=infra.policies(ws), waiting_for_approval=len(infra.pending(ws)))


def cmd_proxmox_set(a, ws) -> int:
    values = {"hosts": "\n".join(h.strip() for h in a.hosts.split(",") if h.strip()), "token_id": a.token_id}
    secret = sys.stdin.readline().strip() if a.secret_stdin else os.environ.get("PROXMOX_TOKEN_SECRET", "").strip()
    values["token_secret"] = secret                      # blank keeps the stored one
    if a.ca_file:
        values["ca_pem"] = Path(a.ca_file).read_text(encoding="utf-8")
    elif a.ca_stdin:
        values["ca_pem"] = sys.stdin.read()
    else:
        values["ca_pem"] = (infra.credentials(ws, "proxmox").get("ca_pem") or "")
    ok, msg = infra.save_credentials(ws, "proxmox", values, None)
    return _out(ok, message=msg, token_secret="set" if infra.credentials(ws, "proxmox").get("token_secret") else "NOT set")


def cmd_proxmox_test(a, ws) -> int:
    if not infra.configured(ws, "proxmox"):
        return _out(False, error="Proxmox is not configured yet")
    res = infra_proxmox.test(ws)
    infra.log(ws, None, "proxmox", "test", "connection", "operator script", res["ok"], res["detail"])
    return _out(res["ok"], detail=res["detail"])


def cmd_ssh_set_hosts(a, ws) -> int:
    lines = []
    for item in a.hosts.split(","):
        item = item.strip()
        if not item:
            continue
        name, _, rest = item.partition("=")
        lines.append(f"{name.strip()} {rest.strip()}")
    ok, msg = infra.save_credentials(ws, "ssh", {"hosts": "\n".join(lines)}, None)
    return _out(ok, message=msg)


def cmd_ssh_genkey(a, ws) -> int:
    if infra.credentials(ws, "ssh").get("private_key") and not a.force:
        return _out(False, error="a key already exists; use --force to replace it (the old one then stops being used)")
    ok, msg = infra_ssh._keygen(ws, None)
    pub = infra.credentials(ws, "ssh").get("public_key", "") if ok else ""
    return _out(ok, message=msg, public_key=pub)


def cmd_ssh_pin(a, ws) -> int:
    if a.trust_scan:
        ok, msg = infra_ssh._pin(ws, None)
        return _out(ok, message=msg, fingerprints=infra.credentials(ws, "ssh").get("fingerprints", "").splitlines(), verified=False)
    expected = {}
    for item in a.expect or []:
        name, sep, fp = item.partition("=")
        if not sep or not fp.strip():
            return _out(False, error=f"--expect wants name=SHA256:fingerprint, got {item!r}")
        expected[name.strip()] = fp.strip()
    if not expected:
        return _out(False, error="give --expect name=SHA256:... for each host (or --trust-scan to pin without checking)")
    ok, msg = infra_ssh.pin_verified(ws, expected, None)
    return _out(ok, message=msg, fingerprints=infra.credentials(ws, "ssh").get("fingerprints", "").splitlines() if ok else [], verified=ok)


def cmd_cloudflare_set(a, ws) -> int:
    values = {"api_token": sys.stdin.readline().strip() if a.secret_stdin else os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()}     # blank keeps the stored one
    if a.account_id is not None:
        values["account_id"] = a.account_id
    else:
        values["account_id"] = infra.credentials(ws, "cloudflare").get("account_id", "")
    ok, msg = infra.save_credentials(ws, "cloudflare", values, None)
    return _out(ok, message=msg, api_token="set" if infra.credentials(ws, "cloudflare").get("api_token") else "NOT set")


def cmd_cloudflare_test(a, ws) -> int:
    if not infra.configured(ws, "cloudflare"):
        return _out(False, error="Cloudflare is not configured yet (an API token)")
    res = infra_cloudflare.test(ws)
    infra.log(ws, None, "cloudflare", "test", "connection", "operator script", res["ok"], res["detail"])
    return _out(res["ok"], detail=res["detail"])


def cmd_github_set(a, ws) -> int:
    values = {"token": sys.stdin.readline().strip() if a.secret_stdin else os.environ.get("GITHUB_API_TOKEN", "").strip()}     # blank keeps the stored one
    values["owner"] = a.owner if a.owner is not None else infra.credentials(ws, "github").get("owner", "")
    ok, msg = infra.save_credentials(ws, "github", values, None)
    return _out(ok, message=msg, token="set" if infra.credentials(ws, "github").get("token") else "NOT set")


def cmd_github_test(a, ws) -> int:
    if not infra.configured(ws, "github"):
        return _out(False, error="GitHub is not configured yet (a token)")
    res = infra_github.test(ws)
    infra.log(ws, None, "github", "test", "connection", "operator script", res["ok"], res["detail"])
    return _out(res["ok"], detail=res["detail"])


def cmd_ssh_test(a, ws) -> int:
    if not infra.configured(ws, "ssh"):
        return _out(False, error="SSH is not configured yet (hosts and a key)")
    res = infra_ssh.test(ws)
    infra.log(ws, None, "ssh", "test", "connection", "operator script", res["ok"], res["detail"])
    return _out(res["ok"], detail=res["detail"])


def cmd_enable(a, ws) -> int:
    if a.service not in infra.SERVICES:
        return _out(False, error=f"no such service; one of: {', '.join(infra.SERVICES)}")
    ok, msg = infra.set_enabled(ws, a.service, a.command == "enable", None)
    return _out(ok, message=msg, enabled=infra.enabled(a.service, ws))


def cmd_policy(a, ws) -> int:
    ok, msg = infra.set_policy(ws, a.permission, a.value, None)
    return _out(ok, message=msg, policy=infra.policy(ws, a.permission) if a.permission in infra.PERMISSIONS else None)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Set up Nori's infrastructure tools from the command line (see the module docstring).")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    s = sub.add_parser("proxmox-set")
    s.add_argument("--hosts", required=True, help="comma-separated node addresses, e.g. 10.0.0.7,10.0.0.93 (https and port 8006 are assumed)")
    s.add_argument("--token-id", required=True, help="user@realm!tokenname")
    s.add_argument("--secret-stdin", action="store_true", help="read the token secret from the first line of standard input (else the PROXMOX_TOKEN_SECRET variable)")
    s.add_argument("--ca-file", help="path (inside the container) of the cluster CA certificate, PEM")
    s.add_argument("--ca-stdin", action="store_true", help="read the CA certificate from standard input (not with --secret-stdin)")
    s.set_defaults(fn=cmd_proxmox_set)
    sub.add_parser("proxmox-test").set_defaults(fn=cmd_proxmox_test)
    s = sub.add_parser("cloudflare-set")
    s.add_argument("--account-id", help="the 32-character account id (only needed if the token can see more than one account)")
    s.add_argument("--secret-stdin", action="store_true", help="read the API token from the first line of standard input (else the CLOUDFLARE_API_TOKEN variable)")
    s.set_defaults(fn=cmd_cloudflare_set)
    sub.add_parser("cloudflare-test").set_defaults(fn=cmd_cloudflare_test)
    s = sub.add_parser("github-set")
    s.add_argument("--owner", help="the default owner (your user name or organisation), so a repository can be named without it")
    s.add_argument("--secret-stdin", action="store_true", help="read the token from the first line of standard input (else the GITHUB_API_TOKEN variable)")
    s.set_defaults(fn=cmd_github_set)
    sub.add_parser("github-test").set_defaults(fn=cmd_github_test)
    s = sub.add_parser("ssh-set-hosts")
    s.add_argument("--hosts", required=True, help="comma-separated name=user@address[:port], e.g. pve1=root@10.0.0.7 (use the Proxmox node names)")
    s.set_defaults(fn=cmd_ssh_set_hosts)
    s = sub.add_parser("ssh-genkey")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_ssh_genkey)
    s = sub.add_parser("ssh-pin")
    s.add_argument("--expect", action="append", metavar="NAME=SHA256:FP", help="the fingerprint of that host's key, read from the server itself; repeat per host")
    s.add_argument("--trust-scan", action="store_true", help="pin whatever the servers offer, unchecked (not recommended)")
    s.set_defaults(fn=cmd_ssh_pin)
    sub.add_parser("ssh-test").set_defaults(fn=cmd_ssh_test)
    for name in ("enable", "disable"):
        s = sub.add_parser(name)
        s.add_argument("service")
        s.set_defaults(fn=cmd_enable)
    s = sub.add_parser("policy")
    s.add_argument("permission")
    s.add_argument("value", choices=infra.POLICIES)
    s.set_defaults(fn=cmd_policy)
    return p


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    if getattr(a, "secret_stdin", False) and getattr(a, "ca_stdin", False):
        return _out(False, error="--secret-stdin and --ca-stdin cannot both read standard input; use --ca-file")
    store.init()
    try:
        return a.fn(a, _ws())
    except infra_ssh.SshError as exc:
        return _out(False, error=str(exc))
    except OSError as exc:
        return _out(False, error=f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    sys.exit(main())
