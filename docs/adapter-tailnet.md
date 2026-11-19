# Tailnet host inventory adapter

`packages/adapters/memfw-adapter-tailnet` (import `memfw_adapter_tailnet`, version 0.1.0)
implements the `Ingestor` port ([ports-and-recall.md](ports-and-recall.md)) for the machine
list of a [Tailscale](https://tailscale.com/) tailnet. It reads the JSON that
`tailscale status --json` prints, keeps a few fields per machine, compares them with the
previous run and stages one **host-fact candidate** per machine that is new, returned, gone
or changed. Nothing becomes memory until a person reviews the candidate. A small
`report.json` with counts and fixed codes (no names) feeds alerting.

| | |
|---|---|
| External project | The Tailscale CLI, run by the operator or exported to a file. Not vendored, not a dependency |
| How it runs | `memfw-adapter-tailnet scan` (one-shot; cron or a timer can run it) |
| Port | `Ingestor`; registry entry point `tailnet` (kind `ingestor`) |
| Default | Off. memfw never imports the adapter |
| Network | None of its own. Live mode runs the local CLI once; it never scans address ranges and never calls the Tailscale API |
| Dependencies | Standard library and `memfw-ports` only |

## Why discovery only proposes and alerts

The list of machines that may *receive* data (Wiki workers, batch embedding hosts) stays a
fixed list a person approves. Discovery must not widen it: a machine appearing on the
tailnet is a reason to look, not a reason to send it anything. So the adapter writes only
staged candidates and `report.json`, and it only *reads* the operator's allowlist.

## Input

`--input FILE` reads an exported `tailscale status --json` (preferred for tests, and for
operators who export it on another machine). `--live` runs exactly
`[<tailscale>, "status", "--json"]`:

- no shell; the arguments after the executable are fixed (`--tailscale PATH` picks the
  executable, default `tailscale` on `PATH`);
- stdin closed, stderr discarded, proxy variables (`http_proxy`, `https_proxy`,
  `all_proxy`, `no_proxy`, `ftp_proxy`, `socks_proxy`, any case) removed from the child's
  environment;
- a deadline (`--timeout`, default 15 s, at most 120 s) and an 8 MiB output cap; the child
  runs in its own session and its whole process group (grandchildren included) is killed on
  either.

Either way the document must be a JSON object of at most 8 MiB.

### What is read

The schema differs across Tailscale versions, so the parser assumes nothing beyond "an
object". It reads `BackendState`, `Self` and the values of `Peer`; from each node only:

| JSON field | Kept as | Notes |
|---|---|---|
| `ID`, else `StableID`, else `PublicKey` / the `Peer` map key | `host_id` | hashed (below); the raw value is never stored |
| `ShareeNode`, `Location` | (nothing) | only to leave a node out (below) |
| `HostName` | `name` | display only; falls back to the first DNS label, then `unnamed`. Never used for allowlist matching |
| `DNSName` | `dns_label`, `dns_suffix_hash` | first label kept; the suffix (it names the tailnet) only as `sfx-` + 16 hex of SHA-256 |
| `TailscaleIPs` | `ips` | each validated as an IP address; IPv4 first, then IPv6; at most 16 |
| `OS` | `os` | |
| `Online` | `online` | `true`, `false`, or `null` when absent (old versions) |
| `LastSeen` | `last_seen` | RFC 3339 → `YYYY-MM-DDTHH:MM:SSZ`; Go's zero time (`0001-01-01…`) → `null` |
| `Tags` | `tags` | sorted; at most 64 |

Everything else is never read: the top-level `User` map (login names, display names,
profile pictures), `CurrentTailnet`, `MagicDNSSuffix`, `CertDomains`, `ExtraRecords`, and per
node `UserID`, `NodeID`, `PublicKey` (except hashed, as the id of last resort),
`sshHostKeys`, `CapMap`, `AllowedIPs`, `PrimaryRoutes`, endpoint addresses (`Addrs`,
`CurAddr`), `PeerAPIURL`, relays, byte counters, handshake and expiry times. A field of the
wrong type counts as absent; control and bidirectional-override characters are stripped
from text. A `Peer` value that is not an object, or a second node with the same id, is
skipped and counted (`skipped`). `Self` without any id is skipped too. A missing or `null`
`Peer` means "no peers".

`BackendState` other than `Running` (`Stopped`, `NeedsLogin`, `Starting`, …) is refused
with `backend_not_running`: the peer list is then empty or stale, and diffing it would
report every machine gone.

### Nodes left out

- **Shared-in nodes** (`ShareeNode: true`) are left out by default and counted in
  `skipped_shared`. They may be the owner's own machines in another tailnet, or a third
  party's; the owner decides whether they belong in the inventory and opts in with
  `--include-shared` (registry setting `include_shared: true`).
- **Nodes with a `Location`** (provider exit nodes such as Mullvad's) are always left out
  and counted in `skipped_location`: they are a service, not a machine of the operator.

### Host id

`host_id` is `host-` plus the first 24 hex characters of SHA-256 over a fixed namespace
and the node's `ID` (Tailscale's stable node ID; `StableID` on versions that name it so).
Versions without either fall back to the node key; there a key rotation looks like one
host gone and one new. The id follows the machine through renames and address changes, and
the raw node ID or key never appears in any output.

## Output

### Candidates

With `--vault VAULT`, each change is staged through `TailnetIngestor.stage` (the reference
`stage_candidates`) as `<vault>/_candidates/tailnet/cand-*.json`, under source
`tailscale-status` and proposal id `tailnet-scan-<UTC time>` (or `--proposal-id`). The
candidate has:

- `title`: `tailnet host <changes>: <name>`, e.g. `tailnet host renamed, ip_changed: example-nas-2`;
- `body`: JSON `{"format": "memfw.tailnet.host-fact", "version": 1, "host_id", "since",
  "changes": [...], "host": {...} | null, "previous": {...} | null}` where `host` holds
  `host_id, name, dns_label, dns_suffix_hash, ips, os, tags, self`, `previous` the same
  identity fields from the last committed run, and `since` that run's `taken_at` (`null` on
  the first run);
