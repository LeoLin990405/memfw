"""``memfw-ocr-proxy``: make a proxy import file for one attachment (offline, explicit).

    memfw-ocr-proxy run --settings SETTINGS.json --attachment-id att-... --source FILE
                        --sha256 HEX --media-type application/pdf --privacy LEVEL --out OUT.json
    memfw-ocr-proxy check --settings SETTINGS.json

``SETTINGS.json`` holds the adapter settings (the ``adapters.ocr`` mapping).
``run`` hands the engine only a private copy of FILE, and only when the
settings' ``clearance`` admits LEVEL; it verifies FILE's sha256 before and
after, writes OUT (``0600``, never replaced) and prints a one-line summary
(no text). ``check`` prints the backend's availability. Errors print
``{"error": "<code>"}`` and exit 1.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from .backend import OcrError
from .deriver import OcrDeriver, Settings
from .proxy import generate_proxy, write_proxy

MAX_SETTINGS_BYTES = 1 << 20


def _settings(path: Path) -> Settings:
    if path.stat().st_size > MAX_SETTINGS_BYTES:
        raise OcrError("settings_too_large")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise OcrError("bad_settings")
    return Settings.from_mapping(raw)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="memfw-ocr-proxy")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="make one proxy import file")
    run.add_argument("--settings", required=True, type=Path)
    run.add_argument("--attachment-id", required=True)
    run.add_argument("--source", required=True, type=Path)
    run.add_argument("--sha256", required=True)
    run.add_argument("--media-type", required=True)
    run.add_argument("--privacy", required=True)
    run.add_argument("--out", required=True, type=Path)
    check = sub.add_parser("check", help="report whether the configured backend is usable")
    check.add_argument("--settings", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        settings = _settings(args.settings)
        deriver = OcrDeriver(settings)
        if args.command == "check":
            availability = deriver.backend.available() if deriver.backend else None
            reason = deriver.reason if availability is None else availability.reason
            print(
                json.dumps({"backend": settings.backend, "available": not reason, "reason": reason})
            )
            return 0 if not reason else 1
        if deriver.backend is None:
            raise OcrError(deriver.reason or "no_backend")
        candidate = generate_proxy(
            args.source,
            backend=deriver.backend,
            attachment_id=args.attachment_id,
            sha256=args.sha256.strip().lower(),
            media_type=args.media_type,
            privacy=args.privacy,
            clearance=frozenset(settings.clearance),
            budget=settings.budget,
        )
        write_proxy(args.out, candidate)
        print(
            json.dumps(
                {
                    "attachment_id": candidate["attachment_id"],
                    "coverage": candidate["coverage"]["detail"],
                    "segments": len(candidate["segments"]),
                    "out": str(args.out),
                }
            )
        )
        return 0
    except OcrError as exc:
        print(json.dumps({"error": exc.code}))
        return 1
    except (OSError, ValueError):
        print(json.dumps({"error": "unreadable"}))
        return 1


__all__ = ["main"]
