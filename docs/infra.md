# Infrastructure tools

Tools that let an agent (built for Iron) work with the servers it lives beside: Proxmox first, then SSH, with Cloudflare and GitHub planned through MCP.
They are the part of Nori most able to cause real damage, so the design is mostly about refusing. (`infra.py`, `infra_proxmox.py`; added 2026-10-30.)

**Status:** the framework and **Proxmox, read-only** are built. Changes (power, snapshots), SSH (named commands, then approved ad-hoc), and the Cloudflare and
GitHub MCP connections are next. The change/approval machinery is already in place and tested, so the first change action only has to name its permission.

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

## What is not here yet

- **Changes.** None exist, so the "changes" part of the settings page says so. When one is added (say, "start/stop a guest"), it registers a permission and an
  action with `infra.register_permission()` / `infra.register_action()` and appears on that page set to *ask*.
- **SSH.** Planned as named, parameterised commands (patch, `docker ps`, logs, snapshot...) on allowlisted hosts, plus ad-hoc commands that always need approval.
- **Cloudflare and GitHub.** Planned as MCP connections (Settings → MCP) rather than native tools, behind the same off-by-default switch.

## Adding a service

Call `infra.register_service(key, label, fields, test=, validate=)` at import, register its tools with `min_role="admin"` and `enabled=infra.tool_gate(key)`, give
it one request chokepoint, log through `infra.log`, and (for changes) register permissions and actions. Do not add it to `peers.register_peer_requestable` or to a
sub-agent's tool list. `tests/test_nori_infra.py` shows each rule being checked.

## Tests

`tests/test_nori_infra.py` (54): off by default and invisible; the agent cannot switch on (and can switch off); peers and sub-agents cannot reach it; credentials
encrypted and never shown; every Proxmox request is a verified GET through the allowlist and the model cannot smuggle a path; each read tool's output, text caps
and dropped fields, node failover and plain-English errors; the approval engine (ask by default, runs once, deny, expiry, only the requester, no duplicates,
auto and deny policies, concurrent clicks); the admin page and chat card endpoints; and the debug section.
