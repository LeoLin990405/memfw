---
privacy: public
tags: [ops, backup]
---
# Backup runbook

## Nightly snapshot

A restic snapshot of the homelab volumes goes to the storage box every night at
02:00. Snapshots older than ninety days are pruned on Sundays.

## Restore drill

Once a month a random snapshot is restored into a scratch volume and every file
checksum is compared against the manifest. A mismatch opens an incident.
