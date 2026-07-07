#!/usr/bin/env python3
"""Full real-vault integration: daemon + cred against REAL Bitwarden.

READ-ONLY. It logs into a test account that has a pre-seeded, stable login item and verifies
the whole path — unlock, `cred with` returning the EXACT secret (compared by sha256, never
printed), an unauthorized fetch being denied, and `find` — without creating or deleting
anything. Nothing to clean up, so repeated CI runs never touch the account's contents.

Runs only when all of these are set (from repo secrets in a push-only CI job); otherwise skips:
  TEST_BITWARDEN_ACCOUNT        the account email
  TEST_BITWARDEN_PASSWORD       its master password
  TEST_BITWARDEN_ITEM           name/id of the pre-seeded login item (e.g. "agent-cred-ci")
  TEST_BITWARDEN_SECRET_SHA256  sha256 hex of that item's password
Optional: BW_CLIENTID / BW_CLIENTSECRET to log in by API key (avoids datacenter-IP captcha).
"""
import hashlib, json, os, shutil, subprocess, sys, tempfile, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
CRED = os.path.join(REPO, "cred")
DAEMON = os.path.join(REPO, "cred-brokerd.py")
BW = shutil.which("bw")
ACCOUNT = os.environ.get("TEST_BITWARDEN_ACCOUNT")
PASSWORD = os.environ.get("TEST_BITWARDEN_PASSWORD")
ITEM = os.environ.get("TEST_BITWARDEN_ITEM")
SHA = os.environ.get("TEST_BITWARDEN_SECRET_SHA256")


@unittest.skipUnless(BW and ACCOUNT and PASSWORD and ITEM and SHA,
                     "needs real bw + TEST_BITWARDEN_ACCOUNT/PASSWORD/ITEM/SECRET_SHA256")
class RealBwAccount(unittest.TestCase):
    def _bw(self, *args, **kw):
        env = dict(self.bwenv)
        env.update(kw.pop("env", {}))
        return subprocess.run([BW, *args], capture_output=True, text=True, env=env, **kw)

    def setUp(self):
        self.appdata = tempfile.mkdtemp(prefix="bwtest-")
        self.run_dir = tempfile.mkdtemp(prefix="bwrun-")
        self.bwenv = dict(os.environ)
        self.bwenv["BITWARDENCLI_APPDATA_DIR"] = self.appdata

        if os.environ.get("BW_CLIENTID") and os.environ.get("BW_CLIENTSECRET"):
            r = self._bw("login", "--apikey", "--raw")
            self.assertEqual(r.returncode, 0, "apikey login failed: " + r.stderr)
        else:
            r = self._bw("login", ACCOUNT, "--passwordenv", "TEST_BITWARDEN_PASSWORD", "--raw",
                         env={"TEST_BITWARDEN_PASSWORD": PASSWORD})
            self.assertEqual(r.returncode, 0, "password login failed: " + r.stderr)
        self._bw("sync")

        with open(os.path.join(self.run_dir, "config.json"), "w") as f:
            json.dump({"run_dir": self.run_dir, "bw_bin": BW, "proxy": None}, f)
        self.denv = dict(self.bwenv)
        self.denv["CRED_CONFIG"] = os.path.join(self.run_dir, "config.json")
        self.dlog = open(os.path.join(self.run_dir, "daemon.err"), "w")
        self.proc = subprocess.Popen([sys.executable, DAEMON], env=self.denv,
                                     stdout=self.dlog, stderr=self.dlog)
        sock = os.path.join(self.run_dir, "cred.sock")
        for _ in range(50):
            if os.path.exists(sock):
                break
            time.sleep(0.1)
        else:
            self.fail("daemon did not create socket; see " + self.dlog.name)

    def tearDown(self):
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        try:
            self._bw("logout")           # read-only: nothing to delete
        except Exception:
            pass
        try:
            self.dlog.close()
        except Exception:
            pass
        shutil.rmtree(self.appdata, ignore_errors=True)
        shutil.rmtree(self.run_dir, ignore_errors=True)

    def cred(self, *args, pw=None):
        p = subprocess.run([sys.executable, CRED, *args], env=self.denv,
                           capture_output=True, text=True,
                           input=(pw + "\n") if pw is not None else None)
        return p.returncode, p.stdout, p.stderr

    def test_unlock_fetch_find(self):
        rc, o, e = self.cred("unlock", ITEM, pw=PASSWORD)
        self.assertIn("cred: UNLOCKED", o)

        # fetch and verify the EXACT secret by sha256 — the value is never printed
        rc, o, e = self.cred("with", ITEM, "--", sys.executable, "-c",
                             "import os,hashlib,sys;"
                             "sys.stdout.write(hashlib.sha256(os.environ['CRED'].encode()).hexdigest())")
        self.assertEqual(o.strip(), SHA, "fetched secret hash mismatch (stderr: %s)" % e)

        # an unauthorized item is denied
        rc, o, e = self.cred("with", "no-such-item-xyz", "-c", 'printf "%s" "$CRED"')
        self.assertIn("cred:", o)
        self.assertNotEqual(rc, 0)

        # find returns metadata only
        rc, o, e = self.cred("find", ITEM)
        self.assertTrue(o.startswith("cred: FOUND"), o)
        self.assertIn(ITEM, o)


if __name__ == "__main__":
    unittest.main()
