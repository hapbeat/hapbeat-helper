"""OS-level service management for hapbeat-helper.

Dispatches to the appropriate OS implementation:
- macOS   → service.macos   (launchd plist)
- Windows → service.windows (Task Scheduler via schtasks)
"""

from __future__ import annotations

import sys


def get_service_manager():
    """Return the platform-specific service manager module."""
    if sys.platform == "darwin":
        from hapbeat_helper.service import macos as _m
        return _m
    if sys.platform == "win32":
        from hapbeat_helper.service import windows as _m
        return _m
    raise NotImplementedError(f"Unsupported platform: {sys.platform}")


__all__ = ["get_service_manager"]
