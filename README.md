# agent-cred

[![CI](https://github.com/andylizf/agent-cred/actions/workflows/ci.yml/badge.svg)](https://github.com/andylizf/agent-cred/actions/workflows/ci.yml)

**A credential manager for AI agents.** Let an autonomous agent *use* your passwords — log
into a site, hit an authenticated API, run a deploy — **without the secret ever entering the
agent's context.** A human authorizes a specific vault item with one command; the agent then
flows that secret straight into a command's environment and only ever sees the command's
output.

The CLI is `cred`. It talks to a small local daemon that holds your [Bitwarden][bw] session
in memory, so the agent never holds the session or your master password.

```
human $ cred unlock github.com          # authorize one item (asks for master password, once)
agent $ cred with github.com -- gh auth login --with-token   # secret → $CRED → command; never printed
```

[bw]: https://bitwarden.com/help/cli/

---

## Why

Giving an AI agent a password usually means one of two bad options: paste it into the prompt
(now it's in the model's context, the transcript, maybe the training set), or hand the agent
your whole vault. `agent-cred` gives you a third option:

- **Write-only secrets.** The agent runs `cred with <item> -- <cmd>`. The secret is injected
  as the `$CRED` environment variable of `<cmd>` and nothing else. `cred` prints nothing; the
  agent sees only `<cmd>`'s output. The raw value never touches the agent's stdout.
- **Human-in-the-loop, per item.** Nothing is fetchable until *you* run `cred unlock <item>`,
  which is gated by your Bitwarden master password. Unlocking `github.com` authorizes
  `github.com` — not the rest of your vault.
- **Secure-by-construction `get`.** `cred get` (which *does* print the raw secret) refuses to
  run unless stdout is a real TTY. An agent's stdout is a pipe, so it is physically prevented
  from pulling a plaintext secret into its context — it must use `cred with`.
- **The tool documents itself.** Every response starts with a machine-readable
  `cred: <STATE>` line and tells the agent the single next action. There is no mental model to
  memorize and get wrong.

## How it works

```
   ┌─────────┐   cred with X -- cmd    ┌──────────────┐   BW_SESSION    ┌────────────┐
   │  agent  │ ──────────────────────► │ cred-brokerd │ ──────────────► │ bw (vault) │
   └─────────┘   (0600 unix socket)    │  (holds the  │                 └────────────┘
   ┌─────────┐   cred unlock X         │   session)   │
   │  human  │ ──────────────────────► │              │
   └─────────┘   (master password)     └──────────────┘
```

- **`cred-brokerd`** — a daemon. After you unlock, it holds the Bitwarden session in memory
  (auto-locking after a timeout). It enforces which items are authorized and is the only thing
  that ever sees a plaintext secret, momentarily, to hand it to a child process.
- **`cred`** — the CLI both you and the agent call. It never holds the session; it just talks
  to the daemon over a `0600` unix socket in your run directory.

`find` works even while locked, from a cached catalog of **non-secret** metadata
(names/usernames/URIs only — never passwords).

## Install

Requirements: **Python 3.8+** and the **[Bitwarden CLI][bw]** (`bw`), logged in
(`bw login`). No other dependencies.

```bash
git clone https://github.com/andylizf/agent-cred
cd agent-cred
./install.sh          # copies `cred` + `cred-brokerd.py`, sets up the run dir, installs the service
```

`install.sh` installs a launchd agent (macOS) or systemd user service (Linux) that keeps the
daemon running. Or run it by hand: `python3 cred-brokerd.py &`.

## Usage

### Human

```bash
cred unlock github.com aws-prod   # authorize one or more items (one master-password prompt)
cred status                       # what's authorized, time left, cache freshness
cred sync                         # pull the latest vault from the server (rotated a password?)
cred lock                         # drop the session now
```

`unlock` is **additive**: running it again keeps what was already authorized and adds more.
Each `unlock` re-verifies your master password — which is exactly why an agent (who doesn't
have it) can never expand its own authorization.

### Agent

```bash
cred find stripe                            # discover items (metadata only, no secrets)

# Two ways to consume the secret — pick by how the target tool takes its value:
cred with stripe-prod -- deploy-tool        # tool reads $CRED from its ENV — safest (no argv/ps leak)
cred with stripe-prod -c 'curl -u "user:$CRED" https://api.stripe.com/v1/charges'   # tool needs it as an arg
```

**Getting the secret into a command.** `cred` injects the secret as the `$CRED` environment
variable of the command it runs. There are two forms:

- `cred with <item> -- <argv…>` execs the command **directly, with no shell**. Use it when the
  tool reads `$CRED` from its **environment**. The secret never appears in the command line, so
  it can't leak via `ps`. This is the preferred form.
- `cred with <item> -c '<shell command>'` runs the command **in a shell**, so `"$CRED"`
  expands. Use it when a tool only accepts the value as an **argument**
  (e.g. `curl -u "user:$CRED"`). `cred` supplies the shell — you never write `bash -c` yourself.

> A literal `$CRED` only expands inside a shell. If a tool needs the value as an argument, use
> the **`-c` form** so a shell expands it. The `-- <argv>` form runs with no shell, so a literal
> `$CRED` there is passed verbatim (you'd send the string `$CRED`, not the secret). Never
> `echo`/`cat`/`print` `$CRED`, and never hide it in a script `cred` can't see.

The agent contract, machine-readable:

- Every response's **first line** is `cred: <STATE> ...` where `<STATE>` is one of
  `UNLOCKED`, `LOCKED`, `FOUND`, `OK`, `DENIED`, `NOT-FOUND`, `AMBIGUOUS`, `SYNCED`, `ERROR`,
  `REFUSED`.
- Lines beginning `→ agent:` or `→ human:` give the **single next action**. If a fetch is
  `DENIED` (item not authorized), relay the exact `cred unlock <item>` to the human, then
  re-run your command.

## Freshness

The daemon reads Bitwarden's local cache, which only refreshes on `bw sync`. `agent-cred`
syncs automatically on every `unlock` and before any fetch when the cache is older than
`sync_ttl_seconds` (default 60s), so a rotated or newly added secret is fresh on first use.
You can also force it with `cred sync`.

## Configuration

Optional JSON at `~/.config/cred/config.json` (or `$CRED_CONFIG`). All keys optional:

```json
{
  "run_dir": "~/.cred",
  "autolock_hours": 8,
  "sync_ttl_seconds": 60,
  "proxy": null,
  "bw_bin": "bw"
}
```

- `run_dir` — where the socket, catalog, and log live (created `0700`).
- `autolock_hours` — session auto-locks this long after `unlock`.
- `sync_ttl_seconds` — re-sync before a fetch if the local cache is older than this.
- `proxy` — optional http(s) proxy for `bw`'s network calls.
- `bw_bin` — path to the Bitwarden CLI if it isn't on `PATH`.

## Security model & threats

- **The master password** transits only the local `0600` socket, is used immediately for
  `bw unlock`, and is never stored.
- **The session** lives in the daemon's memory only. Agents get secrets one command at a time,
  never the session.
- **Scope of trust.** The socket is `0600` — anyone who can already act as your user can talk
  to the daemon. `agent-cred` defends against *your agent leaking a secret into its context or
  authorizing itself* — not against a full local-account compromise. Treat it accordingly.
- **`get` vs `with`.** `get` prints plaintext and is TTY-gated for humans. Agents must use
  `with`. If you pass `--force` to `get` in a pipe, that's on you.

## Roadmap (not in v1 — YAGNI)

The vault backend (Bitwarden) and the approval model (a human `unlock`) are behind clean
seams. Future, if there's demand: other vault backends (1Password `op`, `pass`), and optional
remote approval plugins (approve a fetch from your phone via Notion / Telegram / ntfy).

## Development

Tests are pure-stdlib (no dependencies). They run the **real daemon and CLI** against a fake
`bw` (`tests/fake-bw`) backed by a JSON vault, so the whole flow — unlock, additive scopes,
`with` (`-c` and `--`), `get` refusal, `find`, `sync` — is exercised without real Bitwarden or
a network:

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

CI runs this on Ubuntu and macOS across Python 3.9 and 3.12. A separate job also runs an
integration smoke against the **real, version-pinned Bitwarden CLI** (`@bitwarden/cli@2026.6.0`)
to catch drift in the binary, flags, or error output. `tests/test_realbw.py` skips itself when
`bw` isn't installed, so local runs and the fake-bw job are unaffected.

A further **push-only** job (`tests/test_realbw_account.py`) runs the full path against a real,
throwaway Bitwarden test account that holds one pre-seeded login item. It logs in, then checks
`unlock` → `with` (the fetched secret is compared by sha256, never printed) → an unauthorized
fetch is denied → `find`. It is **read-only** — nothing is created or deleted — so repeated runs
never mutate the account. It skips unless the account secrets are set, and is push-only so those
secrets are never exposed to pull requests or forks.

## License

MIT © andylizf
