#!/usr/bin/env python3
"""Integration checks against the REAL Bitwarden CLI (`bw`), if one is on PATH.

These validate that the daemon drives the actual `bw` binary correctly for the paths that
need no account: the binary/version is present, the daemon starts against it, status reads
LOCKED, and a failed `bw unlock` surfaces as our ERROR path. The whole class skips cleanly
when `bw` isn't installed (e.g. the default fake-bw CI job and most local runs).

Success paths (unlock/get/find against a live vault) need a real logged-in account and are
therefore left to a secrets-gated CI job, not run here.
"""
import json, os, shutil, subprocess, sys, tempfile, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
CRED = os.path.join(REPO, "cred")
DAEMON = os.path.join(REPO, "cred-brokerd.py")
BW = shutil.which("bw")


@unittest.skipUnless(BW, "real Bitwarden CLI (bw) not on PATH")
class RealBw(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="credrealbw-")
        with open(os.path.join(self.tmp, "config.json"), "w") as f:
            json.dump({"run_dir": self.tmp, "bw_bin": BW, "proxy": None}, f)
        self.env = dict(os.environ)
        self.env["CRED_CONFIG"] = os.path.join(self.tmp, "config.json")
        self.dlog = open(os.path.join(self.tmp, "daemon.err"), "w")
        self.proc = subprocess.Popen([sys.executable, DAEMON], env=self.env,
                                     stdout=self.dlog, stderr=self.dlog)
        sock = os.path.join(self.tmp, "cred.sock")
        for _ in range(100):
            if os.path.exists(sock):
                break
            time.sleep(0.05)
        else:
            self.fail("daemon did not create socket; see " + self.dlog.name)

    def tearDown(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()
        self.dlog.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cred(self, *args, pw=None):
        p = subprocess.run([sys.executable, CRED, *args], env=self.env,
                           capture_output=True, text=True,
                           input=(pw + "\n") if pw is not None else None)
        return p.returncode, p.stdout, p.stderr

    def test_bw_binary_and_version(self):
        v = subprocess.run([BW, "--version"], capture_output=True, text=True)
        self.assertEqual(v.returncode, 0, v.stderr)
        self.assertRegex(v.stdout.strip(), r"^\d+\.\d+\.\d+")

    def test_status_locked_against_real_bw(self):
        rc, o, e = self.cred("status")
        self.assertTrue(o.startswith("cred: LOCKED"), o)

    def test_unlock_failure_surfaces_as_error(self):
        # A bogus password (or a not-logged-in vault) makes the real `bw unlock` fail;
        # the daemon must turn that into our ERROR path, not hang or crash.
        rc, o, e = self.cred("unlock", "whatever", pw="not-the-real-master-password")
        self.assertIn("cred: ERROR", o)
        self.assertNotEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
