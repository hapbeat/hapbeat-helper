"""Material ledger — where each downloaded sound file came from.

Free sound-effect sites are downloaded from in bulk and the origin of a file is
soon forgotten. This module records it at download time (site, source page,
license) and ties everything to the **SHA-256 of the file content**, so renames
and moves do not lose it. Studio registers the files it derives from these
(``hapbeat-derived@1``), and a credits list can be generated for a Kit.

Layout of the materials dir (default ``~/HapbeatMaterials``, config key
``materials_dir``)::

    store/<site>/<YYYY-MM>/<name>   read-only copy of each original
    ledger.jsonl                    append-only records (one JSON per line)
    sites.json                      domain -> license rules (edited by people)
    MATERIALS.md                    derived overview, regenerated on change
    state.json                      internal (last ingest time)

Licenses are never baked into the ledger: they are resolved from the current
``sites.json`` (and per-file overrides) every time, so fixing a rule later
applies to every material from that site.

Standard library only.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import stat
import sys
import threading
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Optional
from urllib.parse import urlparse

from hapbeat_helper import update_check

logger = logging.getLogger(__name__)

AUDIO_EXTS = frozenset({".wav", ".mp3", ".ogg", ".flac", ".m4a", ".aif", ".aiff", ".opus"})
ARCHIVE_EXTS = frozenset({".zip"})

FORMAT_MATERIAL = "hapbeat-material@1"
FORMAT_DERIVED = "hapbeat-derived@1"
FORMAT_OVERRIDE = "hapbeat-license-override@1"
FORMAT_SITES = "hapbeat-sites@1"

UNKNOWN_SITE = "_unknown"
DEFAULT_SINCE_DAYS = 30
# derived -> parents walk limit (a cycle is also cut by the visited set).
RESOLVE_MAX_DEPTH = 32
# A zip member bigger than this is not a sound effect; skip rather than load it.
ZIP_MEMBER_MAX_BYTES = 512 * 1024 * 1024

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

UNKNOWN_LICENSE: dict[str, Any] = {
    "id": "unknown",
    "name": "不明",
    "url": None,
    "creditText": None,
    "attributionRequired": False,
    "redistributionNote": None,
    "verified": False,
}

# Seed written when sites.json does not exist. ``verified`` is true only where
# the terms were checked against the site itself (2026-10-01).
DEFAULT_SITES: dict[str, Any] = {
    "format": FORMAT_SITES,
    "sites": {
        "maou.audio": {
            "aliases": ["maoudamashii.jokersounds.com"],
            "license": {
                "id": "CC-BY-4.0",
                "name": "魔王魂 利用規約 (CC BY 4.0)",
                "url": "https://maou.audio/rule/",
                "creditText": "魔王魂",
                "attributionRequired": True,
                "redistributionNote": "素材単品のままの再配布は不可。加工版の公開・配布は可",
                "verified": True,
            },
        },
        "otologic.jp": {
            "license": {
                "id": "CC-BY-4.0",
                "name": "OtoLogic (CC BY 4.0)",
                "url": "https://otologic.jp/free/license.html",
                "creditText": "OtoLogic",
                "attributionRequired": True,
                "redistributionNote": None,
                "verified": False,
            },
        },
        "kenney.nl": {
            "license": {
                "id": "CC0-1.0",
                "name": "CC0 1.0",
                "url": "https://creativecommons.org/publicdomain/zero/1.0/",
                "creditText": "Kenney (kenney.nl)",
                "attributionRequired": False,
                "verified": False,
            },
        },
        # Each asset carries its own license -> needs review per file.
        "opengameart.org": {"perAsset": True},
        "freesound.org": {"perAsset": True},
    },
}

# Short labels for the credits headings.
_LICENSE_LABELS = {
    "CC-BY-4.0": "CC BY 4.0",
    "CC-BY-3.0": "CC BY 3.0",
    "CC-BY-SA-4.0": "CC BY-SA 4.0",
    "CC0-1.0": "CC0 1.0",
}


# ──────────────────────────────────────────────────────────────
# config


def load_config() -> dict[str, Any]:
    """Top-level keys of ``<config dir>/config.toml`` ({} when absent/broken)."""
    path = update_check.config_dir() / "config.toml"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        import tomllib  # Python 3.11+
    except ModuleNotFoundError:
        return _parse_flat_toml(text)
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        logger.warning("config.toml is not valid TOML, ignoring it: %s", exc)
        return {}


def _parse_flat_toml(text: str) -> dict[str, Any]:
    """Python 3.10 fallback: top-level ``key = "string" | true | false`` only."""
    out: dict[str, Any] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("["):
            break  # a table starts; top-level keys are over
        m = re.match(r'^([A-Za-z0-9_-]+)\s*=\s*(.+?)\s*$', line)
        if not m:
            continue
        key, value = m.group(1), m.group(2)
        if value in ("true", "false"):
            out[key] = value == "true"
        elif len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            out[key] = value[1:-1]
    return out


def default_materials_dir(config: Optional[dict[str, Any]] = None) -> Path:
    cfg = load_config() if config is None else config
    configured = cfg.get("materials_dir")
    if isinstance(configured, str) and configured.strip():
        return Path(configured).expanduser()
    return Path.home() / "HapbeatMaterials"


def default_downloads_dir() -> Path:
    return Path.home() / "Downloads"


def watch_downloads_enabled(config: Optional[dict[str, Any]] = None) -> bool:
    cfg = load_config() if config is None else config
    return cfg.get("materials_watch_downloads") is True


# ──────────────────────────────────────────────────────────────
# small helpers


def is_sha256(value: Any) -> bool:
    return isinstance(value, str) and SHA256_RE.fullmatch(value) is not None


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _ts_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def _read_zone_text(path: Path) -> Optional[str]:
    """Raw ``Zone.Identifier`` stream of *path* (Windows NTFS only)."""
    if sys.platform != "win32":
        return None
    try:
        with open(str(path) + ":Zone.Identifier", "rb") as f:
            data = f.read()
    except OSError:
        return None
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    return data.decode("utf-8", errors="replace")


def read_zone_identifier(path: Path) -> dict[str, Optional[str]]:
    """``{"hostUrl", "referrerUrl"}`` from the download mark (None when absent)."""
    out: dict[str, Optional[str]] = {"hostUrl": None, "referrerUrl": None}
    text = _read_zone_text(path)
    if not text:
        return out
    for line in text.splitlines():
        key, sep, value = line.strip().partition("=")
        if not sep:
            continue
        value = value.strip()
        if not value.lower().startswith(("http://", "https://")):
            continue  # e.g. "about:internet"
        if key == "HostUrl":
            out["hostUrl"] = value
        elif key == "ReferrerUrl":
            out["referrerUrl"] = value
    return out


def _host_of(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    try:
        host = urlparse(url).hostname
    except ValueError:
        return None
    if not host:
        return None
    host = host.lower()
    return host[4:] if host.startswith("www.") else host


def canonical_site(host: Optional[str], sites: dict[str, Any]) -> str:
    """sites.json key for *host* (via aliases / subdomains), else the host."""
    if not host:
        return UNKNOWN_SITE
    for key, rule in sites.items():
        names = [key] + list((rule or {}).get("aliases") or [])
        for name in names:
            name = str(name).lower()
            if host == name or host.endswith("." + name):
                return key
    return host


def _normalize_license(lic: dict[str, Any]) -> dict[str, Any]:
    out = dict(UNKNOWN_LICENSE)
    out.update({k: v for k, v in lic.items() if k in UNKNOWN_LICENSE})
    return out


def license_label(lic: dict[str, Any]) -> str:
    lid = lic.get("id") or "unknown"
    return _LICENSE_LABELS.get(lid) or lic.get("name") or lid


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    n = 2
    while True:
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def _make_read_only(path: Path) -> None:
    try:
        os.chmod(path, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
    except OSError as exc:
        logger.warning("could not mark %s read-only: %s", path, exc)


# ──────────────────────────────────────────────────────────────
# ingest result


@dataclass
class IngestResult:
    new: list[dict[str, Any]] = field(default_factory=list)
    # {"path", "member", "sha256", "storePath"} — storePath of the earlier copy
    duplicates: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)
    needs_review: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class _Candidate:
    """One original to register: a plain file or a zip member."""

    source: Path
    sha256: str
    size: int
    original_name: str
    mtime: float
    zone: dict[str, Optional[str]]
    data: Optional[bytes] = None  # zip member bytes; None = copy *source*
    archive: Optional[dict[str, Any]] = None


# ──────────────────────────────────────────────────────────────
# store


class MaterialStore:
    """Data layer over one materials dir.

    All ledger access in this process goes through ``self._lock``. Appends are
    single ``write`` calls of one line on a file opened in append mode, so a
    CLI and the daemon appending at the same time do not interleave records.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._lock = threading.RLock()

    # paths
    @property
    def ledger_path(self) -> Path:
        return self.root / "ledger.jsonl"

    @property
    def sites_path(self) -> Path:
        return self.root / "sites.json"

    @property
    def state_path(self) -> Path:
        return self.root / "state.json"

    @property
    def materials_md_path(self) -> Path:
        return self.root / "MATERIALS.md"

    # ── ledger ───────────────────────────────────────────────

    def read_ledger(self) -> list[dict[str, Any]]:
        """All records; a broken line is skipped with a warning."""
        with self._lock:
            try:
                f = open(self.ledger_path, "r", encoding="utf-8")
            except FileNotFoundError:
                return []
            records: list[dict[str, Any]] = []
            with f:
                for lineno, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        logger.warning("ledger.jsonl line %d is broken, skipped", lineno)
                        continue
                    if not isinstance(rec, dict):
                        logger.warning("ledger.jsonl line %d is not an object, skipped", lineno)
                        continue
                    records.append(rec)
            return records

    def _append(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            with open(self.ledger_path, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()

    # ── sites.json / state.json ──────────────────────────────

    def load_sites(self) -> dict[str, Any]:
        """``sites`` mapping. Writes the seed when sites.json does not exist."""
        with self._lock:
            if not self.sites_path.exists():
                self._write_json(self.sites_path, DEFAULT_SITES)
            try:
                doc = json.loads(self.sites_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("sites.json unreadable, using no site rules: %s", exc)
                return {}
            sites = doc.get("sites") if isinstance(doc, dict) else None
            return sites if isinstance(sites, dict) else {}

    def save_sites(self, sites: dict[str, Any]) -> None:
        with self._lock:
            self._write_json(self.sites_path, {"format": FORMAT_SITES, "sites": sites})

    def load_state(self) -> dict[str, Any]:
        try:
            doc = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return doc if isinstance(doc, dict) else {}

    def save_state(self, state: dict[str, Any]) -> None:
        with self._lock:
            self._write_json(self.state_path, state)

    def _write_json(self, path: Path, doc: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)

    # ── license / resolve ────────────────────────────────────

    def _index(self) -> tuple[dict[str, dict], dict[str, list[dict]], dict[str, dict]]:
        materials: dict[str, dict] = {}
        derived: dict[str, list[dict]] = {}
        overrides: dict[str, dict] = {}
        for rec in self.read_ledger():
            sha = rec.get("sha256")
            if not is_sha256(sha):
                continue
            fmt = rec.get("format")
            if fmt == FORMAT_MATERIAL:
                materials.setdefault(sha, rec)
            elif fmt == FORMAT_DERIVED:
                derived.setdefault(sha, []).append(rec)
            elif fmt == FORMAT_OVERRIDE and isinstance(rec.get("license"), dict):
                overrides[sha] = rec["license"]  # latest wins
        return materials, derived, overrides

    @staticmethod
    def _resolve_license(
        sha: str, site: str, sites: dict[str, Any], overrides: dict[str, dict],
    ) -> tuple[dict[str, Any], bool]:
        """Order: per-file override -> sites.json (perAsset = unknown + review) -> unknown."""
        if sha in overrides:
            lic = _normalize_license(overrides[sha])
            return lic, lic["id"] == "unknown"
        rule = sites.get(site)
        if isinstance(rule, dict):
            if rule.get("perAsset"):
                return dict(UNKNOWN_LICENSE), True
            if isinstance(rule.get("license"), dict):
                lic = _normalize_license(rule["license"])
                return lic, lic["id"] == "unknown"
        return dict(UNKNOWN_LICENSE), True

    def _original_entry(
        self, rec: dict, sites: dict[str, Any], overrides: dict[str, dict],
    ) -> dict[str, Any]:
        sha = rec["sha256"]
        # Re-map through the current aliases so a later rule edit applies.
        site = rec.get("site") or UNKNOWN_SITE
        if site != UNKNOWN_SITE:
            site = canonical_site(site, sites)
        lic, needs_review = self._resolve_license(sha, site, sites, overrides)
        return {
            "sha256": sha,
            "site": site,
            "referrerUrl": rec.get("referrerUrl"),
            "hostUrl": rec.get("hostUrl"),
            "originalName": rec.get("originalName"),
            "storePath": rec.get("storePath"),
            "license": lic,
            "needsReview": needs_review,
        }

    def resolve_many(self, shas: Iterable[str]) -> dict[str, dict[str, Any]]:
        materials, derived, overrides = self._index()
        sites = self.load_sites()
        out: dict[str, dict[str, Any]] = {}
        for sha in shas:
            out[sha] = self._resolve_one(sha, materials, derived, overrides, sites)
        return out

    def resolve(self, sha: str) -> dict[str, Any]:
        return self.resolve_many([sha])[sha]

    def _resolve_one(
        self, sha: str, materials: dict, derived: dict, overrides: dict, sites: dict,
    ) -> dict[str, Any]:
        if sha in materials:
            return {
                "kind": "material",
                "originals": [self._original_entry(materials[sha], sites, overrides)],
            }
        if sha not in derived:
            return {"kind": "unknown", "originals": []}
        originals: list[dict[str, Any]] = []
        found: set[str] = set()
        visited: set[str] = set()

        def walk(node: str, depth: int) -> None:
            if node in visited or depth > RESOLVE_MAX_DEPTH:
                return
            visited.add(node)
            if node in materials:
                if node not in found:
                    found.add(node)
                    originals.append(self._original_entry(materials[node], sites, overrides))
                return
            for rec in derived.get(node, []):
                for parent in rec.get("parents") or []:
                    if is_sha256(parent):
                        walk(parent, depth + 1)

        walk(sha, 0)
        return {"kind": "derived", "originals": originals}

    def display_name(self, sha: str) -> str:
        """Best label for a bare hash: derived name, original name, short hash."""
        materials, derived, _ = self._index()
        for rec in derived.get(sha, []):
            if rec.get("name"):
                return str(rec["name"])
        if sha in materials and materials[sha].get("originalName"):
            return str(materials[sha]["originalName"])
        return sha[:12]

    # ── writes ───────────────────────────────────────────────

    def register_derived(
        self,
        sha256: str,
        parents: list[str],
        tool: str,
        name: Optional[str] = None,
        note: Optional[str] = None,
    ) -> bool:
        """Append a derived record. False when the same sha+parents already exists."""
        with self._lock:
            _, derived, _ = self._index()
            wanted = sorted(set(parents))
            for rec in derived.get(sha256, []):
                if sorted(set(rec.get("parents") or [])) == wanted:
                    return False
            record: dict[str, Any] = {
                "format": FORMAT_DERIVED,
                "sha256": sha256,
                "parents": list(parents),
                "tool": tool,
                "name": name,
                "note": note,
                "createdAt": _now_iso(),
            }
            self._append(record)
            return True

    def set_site_license(self, domain: str, license: dict[str, Any]) -> None:
        with self._lock:
            sites = self.load_sites()
            rule = dict(sites.get(domain) or {})
            rule.pop("perAsset", None)
            rule["license"] = _normalize_license(license)
            sites[domain] = rule
            self.save_sites(sites)
            self.write_materials_md()

    def set_override(self, sha256: str, license: dict[str, Any]) -> None:
        with self._lock:
            self._append({
                "format": FORMAT_OVERRIDE,
                "sha256": sha256,
                "license": _normalize_license(license),
                "setAt": _now_iso(),
            })
            self.write_materials_md()

    # ── ingest ───────────────────────────────────────────────

    def scan_candidates(self, directory: Path, since_ts: Optional[float]) -> list[Path]:
        """Audio/zip files directly in *directory* modified at/after *since_ts*."""
        out: list[Path] = []
        try:
            entries = sorted(Path(directory).iterdir())
        except OSError:
            return out
        root = self.root.resolve()
        for p in entries:
            ext = p.suffix.lower()
            if ext not in AUDIO_EXTS and ext not in ARCHIVE_EXTS:
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            if not p.is_file():
                continue
            if since_ts is not None and st.st_mtime < since_ts:
                continue
            try:
                if p.resolve().is_relative_to(root):
                    continue  # never re-ingest our own store
            except (OSError, ValueError):
                pass
            out.append(p)
        return out

    def _candidates_for(self, path: Path) -> list[_Candidate]:
        st = path.stat()
        zone = read_zone_identifier(path)
        if path.suffix.lower() in ARCHIVE_EXTS:
            archive_sha = sha256_file(path)
            out: list[_Candidate] = []
            with zipfile.ZipFile(path) as zf:
                for info in zf.infolist():
                    if info.is_dir() or info.filename.startswith("__MACOSX/"):
                        continue
                    member_name = info.filename.rsplit("/", 1)[-1]
                    if Path(member_name).suffix.lower() not in AUDIO_EXTS:
                        continue
                    if info.file_size > ZIP_MEMBER_MAX_BYTES:
                        logger.warning("%s: member %s too large, skipped", path, info.filename)
                        continue
                    data = zf.read(info)
                    out.append(_Candidate(
                        source=path,
                        sha256=hashlib.sha256(data).hexdigest(),
                        size=len(data),
                        original_name=member_name,
                        mtime=st.st_mtime,
                        zone=zone,
                        data=data,
                        archive={
                            "sha256": archive_sha,
                            "originalName": path.name,
                            "member": info.filename,
                        },
                    ))
            return out
        return [_Candidate(
            source=path,
            sha256=sha256_file(path),
            size=st.st_size,
            original_name=path.name,
            mtime=st.st_mtime,
            zone=zone,
        )]

    def ingest_paths(self, paths: Iterable[Path], dry_run: bool = False) -> IngestResult:
        """Register *paths* (audio files or zips) as originals. Copies, never moves."""
        result = IngestResult()
        with self._lock:
            materials, _, overrides = self._index()
            sites = self.load_sites()
            known: dict[str, Optional[str]] = {
                sha: rec.get("storePath") for sha, rec in materials.items()
            }
            month = datetime.now().strftime("%Y-%m")
            for path in paths:
                path = Path(path)
                try:
                    candidates = self._candidates_for(path)
                except (OSError, zipfile.BadZipFile) as exc:
                    result.errors.append({"path": str(path), "error": str(exc)})
                    continue
                for cand in candidates:
                    member = cand.archive["member"] if cand.archive else None
                    if cand.sha256 in known:
                        result.duplicates.append({
                            "path": str(path), "member": member,
                            "sha256": cand.sha256, "storePath": known[cand.sha256],
                        })
                        continue
                    host = _host_of(cand.zone["referrerUrl"]) or _host_of(cand.zone["hostUrl"])
                    site = canonical_site(host, sites)
                    rel = Path("store") / site / month / cand.original_name
                    if not dry_run:
                        try:
                            dest = _unique_path(self.root / rel)
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            if cand.data is not None:
                                dest.write_bytes(cand.data)
                                os.utime(dest, (cand.mtime, cand.mtime))
                            else:
                                shutil.copy2(cand.source, dest)
                            _make_read_only(dest)
                        except OSError as exc:
                            result.errors.append({"path": str(path), "error": str(exc)})
                            continue
                        rel = dest.relative_to(self.root)
                    record = {
                        "format": FORMAT_MATERIAL,
                        "sha256": cand.sha256,
                        "bytes": cand.size,
                        "storePath": rel.as_posix(),
                        "originalName": cand.original_name,
                        "ingestedAt": _now_iso(),
                        "downloadedAt": _ts_iso(cand.mtime),
                        "hostUrl": cand.zone["hostUrl"],
                        "referrerUrl": cand.zone["referrerUrl"],
                        "archive": cand.archive,
                        "site": site,
                    }
                    if not dry_run:
                        self._append(record)
                    known[cand.sha256] = record["storePath"]
                    result.new.append(record)
                    _, needs_review = self._resolve_license(cand.sha256, site, sites, overrides)
                    if needs_review:
                        result.needs_review.append(record)
            if result.new and not dry_run:
                self.write_materials_md()
        return result

    def ingest_dir(
        self,
        directory: Path,
        since_ts: Optional[float] = None,
        dry_run: bool = False,
        now: Optional[float] = None,
    ) -> IngestResult:
        """Ingest *directory*. *since_ts* None = since the last ingest (first: 30 days)."""
        started = time.time() if now is None else now
        if since_ts is None:
            last = self.load_state().get("lastIngestAt")
            since_ts = float(last) if isinstance(last, (int, float)) else started - DEFAULT_SINCE_DAYS * 86400
        result = self.ingest_paths(self.scan_candidates(directory, since_ts), dry_run=dry_run)
        if not dry_run:
            self.mark_ingested(started)
        return result

    def mark_ingested(self, ts: float) -> None:
        """Record *ts* as the last ingest time (the next default ``--since``)."""
        with self._lock:
            state = self.load_state()
            state["lastIngestAt"] = ts
            self.save_state(state)

    # ── derived outputs ──────────────────────────────────────

    def list_materials(
        self, needs_review: bool = False, site: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Resolved view of every original (ledger order), optionally filtered."""
        materials, _, overrides = self._index()
        sites = self.load_sites()
        out: list[dict[str, Any]] = []
        for rec in materials.values():
            entry = self._original_entry(rec, sites, overrides)
            entry["ingestedAt"] = rec.get("ingestedAt")
            entry["archive"] = rec.get("archive")
            if needs_review and not entry["needsReview"]:
                continue
            if site and entry["site"] != site:
                continue
            out.append(entry)
        return out

    def write_materials_md(self) -> None:
        entries = self.list_materials()
        review = sum(1 for e in entries if e["needsReview"])
        lines = [
            "<!-- generated by hapbeat-helper from the material ledger — do not edit -->",
            "# Materials",
            "",
            f"要確認: {review} 件 / 全 {len(entries)} 件",
            "",
        ]
        by_site: dict[str, dict[str, list[dict]]] = {}
        for e in entries:
            month = str(e.get("ingestedAt") or "")[:7] or "unknown"
            by_site.setdefault(e["site"], {}).setdefault(month, []).append(e)
        for site in sorted(by_site):
            lines += [f"## {site}", ""]
            for month in sorted(by_site[site]):
                lines += [
                    f"### {month}",
                    "",
                    "| ファイル | store | 元ページ | ライセンス | 要確認 |",
                    "|---|---|---|---|---|",
                ]
                for e in by_site[site][month]:
                    page = e.get("referrerUrl") or e.get("hostUrl") or ""
                    lines.append(
                        f"| {_md_cell(e.get('originalName'))} | {_md_cell(e.get('storePath'))} "
                        f"| {_md_cell(page)} | {_md_cell(e['license']['name'])} "
                        f"| {'要確認' if e['needsReview'] else ''} |"
                    )
                lines.append("")
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            self.materials_md_path.write_text("\n".join(lines), encoding="utf-8")

    def credits_markdown(
        self, items: Iterable[tuple[Optional[str], str]], tool_name: str,
    ) -> str:
        """CREDITS.md for ``(display name | None, sha256)`` items.

        Originals are grouped by site + license. Items with no known original
        go under 出典不明（要確認）.
        """
        seen: set[str] = set()
        uniq: list[tuple[Optional[str], str]] = []
        for name, sha in items:
            if sha in seen:
                continue
            seen.add(sha)
            uniq.append((name, sha))
        resolved = self.resolve_many([sha for _, sha in uniq])

        groups: dict[tuple, dict[str, Any]] = {}
        unknown: list[str] = []
        for name, sha in uniq:
            display = name or self.display_name(sha)
            originals = resolved[sha]["originals"]
            if not originals:
                unknown.append(display)
                continue
            for orig in originals:
                lic = orig["license"]
                key = (orig["site"], lic["id"], lic["name"], lic["url"], lic["creditText"])
                group = groups.setdefault(key, {"site": orig["site"], "license": lic, "lines": []})
                page = orig.get("referrerUrl") or orig.get("hostUrl")
                line = f"- {display} ← {orig.get('originalName') or orig['sha256'][:12]}"
                if page:
                    line += f"（{page}）"
                if line not in group["lines"]:
                    group["lines"].append(line)

        lines = [
            f"<!-- generated by {tool_name} from the material ledger — do not edit -->",
            "# Credits",
            "",
        ]
        for group in sorted(groups.values(), key=lambda g: (g["site"], g["license"]["id"])):
            lic = group["license"]
            if lic["id"] == "unknown":
                heading = f"## {group['site']} — ライセンス未確認（要確認）"
            else:
                title = lic.get("creditText") or group["site"]
                heading = f"## {title} — {license_label(lic)}"
                if lic.get("url"):
                    heading += f"（{lic['url']}）"
            lines.append(heading)
            if lic.get("creditText"):
                lines.append(f"クレジット: {lic['creditText']}")
            lines += group["lines"]
            lines.append("")
        if unknown:
            lines.append("## 出典不明（要確認）")
            lines += [f"- {name}" for name in unknown]
            lines.append("")
        return "\n".join(lines)


def _md_cell(value: Any) -> str:
    return "" if value is None else str(value).replace("|", "\\|")


# ──────────────────────────────────────────────────────────────
# Downloads watcher (daemon, opt-in via materials_watch_downloads)


class DownloadsWatcher:
    """Ingests new audio/zip files that appear in a downloads folder.

    A file counts as finished once its size is the same on two consecutive
    polls. Files already present when the watcher starts (older than the last
    ingest) are left to ``materials ingest``.
    """

    def __init__(
        self,
        store: MaterialStore,
        directory: Path,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.directory = Path(directory)
        self._clock = clock
        last = store.load_state().get("lastIngestAt")
        self._since = float(last) if isinstance(last, (int, float)) else clock()
        # path -> size seen on the previous poll
        self._pending: dict[Path, int] = {}
        # path -> (mtime, size) already handled, so it is not re-hashed
        self._done: dict[Path, tuple[float, int]] = {}

    def poll(self) -> Optional[IngestResult]:
        """One poll. Returns the ingest result when something was ingested."""
        ready: list[Path] = []
        present: set[Path] = set()
        for path in self.store.scan_candidates(self.directory, self._since):
            try:
                st = path.stat()
            except OSError:
                continue
            present.add(path)
            if self._done.get(path) == (st.st_mtime, st.st_size):
                continue
            prev = self._pending.get(path)
            if prev is not None and prev == st.st_size and st.st_size > 0:
                ready.append(path)
                self._pending.pop(path, None)
                self._done[path] = (st.st_mtime, st.st_size)
            else:
                self._pending[path] = st.st_size
        for gone in [p for p in self._pending if p not in present]:
            self._pending.pop(gone, None)
        if not ready:
            return None
        result = self.store.ingest_paths(ready)
        self.store.mark_ingested(self._clock())
        return result
