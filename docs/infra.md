# Infrastructure tools

Tools that let an agent (built for Iron) work with the servers and services it lives beside: Proxmox first, then SSH, Cloudflare and GitHub.
They are the part of Nori most able to cause real damage, so the design is mostly about refusing. (`infra.py`, `infra_proxmox.py`; added 2026-10-30.)

**Status:** the framework, **Proxmox reads and changes**, **SSH shell access** (on the nodes and inside guests) and **Cloudflare** (reads, and changes to DNS, cache,
zone settings, tunnels and rules, plus a free-form change) and **GitHub** (reads, and changes to issues, pull requests, branches, files, workflow runs, plus a free-form
change) are built. Changes and the shell are the part most able to cause damage, so each one is a permission with a policy; see below.

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

## Cloudflare (`infra_cloudflare.py` reads, `infra_cloudflare_write.py` changes)

Native rather than MCP, for the reason Proxmox is: the switch, the policy, the approval card and the audit log have to sit around every call. Settings → Infrastructure →
Cloudflare has the setup steps (open until credentials are saved). In short: make an **API token** (My Profile → API Tokens → Create Custom Token; never the Global API
Key), start with read permissions, and lock it to this server's IP address in the dashboard. Paste it; the account id is only needed if the token sees several accounts.
**Test the connection**, then **switch on**. The token's own permissions are the second lock: a read-only token cannot change anything whatever Nori is told, and
adding Edit permissions to it (never to Nori) is how you widen what it may do. Going back to read-only is removing them from the token. The address is fixed
(`api.cloudflare.com`), TLS is always verified, and nothing the model says can point a call elsewhere. `scripts/infra_provision.py cloudflare-set` / `cloudflare-test`
does the same from the command line (the token from `CLOUDFLARE_API_TOKEN` or `--secret-stdin`).

Reads (free once on; each logged; GET only; every result labelled as data, free text cut and stripped, secret-looking keys dropped):

| tool | what it shows |
|---|---|
| `cloudflare_overview` | whether the token is active, the accounts, every zone with status and plan. Start here. |
| `cloudflare_zone` | one zone: status, plan, name servers, DNSSEC and its settings (SSL mode, always-HTTPS, minimum TLS, security level ...) |
| `cloudflare_dns` | a zone's records (id, type, name, content, proxied, TTL), filtered by type, name or content |
| `cloudflare_tunnels` | tunnels with connector counts; one tunnel's ingress rules (hostname → internal service) and connectors |
| `cloudflare_access` | Zero Trust Access applications and, for one, its policies |
| `cloudflare_rules` | a zone's rulesets; for a phase (firewall, redirect, rate_limit, waf_managed, transform, cache, config) its rules |
| `cloudflare_account` | Workers, Pages, R2, KV or D1 inventories |
| `cloudflare_audit` | the account audit log: who changed what, when, from where |
| `cloudflare_api_get` | any other read under `/zones/<id>` or `/accounts/<id>`; tokens, secrets, billing, members, keys, Access service tokens and log destinations are refused |

