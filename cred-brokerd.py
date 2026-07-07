#!/usr/bin/env python3
"""cred-brokerd — the credential broker daemon.

Holds a Bitwarden CLI session in memory so that AI agents can USE secrets without
ever seeing them. The security model in one line:

    A human authorizes a specific vault item (`cred unlock <item>`, gated by the
    Bitwarden master password). Agents may then FETCH that item's secret — but only
    INTO a command's environment, never onto their stdout. The daemon holds the vault
    session; agents talk to it over a 0600 unix socket via the `cred` CLI and never
    hold the session or the master password themselves.

Commands (see the `cred` CLI for the human/agent-facing surface):
  unlock <item...> : human, once → master password verifies + opens a session; the named
                     items are authorized (added to the scope set). Stays open until the
                     autolock timeout or `cred lock`. Re-running adds more items.
  lock / status    : human, manage the session.
  sync             : refresh the local Bitwarden cache from the server (also runs
                     automatically before a fetch if the cache is older than SYNC_TTL).
  find <query>     : agent, FREE → returns names/usernames/uris only (NEVER passwords).
  get <item>       : agent → returns ONE authorized secret (the `cred` CLI injects it as
                     $CRED into a child command; the raw value is refused to non-TTY stdout).

No external approval service is required: the human's `cred unlock <item>` IS the approval.
"""
import json, os, socket, threading, time, subprocess

HOME = os.path.expanduser("~")

# ---- config -----------------------------------------------------------------
# Optional JSON config at $CRED_CONFIG or ~/.config/cred/config.json. All keys optional.
DEFAULTS = {
    "run_dir": os.path.join(HOME, ".cred"),   # socket + catalog + log live here (0700)
    "autolock_hours": 8,                       # session auto-locks this long after unlock
    "sync_ttl_seconds": 60,                    # re-sync before a fetch if cache is older than this
    "proxy": None,                             # optional http(s) proxy for `bw` network calls
    "bw_bin": "bw",                            # Bitwarden CLI binary
}

def load_config():
    path = os.environ.get("CRED_CONFIG") or os.path.join(HOME, ".config", "cred", "config.json")
    cfg = dict(DEFAULTS)
    try:
        with open(path) as f:
            cfg.update({k: v for k, v in json.load(f).items() if v is not None})
    except FileNotFoundError:
        pass
    except Exception as e:
        # Bad config should not silently fall back to surprising defaults.
        raise SystemExit("cred-brokerd: bad config %s: %s" % (path, e))
    cfg["run_dir"] = os.path.expanduser(cfg["run_dir"])
    return cfg

CFG = load_config()
RUN_DIR = CFG["run_dir"]
SOCK = os.path.join(RUN_DIR, "cred.sock")
LOG = os.path.join(RUN_DIR, "broker.log")
CATALOG = os.path.join(RUN_DIR, "catalog.json")   # names/usernames/uris ONLY — lets `find` work while locked
AUTOLOCK_HOURS = float(CFG["autolock_hours"])
SYNC_TTL = float(CFG["sync_ttl_seconds"])

# scopes: the set of item-refs (as the human typed them) authorized this session.
state = {"session": None, "unlocked_at": 0.0, "last_sync": 0.0, "scopes": set()}
slock = threading.Lock()


def log(m):
    with open(LOG, "a") as f:
        f.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), m))


def bw(args, session=None):
    env = dict(os.environ)
    env["HOME"] = HOME
    if CFG["proxy"]:
        env["HTTPS_PROXY"] = CFG["proxy"]
        env["HTTP_PROXY"] = CFG["proxy"]
    if session:
        env["BW_SESSION"] = session
    return subprocess.run([CFG["bw_bin"]] + args, capture_output=True, text=True, env=env)


def meta(i):
    login = i.get("login") or {}
    return {"id": i["id"], "name": i.get("name"), "username": login.get("username"),
            "uris": [u.get("uri") for u in login.get("uris", [])]}


def build_catalog(session):
    """Dump non-secret metadata (id/name/username/uris — NO passwords) so `find` works locked."""
    r = bw(["list", "items"], session)
    if r.returncode != 0:
        log("catalog build failed: " + r.stderr.strip()[:120])
        return 0
    cat = [meta(i) for i in json.loads(r.stdout or "[]")]
    with open(CATALOG, "w") as f:
        json.dump(cat, f)
    os.chmod(CATALOG, 0o600)
    log("catalog refreshed: %d items" % len(cat))
    return len(cat)


def do_sync(reason=""):
    """Pull latest from the Bitwarden server, then rebuild the catalog. Caller holds a session."""
    bw(["sync"], state["session"])
    n = build_catalog(state["session"])
    state["last_sync"] = time.time()
    if reason:
        log("synced (%s): %d items" % (reason, n))
    return n


def sync_if_stale():
    if time.time() - state["last_sync"] > SYNC_TTL:
        do_sync("stale cache before fetch")


def session_ok():
    with slock:
        if not state["session"]:
            return False
        if time.time() - state["unlocked_at"] > AUTOLOCK_HOURS * 3600:
            state["session"] = None
            state["scopes"] = set()
            log("auto-locked (timeout)")
            return False
        return True


