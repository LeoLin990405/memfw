# memfw-adapter-tailnet

A memfw `Ingestor` that turns the output of `tailscale status --json` into reviewable
host-fact candidates: one per machine that is new, returned, gone, renamed, has new tailnet
IPs, a new OS or new tags since the previous run. Unchanged machines produce nothing. A
names-free `report.json` feeds alerting (allowlisted machines offline too long, entries that
match nothing or several machines, machines no entry vouches for).

- **Input:** an exported file (`--input FILE`) or one run of `tailscale status --json`
  (`--live`): fixed argv, no shell, 15 s deadline, 8 MiB cap, proxy variables removed. It
  never scans networks and never calls the Tailscale API.
- **Kept per machine:** name, first MagicDNS label (the tailnet suffix only as a hash),
  tailnet IPs, OS, online, last seen, tags, and a hashed host id derived from the Tailscale
  node ID. User and login data, keys, endpoints and everything else in the JSON are never
  read. Shared-in nodes are left out unless `--include-shared`; provider exit nodes always.
- **Privacy:** candidates are `local-private` unless the operator sets `--privacy` (or
  `adapters.tailnet.privacy`).
- **Never writes authority.** Candidates land in `<vault>/_candidates/tailnet/` and go
  through the normal review. The allowlist is the operator's file; scans only read it.
- **Off by default.** Standard library only; depends on `memfw-ports`.

```sh
memfw-adapter-tailnet scan --input status.json --state ~/.local/state/memfw-tailnet \
    --vault ~/memfw-vault --allowlist expected-hosts.txt --offline-hours 24
```

See `docs/adapter-tailnet.md` in the memfw repository for the candidate and report formats,
the diff rules, exit codes and failure modes.
