# memfw-ports

The memfw port contracts as a standalone, standard-library-only wheel: the
`RecallProvider`, `Projector`, `Ingestor`, `Deriver`, `Clock`, `IdGen`,
`ClaimVerifier`, `ClaimStager`, `ContextSlotProvider` and `ModelBackend` protocols, their value
types, candidate staging, the adapter registry, the contract kit
(`memfw_ports.testing`) and the reference implementations
(`memfw_ports.reference`).

An adapter author installs this wheel and checks an implementation without
installing memfw:

```python
from memfw_ports.testing import check_recall_provider

check_recall_provider(MyProvider(), vault=my_fixture_vault, queries=["..."])
```

Inside memfw the same modules are `memfw.ports`; only the top-level name
differs. They are separate copies: inside a memfw process use `memfw.ports`,
because `memfw_ports` classes never match memfw's in `isinstance`. The wheel
version is `memfw.ports.PORTS_API_VERSION`. The sources are memfw's `src/memfw/ports`; see
`docs/ports-and-recall.md` section 1.1 in the memfw repository.
