# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Infrastructure CHANGES and SSH (infra_proxmox_write.py, infra_ssh.py, 2026-11-01). The rules pinned here: a change never runs from a tool call unless its permission
says "auto" (and the dangerous kinds cannot say it); what the owner approves is exactly what runs; nothing the model types becomes a path, an address or a shell
fragment; and every network call and every local program is a seam the tests replace, so no test touches a cluster or a server. Real server, SQLite and config."""
import json
import os
import re
import sys
import unittest
import urllib.error
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ["NORI_TOOL_RATE_LIMIT"] = "100000"          # these tests make hundreds of calls to one tool in a minute
import test_nori_infra as base  # noqa: E402  (sets up the scratch instance, the admin, the web helper and the pretend Proxmox reads)

import infra  # noqa: E402
import infra_proxmox as px  # noqa: E402
import infra_proxmox_write as pw  # noqa: E402
import infra_ssh as ssh  # noqa: E402
import store  # noqa: E402

WS, UID, SESS, call, names, web = base.WS, base.UID, base.SESS, base.call, base.names, base.web

# ── pretend Proxmox: reads from the other test file, plus tasks that finish and a writer that records ──
SENT: list[dict] = []
SEND_FAIL: dict = {}
_UPID = "UPID:{node}:0000AAAA:0000BBBB:67000000:qmaction:{vmid}:iron@pve!agent:"


def _task_ok(path):
    return re.match(r"^/nodes/[^/]+/tasks/UPID:[^/]+/status$", path)


def fetch(url, headers, ca_pem, timeout):
    u = urllib.parse.urlsplit(url)
    path = u.path[len("/api2/json"):]
    if _task_ok(path):
        base.CALLS.append({"url": url})
        return 200, json.dumps({"data": {"status": "stopped", "exitstatus": SEND_FAIL.get("exit", "OK")}}).encode()
    if path == "/access/permissions":
        return 200, json.dumps({"data": {"/": SEND_FAIL.get("perms", {"VM.PowerMgmt": 1, "VM.Snapshot": 1})}}).encode()
    return base.fake_fetch(url, headers, ca_pem, timeout)


def send(method, url, headers, data, ca_pem, timeout):
    u = urllib.parse.urlsplit(url)
    SENT.append({"method": method, "url": url, "path": u.path[len("/api2/json"):], "headers": dict(headers), "body": dict(urllib.parse.parse_qsl((data or b"").decode())),
                 "ca": ca_pem, "timeout": timeout})
    if u.hostname in SEND_FAIL.get("dead", set()):
        raise urllib.error.URLError(ConnectionRefusedError("refused"))
    if SEND_FAIL.get("timeout"):
        raise urllib.error.URLError(TimeoutError("timed out"))
    if "status" in SEND_FAIL:
        return SEND_FAIL["status"], json.dumps({"message": "denied for the test", "errors": {"x": "no"}}).encode()
    m = re.match(r"^/api2/json/nodes/([^/]+)/(?:qemu|lxc)/(\d+)/", u.path)
    node, vmid = (m.group(1), m.group(2)) if m else ("p2", "0")
    return 200, json.dumps({"data": _UPID.format(node=node, vmid=vmid)}).encode()


px._fetch = fetch
pw._send = send
pw._sleep = lambda s: None


def reset():
    base.reset()
    SENT.clear()
    SEND_FAIL.clear()
    from config import set as cset
    cset("workspace", WS, "infra_ssh_enabled", False)
    store.write(lambda c: c.execute("DELETE FROM messages WHERE kind='proactive'"))


def cards():
    return base._cards()


def last_message_text():
    return store.read(lambda c: c.execute("SELECT content FROM messages WHERE user_id=? ORDER BY id DESC LIMIT 1", (UID,)).fetchone()["content"])


# ── the policy: some kinds can never run on their own ────────────────────
NEVER = ("proxmox.power.force", "proxmox.snapshot.destroy", "proxmox.destroy", "proxmox.restore", "proxmox.node.power", "ssh.exec", "ssh.guest_exec")
MAY = ("proxmox.power", "proxmox.snapshot", "proxmox.backup", "proxmox.config", "ssh.maintenance")


class ThePolicy(unittest.TestCase):
    def setUp(self):
        reset()

    def test_every_new_permission_defaults_to_ask(self):
        for p in NEVER + MAY:
            self.assertIn(p, infra.PERMISSIONS)
            self.assertEqual(infra.policy(WS, p), "ask", p)

    def test_the_dangerous_ones_cannot_be_set_to_auto(self):
        for p in NEVER:
            self.assertTrue(infra.PERMISSIONS[p]["never_auto"], p)
            ok, msg = infra.set_policy(WS, p, "auto", UID)
            self.assertFalse(ok, p)
            self.assertIn("always asks", msg)
            self.assertEqual(infra.policy(WS, p), "ask")
            self.assertTrue(infra.set_policy(WS, p, "deny", UID)[0], "but they can be switched to never")
            infra.set_policy(WS, p, "ask", UID)
        for p in MAY:
            self.assertFalse(infra.PERMISSIONS[p]["never_auto"], p)
            self.assertTrue(infra.set_policy(WS, p, "auto", UID)[0], p)

    def test_a_stored_auto_for_a_dangerous_one_is_read_as_ask(self):
        store.write(lambda c: c.execute("INSERT INTO infra_policies(workspace_id, permission, policy, updated_ts) VALUES (?,?,?,0)", (WS, "proxmox.destroy", "auto")))
        self.assertEqual(infra.policy(WS, "proxmox.destroy"), "ask")

    def test_the_settings_page_does_not_offer_auto_for_them(self):
        infra.save_credentials(WS, "proxmox", base.CREDS, UID)
        status, page = web("/settings?tab=infra")
        row_destroy = page[page.index("name=permission value='proxmox.destroy'"):]
        row_destroy = row_destroy[:row_destroy.index("</form>")]
        self.assertNotIn("do it without asking", row_destroy)
        self.assertIn("never", row_destroy)
        row_snap = page[page.index("name=permission value='proxmox.snapshot'"):]
        row_snap = row_snap[:row_snap.index("</form>")]
        self.assertIn("do it without asking", row_snap)
        self.assertIn("always asks", page)
        status, page = web("/admin/infra/proxmox/policy", {"permission": "proxmox.destroy", "policy": "auto"})
        self.assertIn("always asks you first", page)
        self.assertEqual(infra.policy(WS, "proxmox.destroy"), "ask")


# ── Proxmox changes ──────────────────────────────────────────────────────
CHANGE_TOOLS = {"proxmox_guest_power", "proxmox_snapshot", "proxmox_backup_now", "proxmox_guest_config", "proxmox_destroy", "proxmox_restore", "proxmox_node_power"}


class ProxmoxChanges(unittest.TestCase):
    def setUp(self):
        reset()
        base.turn_on()

    def approve(self, res):
        self.assertEqual(res["status"], "awaiting_approval", res)
        return infra.resolve(UID, res["approval_id"], True)

    def test_the_tools_exist_only_while_on_and_never_for_a_member_or_a_peer_or_a_sub_agent(self):
        infra.set_enabled(WS, "proxmox", False, UID)
        self.assertFalse(CHANGE_TOOLS & names())
        base.turn_on()
        self.assertTrue(CHANGE_TOOLS <= names())
        self.assertFalse(CHANGE_TOOLS & names(base.MSESS))
        import peers
        for t in CHANGE_TOOLS | {"ssh_named", "ssh_exec", "ssh_hosts"}:
            self.assertNotIn(t, peers._PEER_REQUESTABLE)
        src = (base.ROOT / "jobs.py").read_text(encoding="utf-8")
        for t in CHANGE_TOOLS | {"ssh_"}:
            self.assertNotIn(t, src)

    def test_a_power_change_asks_first_shows_exactly_what_and_runs_once_on_approval(self):
        res = call("proxmox_guest_power", {"vmid": 108, "action": "reboot"})
        self.assertEqual(res["status"], "awaiting_approval")
        self.assertEqual(SENT, [], "nothing reached the cluster")
        (mid, card), = cards()
        self.assertIn("108", card["summary"])
        self.assertIn("Docker", card["summary"])
        self.assertIn("p2", card["summary"])
        self.assertIn("reboot", card["summary"])
        ok, msg, status = self.approve(res)
        self.assertEqual(status, "done", msg)
        self.assertEqual([s["method"] for s in SENT], ["POST"])
        s = SENT[0]
        self.assertEqual(s["path"], "/nodes/p2/lxc/108/status/reboot")
        self.assertEqual(s["headers"]["Authorization"], f"PVEAPIToken={base.TOKEN_ID}={base.SECRET}")
        self.assertTrue(s["url"].startswith("https://10.1.1.7:8006/"))
        self.assertIn("Done:", last_message_text())
        self.assertFalse(infra.resolve(UID, res["approval_id"], True)[0], "a second click does nothing")
        self.assertEqual(len(SENT), 1)

    def test_the_node_and_type_come_from_the_cluster_not_from_the_model(self):
        self.assertIn("error", call("proxmox_guest_power", {"vmid": 108, "action": "start", "node": "evil", "kind": "qemu"}))
        res = call("proxmox_guest_power", {"vmid": 105, "action": "start"})
        self.assertIn("VM on prox1", cards()[-1][1]["summary"], "the card names the real type and node")
        self.approve(res)
        self.assertEqual(SENT[0]["path"], "/nodes/prox1/qemu/105/status/start")
        self.assertIn("error", call("proxmox_guest_power", {"vmid": 99999, "action": "start"}), "no such guest")
        self.assertIn("error", call("proxmox_guest_power", {"vmid": 108, "action": "explode"}))

    def test_an_auto_policy_runs_it_at_once_and_is_logged(self):
        infra.set_policy(WS, "proxmox.power", "auto", UID)
        res = call("proxmox_guest_power", {"vmid": 108, "action": "start"})
        self.assertTrue(res["done"], res)
        self.assertEqual(len(SENT), 1)
        self.assertEqual(cards(), [])
        w = [r for r in infra.recent_log(10, "proxmox") if r["kind"] == "write"]
        self.assertEqual((w[0]["action"], w[0]["ok"]), ("proxmox_guest_start", 1))
        self.assertIn("auto policy", w[0]["detail"])

    def test_hard_stop_and_destroy_ask_even_if_the_database_says_auto(self):
        store.write(lambda c: c.executemany("INSERT INTO infra_policies(workspace_id, permission, policy, updated_ts) VALUES (?,?,?,0)",
                                            [(WS, "proxmox.power.force", "auto"), (WS, "proxmox.destroy", "auto")]))
        self.assertEqual(call("proxmox_guest_power", {"vmid": 108, "action": "stop"})["status"], "awaiting_approval")
        self.assertEqual(call("proxmox_destroy", {"what": "guest", "vmid": 109})["status"], "awaiting_approval")
        self.assertEqual(SENT, [])

    def test_deny_refuses(self):
        infra.set_policy(WS, "proxmox.power", "deny", UID)
        self.assertIn("disallowed", call("proxmox_guest_power", {"vmid": 108, "action": "start"})["error"])
        self.assertEqual((SENT, cards()), ([], []))

    def test_the_chokepoint_refuses_anything_outside_its_allowlist(self):
        bad = [("POST", "/access/users"), ("POST", "/nodes/p2/lxc/108/status/../../../access"), ("DELETE", "/cluster/config"), ("GET", "/nodes/p2/lxc/108/status/start"),
               ("POST", "/nodes/p2/lxc/108/agent/exec"), ("PUT", "/nodes/p2/lxc/108/status/start"), ("POST", "/nodes/p2/lxc/108/status/start?x=1"),
               ("POST", "//nodes/p2/vzdump"), ("DELETE", "/nodes/p2/storage/x/content/../../y"), ("POST", "/nodes/p2/lxc/abc/status/start"),
               ("DELETE", "/nodes/p2/storage/DataExternal/content/DataExternal:backup/vzdump-lxc-108-2026_01_01-00_00_00.tar.zst")]
        for method, path in bad:
            with self.assertRaises(px.ProxmoxError, msg=(method, path)):
                pw._call(WS, method, path)
        for body in ({"evil": 1}, {"snapname": ["a"]}, {"snapname": "x" * 500}, {"hookscript": "x"}):
            with self.assertRaises(px.ProxmoxError, msg=body):
                pw._call(WS, "POST", "/nodes/p2/lxc/108/snapshot", body)
        self.assertEqual(SENT, [])

    def test_a_guest_that_changed_since_it_was_approved_is_not_touched(self):
        res = call("proxmox_guest_power", {"vmid": 108, "action": "shutdown"})
        old = base.ROUTES["/cluster/resources"]
        try:
            base.ROUTES["/cluster/resources"] = lambda: [dict(r, name="SOMETHING ELSE") if r.get("vmid") == 108 else r for r in base._resources()]
            ok, msg, status = infra.resolve(UID, res["approval_id"], True)
        finally:
            base.ROUTES["/cluster/resources"] = old
        self.assertEqual(status, "failed")
        self.assertIn("no longer", msg)
        self.assertEqual(SENT, [])

    def test_snapshots(self):
        self.assertEqual(self.approve(call("proxmox_snapshot", {"vmid": 108, "op": "create", "name": "before_patch", "description": "pre patch"}))[2], "done")
        self.assertEqual((SENT[-1]["path"], SENT[-1]["body"]), ("/nodes/p2/lxc/108/snapshot", {"snapname": "before_patch", "description": "pre patch"}))
        for bad in ("bad name", "../x", "1abc", "", "x" * 41, "a;b"):
            self.assertIn("error", call("proxmox_snapshot", {"vmid": 108, "op": "create", "name": bad}), bad)
        n = len(SENT)
        r = call("proxmox_snapshot", {"vmid": 108, "op": "rollback", "name": "pre_update_20261025"})
        self.assertIn("ROLL BACK", cards()[-1][1]["summary"])
        self.assertEqual(self.approve(r)[2], "done")
        self.assertEqual(SENT[n]["path"], "/nodes/p2/lxc/108/snapshot/pre_update_20261025/rollback")
        d = call("proxmox_snapshot", {"vmid": 108, "op": "delete", "name": "pre_update_20261025"})
        self.assertEqual(self.approve(d)[2], "done")
        self.assertEqual((SENT[-1]["method"], SENT[-1]["path"]), ("DELETE", "/nodes/p2/lxc/108/snapshot/pre_update_20261025"))
        infra.set_policy(WS, "proxmox.snapshot", "auto", UID)
        self.assertTrue(call("proxmox_snapshot", {"vmid": 108, "op": "create", "name": "auto_one"})["done"])
        self.assertEqual(call("proxmox_snapshot", {"vmid": 108, "op": "delete", "name": "auto_one"})["status"], "awaiting_approval", "delete still asks")

    def test_backup_now(self):
        self.assertEqual(self.approve(call("proxmox_backup_now", {"vmid": 108, "storage": "DataExternal"}))[2], "done")
        s = SENT[-1]
        self.assertEqual((s["path"], s["body"]["vmid"], s["body"]["storage"], s["body"]["mode"]), ("/nodes/p2/vzdump", "108", "DataExternal", "snapshot"))
        self.assertIn("error", call("proxmox_backup_now", {"vmid": 108, "storage": "../x"}))
        self.assertIn("error", call("proxmox_backup_now", {"vmid": 108, "storage": "DataExternal", "mode": "rm -rf"}))

    def test_config_changes_are_limited_to_a_short_list_and_checked(self):
        self.assertEqual(self.approve(call("proxmox_guest_config", {"vmid": 108, "changes": {"memory": 4096, "cores": 4, "description": "web", "onboot": 1, "tags": "a;b"}}))[2], "done")
        s = SENT[-1]
        self.assertEqual((s["method"], s["path"]), ("PUT", "/nodes/p2/lxc/108/config"))
        self.assertEqual((s["body"]["memory"], s["body"]["cores"], s["body"]["onboot"]), ("4096", "4", "1"))
        for bad in ({"hookscript": "local:snippets/x.sh"}, {"net0": "bridge=vmbr9"}, {"memory": "lots"}, {"memory": 1}, {"cores": 0}, {"onboot": 2}, {"tags": "a b!"}, {}, {"rootfs": "x"}):
            self.assertIn("error", call("proxmox_guest_config", {"vmid": 108, "changes": bad}), bad)
        self.assertEqual(len(SENT), 1)

    def test_destroying_a_guest_always_asks_and_refuses_a_running_one(self):
        r = call("proxmox_destroy", {"what": "guest", "vmid": 108})
        self.assertIn("DESTROY", cards()[-1][1]["summary"])
        ok, msg, status = infra.resolve(UID, r["approval_id"], True)
        self.assertEqual(status, "failed")
        self.assertIn("running", msg)
        self.assertEqual(SENT, [])
        self.assertEqual(self.approve(call("proxmox_destroy", {"what": "guest", "vmid": 109}))[2], "done")
        self.assertEqual((SENT[-1]["method"], SENT[-1]["path"]), ("DELETE", "/nodes/prox3/lxc/109"))

    def test_deleting_a_backup_checks_the_volume_id(self):
        volid = "DataExternal:backup/vzdump-lxc-108-2026_10_25-02_30_00.tar.zst"
        self.assertEqual(self.approve(call("proxmox_destroy", {"what": "backup", "node": "prox1", "storage": "DataExternal", "volid": volid}))[2], "done")
        self.assertEqual((SENT[-1]["method"], SENT[-1]["path"]), ("DELETE", "/nodes/prox1/storage/DataExternal/content/DataExternal:backup%2Fvzdump-lxc-108-2026_10_25-02_30_00.tar.zst"))
        for bad in ("DataExternal:../../etc/passwd", "DataExternal:backup/../x", "Other:backup/vzdump-lxc-108-2026_10_25-02_30_00.tar.zst", "x", ""):
            self.assertIn("error", call("proxmox_destroy", {"what": "backup", "node": "prox1", "storage": "DataExternal", "volid": bad}), bad)
        self.assertIn("error", call("proxmox_destroy", {"what": "everything"}))

    def test_restore_must_be_a_backup_of_that_very_guest_and_a_stopped_one(self):
        good = "DataExternal:backup/vzdump-lxc-109-2026_10_25-02_30_00.tar.zst"
        self.assertIn("error", call("proxmox_restore", {"vmid": 109, "volid": "DataExternal:backup/vzdump-lxc-108-2026_10_25-02_30_00.tar.zst"}), "another guest's backup")
        self.assertIn("error", call("proxmox_restore", {"vmid": 109, "volid": "DataExternal:backup/vzdump-qemu-109-2026_10_25-02_30_00.vma.zst"}), "wrong type")
        r = call("proxmox_restore", {"vmid": 108, "volid": "DataExternal:backup/vzdump-lxc-108-2026_10_25-02_30_00.tar.zst"})
        self.assertEqual(infra.resolve(UID, r["approval_id"], True)[2], "failed", "108 is running")
        self.assertEqual(SENT, [])
        self.assertEqual(self.approve(call("proxmox_restore", {"vmid": 109, "volid": good}))[2], "done")
        s = SENT[-1]
        self.assertEqual((s["method"], s["path"], s["body"]["ostemplate"], s["body"]["restore"], s["body"]["force"]), ("POST", "/nodes/prox3/lxc", good, "1", "1"))

    def test_node_power_always_asks_and_only_for_a_real_node(self):
        self.assertEqual(call("proxmox_node_power", {"node": "prox1", "command": "reboot"})["status"], "awaiting_approval")
        self.assertIn("REBOOT the whole node prox1", cards()[-1][1]["summary"])
        for bad in ({"node": "../x", "command": "reboot"}, {"node": "prox1", "command": "poweroff"}, {"node": "", "command": "reboot"}):
            self.assertIn("error", call("proxmox_node_power", bad), bad)
        r = call("proxmox_node_power", {"node": "nonexistent", "command": "shutdown"})
        self.assertEqual(infra.resolve(UID, r["approval_id"], True)[2], "failed")
        self.assertEqual(SENT, [])

    def test_a_failed_task_is_reported_as_failed_not_done(self):
        SEND_FAIL["exit"] = "VM is locked (backup)"
        r = call("proxmox_guest_power", {"vmid": 108, "action": "start"})
        ok, msg, status = infra.resolve(UID, r["approval_id"], True)
        self.assertEqual(status, "failed")
        self.assertIn("locked", msg)
        self.assertIn("failed", last_message_text())

    def test_a_role_that_may_not_is_explained_in_plain_words(self):
        SEND_FAIL["status"] = 403
        r = call("proxmox_guest_power", {"vmid": 108, "action": "start"})
        ok, msg, status = infra.resolve(UID, r["approval_id"], True)
        self.assertEqual(status, "failed")
        self.assertIn("PVEVMAdmin", msg)

    def test_a_dead_node_is_skipped_but_a_timeout_is_never_sent_twice(self):
        SEND_FAIL["dead"] = {"10.1.1.7"}
        self.approve(call("proxmox_guest_power", {"vmid": 108, "action": "start"}))
        self.assertEqual([s["url"].split("/")[2] for s in SENT], ["10.1.1.7:8006", "10.1.1.93:8006"])
        SENT.clear()
        SEND_FAIL.clear()
        SEND_FAIL["timeout"] = True
        r = call("proxmox_guest_power", {"vmid": 108, "action": "start"})
        ok, msg, status = infra.resolve(UID, r["approval_id"], True)
        self.assertEqual(status, "failed")
        self.assertIn("may or may not", msg)
        self.assertEqual(len(SENT), 1, "not repeated on the other node")

    def test_the_test_button_says_whether_the_token_can_change_things(self):
        self.assertIn("CAN change guests", px.test(WS)["detail"])
        SEND_FAIL["perms"] = {"Sys.Audit": 1, "VM.Audit": 1}
        self.assertIn("read-only", px.test(WS)["detail"])

    def test_the_read_module_still_cannot_send_anything_but_get(self):
        src = (base.ROOT / "infra_proxmox.py").read_text(encoding="utf-8")
        for verb in ('method="POST"', 'method="PUT"', 'method="DELETE"', "pw.", "infra_proxmox_write"):
            self.assertNotIn(verb, src, verb)
        wsrc = (base.ROOT / "infra_proxmox_write.py").read_text(encoding="utf-8")
        for bad in ("CERT_NONE", "check_hostname = False", "_create_unverified_context", "verify=False"):
            self.assertNotIn(bad, wsrc)

    def test_every_change_is_in_the_audit_log_without_the_secret(self):
        self.approve(call("proxmox_guest_power", {"vmid": 108, "action": "start"}))
        kinds = {r["kind"] for r in infra.recent_log(30, "proxmox")}
        self.assertTrue({"approval_requested", "write"} <= kinds)
        self.assertNotIn(base.SECRET, json.dumps(infra.recent_log(50)))


# ── SSH ──────────────────────────────────────────────────────────────────
PRIV = "-----BEGIN OPENSSH PRIVATE KEY-----\nPLANTEDPRIVATEKEYMATERIAL0123456789\n-----END OPENSSH PRIVATE KEY-----"
PUB = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPLANTEDPUBLICKEY nori-agent"
HOSTS = "pve1 root@10.1.1.7\np2 root@10.1.1.93:2222"
KNOWN = "10.1.1.7 ssh-ed25519 AAAAC3HOSTKEYONE\n[10.1.1.93]:2222 ssh-ed25519 AAAAC3HOSTKEYTWO"
RAN: list[dict] = []
EXEC_ANSWER: dict = {}
SSH_TOOLS = {"ssh_hosts", "ssh_named", "ssh_exec"}


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _write(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def fake_exec(argv, stdin, timeout, env):
    rec = {"argv": list(argv), "env": dict(env), "timeout": timeout}
    if argv[0] == "ssh":
        key = argv[argv.index("-i") + 1]
        rec["key_mode"] = oct(os.stat(key).st_mode & 0o777)
        rec["key_text"] = _read(key)
        kh = [a for a in argv if a.startswith("UserKnownHostsFile=")][0].split("=", 1)[1]
        rec["known_hosts"] = _read(kh)
        rec["key_path"] = key
    RAN.append(rec)
    if argv[0] == "ssh-keygen" and "-q" in argv:
        path = argv[argv.index("-f") + 1]
        _write(path, PRIV + "\n")
        _write(path + ".pub", PUB + "\n")
        return 0, b"", b""
    if argv[0] == "ssh-keyscan":
        host = argv[-1]
        if host == "10.1.1.93":
            return 0, b"# 10.1.1.93:2222 SSH-2.0-OpenSSH\n10.1.1.93 ssh-ed25519 AAAAC3HOSTKEYTWO\n", b""
        if host == "10.1.1.7":
            return 0, b"10.1.1.7 ssh-ed25519 AAAAC3HOSTKEYONE\n", b""
        return 1, b"", b"connection refused"
    if argv[0] == "ssh-keygen" and "-lf" in argv:
        first = _read(argv[-1]).split()[0]
        return 0, f"256 SHA256:FINGERPRINTOF{first.strip('[]').replace(':', '')} {first} (ED25519)\n".encode(), b""
    ans = EXEC_ANSWER.get("answer")
    if ans is not None:
        return ans(argv) if callable(ans) else ans
    return 0, b"ok output\n", b""


ssh._exec = fake_exec


def ssh_on(known=KNOWN):
    ok, msg = infra.save_credentials(WS, "ssh", {"hosts": HOSTS}, UID)
    assert ok, msg
    infra.set_credential_values(WS, "ssh", {"private_key": PRIV, "public_key": PUB, "known_hosts": known, "fingerprints": "pve1: SHA256:abc (ED25519)"}, UID)
    assert infra.set_enabled(WS, "ssh", True, UID)[0]


class TheSshHosts(unittest.TestCase):
    def test_parsing(self):
        h = ssh.parse_hosts("# comment\npve1 root@10.1.1.7\n\np2   admin@host.example.com:2222")
        self.assertEqual(h["pve1"], {"user": "root", "address": "10.1.1.7", "port": 22})
        self.assertEqual(h["p2"], {"user": "admin", "address": "host.example.com", "port": 2222})
        for bad in ("pve1", "pve1 10.1.1.7", "pve1 root@-oProxyCommand=x", "1pve root@h", "pve1 root@h:99999", "pve1 root@h\npve1 root@i", "pve1 ro ot@h",
                    "pve1 root@h; id", "pve1 root@$(id)", "", "\n".join(f"h{i} root@10.0.0.{i}" for i in range(21))):
            with self.assertRaises(ssh.SshError, msg=bad):
                ssh.parse_hosts(bad)
        self.assertIn("name user@address", infra.save_credentials(WS, "ssh", {"hosts": "nonsense"}, UID)[1])


class SshOffAndOn(unittest.TestCase):
    def setUp(self):
        reset()
        RAN.clear()
        EXEC_ANSWER.clear()

    def test_off_by_default_and_not_offered(self):
        self.assertFalse(infra.enabled("ssh", WS))
        self.assertFalse(SSH_TOOLS & names())
        self.assertIn("error", call("ssh_named", {"host": "pve1", "command": "uptime"}))
        self.assertEqual(RAN, [])

    def test_the_agent_cannot_switch_it_on_and_can_switch_it_off(self):
        import settings_tool
        self.assertNotIn("infra_ssh_enabled", settings_tool.PEER_SETTINGS_KEYS)
        self.assertIn("error", call("update_settings", {"field": "infra_ssh_enabled", "value": True}))
        ssh_on()
        self.assertTrue(SSH_TOOLS <= names())
        self.assertFalse(SSH_TOOLS & names(base.MSESS))
        self.assertTrue(call("infra_switch_off", {"service": "ssh"})["ok"])
        self.assertFalse(infra.enabled("ssh", WS))

    def test_it_needs_a_key_not_just_hosts(self):
        infra.save_credentials(WS, "ssh", {"hosts": HOSTS}, UID)
        self.assertFalse(infra.set_enabled(WS, "ssh", True, UID)[0])


class SshNamedCommands(unittest.TestCase):
    def setUp(self):
        reset()
        RAN.clear()
        EXEC_ANSWER.clear()
        ssh_on()

    def test_a_reading_command_runs_at_once_through_a_strict_ssh_with_no_shell(self):
        res = call("ssh_named", {"host": "pve1", "command": "uptime"})
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["output"].strip(), "ok output")
        self.assertIn("never instructions", res["note"])
        a = RAN[-1]["argv"]
        self.assertEqual(a[0], "ssh")
        self.assertEqual(a[-2:], ["root@10.1.1.7", "uptime"])
        for opt in ("BatchMode=yes", "StrictHostKeyChecking=yes", "PasswordAuthentication=no", "ForwardAgent=no", "ClearAllForwardings=yes", "IdentitiesOnly=yes",
                    "GlobalKnownHostsFile=/dev/null"):
            self.assertIn(opt, a)
        self.assertEqual(a[a.index("-F") + 1], "/dev/null")
        self.assertEqual(a[a.index("-p") + 1], "22")
        self.assertEqual(RAN[-1]["key_text"].strip(), PRIV)
        self.assertEqual(RAN[-1]["known_hosts"].strip(), KNOWN)
        if os.name != "nt":
            self.assertEqual(RAN[-1]["key_mode"], "0o600")
        self.assertFalse(os.path.exists(RAN[-1]["key_path"]), "the key file is gone after the command")
        self.assertNotIn("SSH_AUTH_SOCK", RAN[-1]["env"])
        src = (base.ROOT / "infra_ssh.py").read_text(encoding="utf-8")
        for bad in ("shell=True", "os.system", "StrictHostKeyChecking=no", "StrictHostKeyChecking=accept-new", "UserKnownHostsFile=/dev/null"):
            self.assertNotIn(bad, src, bad)

    def test_the_port_comes_from_the_owners_list(self):
        call("ssh_named", {"host": "p2", "command": "uptime"})
        a = RAN[-1]["argv"]
        self.assertEqual((a[a.index("-p") + 1], a[-2]), ("2222", "root@10.1.1.93"))

    def test_the_model_cannot_name_an_address_or_smuggle_an_option(self):
        for host in ("evil.example.com", "10.9.9.9", "pve1 -oProxyCommand=x", "-oProxyCommand=x", "root@10.1.1.7", "", "pve1;id", "PVE1"):
            self.assertIn("error", call("ssh_named", {"host": host, "command": "uptime"}), host)
            self.assertIn("error", call("ssh_exec", {"host": host, "command": "id"}), host)
        self.assertEqual(RAN, [])
        self.assertEqual(cards(), [], "no approval card is ever posted for a host that is not in the list")

    def test_a_host_whose_key_is_not_pinned_is_refused(self):
        infra.set_credential_values(WS, "ssh", {"known_hosts": "[10.1.1.93]:2222 ssh-ed25519 AAAAC3HOSTKEYTWO"}, UID)
        res = call("ssh_named", {"host": "pve1", "command": "uptime"})
        self.assertIn("not pinned", res["error"])
        self.assertEqual(RAN, [])

    def test_parameters_are_checked_and_quoted_never_pasted(self):
        for bad in ({"command": "service_status", "params": {"unit": "nginx; rm -rf /"}}, {"command": "service_status", "params": {"unit": "$(id)"}},
                    {"command": "service_status", "params": {"unit": "-oProxy"}}, {"command": "service_status", "params": {}}, {"command": "docker_logs", "params": {"container": "a b"}},
                    {"command": "docker_logs", "params": {"container": "x`id`"}}, {"command": "docker_logs", "params": {"container": "ok", "lines": 99999}},
                    {"command": "journal", "params": {"lines": "50; id"}}, {"command": "journal", "params": {"unit": "a|b"}}, {"command": "nonsense"}, {"command": "uptime", "params": "x"}):
            self.assertIn("error", call("ssh_named", {"host": "pve1", **bad}), bad)
        self.assertEqual(RAN, [])
        call("ssh_named", {"host": "pve1", "command": "service_status", "params": {"unit": "nginx.service"}})
        self.assertEqual(RAN[-1]["argv"][-1], "systemctl status nginx.service --no-pager -n 20")
        call("ssh_named", {"host": "pve1", "command": "journal", "params": {"lines": 20, "unit": "ssh@x.service"}})
        self.assertEqual(RAN[-1]["argv"][-1], "journalctl -n 20 --no-pager -u ssh@x.service")
        call("ssh_named", {"host": "pve1", "command": "docker_logs", "params": {"container": "web-1", "lines": 5}})
        self.assertEqual(RAN[-1]["argv"][-1], "docker logs --tail 5 web-1 2>&1")

    def test_every_catalog_entry_builds_a_command(self):
        for name, (reads, desc, params, build) in ssh.NAMED.items():
            ok_params = {"unit": "nginx.service", "container": "web"}
            self.assertIsInstance(build(ok_params), str, name)
        self.assertEqual({n for n, v in ssh.NAMED.items() if not v[0]}, {"service_restart", "docker_restart", "apt_update", "apt_upgrade"})

    def test_a_maintenance_command_asks_first_then_runs_what_was_shown(self):
        res = call("ssh_named", {"host": "pve1", "command": "service_restart", "params": {"unit": "nginx.service"}})
        self.assertEqual(res["status"], "awaiting_approval")
        self.assertEqual(RAN, [])
        self.assertIn("systemctl restart nginx.service", cards()[-1][1]["summary"])
        ok, msg, status = infra.resolve(UID, res["approval_id"], True)
        self.assertEqual(status, "done", msg)
        self.assertEqual(RAN[-1]["argv"][-1], "systemctl restart nginx.service")
        infra.set_policy(WS, "ssh.maintenance", "auto", UID)
        self.assertEqual(call("ssh_named", {"host": "pve1", "command": "docker_restart", "params": {"container": "web"}})["done"], True)
        self.assertEqual(RAN[-1]["argv"][-1], "docker restart web")

    def test_inside_a_container_and_a_vm_through_the_node(self):
        call("ssh_named", {"host": "p2", "command": "uptime", "guest": 108, "guest_type": "lxc"})
        self.assertEqual(RAN[-1]["argv"][-1], "pct exec 108 -- sh -c uptime")
        call("ssh_named", {"host": "p2", "command": "disk", "guest": 105, "guest_type": "vm"})
        self.assertTrue(RAN[-1]["argv"][-1].startswith("qm guest exec 105 --timeout "))
        for bad in ({"guest": "108", "guest_type": "lxc"}, {"guest": 108, "guest_type": "docker"}, {"guest": 108}, {"guest": 0, "guest_type": "lxc"}, {"guest": True, "guest_type": "lxc"}):
            self.assertIn("error", call("ssh_named", {"host": "p2", "command": "uptime", **bad}), bad)
        EXEC_ANSWER["answer"] = (0, json.dumps({"exitcode": 3, "exited": 1, "out-data": "from the vm\n", "err-data": "oops"}).encode(), b"")
        res = call("ssh_named", {"host": "p2", "command": "uptime", "guest": 105, "guest_type": "vm"})
        self.assertEqual((res["exit_code"], res["output"].strip(), res["stderr"]), (3, "from the vm", "oops"))

    def test_output_is_cleaned_capped_and_labelled(self):
        EXEC_ANSWER["answer"] = (0, b"\x1b[31mred\x07 " + b"x" * 60000, b"")
        res = call("ssh_named", {"host": "pve1", "command": "uptime"})
        self.assertNotIn("\x1b", res["output"])
        self.assertNotIn("\x07", res["output"])
        self.assertLess(len(res["output"]), ssh.MAX_OUT + 200)
        self.assertIn("left out", res["output"])

    def test_ssh_failures_are_explained(self):
        for stderr, want in ((b"Host key verification failed.", "does not match"), (b"root@x: Permission denied (publickey).", "authorized_keys"),
                             (b"ssh: connect to host x port 22: Connection refused", "refused the connection"), (b"Connection timed out", "timed out"),
                             (b"ssh: Could not resolve hostname x: Name or service not known", "find that address")):
            EXEC_ANSWER["answer"] = (255, b"", stderr)
            self.assertIn(want, call("ssh_named", {"host": "pve1", "command": "uptime"})["error"], stderr)
        EXEC_ANSWER["answer"] = (2, b"", b"nope")
        res = call("ssh_named", {"host": "pve1", "command": "uptime"})
        self.assertEqual((res["ok"], res["exit_code"]), (True, 2), "a command that fails is a result, not an ssh error")
        self.assertFalse(infra.recent_log(1)[0]["ok"])


class SshAnyCommand(unittest.TestCase):
    def setUp(self):
        reset()
        RAN.clear()
        EXEC_ANSWER.clear()
        ssh_on()

    def test_it_is_always_shown_in_full_and_runs_only_on_approve(self):
        cmd = "cat /etc/hostname && echo '*bold*' _under_"
        res = call("ssh_exec", {"host": "pve1", "command": cmd})
        self.assertEqual(res["status"], "awaiting_approval")
        self.assertEqual(RAN, [])
        (mid, card), = cards()
        self.assertIn(cmd, card["summary"])
        self.assertIn("pve1", card["summary"])
        msg = store.read(lambda c: c.execute("SELECT content FROM messages WHERE id=?", (mid,)).fetchone()["content"])
        self.assertIn("```\n", msg, "the message shows it in a code block, so the chat does not restyle it")
        self.assertIn(cmd, msg)
        ok, text, status = infra.resolve(UID, res["approval_id"], True)
        self.assertEqual(status, "done", text)
        self.assertEqual(RAN[-1]["argv"][-1], cmd)
        self.assertEqual(len([r for r in RAN if r["argv"][0] == "ssh"]), 1)

    def test_it_cannot_be_allowed_to_run_by_itself(self):
        self.assertFalse(infra.set_policy(WS, "ssh.exec", "auto", UID)[0])
        store.write(lambda c: c.execute("INSERT INTO infra_policies(workspace_id, permission, policy, updated_ts) VALUES (?,?,?,0)", (WS, "ssh.exec", "auto")))
        store.write(lambda c: c.execute("INSERT INTO infra_policies(workspace_id, permission, policy, updated_ts) VALUES (?,?,?,0)", (WS, "ssh.guest_exec", "auto")))
        self.assertEqual(call("ssh_exec", {"host": "pve1", "command": "id"})["status"], "awaiting_approval")
        self.assertEqual(call("ssh_exec", {"host": "pve1", "command": "id", "guest": 108, "guest_type": "lxc"})["status"], "awaiting_approval")
        self.assertEqual(RAN, [])

    def test_deny_and_deny_all_are_honoured(self):
        infra.set_policy(WS, "ssh.exec", "deny", UID)
        self.assertIn("disallowed", call("ssh_exec", {"host": "pve1", "command": "id"})["error"])
        self.assertEqual((RAN, cards()), ([], []))

    def test_a_denied_command_never_runs(self):
        res = call("ssh_exec", {"host": "pve1", "command": "id"})
        self.assertEqual(infra.resolve(UID, res["approval_id"], False)[2], "denied")
        self.assertEqual(RAN, [])
        self.assertFalse(infra.resolve(UID, res["approval_id"], True)[0], "and approving it afterwards does nothing")
        self.assertEqual(RAN, [])

    def test_a_command_in_a_guest_is_wrapped_and_quoted_on_the_node(self):
        cmd = "echo 'a b' ; ls /tmp"
        res = call("ssh_exec", {"host": "p2", "command": cmd, "guest": 108, "guest_type": "lxc"})
        self.assertIn("inside container 108 on p2", cards()[-1][1]["summary"])
        infra.resolve(UID, res["approval_id"], True)
        import shlex
        self.assertEqual(RAN[-1]["argv"][-1], "pct exec 108 -- sh -c " + shlex.quote(cmd))

    def test_bad_input_is_refused_before_a_card_is_posted(self):
        for bad in ({"command": ""}, {"command": "   "}, {"command": "x" * 1001}, {"command": "a\x00b"}, {"command": "id", "timeout": 0}, {"command": "id", "timeout": 9999},
                    {"command": "id", "timeout": "5"}, {"command": ["id"]}):
            self.assertIn("error", call("ssh_exec", {"host": "pve1", **bad}), bad)
        self.assertEqual((RAN, cards()), ([], []))

    def test_the_same_request_twice_is_one_card(self):
        a = call("ssh_exec", {"host": "pve1", "command": "id"})
        b = call("ssh_exec", {"host": "pve1", "command": "id"})
        self.assertEqual(a["approval_id"], b["approval_id"])
        self.assertEqual(len(cards()), 1)

    def test_it_is_logged_and_the_key_is_in_no_log_or_result(self):
        res = call("ssh_exec", {"host": "pve1", "command": "id"})
        infra.resolve(UID, res["approval_id"], True)
        blob = json.dumps(infra.recent_log(50)) + json.dumps(res) + json.dumps(cards())
        self.assertNotIn("PLANTEDPRIVATEKEYMATERIAL", blob)
        self.assertTrue({"approval_requested", "write"} <= {r["kind"] for r in infra.recent_log(20, "ssh")})


class SshSettingsPage(unittest.TestCase):
    def setUp(self):
        reset()
        RAN.clear()
        EXEC_ANSWER.clear()

    def test_generating_a_key_stores_it_encrypted_and_shows_only_the_public_half(self):
        infra.save_credentials(WS, "ssh", {"hosts": HOSTS}, UID)
        status, page = web("/admin/infra/ssh/do-genkey", {})
        self.assertIn("a new key was made", page)
        self.assertIn(PUB, page)
        self.assertNotIn("PLANTEDPRIVATEKEYMATERIAL", page)
        self.assertEqual(infra.credentials(WS, "ssh")["private_key"].strip(), PRIV)
        raw = store.read(lambda c: c.execute("SELECT data_enc FROM infra_credentials WHERE service='ssh'").fetchone()["data_enc"])
        self.assertNotIn("PLANTEDPRIVATEKEYMATERIAL", raw)
        status, page = web("/settings?tab=infra")
        self.assertNotIn("PLANTEDPRIVATEKEYMATERIAL", page)
        self.assertIn("made and saved (never shown)", page)
        self.assertNotIn("PLANTEDPRIVATEKEYMATERIAL", json.dumps(__import__("debug_api").infra_section({})))

    def test_saving_the_form_again_keeps_the_key_and_the_pins(self):
        ssh_on()
        status, page = web("/admin/infra/ssh/credentials", {"hosts": HOSTS + "\npve3 root@10.1.1.89"})
        c = infra.credentials(WS, "ssh")
        self.assertEqual((c["private_key"].strip(), c["known_hosts"], c["public_key"]), (PRIV, KNOWN, PUB))
        self.assertIn("pve3", c["hosts"])

    def test_pinning_scans_each_host_and_shows_the_fingerprints_to_compare(self):
        infra.save_credentials(WS, "ssh", {"hosts": HOSTS + "\ngone root@10.9.9.9"}, UID)
        status, page = web("/admin/infra/ssh/do-pin", {})
        self.assertIn("pinned the host keys of 2 of 3", page)
        self.assertIn("gone", page)
        c = infra.credentials(WS, "ssh")
        self.assertIn("10.1.1.7 ssh-ed25519 AAAAC3HOSTKEYONE", c["known_hosts"])
        self.assertIn("[10.1.1.93]:2222 ssh-ed25519 AAAAC3HOSTKEYTWO", c["known_hosts"], "a non-standard port is pinned under [address]:port, where ssh looks it up")
        self.assertNotIn("# 10.1.1.93", c["known_hosts"])
        self.assertIn("SHA256:", page)
        scan = [r["argv"] for r in RAN if r["argv"][0] == "ssh-keyscan"]
        self.assertEqual([a[a.index("-p") + 1] for a in scan], ["22", "2222", "22"])

    def test_the_buttons_need_an_admin_and_a_csrf_token_and_known_names(self):
        self.assertEqual(web("/admin/infra/ssh/do-genkey", {}, token=base.MEMBER_TOKEN, csrf=base.MEMBER_CSRF)[0], 403)
        self.assertEqual(web("/admin/infra/ssh/do-nonsense", {})[0], 404)
        self.assertEqual(web("/admin/infra/proxmox/do-genkey", {})[0], 404)

    def test_the_test_button_logs_in_to_each_host(self):
        ssh_on()
        EXEC_ANSWER["answer"] = lambda argv: (0, (b"pve1\n" if argv[-2].endswith("10.1.1.7") else b"p2\n"), b"")
        status, page = web("/admin/infra/ssh/test", {})
        self.assertIn("logged in to 2 hosts: pve1, p2", page)
        EXEC_ANSWER["answer"] = (255, b"", b"Permission denied (publickey)")
        status, page = web("/admin/infra/ssh/test", {})
        self.assertIn("authorized_keys", page)

    def test_the_whole_thing_is_off_until_switched_on_here(self):
        status, page = web("/settings?tab=infra")
        self.assertIn("SSH shell", page)
        self.assertIn("pin host keys", page)
        self.assertIn("generate a key", page)
        self.assertIn("always asks", page)
        status, page = web("/admin/infra/ssh/switch", {"on": "1"})
        self.assertIn("enter the credentials first", page)


if __name__ == "__main__":
    unittest.main()
