"""The Claude Code binary the Agent SDK bundles, and whether it is new enough for the configured
models (config.MODEL_MIN_CLI). A model id can need a newer binary than the pinned SDK ships
(2026-10-03: Opus 5.5 needed 2.1.280, the pin bundled 2.1.179, every Opus call failed with a 400).
Pure Python plus one `claude --version` subprocess; never raises."""
from __future__ import annotations

import os
import re
import subprocess

from scout import config


def bundled_cli_path() -> str | None:
    try:
        import claude_agent_sdk
        root = os.path.dirname(os.path.abspath(claude_agent_sdk.__file__))
    except Exception:
        return None
    for name in ("claude", "claude.exe"):
        p = os.path.join(root, "_bundled", name)
        if os.path.exists(p):
            return p
    return None


def cli_version(path: str | None = None) -> str | None:
    """'2.1.286' from `claude --version`, or None when it cannot be read."""
    path = path or bundled_cli_path()
    if not path:
        return None
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=30)
    except Exception:
        return None
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", (out.stdout or "") + (out.stderr or ""))
    return m.group(0) if m else None


def _tuple(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3]) or (0,)


def too_old(version: str | None = None, minimum: str | None = None) -> str | None:
    """The one-line reason the binary cannot run the configured models, or None when it can (or
    when no binary is present, which the SDK reports on its own)."""
    minimum = minimum or config.MODEL_MIN_CLI
    version = version if version is not None else cli_version()
    if not version:
        return None
    if _tuple(version) < _tuple(minimum):
        return (f"Claude Code {version} (bundled by the pinned claude-agent-sdk) does not support the configured "
                f"models; {minimum} or newer is required. Bump claude-agent-sdk in requirements.txt and "
                f"requirements-engine.txt and re-pip this environment.")
    return None