Changes (six tools that only ever *ask*; what happens next is the permission's policy):

| permission | what | policy |
|---|---|---|
| `cloudflare.dns` | create or edit an A, AAAA, CNAME, TXT, MX or NS record | ask; may be set to auto |
| `cloudflare.cache` | purge everything, or up to 30 named URLs on the zone | ask; may be set to auto |
| `cloudflare.settings` | one of a short list of zone settings (SSL flexible/full/strict, always-HTTPS, minimum TLS, security level, development mode, cache level ...) | ask; may be set to auto |
| `cloudflare.tunnel` | create an empty tunnel (its connector token is never shown to the agent) | ask; may be set to auto |
| `cloudflare.dns.delete` | delete a record | **always asks** |
| `cloudflare.tunnel.config` | add or remove a tunnel route (puts an internal service on the internet under a hostname) | **always asks** |
| `cloudflare.tunnel.delete` | delete a tunnel | **always asks** |
| `cloudflare.rules` | add, enable, disable or delete a custom WAF rule or a single redirect | **always asks** |
| `cloudflare.api` | any other POST/PUT/PATCH/DELETE under `/zones/<id>` or `/accounts/<id>`, shown in full on the card | **always asks** |

How a change is kept honest: the zone, record, tunnel and rule are always the ones Cloudflare itself reports (the model gives a domain name or an id, never a path);
the card says exactly what changes (for a record: its old and new content; for a route: the hostname, the service and whether a DNS record is made); at run time the
thing is **read again and compared with what the card showed**: a record, rule or tunnel route list that changed in the meantime, a zone that was renamed, a DNS name
that appeared since, all stop it with nothing done. A route addition changes the tunnel and then makes the proxied CNAME; a record that is already taken is refused
*before* the tunnel is touched. Deleting a tunnel is refused while a connector is attached. A timed-out change is never re-sent. A 403 is explained in plain words
(the token's permissions do not allow it). One chokepoint (`_call`) accepts only an allowlist of (method, path) pairs, or for the free-form tool a path under a zone or
account outside the denied areas, never the deletion of a whole zone or account, with a JSON body of bounded size.

Tunnel routes need a tunnel managed from Cloudflare (created from the dashboard or by `cloudflare_tunnel_change create`); a tunnel configured by a `config.yml` on its host
is reported as such and is edited over SSH instead. The permissions to give the token for each change are listed in step 5 of the setup guide.

## GitHub (`infra_github.py` reads, `infra_github_write.py` changes)

Native rather than MCP, for the same reason. Settings → Infrastructure → GitHub has the setup steps. In short: make a **fine-grained personal access token** (GitHub →
Settings → Developer settings → Personal access tokens → Fine-grained tokens), set the resource owner, a **short expiry**, **Only select repositories** (exactly the ones
the agent may see) and read-only repository permissions (Contents, Issues, Pull requests, Actions, Commit statuses, Checks). Paste it; the default owner is optional (so
the agent can say just `my-repo`). **Test the connection**, then **switch on**. Unlike Cloudflare, GitHub cannot lock a personal token to an IP address, so the limits
are the repositories chosen, the permissions ticked and the expiry; the test button warns if the token is a classic one (which cannot be limited to chosen repositories).
The token's own permissions are the second lock: a read-only token cannot change anything whatever Nori is told, and widening it (Issues / Pull requests / Contents /
Actions: Read and write) is how you let the agent do more. The address is fixed (`api.github.com`) and TLS is always verified. `scripts/infra_provision.py github-set`
(token from `GITHUB_API_TOKEN` or `--secret-stdin`, `--owner`) and `github-test` do the same from the command line.

**One connection for your account and every organisation: a GitHub App.** A fine-grained token reaches one account (yours, or one organisation's). To reach
all of them with a single connection, the way hosted services connect to GitHub, sign in with a GitHub App instead (`infra_github_app.py`):

1. Once, on GitHub: Settings → Developer settings → GitHub Apps → **New GitHub App**. Any name and homepage address; untick **Webhook → Active**; tick **Enable
   Device Flow**; repository permissions as for a token (read-only to start, read and write for what it may change); *Where can this GitHub App be installed?*
   **Any account**, so your organisations can install it. You do not need a client secret or a private key.
2. On the app's page, **Install App** on your account and on each organisation, choosing all repositories or only some each time (an organisation may need an
   owner to approve it). Adding an organisation later is just installing it there; nothing changes in Nori.
3. In Nori: paste the app's **client ID** (and optionally its name from `github.com/apps/<name>`, for an install link), save, press **sign in with the GitHub App**,
   enter the code at `github.com/login/device`, then press **finish GitHub App sign-in**. The test lists every account it is installed on.

What the connection can reach is what both you and the app can reach, in the repositories each installation was given; the app's permissions are the second
lock, as a token's are. The device flow needs no secret, for the sign-in or for renewing it: the token lasts 8 hours and Nori renews it under a lock (GitHub
replaces the refresh token on every renewal, so two at once would sign Nori out); the renewal lasts six months, and a revoked or lapsed sign-in is dropped with
a message to sign in again. When both a token and an app sign-in are saved, the app sign-in is used; **sign out of the GitHub App** goes back to the token. The
only POSTs in the GitHub code are this module's, to GitHub's two sign-in addresses; `infra_github.py` stays GET-only.

Reads (free once on; each logged; GET only; every result labelled as data, free text cut and stripped, anything shaped like a token or private key scrubbed, secret-looking
keys dropped):

