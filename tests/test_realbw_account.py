#!/usr/bin/env python3
"""Full real-vault integration: daemon + cred against REAL Bitwarden, using a throwaway
test account. Seeds a login item, verifies `cred with` returns the EXACT secret (compared
by sha256 — the value is never printed), then deletes the item and logs out.

Runs only when `bw` is installed AND TEST_BITWARDEN_ACCOUNT / TEST_BITWARDEN_PASSWORD are in
the environment; otherwise it skips. In CI those come from repo secrets (a push-only job). If
BW_CLIENTID / BW_CLIENTSECRET are also set, login uses the API key (avoids datacenter-IP
captcha); otherwise it falls back to email+password login.
"""
import hashlib, json, os, shutil, subprocess, sys, tempfile, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
CRED = os.path.join(REPO, "cred")
DAEMON = os.path.join(REPO, "cred-brokerd.py")
BW = shutil.which("bw")
ACCOUNT = os.environ.get("TEST_BITWARDEN_ACCOUNT")
PASSWORD = os.environ.get("TEST_BITWARDEN_PASSWORD")


@unittest.skipUnless(BW and ACCOUNT and PASSWORD,
                     "needs real bw + TEST_BITWARDEN_ACCOUNT/PASSWORD")
class RealBwAccount(unittest.TestCase):
    def _bw(self, *args, **kw):
        env = dict(self.bwenv)
        env.update(kw.pop("env", {}))
        return subprocess.run([BW, *args], capture_output=True, text=True, env=env, **kw)

    def setUp(self):
        self.item_id = None
        self.session = ""
        self.appdata = tempfile.mkdtemp(prefix="bwtest-")
        self.run_dir = tempfile.mkdtemp(prefix="bwrun-")
        self.bwenv = dict(os.environ)
        self.bwenv["BITWARDENCLI_APPDATA_DIR"] = self.appdata

        if os.environ.get("BW_CLIENTID") and os.environ.get("BW_CLIENTSECRET"):
            r = self._bw("login", "--apikey", "--raw")
            self.assertEqual(r.returncode, 0, "apikey login failed: " + r.stderr)
            u = self._bw("unlock", "--passwordenv", "TEST_BITWARDEN_PASSWORD", "--raw",
                         env={"TEST_BITWARDEN_PASSWORD": PASSWORD})
            self.session = u.stdout.strip()
            self.assertTrue(self.session, "unlock failed: " + u.stderr)
        else:
            r = self._bw("login", ACCOUNT, "--passwordenv", "TEST_BITWARDEN_PASSWORD", "--raw",
                         env={"TEST_BITWARDEN_PASSWORD": PASSWORD})
            self.session = r.stdout.strip()
            self.assertTrue(self.session, "password login failed: " + r.stderr)

        self.secret = "s3cr3t-%d-%d" % (int(time.time()), os.getpid())
        self.name = "agent-cred-selftest-%d" % os.getpid()
        item = json.dumps({"type": 1, "name": self.name, "notes": None, "favorite": False,
                           "login": {"username": "tester", "password": self.secret,
                                     "uris": [{"uri": "https://agent-cred.test"}]}})
        enc = self._bw("encode", input=item)
        cr = self._bw("create", "item", "--session", self.session, input=enc.stdout)
        self.assertEqual(cr.returncode, 0, "seed failed: " + cr.stderr)
        self.item_id = json.loads(cr.stdout)["id"]
        self._bw("sync", "--session", self.session)

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
        if self.item_id:
            try:
                # --permanent: skip Trash, so repeated CI runs never accumulate cruft
                self._bw("delete", "item", self.item_id, "--permanent", "--session", self.session)
            except Exception:
                pass
        try:
            self._bw("logout")
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
        rc, o, e = self.cred("unlock", self.item_id, pw=PASSWORD)
        self.assertIn("cred: UNLOCKED", o)
        self.assertIn(self.item_id, o)

        # fetch and verify the EXACT secret by sha256 — the value is never printed
        want = hashlib.sha256(self.secret.encode()).hexdigest()
        rc, o, e = self.cred("with", self.item_id, "--", sys.executable, "-c",
                             "import os,hashlib,sys;"
                             "sys.stdout.write(hashlib.sha256(os.environ['CRED'].encode()).hexdigest())")
        self.assertEqual(o.strip(), want, "fetched secret hash mismatch (stderr: %s)" % e)

        # unauthorized item is denied
        rc, o, e = self.cred("with", "no-such-item-xyz", "-c", 'printf "%s" "$CRED"')
        self.assertIn("cred:", o)
        self.assertNotEqual(rc, 0)

        # find returns metadata only
        rc, o, e = self.cred("find", self.name)
        self.assertTrue(o.startswith("cred: FOUND"), o)
        self.assertIn(self.item_id, o)
        self.assertNotIn(self.secret, o)


if __name__ == "__main__":
    unittest.main()
