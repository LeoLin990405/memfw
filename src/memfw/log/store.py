"""Append-only event stream and deterministic projection."""

from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from ..core.context import RepositoryHost
from ..core.errors import IntegrityError
from ..core.models import Event, EventKind


class EventLogMixin:
    _event_cursor_cache: tuple[tuple[int, int, int, int, int], dict[str, Any]] | None

    def recover_event_append(self: RepositoryHost) -> bool:
        """Complete only an append whose exact canonical event is recorded in WAL."""
        intent_path = self.root / ".event-append.pending.json"
        if not intent_path.exists():
            return False
        try:
            intent = json.loads(intent_path.read_text(encoding="utf-8"))
            if intent.get("schema_version") != 1:
                raise ValueError("unsupported event append intent")
            offset = intent["offset"]
            line = intent["line"].encode("utf-8")
            event = Event.model_validate_json(line)
            if (
                not isinstance(offset, int)
                or offset < 0
                or not line.endswith(b"\n")
                or event.model_dump_json().encode("utf-8") + b"\n" != line
                or event.event_hash != intent.get("event_hash")
                or event.prev_event_hash != intent.get("previous_hash")
            ):
                raise ValueError("invalid event append payload")
            from ..log import event_digest

            if event_digest(event) != event.event_hash:
                raise ValueError("event append digest mismatch")
            size = self.events_path.stat().st_size if self.events_path.exists() else 0
            if size < offset or size > offset + len(line):
                raise ValueError("event log size is outside append intent bounds")
            with self.events_path.open("rb") as stream:
                stream.seek(offset)
                suffix = stream.read()
            if suffix != line and not line.startswith(suffix):
                raise ValueError("event log tail does not match pending append")
            if offset:
                prior = _read_event_before_offset(self.events_path, offset)
                if prior.event_hash != event.prev_event_hash:
                    raise ValueError("pending append prior event does not match log tail")
            else:
                archived, _ = self._log_segments()
                archived_hash = archived[-1].event_hash if archived else "0" * 64
                if archived_hash != event.prev_event_hash:
                    raise ValueError("pending append does not match archived anchor")
            head = self._read_head_hash()
            if head not in {event.prev_event_hash, event.event_hash}:
                raise ValueError("event head does not match pending append")
            if suffix != line:
                with self.events_path.open("r+b") as stream:
                    stream.truncate(offset)
                    stream.flush()
                    os.fsync(stream.fileno())
                    stream.seek(offset)
                    stream.write(line)
                    stream.flush()
                    os.fsync(stream.fileno())
            recovering_before = self._recovering
            self._recovering = True
            try:
                verified = self.events()
            finally:
                self._recovering = recovering_before
            if (
                len(verified) != intent["event_count"]
                or not verified
                or verified[-1].event_hash != event.event_hash
            ):
                raise IntegrityError("Recovered event append does not match full log projection")
            stat = self.events_path.stat()
            cursor = self._seed_event_cursor(int(intent["event_count"]), event.event_hash)
            self._event_cursor_cache = (
                (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns),
                cursor,
            )
            intent_path.unlink(missing_ok=True)
            return True
        except IntegrityError:
            raise
        except Exception as exc:
            raise IntegrityError(f"Unable to recover event append intent: {exc}") from exc

    def _log_segments(self: RepositoryHost) -> tuple[list[Event], list[dict[str, Any]]]:
        if not self.log_catalog_path.exists():
            return [], []
        try:
            catalog = json.loads(self.log_catalog_path.read_text(encoding="utf-8"))
            catalog_schema = catalog.get("schema_version")
            if catalog_schema not in {1, 2} or not isinstance(catalog.get("segments"), list):
                raise ValueError("unsupported catalog")
        except (OSError, ValueError, TypeError) as exc:
            raise IntegrityError(f"Invalid event segment catalog: {exc}") from exc
        if catalog_schema == 2:
            return _read_containerized_segments(self, catalog)
        catalog_bytes = self.log_catalog_path.read_bytes()
        compressed_segments: list[bytes] = []
        hashes: list[str] = []
        for record in catalog["segments"]:
            if not isinstance(record, dict):
                raise IntegrityError("Invalid event segment catalog record")
            name = record.get("name")
            if (
                not isinstance(name, str)
                or Path(name).name != name
                or not name.endswith(".tar.zst")
            ):
                raise IntegrityError("Invalid event segment filename")
            try:
                compressed = (self.log_segments_dir / name).read_bytes()
            except OSError as exc:
                raise IntegrityError(f"Unable to read event segment {name}: {exc}") from exc
            digest = hashlib.sha256(compressed).hexdigest()
            if digest != record.get("compressed_hash"):
                raise IntegrityError(f"Event segment hash mismatch: {name}")
            compressed_segments.append(compressed)
            hashes.append(digest)
        cache_key = (hashlib.sha256(catalog_bytes).hexdigest(), tuple(hashes))
        cached = getattr(self, "_verified_log_segments_cache", None)
        if cached is not None and cached[0] == cache_key:
            return (
                [event.model_copy(deep=True) for event in cached[1]],
                [dict(record) for record in cached[2]],
            )
        events: list[Event] = []
        records: list[dict[str, Any]] = []
        previous = "0" * 64
        previous_manifest_hash = "0" * 64
        previous_summary_hash = "0" * 64
        for index, record in enumerate(catalog["segments"]):
            if not isinstance(record, dict):
                raise IntegrityError("Invalid event segment catalog record")
            name = record.get("name")
            if (
                not isinstance(name, str)
                or Path(name).name != name
                or not name.endswith(".tar.zst")
            ):
                raise IntegrityError("Invalid event segment filename")
            try:
                compressed = compressed_segments[index]
                import zstandard

                tar_bytes = zstandard.ZstdDecompressor().decompress(compressed)
                with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as archive:
                    names = archive.getnames()
                    if names not in (
                        ["events.jsonl", "snapshot.json", "manifest.json"],
                        ["summaries.jsonl", "snapshot.json", "manifest.json"],
                    ):
                        raise IntegrityError(f"Unexpected event segment members: {name}")
                    content: dict[str, bytes] = {}
                    for member_name in archive.getnames():
                        member = archive.extractfile(member_name)
                        if member is None:
                            raise IntegrityError(f"Missing event segment member: {member_name}")
                        content[member_name] = member.read()
                manifest = json.loads(content["manifest.json"])
                if manifest.get("previous_hash") != previous:
                    raise IntegrityError(f"Broken event segment anchor: {name}")
                schema_version = manifest.get("schema_version")
                if schema_version == 1 and "events.jsonl" in content:
                    parsed = _parse_segment_events(content["events.jsonl"], previous)
                    payload_hash = hashlib.sha256(content["events.jsonl"]).hexdigest()
                    if payload_hash != manifest.get("events_hash"):
                        raise IntegrityError(f"Event segment payload hash mismatch: {name}")
                elif schema_version == 2 and "summaries.jsonl" in content:
                    if manifest.get("previous_manifest_hash") != previous_manifest_hash:
                        raise IntegrityError(f"Broken cross-snapshot manifest link: {name}")
                    if manifest.get("previous_summary_hash") != previous_summary_hash:
                        raise IntegrityError(f"Broken event summary chain anchor: {name}")
                    parsed, previous_summary_hash = _parse_segment_summaries(
                        content["summaries.jsonl"], previous, previous_summary_hash
                    )
                    if previous_summary_hash != manifest.get("last_summary_hash"):
                        raise IntegrityError(f"Event summary segment bounds mismatch: {name}")
                    if hashlib.sha256(content["summaries.jsonl"]).hexdigest() != manifest.get(
                        "summaries_hash"
                    ):
                        raise IntegrityError(f"Event summary payload hash mismatch: {name}")
                else:
                    raise IntegrityError(f"Unsupported event segment format: {name}")
                if not parsed or len(parsed) != manifest.get("event_count"):
                    raise IntegrityError(f"Invalid event segment count: {name}")
                if parsed[0].event_hash != manifest.get("first_hash") or parsed[
                    -1
                ].event_hash != manifest.get("last_hash"):
                    raise IntegrityError(f"Invalid event segment bounds: {name}")
                if record.get("first_hash") not in (None, parsed[0].event_hash) or record.get(
                    "last_hash"
                ) not in (None, parsed[-1].event_hash):
                    raise IntegrityError(f"Event catalog bounds mismatch: {name}")
                snapshot = json.loads(content["snapshot.json"])
                if hashlib.sha256(content["snapshot.json"]).hexdigest() != manifest.get(
                    "snapshot_hash"
                ):
                    raise IntegrityError(f"Event snapshot hash mismatch: {name}")
                if snapshot != _project_events(events + parsed):
                    raise IntegrityError(f"Event snapshot mismatch: {name}")
                previous = parsed[-1].event_hash
                previous_manifest_hash = hashlib.sha256(content["manifest.json"]).hexdigest()
                if schema_version == 1:
                    previous_summary_hash = previous_manifest_hash
                if record.get("manifest_hash") not in (None, previous_manifest_hash):
                    raise IntegrityError(f"Event segment manifest hash mismatch: {name}")
                events.extend(parsed)
                records.append(
                    {
                        **record,
                        "schema_version": schema_version,
                        "manifest_hash": previous_manifest_hash,
                        "summary_hash": previous_summary_hash,
                    }
                )
            except IntegrityError:
                raise
            except Exception as exc:
                raise IntegrityError(f"Unable to verify event segment {name}: {exc}") from exc
        self._verified_log_segments_cache = (
            cache_key,
            [event.model_copy(deep=True) for event in events],
            [dict(record) for record in records],
        )
        return events, records

    def recover_log_compaction(self: RepositoryHost) -> bool:
        """Explicitly rewrite an interrupted duplicate-prefix log to its verified live tail."""
        with self._locked():
            logical = self.events()
            archived, _ = self._log_segments()
            expected_tail = logical[len(archived) :]
            expected_bytes = b"".join(
                (event.model_dump_json() + "\n").encode() for event in expected_tail
            )
            current = self.events_path.read_bytes() if self.events_path.exists() else b""
            live = self._live_events(archived)
            canonical_live = b"".join((event.model_dump_json() + "\n").encode() for event in live)
            if current == canonical_live:
                return False
            self._atomic_write(self.events_path, expected_bytes)
            (self.root / "events.cursor.json").unlink(missing_ok=True)
            self._event_cursor_cache = None
            logical = archived + expected_tail
            self._seed_event_cursor(len(logical), logical[-1].event_hash if logical else "0" * 64)
            return True

    def _live_events(self: RepositoryHost, archived: list[Event]) -> list[Event]:
        from ..log import event_digest, read_events

        if not self.events_path.exists() or not self.events_path.stat().st_size:
            return read_events(
                self.events_path,
                verify_head=not self._recovering,
                expected_prev=archived[-1].event_hash if archived else "0" * 64,
            )
        if not archived:
            return read_events(self.events_path, verify_head=not self._recovering)
        raw = self.events_path.read_bytes()
        if b"\r" in raw or not raw.endswith(b"\n"):
            raise IntegrityError("Event log must use canonical LF line endings")
        try:
            lines = raw[:-1].split(b"\n")
            parsed = [Event.model_validate_json(line) for line in lines]
        except Exception as exc:
            raise IntegrityError(f"Invalid event log during compaction recovery: {exc}") from exc
        if any(
            line.decode("utf-8") != event.model_dump_json()
            for line, event in zip(lines, parsed, strict=True)
        ):
            raise IntegrityError("Non-canonical event in compacted log tail")
        if any(event_digest(event) != event.event_hash for event in parsed):
            raise IntegrityError("Event hash mismatch in compacted log tail")
        first = parsed[0]
        if first.prev_event_hash == archived[-1].event_hash:
            return read_events(
                self.events_path,
                verify_head=not self._recovering,
                expected_prev=archived[-1].event_hash,
            )
        offsets = [
            index for index, event in enumerate(archived) if event.event_hash == first.event_hash
        ]
        if len(offsets) != 1:
            raise IntegrityError("Compacted event log does not link to its archived snapshot")
        offset = offsets[0]
        if first.prev_event_hash != archived[offset].prev_event_hash:
            raise IntegrityError("Compacted event log has an invalid archived anchor")
        if len(parsed) < len(archived) - offset:
            raise IntegrityError("Interrupted compaction log is missing archived events")
        overlap = parsed[: len(archived) - offset]
        if [event.event_hash for event in overlap] != [
            event.event_hash for event in archived[offset:]
        ]:
            raise IntegrityError("Interrupted compaction log differs from its archived segment")
        tail = parsed[len(overlap) :]
        expected_prev = archived[-1].event_hash
        for event in tail:
            if event.prev_event_hash != expected_prev or event_digest(event) != event.event_hash:
                raise IntegrityError("Broken event chain after interrupted compaction")
            expected_prev = event.event_hash
        expected_head = expected_prev if tail else archived[-1].event_hash
        if not self._recovering:
            state_path = self.event_state_path
            if state_path.exists():
                try:
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                except (OSError, ValueError, TypeError) as exc:
                    raise IntegrityError(
                        f"Event state is invalid during compaction recovery: {exc}"
                    ) from exc
                expected_count = len(archived) + len(tail)
                if (
                    state.get("schema_version") != 2
                    or state.get("last_hash") != expected_head
                    or state.get("event_count") != expected_count
                ):
                    raise IntegrityError("Event state does not match the compacted log")
            else:
                head_path = self.root / "events.head"
                if (
                    not head_path.exists()
                    or head_path.read_text(encoding="ascii").strip() != expected_head
                ):
                    raise IntegrityError(
                        "Event head is missing or does not match the compacted log"
                    )
        return tail

    def compact_log(self: RepositoryHost, keep_recent: int | None = None) -> dict[str, int | str]:
        """Compact logical segments into one shared-compression summary container."""
        with self._locked():
            all_events = self.events()
            keep = keep_recent
            if keep is None:
                keep = int(self.read_config().get("log_keep_recent", 100))
            if isinstance(keep, bool) or not isinstance(keep, int) or keep < 1:
                raise ValueError("keep_recent must be a positive integer")
            archived, old_records = self._log_segments()
            tail = all_events[len(archived) :]
            count = max(0, len(tail) - keep)
            migrating = any(not record.get("containerized", False) for record in old_records)
            if count == 0 and not migrating:
                return {
                    "archived_event_count": len(archived),
                    "newly_archived_event_count": 0,
                    "retained_event_count": len(tail),
                    "raw_bytes": 0,
                    "compressed_bytes": 0,
                    "segment_count": len(old_records),
                    "snapshot_count": len(old_records),
                    "verification_level": "summary_and_snapshot",
                }

            end_index = len(archived) + count
            to_archive = all_events[:end_index]
            group_sizes = [int(record["event_count"]) for record in old_records]
            if count:
                group_sizes.append(count)
            if not group_sizes and to_archive:
                group_sizes = [len(to_archive)]
            if sum(group_sizes) != len(to_archive):
                raise IntegrityError("Archived segment boundaries do not match event projection")

            new_records: list[dict[str, Any]] = []
            members: list[tuple[str, bytes]] = []
            previous_manifest_hash = "0" * 64
            previous_summary_hash = "0" * 64
            previous_event_hash = "0" * 64
            offset = 0
            total_raw = 0
            old_names = {str(record["name"]) for record in old_records}
            previous_compressed = sum(
                (self.log_segments_dir / name).stat().st_size
                for name in old_names
                if (self.log_segments_dir / name).exists()
            )
            for segment_index, group_size in enumerate(group_sizes):
                chunk = to_archive[offset : offset + group_size]
                offset += group_size
                prefix = f"segments/{segment_index:08d}"
                summaries, last_summary_hash = _summarize_events(chunk, previous_summary_hash)
                summaries_bytes = ("\n".join(summaries) + "\n").encode()
                snapshot_bytes = _canonical_json(_project_events(to_archive[:offset]))
                manifest = {
                    "schema_version": 2,
                    "event_count": len(chunk),
                    "previous_hash": previous_event_hash,
                    "previous_manifest_hash": previous_manifest_hash,
                    "previous_summary_hash": previous_summary_hash,
                    "first_hash": chunk[0].event_hash,
                    "last_hash": chunk[-1].event_hash,
                    "summaries_hash": hashlib.sha256(summaries_bytes).hexdigest(),
                    "snapshot_hash": hashlib.sha256(snapshot_bytes).hexdigest(),
                    "last_summary_hash": last_summary_hash,
                }
                manifest_bytes = _canonical_json(manifest)
                manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
                new_records.append(
                    {
                        "member_prefix": prefix,
                        "event_count": len(chunk),
                        "first_hash": chunk[0].event_hash,
                        "last_hash": chunk[-1].event_hash,
                        "previous_hash": previous_event_hash,
                        "manifest_hash": manifest_hash,
                        "summary_hash": last_summary_hash,
                        "schema_version": 2,
                    }
                )
                members.extend(
                    [
                        (f"{prefix}/summaries.jsonl", summaries_bytes),
                        (f"{prefix}/snapshot.json", snapshot_bytes),
                        (f"{prefix}/manifest.json", manifest_bytes),
                    ]
                )
                previous_event_hash = chunk[-1].event_hash
                previous_manifest_hash = manifest_hash
                previous_summary_hash = last_summary_hash
                total_raw += len(summaries_bytes) + len(snapshot_bytes) + len(manifest_bytes)

            tar_bytes = _deterministic_tar(members)
            import zstandard

            compressed = zstandard.ZstdCompressor(
                level=19, write_checksum=True, write_content_size=True
            ).compress(tar_bytes)
            compressed_hash = hashlib.sha256(compressed).hexdigest()
            name = f"summary-container-{compressed_hash[:24]}.tar.zst"
            segment_path = self.log_segments_dir / name
            if segment_path.exists() and segment_path.read_bytes() != compressed:
                raise IntegrityError("Event summary container name collision")
            if not segment_path.exists():
                self._atomic_write(segment_path, compressed)

            self._atomic_write(
                self.log_catalog_path,
                _canonical_json(
                    {
                        "schema_version": 2,
                        "container": {"name": name, "compressed_hash": compressed_hash},
                        "segments": new_records,
                    }
                ),
            )
            # Catalog commit precedes truncation. Summary segments retain the
            # original event hash chain; recovery can trim a duplicated old prefix.
            active_tail = all_events[end_index:]
            tail_bytes = b"".join(
                (event.model_dump_json() + "\n").encode() for event in active_tail
            )
            self._atomic_write(self.events_path, tail_bytes)
            (self.root / "events.cursor.json").unlink(missing_ok=True)
            self._event_cursor_cache = None
            self._seed_event_cursor(
                len(all_events), all_events[-1].event_hash if all_events else "0" * 64
            )
            for segment in self.log_segments_dir.glob("*.tar.zst"):
                if segment.name != name:
                    segment.unlink(missing_ok=True)
            return {
                "archived_event_count": end_index,
                "newly_archived_event_count": count,
                "retained_event_count": len(active_tail),
                "raw_bytes": total_raw,
                "compressed_bytes": len(compressed),
                "previous_compressed_bytes": previous_compressed,
                "segment_count": len(new_records),
                "snapshot_count": len(new_records),
                "verification_level": "summary_and_snapshot",
            }

    def _append_event(
        self: RepositoryHost,
        kind: EventKind,
        memory_id: str,
        *,
        actor: str,
        before_hash: str | None = None,
        after_hash: str | None = None,
        details: dict[str, Any] | None = None,
        timestamp: datetime | None = None,
    ) -> Event:
        from ..log import event_digest

        cached = getattr(self._mutation_local, "event_cache", None)
        fast_append = bool(getattr(self._mutation_local, "fast_event_append", False))
        if fast_append:
            cursor = self._validated_event_cursor()
            base_count = int(cursor["event_count"])
            previous = str(cursor["last_hash"])
        elif cached is not None:
            base_count = len(cached)
            previous = cached[-1].event_hash if cached else "0" * 64
        else:
            existing = (
                self.events() if self.events_path.exists() or self.log_catalog_path.exists() else []
            )
            base_count = len(existing)
            previous = existing[-1].event_hash if existing else "0" * 64
        event = Event(
            event_id=self.id_factory(),
            kind=kind,
            memory_id=memory_id,
            timestamp=timestamp or self._now(),
            actor=actor,
            before_hash=before_hash,
            after_hash=after_hash,
            version_ref=after_hash,
            details=details or {},
            prev_event_hash=previous,
            event_hash="",
        )
        event = event.model_copy(update={"event_hash": event_digest(event)})
        self.root.mkdir(parents=True, exist_ok=True)
        line = (event.model_dump_json() + "\n").encode("utf-8")
        if cached is not None:
            cached.append(event)
            used_ids = getattr(self._mutation_local, "event_memory_ids", None)
            if used_ids is not None:
                used_ids.add(event.memory_id)
        if getattr(self._mutation_local, "defer_events", False):
            return event
        self._seed_event_cursor(base_count, previous)
        self._append_event_with_intent(event, line, previous, base_count + 1)
        return event

    def _seed_event_cursor(
        self: RepositoryHost, event_count: int, last_hash: str
    ) -> dict[str, Any]:
        stat = self.events_path.stat()
        catalog_bytes = (
            self.log_catalog_path.read_bytes() if self.log_catalog_path.exists() else b""
        )
        cursor = {
            "schema_version": 2,
            "catalog_hash": hashlib.sha256(catalog_bytes).hexdigest(),
            "offset": stat.st_size,
            "event_count": event_count,
            "last_hash": last_hash,
        }
        cursor_path = self.event_state_path
        try:
            if json.loads(cursor_path.read_text(encoding="utf-8")) != cursor:
                raise ValueError("stale cursor")
        except (OSError, ValueError, TypeError):
            self._atomic_write(
                cursor_path,
                json.dumps(cursor, sort_keys=True, separators=(",", ":")).encode() + b"\n",
            )
        (self.root / "events.head").unlink(missing_ok=True)
        (self.root / "events.cursor.json").unlink(missing_ok=True)
        return cursor

    def _validated_event_cursor(self: RepositoryHost) -> dict[str, Any]:
        """Load a cross-process append cursor, rebuilding it after any file change."""
        cursor_path = self.event_state_path
        catalog_bytes = (
            self.log_catalog_path.read_bytes() if self.log_catalog_path.exists() else b""
        )
        catalog_hash = hashlib.sha256(catalog_bytes).hexdigest()
        stat = self.events_path.stat()
        process_signature = (
            stat.st_dev,
            stat.st_ino,
            stat.st_size,
            stat.st_mtime_ns,
            stat.st_ctime_ns,
        )
        process_cache = self._event_cursor_cache
        if process_cache is not None and process_cache[0] == process_signature:
            cursor = process_cache[1]
            try:
                cached_sidecar = json.loads(cursor_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                cached_sidecar = None
            if (
                cached_sidecar == cursor
                and cursor["catalog_hash"] == catalog_hash
                and self._read_head_hash() == cursor["last_hash"]
            ):
                return cursor
        try:
            persisted_cursor = cast(
                dict[str, Any], json.loads(cursor_path.read_text(encoding="utf-8"))
            )
            if (
                persisted_cursor.get("schema_version") == 2
                and persisted_cursor.get("catalog_hash") == catalog_hash
                and isinstance(persisted_cursor.get("last_hash"), str)
                and isinstance(persisted_cursor.get("event_count"), int)
                and persisted_cursor.get("offset") == stat.st_size
                and self._read_head_hash() == persisted_cursor["last_hash"]
            ):
                events = self.events()
                if (
                    len(events) == persisted_cursor["event_count"]
                    and (events[-1].event_hash if events else "0" * 64)
                    == persisted_cursor["last_hash"]
                ):
                    self._event_cursor_cache = (process_signature, persisted_cursor)
                    return persisted_cursor
        except (OSError, ValueError, TypeError, KeyError, IntegrityError):
            pass

        events = self.events()
        last_hash = events[-1].event_hash if events else "0" * 64
        if self._read_head_hash() != last_hash:
            raise IntegrityError("Event head is missing or does not match the log")
        stat = self.events_path.stat()
        cursor = self._seed_event_cursor(len(events), last_hash)
        self._event_cursor_cache = (process_signature, cursor)
        return cursor

    def _read_head_hash(self: RepositoryHost) -> str:
        if self.event_state_path.exists():
            try:
                state = json.loads(self.event_state_path.read_text(encoding="utf-8"))
                if state.get("schema_version") != 2 or not isinstance(state.get("last_hash"), str):
                    raise ValueError("invalid combined event state")
                if not (self.root / ".event-append.pending.json").exists():
                    legacy_head = self.root / "events.head"
                    if (
                        legacy_head.exists()
                        and legacy_head.read_text(encoding="ascii").strip() != state["last_hash"]
                    ):
                        raise IntegrityError("Legacy event head conflicts with combined state")
                return cast(str, state["last_hash"])
            except (OSError, ValueError, TypeError, KeyError) as exc:
                raise IntegrityError(f"Event state is invalid: {exc}") from exc
        head_path = self.root / "events.head"
        try:
            return head_path.read_text(encoding="ascii").strip()
        except OSError as exc:
            raise IntegrityError("Event head is missing") from exc

    def _append_event_with_intent(
        self: RepositoryHost,
        event: Event,
        line: bytes,
        previous: str,
        event_count: int,
    ) -> None:
        intent_path = self.root / ".event-append.pending.json"
        offset = self.events_path.stat().st_size
        cursor_path = self.event_state_path
        try:
            cursor = json.loads(cursor_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise IntegrityError("Event append cursor is missing or invalid") from exc
        catalog_bytes = (
            self.log_catalog_path.read_bytes() if self.log_catalog_path.exists() else b""
        )
        if (
            cursor.get("schema_version") != 2
            or cursor.get("catalog_hash") != hashlib.sha256(catalog_bytes).hexdigest()
            or cursor.get("offset") != offset
            or cursor.get("event_count") != event_count - 1
            or cursor.get("last_hash") != previous
        ):
            raise IntegrityError("Event append cursor changed while holding the vault lock")
        intent = {
            "schema_version": 1,
            "offset": offset,
            "previous_hash": previous,
            "event_hash": event.event_hash,
            "line": line.decode("utf-8"),
            "event_count": event_count,
        }
        self._atomic_write(
            intent_path,
            json.dumps(intent, sort_keys=True, separators=(",", ":")).encode() + b"\n",
        )
        with self.events_path.open("ab") as stream:
            stream.write(line)
            stream.flush()
            os.fsync(stream.fileno())
        stat = self.events_path.stat()
        catalog_bytes = (
            self.log_catalog_path.read_bytes() if self.log_catalog_path.exists() else b""
        )
        updated: dict[str, Any] = {
            "schema_version": 2,
            "catalog_hash": hashlib.sha256(catalog_bytes).hexdigest(),
            "offset": stat.st_size,
            "event_count": intent["event_count"],
            "last_hash": event.event_hash,
        }
        self._atomic_write(
            cursor_path,
            json.dumps(updated, sort_keys=True, separators=(",", ":")).encode() + b"\n",
        )
        (self.root / "events.head").unlink(missing_ok=True)
        (self.root / "events.cursor.json").unlink(missing_ok=True)
        self._event_cursor_cache = (
            (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns),
            updated,
        )
        intent_path.unlink(missing_ok=True)

    def events(self: RepositoryHost) -> list[Event]:
        cached = getattr(self._mutation_local, "event_cache", None)
        if cached is not None and getattr(self._mutation_local, "depth", False):
            return list(cached)
        if (self.root / ".event-append.pending.json").exists() and not self._recovering:
            raise IntegrityError("A pending event append needs explicit `memfw recover`")
        if (
            self.pending_path.exists()
            and not getattr(self._mutation_local, "depth", 0)
            and not self._recovering
        ):
            raise IntegrityError("A pending transaction needs explicit `memfw recover`")

        archived, _ = self._log_segments()
        live = self._live_events(archived)
        return archived + live

    def replay(self: RepositoryHost) -> dict[str, str | None]:
        """Project terminal committed hashes; validate only retained terminal Markdown objects."""
        state: dict[str, str | None] = {}
        for event in self.events():
            if event.kind in {
                EventKind.CONFIG_CHANGE,
                EventKind.AGENT_PROPOSAL,
                EventKind.HUMAN_DECISION,
                EventKind.SIMULATED_DECISION,
                EventKind.CAPTURE_WAKE,
                EventKind.CAPTURE_SLEEP,
                EventKind.CAPTURE_STATE_CHANGE,
                EventKind.CAPTURE_RECORD,
                EventKind.FORGET,
                EventKind.MEMORY_ACCESS,
                EventKind.CONSOLIDATION_PROPOSAL,
                EventKind.BUDGET_UNSATISFIABLE,
                EventKind.PROPOSAL_ERASE,
                EventKind.KNOWLEDGE_CARD_CREATE,
                EventKind.KNOWLEDGE_SOURCE_MISSING,
                EventKind.KNOWLEDGE_SOURCE_REPOINT,
            }:
                continue
            if event.kind in {EventKind.DELETE, EventKind.EXTERNAL_DELETE} or (
                event.kind == EventKind.EXTERNAL_EDIT and event.details.get("quarantined") is True
            ):
                state[event.memory_id] = None
            elif event.after_hash is not None:
                if event.version_ref != event.after_hash:
                    raise IntegrityError(f"Invalid version reference in event {event.event_id}")
                state[event.memory_id] = event.after_hash
            elif event.memory_id not in state:
                state[event.memory_id] = None
        # Replay is a read-only event projection. It deliberately does not require
        # retained history objects: current files are checked by fsck, and cold
        # restore removes byte-identical active/archive/history duplicates.
        return state

    def iter_event_hashes(self: RepositoryHost) -> Iterable[tuple[str, str | None, str | None]]:
        for event in self.events():
            yield event.memory_id, event.before_hash, event.after_hash


def _read_containerized_segments(
    repository: RepositoryHost, catalog: dict[str, Any]
) -> tuple[list[Event], list[dict[str, Any]]]:
    container = catalog.get("container")
    if not isinstance(container, dict):
        raise IntegrityError("Invalid event summary container catalog")
    name = container.get("name")
    expected_hash = container.get("compressed_hash")
    if (
        not isinstance(name, str)
        or Path(name).name != name
        or not name.endswith(".tar.zst")
        or not isinstance(expected_hash, str)
    ):
        raise IntegrityError("Invalid event summary container reference")
    try:
        compressed = (repository.log_segments_dir / name).read_bytes()
    except OSError as exc:
        raise IntegrityError(f"Unable to read event summary container {name}: {exc}") from exc
    digest = hashlib.sha256(compressed).hexdigest()
    if digest != expected_hash:
        raise IntegrityError(f"Event segment hash mismatch: {name}")
    catalog_bytes = repository.log_catalog_path.read_bytes()
    cache_key = (hashlib.sha256(catalog_bytes).hexdigest(), (digest,))
    cached = getattr(repository, "_verified_log_segments_cache", None)
    if cached is not None and cached[0] == cache_key:
        return (
            [event.model_copy(deep=True) for event in cached[1]],
            [dict(record) for record in cached[2]],
        )

    try:
        import zstandard

        tar_bytes = zstandard.ZstdDecompressor().decompress(compressed)
        with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as archive:
            content: dict[str, bytes] = {}
            archive_member_names = archive.getnames()
            if len(archive_member_names) != len(set(archive_member_names)):
                raise IntegrityError("Duplicate event summary container member")
            for member_name in archive_member_names:
                member = archive.extractfile(member_name)
                if member is None:
                    raise IntegrityError(f"Missing summary container member: {member_name}")
                content[member_name] = member.read()
    except IntegrityError:
        raise
    except Exception as exc:
        raise IntegrityError(f"Unable to decode event summary container {name}: {exc}") from exc

    events: list[Event] = []
    records: list[dict[str, Any]] = []
    previous_event_hash = "0" * 64
    previous_manifest_hash = "0" * 64
    previous_summary_hash = "0" * 64
    expected_members: set[str] = set()
    for index, record in enumerate(catalog["segments"]):
        if not isinstance(record, dict):
            raise IntegrityError("Invalid logical segment record in summary container catalog")
        prefix = f"segments/{index:08d}"
        if record.get("member_prefix") != prefix:
            raise IntegrityError("Invalid logical segment member prefix")
        member_names = {
            "summaries": f"{prefix}/summaries.jsonl",
            "snapshot": f"{prefix}/snapshot.json",
            "manifest": f"{prefix}/manifest.json",
        }
        if not set(member_names.values()) <= set(content):
            raise IntegrityError(f"Incomplete logical segment in summary container: {index}")
        expected_members.update(member_names.values())
        try:
            summaries_bytes = content[member_names["summaries"]]
            snapshot_bytes = content[member_names["snapshot"]]
            manifest_bytes = content[member_names["manifest"]]
            manifest = json.loads(manifest_bytes)
            if manifest.get("schema_version") != 2:
                raise IntegrityError("Unsupported summary segment schema")
            if manifest.get("previous_hash") != previous_event_hash:
                raise IntegrityError("Broken event summary container event anchor")
            if manifest.get("previous_manifest_hash") != previous_manifest_hash:
                raise IntegrityError("Broken cross-snapshot manifest link")
            if manifest.get("previous_summary_hash") != previous_summary_hash:
                raise IntegrityError("Broken event summary chain anchor")
            parsed, previous_summary_hash = _parse_segment_summaries(
                summaries_bytes, previous_event_hash, previous_summary_hash
            )
            if previous_summary_hash != manifest.get("last_summary_hash"):
                raise IntegrityError("Event summary segment bounds mismatch")
            if hashlib.sha256(summaries_bytes).hexdigest() != manifest.get("summaries_hash"):
                raise IntegrityError("Event summary payload hash mismatch")
            if not parsed or len(parsed) != manifest.get("event_count"):
                raise IntegrityError("Invalid event summary segment count")
            if parsed[0].event_hash != manifest.get("first_hash") or parsed[
                -1
            ].event_hash != manifest.get("last_hash"):
                raise IntegrityError("Invalid event summary segment bounds")
            if record.get("event_count") != len(parsed):
                raise IntegrityError("Event catalog segment count mismatch")
            if (
                record.get("first_hash") != parsed[0].event_hash
                or record.get("last_hash") != parsed[-1].event_hash
            ):
                raise IntegrityError("Event catalog segment bounds mismatch")
            if hashlib.sha256(snapshot_bytes).hexdigest() != manifest.get("snapshot_hash"):
                raise IntegrityError("Event snapshot hash mismatch")
            snapshot = json.loads(snapshot_bytes)
            if snapshot != _project_events(events + parsed):
                raise IntegrityError("Event snapshot mismatch")
            manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
            if record.get("manifest_hash") != manifest_hash:
                raise IntegrityError("Event segment manifest hash mismatch")
            if record.get("summary_hash") != previous_summary_hash:
                raise IntegrityError("Event catalog summary hash mismatch")
            previous_event_hash = parsed[-1].event_hash
            previous_manifest_hash = manifest_hash
            events.extend(parsed)
            records.append(
                {
                    **record,
                    "name": name,
                    "compressed_hash": digest,
                    "schema_version": 2,
                    "manifest_hash": manifest_hash,
                    "summary_hash": previous_summary_hash,
                    "containerized": True,
                }
            )
        except IntegrityError:
            raise
        except Exception as exc:
            raise IntegrityError(
                f"Unable to verify logical summary segment {index}: {exc}"
            ) from exc
    if expected_members != set(content):
        raise IntegrityError("Unexpected event summary container members")
    cast(Any, repository)._verified_log_segments_cache = (
        cache_key,
        [event.model_copy(deep=True) for event in events],
        [dict(record) for record in records],
    )
    return events, records


def _canonical_json(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


def _read_event_before_offset(path: Path, offset: int) -> Event:
    """Read and digest-check the final canonical JSONL event before an offset."""
    if offset <= 0:
        raise IntegrityError("No active event exists before append offset")
    with path.open("rb") as stream:
        stream.seek(offset - 1)
        if stream.read(1) != b"\n":
            raise IntegrityError("Event append offset is not at a line boundary")
        position = offset - 2
        while position >= 0:
            stream.seek(position)
            if stream.read(1) == b"\n":
                position += 1
                break
            position -= 1
        start = max(position, 0)
        stream.seek(start)
        raw = stream.read(offset - start - 1)
    try:
        event = Event.model_validate_json(raw)
        from ..log import event_digest

        if (
            event.model_dump_json().encode("utf-8") != raw
            or event_digest(event) != event.event_hash
        ):
            raise IntegrityError("Event before append offset failed digest verification")
        return event
    except IntegrityError:
        raise
    except Exception as exc:
        raise IntegrityError(f"Invalid event before append offset: {exc}") from exc


def _project_events(events: list[Event]) -> dict[str, str | None]:
    state: dict[str, str | None] = {}
    for event in events:
        if event.kind in {
            EventKind.CONFIG_CHANGE,
            EventKind.AGENT_PROPOSAL,
            EventKind.HUMAN_DECISION,
            EventKind.SIMULATED_DECISION,
            EventKind.CAPTURE_WAKE,
            EventKind.CAPTURE_SLEEP,
            EventKind.CAPTURE_STATE_CHANGE,
            EventKind.CAPTURE_RECORD,
            EventKind.FORGET,
            EventKind.MEMORY_ACCESS,
            EventKind.CONSOLIDATION_PROPOSAL,
            EventKind.BUDGET_UNSATISFIABLE,
            EventKind.PROPOSAL_ERASE,
            EventKind.KNOWLEDGE_CARD_CREATE,
            EventKind.KNOWLEDGE_SOURCE_MISSING,
            EventKind.KNOWLEDGE_SOURCE_REPOINT,
        }:
            continue
        if event.kind in {EventKind.DELETE, EventKind.EXTERNAL_DELETE} or (
            event.kind == EventKind.EXTERNAL_EDIT and event.details.get("quarantined") is True
        ):
            state[event.memory_id] = None
        elif event.after_hash is not None:
            state[event.memory_id] = event.after_hash
        elif event.memory_id not in state:
            state[event.memory_id] = None
    return state


def _parse_segment_events(raw: bytes, expected_prev: str) -> list[Event]:
    # Parse from bytes through a temporary in-memory-compatible path is avoided;
    # duplicate the strict canonical and digest checks here for archive members.
    result: list[Event] = []
    if raw and not raw.endswith(b"\n"):
        raise IntegrityError("Archived event JSONL must end with LF")
    for number, line in enumerate(raw[:-1].split(b"\n") if raw else [], 1):
        try:
            text = line.decode("utf-8")
            event = Event.model_validate_json(text)
        except Exception as exc:
            raise IntegrityError(f"Invalid archived event {number}: {exc}") from exc
        if text != event.model_dump_json() or event.prev_event_hash != expected_prev:
            raise IntegrityError(f"Invalid archived event chain at event {number}")
        from . import event_digest

        if event_digest(event) != event.event_hash:
            raise IntegrityError(f"Archived event hash mismatch at event {number}")
        expected_prev = event.event_hash
        result.append(event)
    return result


_SUMMARY_DETAIL_KEYS = frozenset(
    {
        "proposal_id",
        "candidate_id",
        "decision",
        "source",
        "source_hash",
        "session_id",
        "state",
        "intensity",
        "elapsed_seconds",
        "target_hash",
        "target_ids",
        "target_hashes",
        "approved_by",
        "quarantined",
        "archive_ref",
        "metadata_preserved",
        "kind",
        "source_refs",
        "from_source_id",
        "to_source_id",
        "policy",
        "reason_code",
        "memory_id",
        "operation",
        "source_id",
        "agent_id",
        "signature",
        "target_snapshot_hash",
        "proposal_hash",
        "state_hash",
        "target_id",
        "source_ids",
        "memory_ids",
        "entity_hash",
        "input_hash",
        "dominant_current_component",
        "dominant_component_bytes",
        "physical_overage_bytes",
        "overage_explanation",
        "rebuildable_index_bytes",
        "remaining_anchor_count",
        "remaining_non_anchor_count",
        "budget_bytes",
        "physical_bytes",
        "max_count",
        "anchor_ids",
        "scope",
        "floor_explanation",
        "physical_breakdown",
    }
)


def _event_summary(event: Event) -> dict[str, Any]:
    details = {
        key: value
        for key, value in event.details.items()
        if key in _SUMMARY_DETAIL_KEYS
        and isinstance(value, (str, int, float, bool, list, dict, type(None)))
    }
    return {
        "event_id": event.event_id,
        "kind": event.kind.value,
        "memory_id": event.memory_id,
        "timestamp": event.timestamp.isoformat(),
        "actor": event.actor,
        "before_hash": event.before_hash,
        "after_hash": event.after_hash,
        "version_ref": event.version_ref,
        "details": details,
        "prev_event_hash": event.prev_event_hash,
        "event_hash": event.event_hash,
    }


def _summarize_events(events: list[Event], previous_hash: str) -> tuple[list[str], str]:
    rows: list[str] = []
    previous = previous_hash
    for event in events:
        summary = _event_summary(event)
        payload = {"previous_summary_hash": previous, "event": summary}
        digest = hashlib.sha256(_canonical_json(payload)).hexdigest()
        rows.append(
            json.dumps(
                {**payload, "summary_hash": digest},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        previous = digest
    return rows, previous


def _parse_segment_summaries(
    raw: bytes, expected_event_prev: str, expected_summary_prev: str
) -> tuple[list[Event], str]:
    result: list[Event] = []
    if raw and not raw.endswith(b"\n"):
        raise IntegrityError("Archived event summaries must end with LF")
    event_previous = expected_event_prev
    summary_previous = expected_summary_prev
    for number, line in enumerate(raw[:-1].split(b"\n") if raw else [], 1):
        try:
            row = json.loads(line)
            payload = {"previous_summary_hash": row["previous_summary_hash"], "event": row["event"]}
            summary_hash = hashlib.sha256(_canonical_json(payload)).hexdigest()
            if (
                row["previous_summary_hash"] != summary_previous
                or row["summary_hash"] != summary_hash
            ):
                raise IntegrityError(f"Event summary hash mismatch at summary {number}")
            event_data = row["event"]
            if event_data["prev_event_hash"] != event_previous:
                raise IntegrityError(f"Broken original event hash chain at summary {number}")
            event = Event.model_validate(event_data)
            object.__setattr__(event, "_compacted", True)
            result.append(event)
            event_previous = event.event_hash
            summary_previous = summary_hash
        except IntegrityError:
            raise
        except Exception as exc:
            raise IntegrityError(f"Invalid event summary {number}: {exc}") from exc
    return result, summary_previous


def _deterministic_tar(items: list[tuple[str, bytes]]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:") as archive:
        for name, data in items:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(data))
    return output.getvalue()