| tool | what it shows |
|---|---|
| `github_overview` | who the token is, its kind and expiry, the rate limit, and every repository it can see with your permission on each. Start here. |
| `github_repo` | one repository: visibility, default branch and whether it is protected, merge methods allowed, what the token may do there |
| `github_branches` | branches with their tip commit and whether each is protected |
| `github_commits` | recent commits (by branch, path, author, date), or one commit with the files it changed (`include_patch` adds the diffs) |
| `github_pulls` | the list, or one pull request with description, files, reviews, comments, mergeability and the state of its checks |
| `github_issues` | the list (pull requests excluded), or one issue with its comments |
| `github_actions` | workflow runs (by branch, status, workflow), one run with its jobs and failed steps, the workflows, or the **end of a job's log** |
| `github_file` | a file (text, 20000 characters by default) or a directory, at a branch, tag or commit |
| `github_search` | code, issues, repos or commits within one repository (or the default owner's) |
| `github_api_get` | any other read under `/repos/<owner>/<name>`; secrets, keys, webhooks, collaborators and invitations, tokens, raw blobs and archives are refused |

Two things worth knowing. **Redirects are never followed with the token attached** (the HTTP layer hands a redirect back instead of following it). The one redirect that is
needed, a job log, which GitHub serves from a signed address on its own storage, is fetched by a second request that carries no credentials at all, to an `https` address
on `*.githubusercontent.com` or `*.blob.core.windows.net` only. And **files likely to hold secrets are never read or written**: `.env` (not `.env.example`), `*.pem`, `*.key`,
`id_rsa`, credentials and secrets files, `terraform.tfstate`, `kubeconfig` and the like; the check happens before GitHub is even asked.

Changes (six tools that only ever *ask*; what happens next is the permission's policy):

| permission | what | policy |
|---|---|---|
| `github.issues` | create an issue, comment on an issue or pull request, add or remove a label, close or reopen an issue | ask; may be set to auto |
| `github.pulls` | open a pull request, edit it, close or reopen it, request reviewers | ask; may be set to auto |
| `github.workflow` | re-run a run, re-run only its failed jobs, or cancel it | ask; may be set to auto |
| `github.branch` | create a branch from a branch or commit | ask; may be set to auto |
| `github.merge` | merge a pull request (only the exact commit that was shown) | **always asks** |
| `github.branch.delete` | delete a branch (never the default or a protected one) | **always asks** |
| `github.commit` | create, change or delete one file as a commit on a branch | **always asks** |
| `github.workflow.dispatch` | start a workflow by hand (it can deploy) | **always asks** |
| `github.api` | any other POST/PUT/PATCH/DELETE under `/repos/<owner>/<name>`, shown in full | **always asks** |

How a change is kept honest: the repository is always the one GitHub itself reports (the model gives `owner/name`, never a path, and a repository that was replaced or
archived since is refused), and the card says exactly what happens: the **full text** of an issue, comment or description (and "a PUBLIC repository: anyone can read this"
when it is one), a pull request's head commit and target, the **whole diff** of a file change (a new file's lines, or the before/after of a replacement; a change too big
to show in full is refused), whether a commit goes **straight to the default branch**, the head commit and the state of the checks for a merge. At run time the thing is
**read again and compared with what the card showed**: an issue whose title changed, a pull request with new commits or a different target (a merge also sends GitHub the
commit it was shown, so GitHub refuses it too), a file whose content changed, a branch that moved, a run that was already re-run, all stop it with nothing done. Text that
looks like a secret (a token, a private key) is refused outright rather than posted or committed. A timed-out change is never re-sent. A 403 is explained in plain words (the
token's permissions, or the repository it was not given). One chokepoint (`_call`) accepts only an allowlist of (method, path) pairs, or for the free-form tool a path under
`/repos/<owner>/<name>` outside the denied areas, never the deletion of a repository, and only harmless edits to the repository itself (visibility, name, default branch and
archiving are done on GitHub).

A file change is best made on a branch of its own and proposed as a pull request; writing to `.github/workflows/` additionally needs a token with the Workflows
permission, which is best left off, and is called out on the card.

## What is not here yet

* GitHub: reviews (approving or requesting changes), releases, the GraphQL API (for example marking a draft ready for review), forks and cross-repository pull requests,
  and organisation-level administration have no named tool. Reviews and releases can be done through the free-form change (which always asks and shows the whole request).
* Cloudflare: analytics (GraphQL), Access policy edits, Workers/Pages deploys and R2/KV/D1 data have no named tool. Access, Workers, Pages and the rest can be read,
  and changed one request at a time through the free-form change (which always asks and shows the whole request).
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

`tests/test_nori_infra_cloudflare.py` (57): off by default and invisible; admin-only, never peer-requestable; the agent cannot switch it on; the token encrypted and never
shown (page, log, debug, command line); every read a GET to the one fixed address through the allowlist, the free-form read outside the denied areas and shaped; each
read tool and its filters, paging, text cleaning, plain-English errors; the never-auto permissions; every change asks, shows exactly what, and runs once; records, rules,
routes and tunnels re-read at run time and refused if changed; the chokepoint's allowlist; the connector token never reaching the model; the free-form change refused
in the denied areas; failures and timeouts; and the operator script's `cloudflare-set` / `cloudflare-test`.

`tests/test_nori_infra_github.py` (63): off by default and invisible; admin-only, never peer-requestable; the agent cannot switch it on; the token encrypted and never shown
(page, log, debug, command line); every read a GET to the one fixed address through the allowlist; the real HTTP layer (against a local server) never following a redirect;
job logs fetched without the token and only from GitHub's storage hosts; secret-shaped text scrubbed from diffs, comments and files; files likely to hold secrets neither
read nor written; the free-form read outside the denied areas; each read tool and its filters, paging and plain-English errors; the never-auto permissions; every change
asks, shows exactly what (the whole text, the whole diff), and runs once; the repository, issue, pull request head, file and branch tip re-read at run time and refused if
changed; a merge sending the commit it was shown; the chokepoint's allowlist; the free-form change refused in the denied areas; failures and timeouts; and the operator
script's `github-set` / `github-test`.
