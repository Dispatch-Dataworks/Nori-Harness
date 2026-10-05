"""The sandbox runner (sandbox_service/runner.py).

Two groups. The helper tests (path safety, command allowlist, output capping,
the changed-file manifest) run anywhere. The "real run" tests execute hostile
code through the runner's own run() and need what the runner has inside its
container -- root, Linux, the run uid, /work -- so they SKIP elsewhere and are
run in the sandbox container itself:

    docker compose exec -T sandbox sh -c "cat > /tmp/t.py" < tests/test_nori_sandbox_runner.py
    docker compose exec sandbox python3 /tmp/t.py
"""
import base64
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
for cand in (HERE, os.path.join(HERE, "..", "sandbox_service"), "/opt/runner"):
    if os.path.exists(os.path.join(cand, "runner.py")):
        sys.path.insert(0, cand)
        break
import runner  # noqa: E402

IN_SANDBOX = sys.platform.startswith("linux") and hasattr(os, "geteuid") and os.geteuid() == 0 \
    and os.path.isdir(runner.WORK_DIR) and os.path.exists(runner.SOCK_PATH)


def b64(s: str | bytes) -> str:
    return base64.b64encode(s.encode() if isinstance(s, str) else s).decode()


def _rpc(req: dict) -> dict:
    """Through the live runner's socket, exactly like Nori -- NOT runner.run()
    in this process: the runner is PID 1 in its container, and what it must
    reap (orphans of a killed run) only behaves the same when it's the one
    running the code."""
    import json
    import socket
    import struct
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(runner.SOCK_PATH)
    body = json.dumps(req).encode()
    s.sendall(struct.pack(">I", len(body)) + body)

    def exact(n):
        buf = b""
        while len(buf) < n:
            chunk = s.recv(n - len(buf))
            if not chunk:
                raise RuntimeError("closed")
            buf += chunk
        return buf
    (length,) = struct.unpack(">I", exact(4))
    out = json.loads(exact(length))
    s.close()
    return out


def go(code: str, files=None, timeout=10, cwd="", command=None):
    r = _rpc({"op": "run", "command": command or ["python3", "-c", code], "files": files or {},
              "timeout_s": timeout, "cwd": cwd})
    assert r.get("ok"), r
    return r


class Helpers(unittest.TestCase):
    def test_safe_rel(self):
        self.assertEqual(runner.safe_rel(""), "")
        self.assertEqual(runner.safe_rel("./a//b/./c.txt"), "a/b/c.txt")
        self.assertEqual(runner.safe_rel("a\\b"), "a/b")
        for bad in ("../x", "a/../../x", "/etc/passwd", "C:/x", "a/..", "x\x00y"):
            with self.assertRaises(runner.RunError, msg=bad):
                runner.safe_rel(bad)

    def test_command_allowlist(self):
        self.assertEqual(runner.split_command("python3 -c 'print(1)'"), ["python3", "-c", "print(1)"])
        self.assertEqual(runner.split_command(["python3", "x.py"]), ["python3", "x.py"])
        for bad in ("sh -c ls", "bash", "/bin/sh", "python3x", "", "  ", "curl http://x", ["rm", "-rf", "/"]):
            with self.assertRaises(runner.RunError, msg=str(bad)):
                runner.split_command(bad)
        with self.assertRaises(runner.RunError):
            runner.split_command("python3 'unterminated")

    def test_cap_text_keeps_head_and_tail(self):
        text, trunc = runner.cap_text(b"short")
        self.assertEqual((text, trunc), ("short", False))
        big = b"H" * 50_000 + b"T" * 50_000
        text, trunc = runner.cap_text(big, 3000)
        self.assertTrue(trunc)
        self.assertTrue(text.startswith("HHH") and text.endswith("TTT") and "omitted" in text)
        self.assertLess(len(text), 3200)

    def test_diff_manifest(self):
        before = {"keep": "a", "edit": "b", "gone": "c"}
        after = {"keep": ("a", 1, "/x"), "edit": ("B", 2, "/x"), "new": ("n", 3, "/x")}
        changed, deleted = runner.diff_manifest(before, after)
        self.assertEqual([(c["path"], c["status"]) for c in changed], [("edit", "modified"), ("new", "created")])
        self.assertEqual(deleted, ["gone"])


