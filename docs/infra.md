# Infrastructure tools

Tools that let an agent (built for Iron) work with the servers it lives beside: Proxmox first, then SSH, with Cloudflare and GitHub planned through MCP.
They are the part of Nori most able to cause real damage, so the design is mostly about refusing. (`infra.py`, `infra_proxmox.py`; added 2026-10-30.)

**Status:** the framework, **Proxmox reads and changes**, and **SSH shell access** (on the nodes and inside guests) are built. Cloudflare and GitHub (as MCP
connections) are next. Changes and the shell are the part most able to cause damage, so each one is a permission with a policy; see below.

## The rules

| | |
|---|---|
| **Off by default, per service** | Settings → **Infrastructure** (admins only). A service is on only when its switch is on *and* its credentials are saved. Until then the model is not even offered the tools. |
| **The agent cannot switch one on** | The switch is a config entry that no tool can write (it is deliberately not in `update_settings`' list; `get_settings` can read it). The agent *can* switch a service **off** with `infra_switch_off`, a kill switch for when it suspects it is being manipulated; only the owner switches it back on. |
| **Admin-only, never delegated** | The tools exist only in an admin's session. They are not peer-requestable and sub-agents are not given them, so a message from another agent cannot reach them. |
| **Reads are free; each is logged** | Once on, listing and inspecting need no approval. Every call is a row in the audit log (what, when, which target, whether it worked). |
| **Changes ask first** | A change is an *action* with a *permission* (for example "snapshot a guest"). Each permission's policy is **ask me first** (the default for every permission, including any added later), **do it without asking**, or **never**. Under *ask*, the tool call only creates a request: an approval card appears in the chat saying exactly what would happen, with Approve and Deny, and the model is told to wait and not claim it was done. Only your button runs it, exactly once; a second click, a second tab or two at once cannot run it twice. Requests expire after 30 minutes. |
| **You decide when to trust** | Policies are set in the settings page only, per permission, by an admin. No tool can change a policy. Start with everything on *ask*; when you trust a particular kind of change, set it to *do it without asking*. |
| **Credentials** | Stored encrypted, shown nowhere (the page says "saved", never the value; leaving a secret field blank keeps it), used only inside the service module. Use accounts made for this with the fewest permissions that work. |
| **Seen by the developer** | The debug endpoint has an `infra` section (what is on, which credential fields are *set* but never their values, policies, pending requests, the audit log). |

## Proxmox (read-only)

The setup steps are also on the settings page itself (Settings → Infrastructure → Proxmox → *how to set this up*, open until credentials are saved), with the commands ready to copy. In short: create a dedicated user and API token with a read-only role (`PVEAuditor` on `/`; `pveum user add`, `pveum user token add ... --privsep 1`, `pveum acl modify`), then enter:

- **node addresses**, one per line (any node's address works; several give redundancy), `https://host:8006` or just `host`;
- the **token id** (`user@realm!tokenname`) and **secret**;
- the **cluster CA certificate** (PEM) if the nodes use Proxmox's own CA. Certificates are *always* verified; there is no way to turn that off.

Press **test the connection**, then **switch on**. The tools:

| tool | what it shows |
|---|---|
| `proxmox_cluster` | version, quorum, which nodes are online, how many guests are running. Start here. |
| `proxmox_guests` | every VM and container with status, CPU, memory, disk; filter by node, status, type, name |
| `proxmox_guest` | one guest by id: live status, configuration, snapshots (password-like config keys are dropped) |
| `proxmox_node` | uptime, CPU, memory, root disk, kernel and version, pending updates |
| `proxmox_storage` | every storage with usage; or one storage's backups, ISOs, templates |
| `proxmox_tasks`, `proxmox_task_log` | recent tasks and their outcome (errors only if asked); the tail of one task's log |
| `proxmox_backup_jobs` | the scheduled backup jobs |

How the Proxmox client is kept safe: one chokepoint that sends only `GET`; the path must match an allowlist and the model supplies only validated parts (a node
name, a numeric guest id, a task id), never a path; query keys are allowlisted; the answer is size-capped; free text from the cluster (names, descriptions,
notes) is cut and stripped of control characters; every result is labelled as data, not instructions.

## Proxmox changes (`infra_proxmox_write.py`)

`infra_proxmox.py` stays GET-only; every change lives in this separate module so that "the read half cannot change anything" stays checkable by reading it.
Seven tools (`proxmox_guest_power`, `proxmox_snapshot`, `proxmox_backup_now`, `proxmox_guest_config`, `proxmox_destroy`, `proxmox_restore`, `proxmox_node_power`) only
ever *ask* for a change. What happens next is the permission's policy:

| permission | what | policy |
|---|---|---|
| `proxmox.power` | start, graceful shutdown, graceful reboot of a guest | ask; may be set to auto |
| `proxmox.snapshot` | take a snapshot | ask; may be set to auto |
| `proxmox.backup` | back a guest up now (vzdump) | ask; may be set to auto |
| `proxmox.config` | description, tags, start on boot, memory, cores, CPU limit (nothing else) | ask; may be set to auto |
| `proxmox.power.force` | hard-stop a guest | **always asks** |
| `proxmox.snapshot.destroy` | roll back to, or delete, a snapshot | **always asks** |
| `proxmox.destroy` | destroy a guest and its disks; delete a backup file | **always asks** |
| `proxmox.restore` | restore a backup over an existing guest | **always asks** |
| `proxmox.node.power` | reboot or shut down a node | **always asks** |

"Always asks" is `register_permission(never_auto=True)`: the settings page does not offer *do it without asking* for it, `set_policy` refuses it, and a stored "auto"
is read as "ask" (an administrator can still choose *never*). Everything defaults to ask.

How a change is kept honest: the node and the guest type come from the cluster's own inventory, never from the model's text; the card names the guest, its type
and its node; if the guest has been renamed or moved by the time the owner presses Approve, nothing is done; one chokepoint (`_call`) accepts only an allowlist of
(method, path) pairs and known body keys; a task is waited for, so the owner is told how it ended, not that it began; a timed-out request is never re-sent to another
node; a 403 is explained in plain words (the token's role does not allow it). The token's Proxmox role is the second lock: read-only needs `PVEAuditor`, changes need
`PVEVMAdmin` plus `PVEDatastoreAdmin` for backups (node power needs the `Sys.PowerMgmt` privilege, which only `Administrator` has, so grant it alone through a small custom role on `/nodes`). The settings page's *test the connection* says which the token has.

## SSH shell (`infra_ssh.py`)

A shell on the Proxmox nodes and, through a node, inside its guests (`pct exec` for containers, `qm guest exec` for VMs with the guest agent), so no key has to be
put on every guest. It is its own service with its own switch, off by default. Needs the `ssh` program in the container (`openssh-client` in the Dockerfile).

* **Hosts** are the owner's list in the settings (`pve1 root@10.0.0.7`, optional `:port`); the model picks a *name* and can never give an address or option. Use the
  Proxmox node names so a guest is looked up on its own node.
* **Key**: generated by a button, stored encrypted, written to a private temp directory only while one command runs, never shown. The public half is shown for
  `authorized_keys`.
* **Host keys** are pinned by a button (`ssh-keyscan`) and the fingerprints are shown to compare with the server's own. Connections use
  `StrictHostKeyChecking=yes` with only that file; there is no "accept new keys". Agent/port/X11 forwarding, passwords and `~/.ssh/config` are off.
* **Named commands** (`ssh_named`, catalog in `ssh_hosts`): uptime, disk, memory, processes, network, listening ports, failed services, service status, journal,
  apt pending, docker ps/logs/stats, ZFS status run at once and are logged. Restart a service or container and apt update/upgrade are changes under
  `ssh.maintenance` (ask; may be set to auto). Parameters are checked against patterns and shell-quoted; the model never writes the command line.
* **Any command** (`ssh_exec`, on a node or inside a guest): shown to the owner *in full* on the approval card (and in the chat in a code block, so it is not restyled
  by markdown) and run only on Approve. `ssh.exec` and `ssh.guest_exec` are *never auto*.
* The output of a command is data: capped, stripped of control characters, labelled as information and not instructions.

## Setting it up from the command line (`scripts/infra_provision.py`)

For whoever deploys the instance: run it inside the container (`docker compose exec nori python scripts/infra_provision.py <command>`). It does what the settings page
does through the same code (encrypted credentials, the same validation, the same audit log), so there is never a hand-edited row or a plaintext secret. Commands:
`status`, `proxmox-set` (secret from `PROXMOX_TOKEN_SECRET` or `--secret-stdin`, never an argument; CA from `--ca-file`/`--ca-stdin`), `proxmox-test`, `ssh-set-hosts`,
`ssh-genkey`, `ssh-pin --expect name=SHA256:...`, `ssh-test`, `enable|disable <service>`, `policy <permission> <ask|auto|deny>`. Each prints one JSON object and exits 0 or 1,
and never prints a secret or the private key. `ssh-pin --expect` keeps only host keys whose fingerprint equals the one given for that host (read from the server itself),
and pins nothing at all if any host does not match; `--trust-scan` pins unchecked. It only sets up access and policies; every change still asks the owner in the chat.
`tests/test_nori_infra_provision.py` covers it.

## What is not here yet

* **Cloudflare and GitHub.** Planned as MCP connections (Settings -> MCP) rather than native tools, behind the same off-by-default switch.
* Creating or cloning guests, migrating, editing network or storage definitions.
* Per-host rules for the shell (for example "always ask on pve1, never on the NAS"); today the policy is per permission.

## Adding a service

Call `infra.register_service(key, label, fields, test=, validate=)` at import, register its tools with `min_role="admin"` and `enabled=infra.tool_gate(key)`, give
it one request chokepoint, log through `infra.log`, and (for changes) register permissions and actions. Do not add it to `peers.register_peer_requestable` or to a
sub-agent's tool list. `tests/test_nori_infra.py` shows each rule being checked.

## Tests

`tests/test_nori_infra.py` (55): off by default and invisible; the agent cannot switch on (and can switch off); peers and sub-agents cannot reach it; credentials
encrypted and never shown; every Proxmox request is a verified GET through the allowlist and the model cannot smuggle a path; each read tool's output, text caps
and dropped fields, node failover and plain-English errors; the approval engine (ask by default, runs once, deny, expiry, only the requester, no duplicates,
auto and deny policies, concurrent clicks); the admin page and chat card endpoints; and the debug section.

`tests/test_nori_infra_change.py` (53): the never-auto policy; each Proxmox change asks, shows what, and runs once; node and type from the inventory; the allowlist
and body checks; renamed-guest refusal; snapshots, backups, config, destroy, restore, node power; failed tasks, role errors, failover, timeouts; SSH hosts parsing,
strict argv, key file lifetime, pinning, parameter quoting, guests via the node, output cleaning, error explanations, ad-hoc commands (always ask, shown in full), the
key never in a page, log or debug output.