- `metadata`: `adapter: tailnet`, `kind: host-fact`, `host_id`, `changes` (codes joined by
  `+`), `concept: host:<slug of the name>` (the ontology concept a reviewer updates),
  `privacy`, `epistemic: fact`, `causal: none`;
- `external_ref`: `tailnet:<host_id>:<changes>:<16 hex of SHA-256 over the codes and both
  identities>`.

A candidate holds **identity fields only**: no `online`, `last_seen` or wall-clock time.
Those change every run; if they were in the candidate, its id (which hashes its content)
would change too, and a retry after a crash would stage a second copy. With identity only
and `since` fixed until the next commit, a retry stages byte-identical candidates, which
staging reports as `duplicates`. `online` and `last_seen` live in the snapshot and drive
the alerts.

Change codes, judged on `name` / `dns_label` / `dns_suffix_hash`, `ips`, `os` and `tags`:

| Code | When |
|---|---|
| `new` | the host id was never seen (every host on the first run) |
| `returned` | the host id had left (a tombstone); `previous` is what it looked like then, and identity codes below are added when it changed |
| `gone` | the host id was present in the previous snapshot and is not now (`host` is `null`) |
| `renamed` | `name`, `dns_label` or `dns_suffix_hash` changed |
| `ip_changed` | the set of tailnet IPs changed |
| `os_changed` | `os` changed |
| `tags_changed` | `tags` changed |

**An unchanged host produces no candidate**, and neither does a change of `online` or
`last_seen` alone. A host that leaves (removed, or hidden by an ACL change) is reported
`gone` once and kept as a tombstone (`gone_since`) for 180 days, so if it comes back it is
`returned`, not `new`. After 180 days the tombstone is dropped.

