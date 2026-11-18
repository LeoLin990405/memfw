---
privacy: shared
tags: [ops, network]
---
# Network layout

## VLAN plan

Cameras and other IoT gadgets live on vlan 20 with no route to the management
vlan. The printer sits on vlan 30.

## DNS

The resolver forwards to an unbound recursor; ad blocking runs in front of it.
