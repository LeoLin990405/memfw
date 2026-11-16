"""What the ``handoff`` and ``topics`` slots read, built from other capabilities (C2).

``memfw.context`` is an L3 capability and may not import another one, so the
composition root adapts them here: the conversation share (L3,
``conversation_share`` applies for the calling app) becomes a
``SessionFeed``, and the concept files (``memfw.ontology``) become a
``TopicCatalogue``.
"""

from __future__ import annotations

from pathlib import Path

from memfw.context import SessionFeed, SessionSummary, SlotUnavailable, TopicCatalogue

#: ``merged_into`` hops followed before giving up (the ontology's own bound).
_MAX_MERGE_HOPS = 16


def share_feed(root: Path, app: str) -> SessionFeed:
    """The latest sessions ``app`` may read, as handoff summaries.

    Everything goes through ``ConversationShare``: ``list_sessions`` (policy
    filtered: hidden and opted-out apps never appear, ``can_read`` narrows)
    and ``handoff`` (redacted, system-derived goal and turn refs). No app, the
    reserved operator name, sharing off, or a caller that may not read gives
    no sessions (an empty slot, not an error); a missing store raises
    ``SlotUnavailable``.
    """

    def feed(limit: int) -> list[SessionSummary]:
        from memfw.conversations import OPERATOR, ConversationError, open_share

        caller = app.strip()
        if not caller or caller.lower() == OPERATOR:
            return []
        share = open_share(root, caller)
        try:
            share.policy.scope(share.caller)
        except ConversationError:
            return []
        if not share.source.available():
            raise SlotUnavailable("conversation store is not available")
        listing = share.list_sessions(limit=limit)
        out: list[SessionSummary] = []
        for row in listing["sessions"]:
            try:
                pack = share.handoff(str(row["session_id"]), app=str(row["app"]))
            except ConversationError:
                continue
            system = pack["system"]
            out.append(
                SessionSummary(
                    app=str(row["app"]),
                    session_id=str(row["session_id"]),
                    turns=int(row.get("turns") or 0),
                    updated_at=str(row.get("updated_at") or ""),
                    goal=str(system.get("goal") or ""),
                    refs=tuple(str(turn["ref"]) for turn in system.get("recent_turns", [])),
                )
            )
        return out

    return feed


def concept_catalogue(root: Path) -> TopicCatalogue:
    """Active concepts by oid, each oid (merged ones too) mapped to the oid it counts for.

    Only ``status: active`` concepts that are not merged into another are
    listed; a merged concept's memories count for the concept it was merged
    into (followed for at most ``_MAX_MERGE_HOPS`` hops; a cycle or a dangling
    target stops at the last concept that exists). An invalid concept file
    raises (the slot is then ``degraded: error``).
    """

    from memfw.ontology import load_concepts

    concepts = load_concepts(root)
    labels = {
        oid: concept.pref_label
        for oid, concept in concepts.items()
        if concept.status == "active" and not concept.merged_into
    }
    canonical: dict[str, str] = {}
    for oid in concepts:
        current = oid
        seen = {current}
        for _ in range(_MAX_MERGE_HOPS):
            target = concepts[current].merged_into
            if not target or target not in concepts or target in seen:
                break
            seen.add(target)
            current = target
        if current in labels:
            canonical[oid] = current
    return TopicCatalogue(labels=labels, canonical=canonical)


__all__ = ["concept_catalogue", "share_feed"]