### Privacy

Candidates carry `privacy: local-private` unless the operator sets `--privacy shared|public`
(or `adapters: {tailnet: {privacy: ...}}` in the registry). A machine inventory (names,
tailnet addresses, operating systems, when each machine was last online) maps the
operator's network and daily habits; by default it stays on the machine that holds the
vault. `TailnetIngestor` stamps the configured level on items without one and never makes
an item's level looser than it was (an unknown level becomes `local-private`). The MagicDNS
suffix is hashed because it names the tailnet, which can name its owner.

### State and `report.json`

`--state DIR` holds the adapter's private state. It is created `0700`; an existing
directory owned by the operator is tightened to `0700`; one owned by another user is refused
(`state_insecure`), as is a symlink (`state_is_symlink`).

- `snapshot.json` (`0600`): the hosts of the last committed run by `host_id`, with
  `first_seen`, `offline_since` and, for tombstones, `gone_since`, plus `taken_at`. It holds
  names and addresses, as private as the inventory itself. Never inside the vault. Every
  field is type-checked on load; anything unexpected is `bad_snapshot`.
- `report.json` (`0600`), rewritten on every run, failures included:

```json
{
  "format": "memfw.tailnet.report", "version": 1,
  "generated_at": "2026-01-05T12:00:00Z", "mode": "input",
  "status": "alert", "alerts": ["listed_offline"],
  "committed": true, "first_run": false, "allowlist": true, "offline_hours": 24.0,
  "counts": {"hosts": 3, "online": 2, "offline": 1, "skipped": 0, "skipped_shared": 1,
             "skipped_location": 1, "candidates": 0, "new": 0, "returned": 0, "gone": 0,
             "renamed": 0, "ip_changed": 0, "os_changed": 0, "tags_changed": 0,
             "listed_offline": 1, "listed_missing": 0, "listed_ambiguous": 0, "unknown": 0},
  "staged": 0, "duplicates": 0
}
```

  On failure: `{"format", "version", "generated_at", "mode", "status": "error", "error":
  "<code>", "alerts": [], "committed": false}`. The report never holds names, host ids or
  addresses.
- `.lock`: one scan at a time (`state_locked` otherwise).

A run **commits** (writes `snapshot.json`) only after its candidates were staged. Without
`--vault` the run is a preview: nothing is staged, the snapshot stays, and the next run sees
the same changes. If a run fails at any step, the snapshot stays as it was.

## Allowlist alerts

`--allowlist FILE` is the operator's list of expected machines: one entry per line, `#`
starts a comment, case-insensitive. An entry is one of:

- a host id (`host-` + 24 hex), the strongest form;
- a full MagicDNS name (`example-nas.<tailnet>.ts.net`): compared as first label plus hashed
  suffix;
- a bare first DNS label (`example-nas`).

The self-reported `HostName` is **never** matched: any machine can call itself anything,
so a new machine named like a listed one would otherwise hide from `unknown` and satisfy
`listed_missing`. MagicDNS labels are unique inside a tailnet (a second `example-nas` gets
`example-nas-1`). One risk remains: if the listed machine leaves and a new one takes its
label, a label entry follows the new machine; use host-id entries where that matters.

An entry that matches more than one present host (a bare label shared by hosts in
different tailnets, with `--include-shared`) is **ambiguous**: it vouches for none of them.
The file is at most 1 MiB and 10 000 entries; an entry outside `[a-z0-9][a-z0-9._:-]*`
refuses the run (`bad_allowlist`). Scans only read it.

| Count / alert code | Meaning |
|---|---|
| `listed_offline` | hosts vouched for by an entry that have been `online: false` longer than `--offline-hours` (default 24), measured from `last_seen`, or, when Tailscale gives none, from the first run that saw the host offline |
| `listed_missing` | entries that match no present host (tombstones do not count) |
| `listed_ambiguous` | entries that match more than one present host |
| `unknown` | present hosts no unambiguous entry vouches for (every unlisted host on every run, not only newly seen ones) |

