"""hapbeat-helper — local daemon bridging Studio (web) to Hapbeat devices."""

from __future__ import annotations


def _git_dev_version() -> str | None:
    """Editable / source checkout → derive a LIVE version from git state.

    `0.1.3` on a `v0.1.3` tag, otherwise `0.1.3d<N>` where N = commits since
    the most recent `v*` tag. Computed at import time (not from a generated
    file) so the running daemon ALWAYS reflects the checked-out commit: after
    you update the source and restart `hapbeat-helper`, the `d<N>` advances,
    confirming the new code is actually running. (A generated `_version.py`
    used to do this but went stale — it's gitignored and `pipx install -e`
    never regenerates it, so it could report an old `d<N>` forever.)

    Returns None outside a git checkout (a packaged PyPI wheel) → the caller
    falls back to the packaged release version.
    """
    import re
    import subprocess
    from pathlib import Path

    # src/hapbeat_helper/__init__.py → repo root (holds .git + pyproject.toml).
    root = Path(__file__).resolve().parent.parent.parent
    if not (root / ".git").exists():
        return None

    def git(*args: str) -> str | None:
        try:
            return (
                subprocess.check_output(
                    ["git", *args], cwd=root, stderr=subprocess.DEVNULL
                )
                .decode("utf-8", "replace")
                .strip()
            )
        except Exception:  # noqa: BLE001 — git missing / not a repo / any error
            return None

    try:
        m = re.search(
            r'^\s*version\s*=\s*"([^"]+)"',
            (root / "pyproject.toml").read_text(encoding="utf-8"),
            re.MULTILINE,
        )
    except Exception:  # noqa: BLE001
        return None
    if not m:
        return None
    base = m.group(1)

    # On a release tag → clean version (no d suffix).
    exact = git("describe", "--tags", "--exact-match", "--match", "v*")
    if exact:
        return exact.lstrip("v")

    last_tag = git("describe", "--tags", "--abbrev=0", "--match", "v*")
    count = (
        git("rev-list", "--count", f"{last_tag}..HEAD")
        if last_tag
        else git("rev-list", "--count", "HEAD")
    )
    try:
        n = int(count or "0")
    except ValueError:
        n = 0
    return f"{base}d{n}" if n else base


def _packaged_version() -> str:
    """Installed wheel (pipx / pip): read setuptools-baked PKG-INFO metadata
    = pyproject.toml [project] version. No manual sync needed."""
    try:
        from importlib.metadata import version as _pkg_version

        return _pkg_version("hapbeat-helper")
    except Exception:  # noqa: BLE001 — broken install / missing metadata
        return "0.0.0"


try:
    __version__ = _git_dev_version() or _packaged_version()
except Exception:  # noqa: BLE001 — never let version lookup break import
    __version__ = _packaged_version()
