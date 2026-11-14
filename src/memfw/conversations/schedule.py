"""launchd plist for the conversation worker: printed, never installed.

``TEMPLATE`` is kept byte-identical to
``scripts/launchd/com.memfw.conversations-worker.plist.template`` (a test
checks). :func:`render` fills its placeholders; memfw never writes to
``~/Library/LaunchAgents`` or loads anything into launchd.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from xml.sax.saxutils import escape

from .store import ConversationError

LABEL = "com.memfw.conversations-worker"
DEFAULT_INTERVAL = 900
TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<!--
  Template for a per-user launchd agent that runs the memfw conversation worker.
  memfw never installs this. Render it with the print flag of the memfw
  conversations schedule command, review it, then install it yourself
  (see docs/conversations-share.md).
-->
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.memfw.conversations-worker</string>
  <key>ProgramArguments</key>
  <array>
    <string>__MEMFW_BIN__</string>
    <string>conversations</string>
    <string>worker</string>
    <string>--root</string>
    <string>__VAULT_ROOT__</string>
  </array>
  <key>StartInterval</key>
  <integer>__INTERVAL__</integer>
  <key>RunAtLoad</key>
  <false/>
  <key>ProcessType</key>
  <string>Background</string>
  <key>LowPriorityIO</key>
  <true/>
  <key>Nice</key>
  <integer>10</integer>
  <key>StandardOutPath</key>
  <string>__LOG_DIR__/conversations-worker.log</string>
  <key>StandardErrorPath</key>
  <string>__LOG_DIR__/conversations-worker.log</string>
</dict>
</plist>
"""


def render(
    *,
    vault: Path,
    memfw_bin: str | None = None,
    interval: int = DEFAULT_INTERVAL,
    log_dir: Path | None = None,
) -> str:
    if not 60 <= int(interval) <= 86_400:
        raise ConversationError("interval must be between 60 and 86400 seconds")
    binary = memfw_bin or shutil.which("memfw") or "memfw"
    logs = log_dir if log_dir is not None else Path.home() / "Library" / "Logs" / "memfw"
    values = {
        "__MEMFW_BIN__": binary,
        "__VAULT_ROOT__": str(Path(vault).expanduser().resolve()),
        "__LOG_DIR__": str(logs),
        "__INTERVAL__": str(int(interval)),
    }
    out = TEMPLATE
    for placeholder, value in values.items():
        out = out.replace(placeholder, escape(value))
    return out
