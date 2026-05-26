"""hapbeat-helper — local daemon bridging Studio (web) to Hapbeat devices."""

try:
    # Local dev / editable install: scripts/gen_version.py が git 状態に
    # 応じて 0.1.3.dev1d4 のような細かい版を吐く (_version.py は gitignore)。
    from hapbeat_helper._version import __version__
except ImportError:
    # Installed wheel (pipx / pip): setuptools が PKG-INFO に焼き込んだ
    # メタデータから読む = pyproject.toml の [project] version がそのまま
    # runtime に反映される。手動 sync が不要になる。
    try:
        from importlib.metadata import version as _pkg_version
        __version__ = _pkg_version("hapbeat-helper")
        del _pkg_version
    except Exception:
        # 上記いずれも取れない（壊れた install / metadata 欠落）— 最終手段
        __version__ = "0.0.0"