Without `--allowlist` the four counts are `null` and no alert is raised. A host whose
`online` is unknown (old Tailscale versions) is never counted offline.

## Command line and exit codes

```sh
memfw-adapter-tailnet scan (--input FILE | --live) --state DIR
    [--vault VAULT] [--allowlist FILE] [--offline-hours 24]
    [--privacy local-private|shared|public] [--proposal-id ID]
    [--tailscale PATH] [--timeout SECONDS] [--include-shared]
```

Standard output is one JSON line: `status`, `committed`, `counts`, `alerts` (and `staged`
when a vault was given), or `{"error": "<code>", "status": "error"}`.

| Exit | Meaning |
|---|---|
| `0` | success, no alert (or no allowlist) |
| `1` | refused or failed; `error` names the code; nothing staged, snapshot unchanged |
| `2` | bad command line |
| `3` | success, at least one alert in `report.json` |

Error codes: `input_not_a_file`, `input_unreadable`, `status_too_large`, `bad_status_json`,
`backend_not_running`, `tailscale_not_found`, `tailscale_not_runnable`, `tailscale_timeout`,
`tailscale_failed`, `bad_timeout`, `bad_snapshot`, `state_is_symlink`, `state_insecure`,
`state_unwritable`, `state_locked`, `allowlist_unreadable`, `allowlist_too_large`,
`bad_allowlist`, `staging_failed`, and `internal` for any unexpected exception (its message
is not reported).

## Failure modes

| What fails | What happens | Recovery |
|---|---|---|
| Tailscale stopped or logged out | `backend_not_running`, exit 1, snapshot unchanged (no mass `gone`) | start Tailscale, rerun |
| CLI hangs or floods | process group killed at the deadline / cap, `tailscale_timeout` / `status_too_large` | rerun; raise `--timeout` up to 120 s |
| Corrupt, foreign or mistyped `snapshot.json` | `bad_snapshot`, exit 1; never silently treated as a first run | inspect, or delete it to start over (every host becomes `new` again) |
| Staging refused (no `vault.yaml`, symlinked `_candidates`, bad proposal id) | `staging_failed`, exit 1, snapshot unchanged | fix the vault, rerun: the same changes are staged |
| Crash after staging, before the snapshot is written | the rerun stages byte-identical candidates: `staged: 0`, `duplicates: n` | none needed |
| State directory owned by someone else | `state_insecure` | use a directory you own |
| Two scans at once | the second gets `state_locked` | rerun later |
| A bug | `internal`, exit 1, report says so, snapshot unchanged | report it |
| A failing run | `report.json` says `status: error` with the code | alerting can treat a stale or error report as its own alert |

## Tests

`tests/test_adapter_tailnet.py` covers schema variants (recent with the real schema's extra
fields, old without ids/`Online`/`Tags`, mistyped fields, IPv6 only, no `Peer`,
non-running backends), shared-in and `Location` nodes, a canary that user and login data,
node IDs, keys, `sshHostKeys`, `CapMap`, `CertDomains`, `ExtraRecords`, endpoint addresses
and the DNS suffix reach no output, stable hashed ids, the diff (new, returned, gone,
renamed, IP, OS and tag changes; unchanged and online flaps produce nothing; tombstone
expiry), crash-then-rerun idempotency, privacy defaults, preview and failed-stage runs
leaving the snapshot alone, snapshot type validation, state directory tightening and
refusal, allowlist matching (no `HostName` matching, hashed suffix, ambiguity) and alerts,
live mode (fixed argv, no shell, proxies stripped, timeout kills child and grandchild,
output cap), the ingestor contract kit, and the import-layer rule (stdlib and memfw ports
only). `tests/standalone/wheel_smoke.py --tailnet` runs the contract kit on the built wheel
without memfw.
