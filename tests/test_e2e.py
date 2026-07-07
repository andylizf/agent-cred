#!/usr/bin/env python3
"""End-to-end tests: the real `cred-brokerd` daemon + the real `cred` CLI, driven against
a fake `bw` (see tests/fake-bw). No real Bitwarden, no network.

Run:  python3 -m unittest discover -s tests -v
"""
import json, os, shutil, socket, subprocess, sys, tempfile, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
CRED = os.path.join(REPO, "cred")
DAEMON = os.path.join(REPO, "cred-brokerd.py")
FAKE_BW = os.path.join(HERE, "fake-bw")
VAULT = os.path.join(HERE, "vault.json")
PW = "correct-pw"


class E2E(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="credtest-")
        cfg = {"run_dir": self.tmp, "bw_bin": FAKE_BW, "proxy": None,
               "autolock_hours": 8, "sync_ttl_seconds": 60}
        self.cfg_path = os.path.join(self.tmp, "config.json")
        with open(self.cfg_path, "w") as f:
            json.dump(cfg, f)
        os.chmod(FAKE_BW, 0o755)
        self.env = dict(os.environ)
        self.env.update(CRED_CONFIG=self.cfg_path, FAKE_BW_VAULT=VAULT)
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

    # ---- the full lifecycle, in order (the daemon is stateful) --------------
    def test_full_lifecycle(self):
        # locked at start
        rc, o, e = self.cred("status")
        self.assertTrue(o.startswith("cred: LOCKED"), o)

        # wrong master password -> ERROR, still locked
        rc, o, e = self.cred("unlock", "github.com", pw="wrong-pw")
        self.assertIn("cred: ERROR", o)
        self.assertNotEqual(rc, 0)
        rc, o, e = self.cred("status")
        self.assertTrue(o.startswith("cred: LOCKED"), o)

        # unlock one item
        rc, o, e = self.cred("unlock", "github.com", pw=PW)
        self.assertIn("cred: UNLOCKED  scopes=[github.com]", o)
        rc, o, e = self.cred("status")
        self.assertIn("scopes=[github.com]", o)

        # -c form: shell expands "$CRED" -> the real secret reaches the child
        rc, o, e = self.cred("with", "github.com", "-c", 'printf "%s" "$CRED"')
        self.assertEqual(o, "gh-secret")
        self.assertIn("cred: OK  item=github.com", e)
        self.assertNotIn("gh-secret", self.cred("status")[1])  # never leaks into other output

        # -- form: child reads $CRED from its ENVIRONMENT (no shell, no argv leak)
        rc, o, e = self.cred("with", "github.com", "--", sys.executable, "-c",
                             "import os,sys; sys.stdout.write(os.environ['CRED'])")
        self.assertEqual(o, "gh-secret")

        # fetch of an un-authorized item -> DENIED, names what IS authorized
        rc, o, e = self.cred("with", "aws-prod", "-c", 'printf "%s" "$CRED"')
        self.assertTrue(o.startswith("cred: DENIED  item=aws-prod  reason=not-authorized"), o)
        self.assertIn("authorized=[github.com]", o)
        self.assertEqual(rc, 2)

        # additive unlock: authorize a second item WITHOUT evicting the first
        rc, o, e = self.cred("unlock", "aws-prod", pw=PW)
        self.assertIn("cred: UNLOCKED  scopes=[aws-prod, github.com]", o)
        rc, o, e = self.cred("with", "aws-prod", "-c", 'printf "%s" "$CRED"')
        self.assertEqual(o, "aws-secret")
        # ...and the first is still fetchable
        rc, o, e = self.cred("with", "github.com", "-c", 'printf "%s" "$CRED"')
        self.assertEqual(o, "gh-secret")

        # ambiguous name -> AMBIGUOUS (before any authorization check)
        rc, o, e = self.cred("with", "dup.example", "-c", 'printf "%s" "$CRED"')
        self.assertTrue(o.startswith("cred: AMBIGUOUS  item=dup.example"), o)

        # unknown item -> NOT-FOUND
        rc, o, e = self.cred("with", "ghost", "-c", 'printf "%s" "$CRED"')
        self.assertTrue(o.startswith("cred: NOT-FOUND  item=ghost"), o)

        # authorized but item has no password -> ERROR no-password
        rc, o, e = self.cred("unlock", "nopw-item", pw=PW)
        self.assertIn("scopes=[aws-prod, github.com, nopw-item]", o)
        rc, o, e = self.cred("with", "nopw-item", "-c", 'printf "%s" "$CRED"')
        self.assertIn("cred: ERROR", o)
        self.assertIn("no-password", o)

        # find while unlocked -> live results, metadata only (no password ever)
        rc, o, e = self.cred("find", "github")
        self.assertTrue(o.startswith("cred: FOUND"), o)
        self.assertIn("id-github", o)
        self.assertNotIn("gh-secret", o)

        # sync -> SYNCED
        rc, o, e = self.cred("sync")
        self.assertTrue(o.startswith("cred: SYNCED  count="), o)

        # lock clears the session
        rc, o, e = self.cred("lock")
        self.assertEqual(o.strip(), "cred: LOCKED")
        rc, o, e = self.cred("status")
        self.assertTrue(o.startswith("cred: LOCKED"), o)

        # find while locked -> served from the cached catalog (seeded on unlock/sync)
        rc, o, e = self.cred("find", "github")
        self.assertTrue(o.startswith("cred: FOUND"), o)
        self.assertIn("id-github", o)
        self.assertNotIn("gh-secret", o)

    # ---- independent checks (no session state needed) ----------------------
    def test_get_refused_when_not_a_tty(self):
        # cred's stdout is a pipe here -> get must refuse BEFORE fetching a secret
        self.cred("unlock", "github.com", pw=PW)
        rc, o, e = self.cred("get", "github.com")
        self.assertNotEqual(rc, 0)
        self.assertIn("not-a-tty", e)
        self.assertNotIn("gh-secret", o)
        self.assertNotIn("gh-secret", e)

    def test_with_usage_error(self):
        rc, o, e = self.cred("with", "github.com")     # no -c and no --
        self.assertNotEqual(rc, 0)
        self.assertIn("usage", (o + e).lower())

    def test_unlock_requires_item(self):
        rc, o, e = self.cred("unlock")                 # whole-vault mode is gone
        self.assertNotEqual(rc, 0)
        self.assertIn("usage", (o + e).lower())


if __name__ == "__main__":
    unittest.main()