def item_and_scope(item):
    """Resolve `item` to a single vault item and decide whether it is authorized.

    Returns (item_json, authorized_bool, error_str). One `bw get item` also carries the
    password, so callers need no second call. error_str is set for not-found / ambiguous.
    """
    r = bw(["get", "item", item], state["session"])
    if r.returncode != 0:
        detail = r.stderr.strip().lower()
        if "more than one" in detail or "multiple" in detail:
            return None, False, "ambiguous"
        return None, False, "not-found"
    it = json.loads(r.stdout or "{}")
    hay = " ".join(filter(None, [it.get("id", ""), it.get("name", "")] +
                          [u.get("uri", "") for u in (it.get("login") or {}).get("uris", [])])).lower()
    authorized = any(sc.lower() in hay or sc.lower() == it.get("id", "").lower()
                     for sc in state["scopes"])
    return it, authorized, None


def handle(req):
    cmd = req.get("cmd")

    if cmd == "unlock":
        items = [s for s in (req.get("items") or []) if s and s.strip()]
        if not items:
            return {"error": "no-item", "detail": "name at least one item: cred unlock <item> [item2 ...]"}
        r = subprocess.run([CFG["bw_bin"], "unlock", "--passwordenv", "BW_PASSWORD", "--raw"],
                           capture_output=True, text=True,
                           env={**os.environ, "HOME": HOME, "BW_PASSWORD": req.get("password", ""),
                                **({"HTTPS_PROXY": CFG["proxy"], "HTTP_PROXY": CFG["proxy"]} if CFG["proxy"] else {})})
        if r.returncode != 0:
            return {"error": "unlock-failed", "detail": r.stderr.strip()[:200]}
        with slock:
            state["session"] = r.stdout.strip()
            state["unlocked_at"] = time.time()
            state["scopes"] |= set(items)          # additive: keep previously authorized items
            scopes = sorted(state["scopes"])
        try:
            do_sync("unlock")
        except Exception as e:
            log("sync err on unlock: %s" % e)
        log("unlocked; authorized now: %s" % scopes)
        return {"ok": True, "scopes": scopes, "ttl_hours": AUTOLOCK_HOURS, "added": sorted(items)}

    if cmd == "lock":
        with slock:
            state["session"] = None
            state["scopes"] = set()
        log("locked (manual)")
        return {"ok": True}

    if cmd == "status":
        with slock:
            unlocked = bool(state["session"])
            left = max(0, AUTOLOCK_HOURS * 3600 - (time.time() - state["unlocked_at"])) if unlocked else 0
            scopes = sorted(state["scopes"])
            sync_age = int(time.time() - state["last_sync"]) if state["last_sync"] else None
        return {"unlocked": session_ok(), "seconds_left": int(left), "scopes": scopes, "sync_age": sync_age}

    if cmd == "sync":
        if not session_ok():
            return {"error": "locked"}
        try:
            n = do_sync("manual")
        except Exception as e:
            return {"error": "sync-failed", "detail": str(e)[:160]}
        return {"ok": True, "count": n}

    if cmd == "find":
        q = req.get("query", "")
        if session_ok():                              # unlocked → query the live vault (fresh) + refresh catalog
            try:
                sync_if_stale()
            except Exception:
                pass
            r = bw(["list", "items", "--search", q], state["session"])
            if r.returncode == 0:
                return {"items": [meta(i) for i in json.loads(r.stdout or "[]")], "source": "live"}
        try:                                          # locked → search the cached no-secret catalog
            cat = json.load(open(CATALOG))
        except Exception:
            return {"items": [], "note": "catalog empty — run `cred unlock <item>` once to seed it"}
        ql = q.lower()

        def hit(it):
            hay = " ".join(filter(None, [it.get("name") or "", it.get("username") or ""] +
                                  (it.get("uris") or []))).lower()
            return ql in hay
        return {"items": [it for it in cat if hit(it)], "source": "catalog"}

    if cmd == "get":
        item = req.get("item", "")
        requester = req.get("requester", "an agent")
        if not session_ok():
            return {"error": "locked", "item": item}
        try:
            sync_if_stale()                           # so a rotated/new secret is fresh on first use
        except Exception as e:
            log("sync_if_stale err: %s" % e)
        it, authorized, err = item_and_scope(item)
        if err:
            return {"error": err, "item": item}
        if not authorized:
            return {"error": "not-authorized", "item": it.get("name") or item,
                    "authorized": sorted(state["scopes"])}
        secret = ((it.get("login") or {}).get("password") or "")
        if not secret:
            return {"error": "no-password", "item": it.get("name") or item,
                    "detail": "item has no login.password field"}
        log("delivered '%s' to %s (scoped; session stays open)" % (it.get("name") or item, requester))
        return {"secret": secret, "item": it.get("name") or item}

    return {"error": "unknown-cmd", "detail": str(cmd)}


def serve_one(conn):
    try:
        data = b""
        while not data.endswith(b"\n"):
            chunk = conn.recv(65536)
            if not chunk:
                break
            data += chunk
        req = json.loads(data.decode() or "{}")
        resp = handle(req)
    except Exception as e:
        resp = {"error": "daemon-exception", "detail": str(e)}
    try:
        conn.sendall((json.dumps(resp) + "\n").encode())
    except BrokenPipeError:
        pass                                          # client hung up (e.g. `cred with` exec'd its child) — fine
    finally:
        conn.close()


def serve():
    os.makedirs(RUN_DIR, exist_ok=True)
    os.chmod(RUN_DIR, 0o700)
    try:
        os.unlink(SOCK)
    except FileNotFoundError:
        pass
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(SOCK)
    os.chmod(SOCK, 0o600)
    s.listen(8)
    log("cred-brokerd started (run_dir=%s, autolock=%sh, sync_ttl=%ss)" % (RUN_DIR, AUTOLOCK_HOURS, SYNC_TTL))
    while True:
        conn, _ = s.accept()
        threading.Thread(target=serve_one, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    serve()