@unittest.skipUnless(IN_SANDBOX, "needs the sandbox container (root, Linux, /work)")
class RealRuns(unittest.TestCase):
    def test_stdout_exit_and_user(self):
        r = go("import os; print('hi', os.getuid(), os.getgid(), os.getgroups())")
        self.assertTrue(r["ok"])
        self.assertEqual(r["exit_code"], 0)
        self.assertEqual(r["stdout"].split()[:3], ["hi", str(runner.RUN_UID), str(runner.RUN_GID)])
        self.assertIn("[]", r["stdout"])

    def test_nonzero_exit_and_stderr(self):
        r = go("import sys; sys.stderr.write('boom'); sys.exit(3)")
        self.assertEqual(r["exit_code"], 3)
        self.assertEqual(r["stderr"], "boom")

    def test_timeout_kills_and_nothing_survives(self):
        t0 = time.time()
        r = go("""
import os, subprocess, time
subprocess.Popen(['python3', '-c', 'import time; time.sleep(600)'], start_new_session=True)
while True: pass
""", timeout=2)
        self.assertTrue(r["timed_out"])
        self.assertIsNone(r["exit_code"])
        self.assertLess(time.time() - t0, 8)
        time.sleep(0.3)
        survivors = []
        for pid in os.listdir("/proc"):
            if pid.isdigit():
                try:
                    with open(f"/proc/{pid}/status") as fh:
                        status = fh.read()
                except OSError:
                    continue
                if f"Uid:\t{runner.RUN_UID}" in status:
                    survivors.append(pid)
        self.assertEqual(survivors, [], "a daemonized child outlived its run")

    def test_no_network_at_all(self):
        r = go("""
import socket
res = []
for host in ('1.1.1.1', '10.0.0.1', '127.0.0.1'):
    s = socket.socket(); s.settimeout(2)
    try:
        s.connect((host, 53)); res.append(host + ':connected')
    except Exception as e:
        res.append(host + ':' + type(e).__name__)
try:
    socket.getaddrinfo('example.com', 80); res.append('dns:resolved')
except Exception as e:
    res.append('dns:' + type(e).__name__)
print(res)
""")
        self.assertNotIn("connected", r["stdout"])
        self.assertNotIn("resolved", r["stdout"])

    def test_cannot_write_outside_its_copy(self):
        r = go("""
out = []
for p in ('/usr/local/x', '/etc/x', '/work/x', '/run/sandbox/x', '/opt/runner/x', '/x'):
    try:
        open(p, 'w').write('1'); out.append(p + ':WROTE')
    except Exception as e:
        out.append(p + ':' + type(e).__name__)
print(out)
""")
        self.assertNotIn("WROTE", r["stdout"])

    def test_cannot_read_the_runner_or_other_runs(self):
        go("open('marker.txt', 'w').write('first run')", files={"a.txt": b64("x")})
        r = go("""
import os, glob
print(sorted(os.listdir('/work')), glob.glob('/work/*/ws/*'))
try:
    print(open('/proc/1/environ').read()[:50])
except Exception as e:
    print('environ:', type(e).__name__)
print(os.path.exists('/run/sandbox/runner.sock') and os.access('/run/sandbox/runner.sock', os.R_OK | os.W_OK))
""")
        self.assertNotIn("marker.txt", r["stdout"])
        self.assertIn("environ: PermissionError", r["stdout"])
        self.assertTrue(r["stdout"].rstrip().endswith("False") or "FileNotFound" in r["stdout"])

    def test_environment_is_scrubbed(self):
        r = go("import os; print(sorted(os.environ))")
        names = eval(r["stdout"])
        self.assertTrue(set(names) <= {"PATH", "HOME", "TMPDIR", "LANG", "PYTHONDONTWRITEBYTECODE",
                                       "PYTHONNOUSERSITE", "PYTHONIOENCODING", "LC_CTYPE"}, names)

    def test_files_in_out_and_manifest(self):
        r = go("""
import os
open('out/new.txt', 'w').write('created')
open('data.txt', 'a').write(' + edited')
os.remove('gone.txt')
os.symlink('/etc/passwd', 'link')
""", files={"data.txt": b64("orig"), "gone.txt": b64("bye"), "same.txt": b64("s"), "out/keep.txt": b64("k")})
        by = {c["path"]: c for c in r["changed"]}
        self.assertEqual(set(by), {"out/new.txt", "data.txt"})
        self.assertEqual(by["out/new.txt"]["status"], "created")
        self.assertEqual(base64.b64decode(by["out/new.txt"]["content_b64"]), b"created")
        self.assertEqual(by["data.txt"]["status"], "modified")
        self.assertEqual(base64.b64decode(by["data.txt"]["content_b64"]), b"orig + edited")
        self.assertEqual(r["deleted"], ["gone.txt"])
        self.assertNotIn("link", by, "a symlink must never be followed or returned")

    def test_cwd_is_the_given_subfolder(self):
        r = go("import os; print(os.getcwd().split('/ws')[-1], os.listdir('.'))", cwd="sub/dir",
               files={"sub/dir/f.txt": b64("x"), "top.txt": b64("y")})
        self.assertEqual(r["stdout"].strip(), "/sub/dir ['f.txt']")

    def test_output_is_capped(self):
        r = go("import sys; sys.stdout.write('x' * 2_000_000)")
        self.assertTrue(r["stdout_truncated"])
        self.assertLess(len(r["stdout"]), runner.OUTPUT_CAP + 200)

    def test_memory_limit(self):
        r = go("x = bytearray(3 * 1024 * 1024 * 1024); print('allocated')")
        self.assertNotIn("allocated", r["stdout"])
        self.assertNotEqual(r["exit_code"], 0)

    def test_fork_bomb_is_bounded(self):
        t0 = time.time()
        r = go("""
import os, time
n = 0
for _ in range(1000):
    try:
        if os.fork() == 0:
            time.sleep(30); os._exit(0)
        n += 1
    except OSError:
        break
print('forked', n)
""", timeout=5)
        self.assertLess(time.time() - t0, 12)
        self.assertIn("forked", r["stdout"], r["stderr"])
        n = int(r["stdout"].split()[-1])
        # Some forks succeed (the limit is real but not zero), and it stops at the cap.
        self.assertGreater(n, 5)
        self.assertLess(n, runner.MAX_PROCS + 5)

    def test_rejected_requests(self):
        for req in ({"command": "sh -c id", "files": {}},
                    {"command": "python3 -c 1", "files": {"../escape": b64("x")}},
                    {"command": "python3 -c 1", "files": {"/abs": b64("x")}},
                    {"command": "python3 -c 1", "files": {"a": "!!!notbase64"}},
                    {"command": "python3 -c 1", "cwd": "../.."},
                    {"command": "python3 -c 1", "timeout_s": 100000}):
            r = _rpc({"op": "run", **req})
            self.assertFalse(r["ok"], str(req))
            self.assertTrue(r["error"], str(req))

    def test_workdir_wiped_after_each_run(self):
        go("open('x', 'w').write('1')", files={"y": b64("2")})
        self.assertEqual([d for d in os.listdir(runner.WORK_DIR) if d.startswith("run-")], [])


if __name__ == "__main__":
    unittest.main()
