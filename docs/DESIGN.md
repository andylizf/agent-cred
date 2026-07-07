# agent-cred — design

## Goal

Let an AI agent *use* a credential without the plaintext ever entering the agent's context,
while a human retains per-item, master-password-gated control over what the agent may fetch.

## Model

Two processes, one socket:

- **`cred-brokerd`** (daemon) holds a Bitwarden CLI session in memory after a human unlocks.
  It is the only component that ever sees a plaintext secret, and only momentarily, to hand it
  to a child process. It enforces the authorized-item set (*scopes*) and auto-locks on a timeout.
- **`cred`** (CLI) is called by both the human and the agent. It holds no session; it speaks to
  the daemon over a `0600` unix socket in the run dir.

**Approval == unlock.** There is no separate approval channel. A human running
`cred unlock <item>` — which requires the Bitwarden master password — *is* the authorization.
This deliberately replaces an earlier design that gated each fetch with a remote tap
(Notion), which was unused in practice and added a silent-hang failure mode.

## Scopes (authorized-item set)

`state.scopes` is a set of item-refs, as the human typed them.

- `cred unlock a b c` → `scopes |= {a, b, c}` (**additive** — never evicts what's already
  authorized). Re-verifies the master password every time, which is why an agent (lacking it)
  can never expand its own scope.
- A fetch resolves the requested item via one `bw get item`, then checks whether any scope
  string matches that item's id / name / uris. Match → authorized (no prompt, no tap). The
  same `bw get item` call also carries `login.password`, so a fetch is a single vault call.
- Scoped items stay fetchable for the whole session (supports flaky-script retries) until the
  `autolock_hours` timeout or `cred lock`.

## Freshness

`bw` reads a local cache that only updates on `bw sync`. To avoid serving a stale (e.g.
rotated) secret:

- `unlock` always syncs.
- Before any fetch, sync if the cache is older than `sync_ttl_seconds` (default 60s). Rapid
  retries within the TTL skip the sync, so the retry path stays fast.
- `cred sync` forces it. `cred status` shows `last_sync`.

## Output contract (progressive disclosure)

Agents must not need to memorize a mental model. Every response is self-contained:

- Line 1 is machine-readable: `cred: <STATE> key=val ...`. States: `UNLOCKED`, `LOCKED`,
  `FOUND`, `OK`, `DENIED`, `NOT-FOUND`, `AMBIGUOUS`, `SYNCED`, `ERROR`, `REFUSED`.
- Following lines are plain English, and any `→ agent:` / `→ human:` line gives the single next
  action. `DENIED` names which items *are* authorized and the exact `cred unlock` to relay.
- The write-only-secret reminder prints only on `get` and on errors — not on `find`/`with`
  success — so it never becomes noise an agent filters away.

## Secret-safety invariants

- `cred with <item>` fetches the secret, sets it as `$CRED` in the child env, and `execvpe`s
  the child. `cred` prints nothing on success beyond a one-line `cred: OK` to stderr; the
  plaintext never reaches its stdout. Two forms: `-- <argv…>` execs directly (no shell — best
  when the tool reads `$CRED` from its env, keeping the secret out of argv/`ps`); `-c '<cmd>'`
  runs under `sh -c` so `"$CRED"` expands (for tools that take the value as an argument, without
  the caller writing `bash -c`). `$CRED` is expanded only by a shell, so the `-c` form is the way
  to use the value as an argument; the `--` form is for tools that read `$CRED` from their env.
- `cred get` prints plaintext and refuses unless stdout is a TTY (`--force` overrides, for
  humans). An agent's piped stdout is thus physically blocked from pulling plaintext.
- `find` returns metadata only (id/name/username/uris); the catalog cache holds no passwords.
- The master password transits only the local socket, is used immediately for `bw unlock`, and
  is never stored. The session lives only in daemon memory.

## Non-goals (v1, YAGNI)

- Vault backends other than Bitwarden (seam left at `bw()` / `meta()`).
- Remote/async approval (seam left at "unlock == approval").
- Defense against a full local-account compromise: the `0600` socket trusts the local user.
