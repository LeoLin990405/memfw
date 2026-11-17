# memfw-recall

The memfw recall coordinator as a standalone wheel over `memfw-ports`:
`Coordinator`, the routers, reciprocal-rank fusion, `FilterPolicy`, the
resolver protocol and the open-question labels. Import it as `memfw_recall`;
inside memfw the same modules are `memfw.recall`.

`memfw.recall.builtin` (the vault resolver and the FTS, multihop and hybrid
providers) reaches memfw internals and is not part of this wheel. Callers
supply their own `RecallProvider`s and `Resolver`, for example
`memfw_ports.reference.InMemoryRecallProvider`.

The sources are memfw's `src/memfw/recall`; see `docs/ports-and-recall.md`
section 1.1 in the memfw repository.
