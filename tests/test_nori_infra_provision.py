# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""scripts/infra_provision.py (2026-11-01): what the person who deploys the instance runs inside the container to set up Proxmox and SSH. It must do exactly what
the settings page does, through the same code: encrypted credentials, the same validation, the same audit log, no secret on a command line or in any output, host
keys pinned only against a fingerprint read from the server itself, and the same never-auto rules for policies. Shares the stand-in cluster and ssh of
test_nori_infra_change.py."""
import contextlib
import importlib.util
import io
import json
import os
import sys
import unittest

import test_nori_infra_change as chg  # noqa: E402  (also brings in test_nori_infra: the scratch instance, the pretend Proxmox and the fake ssh programs)

base = chg.base
infra, store, ssh = chg.infra, chg.store, chg.ssh
WS, names, call = chg.WS, chg.names, chg.call


def _script():
    spec = importlib.util.spec_from_file_location("infra_provision", base.ROOT / "scripts" / "infra_provision.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TheOperatorScript(unittest.TestCase):
    def setUp(self):
        chg.reset()
        chg.RAN.clear()
        chg.EXEC_ANSWER.clear()
        self.mod = _script()

    def run_cmd(self, *argv, env=None, stdin=None):
        buf = io.StringIO()
        old_env, old_in = dict(os.environ), sys.stdin
        os.environ.update(env or {})
        if stdin is not None:
            sys.stdin = io.StringIO(stdin)
        try:
            with contextlib.redirect_stdout(buf):
                code = self.mod.main(list(argv))
        finally:
            os.environ.clear()
            os.environ.update(old_env)
            sys.stdin = old_in
        return code, json.loads(buf.getvalue()), buf.getvalue()

    def test_proxmox_is_set_up_through_the_same_validation_and_the_secret_is_never_printed(self):
        code, out, raw = self.run_cmd("proxmox-set", "--hosts", "10.1.1.7,10.1.1.93", "--token-id", base.TOKEN_ID, env={"PROXMOX_TOKEN_SECRET": base.SECRET})
        self.assertEqual((code, out["ok"], out["message"]), (0, True, "saved"))
        self.assertNotIn(base.SECRET, raw)
        c = infra.credentials(WS, "proxmox")
        self.assertEqual((c["token_secret"], c["token_id"], c["hosts"]), (base.SECRET, base.TOKEN_ID, "10.1.1.7\n10.1.1.93"))
        raw_db = store.read(lambda c: c.execute("SELECT data_enc FROM infra_credentials WHERE service='proxmox'").fetchone()["data_enc"])
        self.assertNotIn(base.SECRET, raw_db)
        code, out, raw = self.run_cmd("proxmox-set", "--hosts", "http://10.1.1.7", "--token-id", base.TOKEN_ID, env={"PROXMOX_TOKEN_SECRET": ""})
        self.assertEqual((code, out["ok"]), (1, False))
        self.assertIn("only https", out["message"])
        self.assertEqual(infra.credentials(WS, "proxmox")["hosts"], "10.1.1.7\n10.1.1.93", "a refused change saves nothing")

    def test_a_blank_secret_keeps_the_stored_one_and_stdin_can_carry_it(self):
        self.run_cmd("proxmox-set", "--hosts", "10.1.1.7", "--token-id", base.TOKEN_ID, "--secret-stdin", stdin=base.SECRET + "\n")
        self.assertEqual(infra.credentials(WS, "proxmox")["token_secret"], base.SECRET)
        self.run_cmd("proxmox-set", "--hosts", "10.1.1.7,10.1.1.93", "--token-id", base.TOKEN_ID)
        self.assertEqual(infra.credentials(WS, "proxmox")["token_secret"], base.SECRET)
        code, out, raw = self.run_cmd("proxmox-set", "--hosts", "10.1.1.7", "--token-id", base.TOKEN_ID, "--secret-stdin", "--ca-stdin", stdin="x")
        self.assertEqual(code, 1)

    def test_enable_needs_credentials_then_turns_the_tools_on_and_disable_off(self):
        code, out, _ = self.run_cmd("enable", "proxmox")
        self.assertEqual((code, out["ok"]), (1, False))
        self.assertIn("credentials", out["message"])
        self.run_cmd("proxmox-set", "--hosts", "10.1.1.7,10.1.1.93", "--token-id", base.TOKEN_ID, env={"PROXMOX_TOKEN_SECRET": base.SECRET})
        code, out, _ = self.run_cmd("proxmox-test")
        self.assertEqual((code, out["ok"]), (0, True))
        self.assertIn("Proxmox VE 9.2.21", out["detail"])
        code, out, _ = self.run_cmd("enable", "proxmox")
        self.assertEqual((code, out["enabled"]), (0, True))
        self.assertTrue(chg.CHANGE_TOOLS <= names())
        self.run_cmd("disable", "proxmox")
        self.assertFalse(chg.CHANGE_TOOLS & names())
        self.assertEqual(self.run_cmd("enable", "nonsense")[0], 1)

    def test_ssh_hosts_key_and_verified_pinning(self):
        code, out, _ = self.run_cmd("ssh-set-hosts", "--hosts", "pve1=root@10.1.1.7,p2=root@10.1.1.93:2222")
        self.assertEqual((code, out["ok"]), (0, True))
        self.assertEqual(self.run_cmd("ssh-set-hosts", "--hosts", "pve1=nonsense")[0], 1)
        code, out, raw = self.run_cmd("ssh-genkey")
        self.assertEqual((code, out["public_key"]), (0, chg.PUB))
        self.assertNotIn("PLANTEDPRIVATEKEYMATERIAL", raw)
        self.assertEqual(self.run_cmd("ssh-genkey")[0], 1, "it will not silently replace a key")
        self.assertEqual(self.run_cmd("ssh-genkey", "--force")[0], 0)
        fp1, fp2 = "SHA256:FINGERPRINTOF10.1.1.7", "SHA256:FINGERPRINTOF10.1.1.93]2222"
        code, out, _ = self.run_cmd("ssh-pin", "--expect", f"pve1={fp1}", "--expect", "p2=SHA256:WRONG")
        self.assertEqual((code, out["ok"]), (1, False))
        self.assertIn("expected SHA256:WRONG", out["message"])
        self.assertNotIn("known_hosts", infra.credentials(WS, "ssh"), "a mismatch pins nothing, not even the host that matched")
        code, out, _ = self.run_cmd("ssh-pin", "--expect", f"pve1={fp1}")
        self.assertIn("no expected fingerprint was given for: p2", out["message"])
        self.assertEqual(self.run_cmd("ssh-pin")[0], 1)
        self.assertEqual(self.run_cmd("ssh-pin", "--expect", "pve1")[0], 1)
        code, out, _ = self.run_cmd("ssh-pin", "--expect", f"pve1={fp1}", "--expect", f"p2={fp2}")
        self.assertEqual((code, out["ok"], out["verified"]), (0, True, True))
        c = infra.credentials(WS, "ssh")
        self.assertIn("10.1.1.7 ssh-ed25519", c["known_hosts"])
        self.assertIn("[10.1.1.93]:2222 ssh-ed25519", c["known_hosts"])
        code, out, _ = self.run_cmd("ssh-pin", "--expect", f"pve1={fp1.replace('SHA256:', '')}", "--expect", f"p2={fp2}")
        self.assertEqual(code, 0, "the fingerprint may be given with or without the SHA256: prefix")

    def test_pin_keeps_only_the_key_that_matched(self):
        self.run_cmd("ssh-set-hosts", "--hosts", "pve1=root@10.1.1.7")

        def two_keys(argv, stdin, timeout, env):
            if argv[0] == "ssh-keyscan":
                return 0, b"10.1.1.7 ssh-ed25519 REALKEY\n10.1.1.7 ssh-rsa EVILKEY\n", b""
            if argv[0] == "ssh-keygen" and "-lf" in argv:
                fp = "SHA256:GOOD" if "REALKEY" in chg._read(argv[-1]) else "SHA256:BAD"
                return 0, f"256 {fp} 10.1.1.7 (X)\n".encode(), b""
            return chg.fake_exec(argv, stdin, timeout, env)
        ssh._exec = two_keys
        try:
            code, out, _ = self.run_cmd("ssh-pin", "--expect", "pve1=SHA256:GOOD")
        finally:
            ssh._exec = chg.fake_exec
        self.assertEqual(code, 0)
        kh = infra.credentials(WS, "ssh")["known_hosts"]
        self.assertIn("REALKEY", kh)
        self.assertNotIn("EVILKEY", kh)

    def test_trust_scan_pins_what_is_offered_and_says_it_was_not_verified(self):
        self.run_cmd("ssh-set-hosts", "--hosts", "pve1=root@10.1.1.7")
        code, out, _ = self.run_cmd("ssh-pin", "--trust-scan")
        self.assertEqual((code, out["verified"]), (0, False))

    def test_the_whole_path_to_a_working_shell(self):
        self.run_cmd("ssh-set-hosts", "--hosts", "pve1=root@10.1.1.7")
        self.assertEqual(self.run_cmd("ssh-test")[0], 1, "no key yet")
        self.run_cmd("ssh-genkey")
        self.run_cmd("ssh-pin", "--expect", "pve1=SHA256:FINGERPRINTOF10.1.1.7")
        chg.EXEC_ANSWER["answer"] = (0, b"pve1\n", b"")
        code, out, _ = self.run_cmd("ssh-test")
        self.assertEqual((code, out["detail"]), (0, "logged in to 1 host: pve1"))
        self.assertEqual(self.run_cmd("enable", "ssh")[0], 0)
        self.assertTrue(chg.SSH_TOOLS <= names())
        res = call("ssh_named", {"host": "pve1", "command": "uptime"})
        self.assertTrue(res["ok"], res)
        self.assertEqual(call("ssh_exec", {"host": "pve1", "command": "id"})["status"], "awaiting_approval", "and a free shell still asks")

    def test_status_shows_what_is_set_and_never_a_secret(self):
        self.run_cmd("proxmox-set", "--hosts", "10.1.1.7", "--token-id", base.TOKEN_ID, env={"PROXMOX_TOKEN_SECRET": base.SECRET})
        self.run_cmd("ssh-set-hosts", "--hosts", "pve1=root@10.1.1.7")
        self.run_cmd("ssh-genkey")
        code, out, raw = self.run_cmd("status")
        self.assertEqual(code, 0)
        self.assertNotIn(base.SECRET, raw)
        self.assertNotIn("PLANTEDPRIVATEKEYMATERIAL", raw)
        px_ = out["services"]["proxmox"]
        self.assertEqual((px_["fields"]["token_secret"], px_["fields"]["token_id"], px_["enabled"]), ("set", base.TOKEN_ID, False))
        self.assertEqual(out["services"]["ssh"]["host_keys"], {"pve1": "NOT pinned"})
        self.assertTrue(out["services"]["ssh"]["has_key"])
        self.assertEqual(out["policies"]["proxmox.destroy"], "ask")

    def test_policy_follows_the_same_rules_as_the_settings_page(self):
        self.assertEqual(self.run_cmd("policy", "proxmox.snapshot", "auto")[0], 0)
        self.assertEqual(infra.policy(WS, "proxmox.snapshot"), "auto")
        code, out, _ = self.run_cmd("policy", "ssh.exec", "auto")
        self.assertEqual((code, out["ok"]), (1, False))
        self.assertIn("always asks", out["message"])
        self.assertEqual(self.run_cmd("policy", "no.such", "ask")[0], 1)

    def test_everything_it_does_is_in_the_audit_log(self):
        self.run_cmd("proxmox-set", "--hosts", "10.1.1.7", "--token-id", base.TOKEN_ID, env={"PROXMOX_TOKEN_SECRET": base.SECRET})
        self.run_cmd("enable", "proxmox")
        kinds = {(r["kind"], r["action"]) for r in infra.recent_log(20, "proxmox")}
        self.assertTrue({("credentials", "saved"), ("switch", "on")} <= kinds)
        self.assertNotIn(base.SECRET, json.dumps(infra.recent_log(50)))

    def test_it_never_takes_a_secret_from_the_command_line(self):
        src = (base.ROOT / "scripts" / "infra_provision.py").read_text(encoding="utf-8")
        for bad in ('"--secret"', '"--token-secret"', '"--password"', '"--private-key"'):
            self.assertNotIn(bad, src)


if __name__ == "__main__":
    unittest.main()
