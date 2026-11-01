"""Read a Unity project's Event Maps and triggers into an event inventory (``hapbeat-helper cues scan --unity``).

``hapbeat-contracts`` ``specs/cue-table.md`` section 10. Unity writes scenes, prefabs, Animator
controllers and ScriptableObjects as text YAML (Force Text), so they are read from outside the
editor:

1. The Hapbeat SDK's script GUIDs come from its installed ``*.cs.meta`` (never hard-coded).
2. Event Maps are the ``.asset`` files whose ``m_Script`` is ``HapbeatEventMap``.
3. Triggers are the SDK components in ``.unity`` / ``.prefab`` / ``.controller`` files that name a map
   (``_eventMap``) and entry ids (``_entryId`` ...), plus prefab overrides of those fields.
4. Only the maps something references go into ``events``; the others are listed with
   ``referenced: false``.

helper has no YAML dependency: the reader below understands the subset Unity writes (block
mappings and sequences, flow ``{...}`` / ``[...]``, quoted and folded scalars). Every value is a string.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hapbeat_helper.cue_inventory import normalize_name
from hapbeat_helper.cue_table import is_level_map

SDK_PACKAGE = "com.hapbeat.sdk"
EVENT_MAP_SCRIPT = "HapbeatEventMap"
# Trigger fields that hold an entry id (HapbeatTriggerBase, HapbeatSequenceTrigger, HapbeatStateBehaviour).
ENTRY_FIELDS = ("_entryId", "_onStartEntryId", "_onStopEntryId", "_entryIdOnEnter", "_entryIdOnExit")
TRIGGER_SUFFIXES = (".unity", ".prefab", ".controller")
# HapticMode / BindingCurveType / BindingOutputParameter (Runtime/HapbeatEventEntry.cs, HapbeatParameterBinding.cs).
MODE_STREAM_CLIP = "1"
CURVE_TYPES = {"0": "linear", "1": "easeIn", "2": "easeOut", "3": "exponential", "4": "custom"}
OUTPUT_GAIN, OUTPUT_PAN = "0", "1"
SCRIPTABLE_OBJECT_FILE_ID = "11400000"
FORCE_TEXT = "2"
# Unity writes floats with 7 significant digits; tangents are compared with this tolerance.
TANGENT_TOLERANCE = 2e-3

_DOC_HEADER = re.compile(r"^--- !u!(\d+) &(-?\d+)( stripped)?")
_KEY = re.compile(r"^([^\s:'\"{\[\-][^:]*?):(?:\s+(.*))?$")
_GUID_LINE = re.compile(r"^guid:\s*([0-9a-fA-F]{32})\s*$", re.M)
_FILE_REF = re.compile(r"\{fileID:\s*(-?\d+)(?:,\s*guid:\s*([0-9a-fA-F]{32}))?")
_SCRIPT_LINE = re.compile(r"^  m_Script: \{fileID: -?\d+, guid: ([0-9a-fA-F]{32})", re.M)


class UnityScanError(Exception):
    """A project that cannot be scanned (not a Unity project, no Hapbeat SDK, unreadable file)."""


class BinaryAssetError(UnityScanError):
    """A scene / prefab / controller / Event Map saved as binary (the project is not Force Text)."""


@dataclass
class ScanResult:
    inventory: dict
    wiring: str
    warnings: list[str] = field(default_factory=list)


# ── the YAML subset ──────────────────────────────────────────


def _split_flow(s: str) -> list[str]:
    """Split the inside of a flow collection at top-level commas."""
    parts, depth, quote, cur = [], 0, "", []
    for ch in s:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "'\"":
            quote = ch
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
            continue
        cur.append(ch)
    if "".join(cur).strip():
        parts.append("".join(cur).strip())
    return parts


def _scalar(s: str) -> Any:
    s = s.strip()
    if s.startswith("{") and s.endswith("}"):
        out = {}
        for part in _split_flow(s[1:-1]):
            key, _, value = part.partition(":")
            out[key.strip()] = _scalar(value)
        return out
    if s.startswith("[") and s.endswith("]"):
        return [_scalar(p) for p in _split_flow(s[1:-1])]
    if len(s) >= 2 and s[0] == s[-1] == "'":
        return s[1:-1].replace("''", "'")
    if len(s) >= 2 and s[0] == s[-1] == '"':
        try:
            return json.loads(s)
        except ValueError:
            return s[1:-1]
    return s


def _closed(s: str) -> bool:
    """Whether a quoted / flow scalar that starts ``s`` ends in it."""
    if s[0] == "'":
        i = 1
        while i < len(s):
            if s[i] == "'":
                if i + 1 < len(s) and s[i + 1] == "'":
                    i += 2
                    continue
                return True
            i += 1
        return False
    if s[0] == '"':
        i = 1
        while i < len(s):
            if s[i] == "\\":
                i += 2
                continue
            if s[i] == '"':
                return True
            i += 1
        return False
    depth = 0
    for ch in s:
        depth += ch in "{["
        depth -= ch in "}]"
    return depth <= 0


class _Reader:
    """Indentation-based reader of one YAML document body."""

    def __init__(self, lines: list[str]) -> None:
        self.toks = [[len(raw) - len(raw.lstrip(" ")), raw.strip()] for raw in lines if raw.strip()]
        self.n = len(self.toks)

    @staticmethod
    def is_item(text: str) -> bool:
        return text == "-" or text.startswith("- ")

    def block(self, i: int, indent: int) -> tuple[Any, int]:
        if self.is_item(self.toks[i][1]):
            return self.seq(i, indent)
        return self.mapping(i, indent)

    def value(self, text: str, i: int, indent: int) -> tuple[Any, int]:
        """A scalar that may continue on the following, deeper lines."""
        if text[0] in "'\"{[":
            while not _closed(text) and i < self.n and self.toks[i][0] > indent:
                text += " " + self.toks[i][1]
                i += 1
        else:
            while i < self.n and self.toks[i][0] > indent:
                text += " " + self.toks[i][1]
                i += 1
        return _scalar(text), i

    def mapping(self, i: int, indent: int) -> tuple[dict, int]:
        out: dict = {}
        while i < self.n and self.toks[i][0] >= indent:
            ind, text = self.toks[i]
            if ind > indent or self.is_item(text):
                if ind == indent:
                    break
                i += 1
                continue
            m = _KEY.match(text)
            i += 1
            if not m:
                continue
            key, val = m.group(1), m.group(2)
            if val:
                out[key], i = self.value(val, i, indent)
            elif i < self.n and (
                self.toks[i][0] > indent or (self.toks[i][0] == indent and self.is_item(self.toks[i][1]))
            ):
                out[key], i = self.block(i, self.toks[i][0])
            else:
                out[key] = ""
        return out, i

    def seq(self, i: int, indent: int) -> tuple[list, int]:
        items: list = []
        while i < self.n and self.toks[i][0] == indent and self.is_item(self.toks[i][1]):
            rest = self.toks[i][1][2:].strip()
            if not rest:
                i += 1
                if i < self.n and self.toks[i][0] > indent:
                    item, i = self.block(i, self.toks[i][0])
                else:
                    item = ""
            elif rest[0] not in "'\"{[" and _KEY.match(rest):
                self.toks[i] = [indent + 2, rest]
                item, i = self.mapping(i, indent + 2)
            else:
                item, i = self.value(rest, i + 1, indent)
            items.append(item)
        return items, i


@dataclass
class _Doc:
    cls: int
    file_id: str
    stripped: bool
    lines: list[str]
    _body: dict | None = None

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    def body(self) -> dict:
        """The document's mapping (the part under its class name)."""
        if self._body is None:
            reader = _Reader(self.lines[1:])
            self._body = reader.mapping(0, reader.toks[0][0])[0] if reader.n else {}
        return self._body

    def line_value(self, key: str) -> Any:
        """A top-level ``key: value`` of the body (one line), without parsing the rest."""
        prefix = f"  {key}:"
        for line in self.lines[1:]:
            if line.startswith(prefix):
                return _scalar(line[len(prefix):])
        return None


def _is_binary(data: bytes) -> bool:
    return not data.startswith(b"%YAML") and b"\x00" in data[:8192]


def _read_docs(path: Path) -> list[_Doc] | None:
    """The YAML documents of a Unity text asset (None = not a Unity YAML file). Binary -> BinaryAssetError."""
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise UnityScanError(f"cannot read {path}: {exc}") from exc
    if _is_binary(data):
        raise BinaryAssetError(
            f"{path} is a binary asset; set Edit > Project Settings > Editor > Asset Serialization to "
            "Force Text and save it again"
        )
    if not data.startswith(b"%YAML"):
        return None
    docs: list[_Doc] = []
    current: _Doc | None = None
    for line in data.decode("utf-8", errors="replace").splitlines():
        m = _DOC_HEADER.match(line)
        if m:
            current = _Doc(int(m.group(1)), m.group(2), bool(m.group(3)), [])
            docs.append(current)
        elif current is not None:
            current.lines.append(line)
    return docs


def _ref(value: Any) -> tuple[str, str | None]:
    """``{fileID, guid?}`` -> (fileID, guid)."""
    if isinstance(value, dict):
        return str(value.get("fileID", "0")), (value.get("guid") or None)
    m = _FILE_REF.search(str(value))
    return (m.group(1), m.group(2)) if m else ("0", None)


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _num(x: float) -> float | int:
    """A JSON number without float noise (1.0 -> 1, 0.15000000000000002 -> 0.15)."""
    x = round(x, 6)
    return int(x) if x == int(x) else x


# ── the project ──────────────────────────────────────────────


def _rel(path: Path, root: Path) -> str:
    return Path(os.path.relpath(path, root)).as_posix()


def _meta_guid(meta: Path) -> str | None:
    try:
        m = _GUID_LINE.search(meta.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None
    return m.group(1).lower() if m else None


def _walk(folder: Path, suffixes: tuple[str, ...]):
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if name.endswith(suffixes):
                yield Path(dirpath) / name


def find_sdk(project: Path, sdk_root: Path | None) -> Path | None:
    """The installed Hapbeat SDK package folder (design 2.3 step 1), or None."""
    candidates: list[Path] = []
    manifest = project / "Packages" / "manifest.json"
    try:
        spec = json.loads(manifest.read_text(encoding="utf-8-sig")).get("dependencies", {}).get(SDK_PACKAGE)
    except (OSError, ValueError, AttributeError):
        spec = None
    if isinstance(spec, str) and spec.startswith("file:"):
        target = Path(spec[len("file:"):])
        candidates.append(target if target.is_absolute() else project / "Packages" / target)
    candidates.append(project / "Packages" / SDK_PACKAGE)
    candidates += sorted((project / "Library" / "PackageCache").glob(f"{SDK_PACKAGE}@*"))
    if sdk_root is not None:
        candidates.append(sdk_root)
    for c in candidates:
        if c.is_dir() and any(_walk(c, (f"{EVENT_MAP_SCRIPT}.cs.meta",))):
            return c.resolve()
    return None


def sdk_scripts(sdk: Path) -> dict[str, str]:
    """``{script guid: class name}`` of the SDK's scripts (from ``*.cs.meta``)."""
    out = {}
    for meta in _walk(sdk, (".cs.meta",)):
        guid = _meta_guid(meta)
        if guid:
            out[guid] = meta.name[: -len(".cs.meta")]
    return out


@dataclass
class _Ref:
    map_guid: str
    entry_id: str
    where: str


class _Scanner:
    def __init__(self, root: Path, sdk: Path) -> None:
        self.root = root
        self.sdk = sdk
        self.scripts = sdk_scripts(sdk)
        self.map_script = next((g for g, n in self.scripts.items() if n == EVENT_MAP_SCRIPT), None)
        self.warnings: list[str] = []
        self.guid_paths: dict[str, Path] = {}
        self.file_docs: dict[Path, dict[str, _Doc]] = {}
        self.refs: list[_Ref] = []
        self.maps: list[dict] = []  # {path, guid, name, entries}

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    # ── indexes ──

    def index_guids(self) -> None:
        folders = [self.root / "Assets", self.root / "Packages", self.sdk]
        for folder in folders:
            if not folder.is_dir():
                continue
            for meta in _walk(folder, (".meta",)):
                guid = _meta_guid(meta)
                if guid and guid not in self.guid_paths:
                    self.guid_paths[guid] = meta.with_suffix("")

    def docs_of(self, path: Path) -> dict[str, _Doc]:
        if path not in self.file_docs:
            docs = _read_docs(path) or []
            self.file_docs[path] = {d.file_id: d for d in docs}
        return self.file_docs[path]

    # ── Event Maps ──

    def read_maps(self) -> None:
        needle = f"guid: {self.map_script}"
        for path in sorted(_walk(self.root / "Assets", (".asset",)), key=lambda p: _rel(p, self.root)):
            try:
                data = path.read_bytes()
            except OSError as exc:
                raise UnityScanError(f"cannot read {path}: {exc}") from exc
            if _is_binary(data):
                meta = Path(str(path) + ".meta")
                main = ""
                try:
                    m = re.search(r"mainObjectFileID:\s*(\d+)", meta.read_text(encoding="utf-8", errors="replace"))
                    main = m.group(1) if m else ""
                except OSError:
                    pass
                if main == SCRIPTABLE_OBJECT_FILE_ID:
                    _read_docs(path)  # raises BinaryAssetError
                continue  # a binary asset of another kind (LightingData ...) cannot be an Event Map
            if needle.encode() not in data:
                continue
            for doc in (_read_docs(path) or []):
                if doc.cls != 114 or (_SCRIPT_LINE.search(doc.text) or [None, ""])[1].lower() != self.map_script:
                    continue
                guid = _meta_guid(Path(str(path) + ".meta")) or ""
                entries = doc.body().get("entries")
                self.maps.append({
                    "path": _rel(path, self.root), "guid": guid, "name": doc.line_value("m_Name") or path.stem,
                    "entries": entries if isinstance(entries, list) else [],
                })

    # ── triggers ──

    def go_name(self, path: Path, file_id: str) -> str:
        doc = self.docs_of(path).get(file_id)
        if doc is None:
            return "?"
        if not doc.stripped:
            return doc.line_value("m_Name") or "?"
        src_id, src_guid = _ref(doc.line_value("m_CorrespondingSourceObject"))
        inst_id, _ = _ref(doc.line_value("m_PrefabInstance"))
        inst = self.docs_of(path).get(inst_id)
        if inst is not None:
            for mod in self.modifications(inst):
                tid, tguid = _ref(mod.get("target"))
                if tid == src_id and tguid == src_guid and mod.get("propertyPath") == "m_Name":
                    return str(mod.get("value", "")) or "?"
        prefab = self.guid_paths.get((src_guid or "").lower())
        if prefab is not None and prefab.suffix == ".prefab":
            return self.go_name(prefab, src_id)
        return "?"

    def go_path(self, path: Path, file_id: str) -> str:
        """``Parent/Child`` of a GameObject in a scene / prefab (stops at a prefab instance's object)."""
        docs = self.docs_of(path)
        names, seen = [], set()
        current = file_id
        while current and current != "0" and current not in seen and len(names) < 64:
            seen.add(current)
            names.append(self.go_name(path, current))
            go = docs.get(current)
            if go is None:
                break
            father = "0"
            if go.stripped:
                # An object of a prefab instance: continue at the instance's parent in this file.
                inst = docs.get(_ref(go.line_value("m_PrefabInstance"))[0])
                mod = inst.body().get("m_Modification", {}) if inst is not None else {}
                father = _ref(mod.get("m_TransformParent") if isinstance(mod, dict) else None)[0]
            else:
                for comp_id in re.findall(r"- component: \{fileID: (-?\d+)\}", go.text):
                    tr = docs.get(comp_id)
                    if tr is not None and tr.cls in (4, 224) and not tr.stripped:
                        father = _ref(tr.line_value("m_Father"))[0]
                        break
            current = self.transform_object(path, father)
        return "/".join(reversed(names))

    def transform_object(self, path: Path, transform_id: str) -> str:
        """The GameObject of a Transform in this file ("0" = none)."""
        tr = self.docs_of(path).get(transform_id)
        if tr is None:
            return "0"
        if not tr.stripped:
            return _ref(tr.line_value("m_GameObject"))[0]
        # A stripped Transform of a prefab instance: its GameObject is the stripped one of the same instance.
        src_id, src_guid = _ref(tr.line_value("m_CorrespondingSourceObject"))
        inst_id = _ref(tr.line_value("m_PrefabInstance"))[0]
        prefab = self.guid_paths.get((src_guid or "").lower())
        pdoc = self.docs_of(prefab).get(src_id) if prefab is not None and prefab.suffix == ".prefab" else None
        if pdoc is None:
            return "0"
        go_src = _ref(pdoc.line_value("m_GameObject"))[0]
        for doc in self.docs_of(path).values():
            if doc.cls == 1 and doc.stripped and _ref(doc.line_value("m_PrefabInstance"))[0] == inst_id \
                    and _ref(doc.line_value("m_CorrespondingSourceObject"))[0] == go_src:
                return doc.file_id
        return "0"

    def state_name(self, path: Path, file_id: str) -> str:
        """The Animator state (or state machine) that holds a StateMachineBehaviour."""
        pattern = re.compile(r"m_StateMachineBehaviours:(?:\s*- \{fileID: -?\d+\})*\s*- \{fileID: " + re.escape(file_id) + r"\}")
        for doc in self.docs_of(path).values():
            if doc.cls in (1102, 1107) and pattern.search(doc.text):
                return doc.line_value("m_Name") or "?"
        return "?"

    @staticmethod
    def modifications(inst: _Doc) -> list[dict]:
        mods = inst.body().get("m_Modification", {})
        mods = mods.get("m_Modifications", []) if isinstance(mods, dict) else []
        return [m for m in mods if isinstance(m, dict)] if isinstance(mods, list) else []

    def where(self, path: Path, owner: str, script: str, field_name: str, body: dict, extra: str = "") -> str:
        notes = [f"{script}.{field_name}"]
        mult = _float(body.get("_gainMultiplier", 1), 1.0)
        if mult != 1:
            notes.append(f"gain x{_num(mult)}")
        if extra:
            notes.append(extra)
        return f"{_rel(path, self.root)}: {owner} ({', '.join(notes)})"

    def owner(self, path: Path, doc: _Doc) -> str:
        go_id, _ = _ref(doc.line_value("m_GameObject"))
        if go_id != "0":
            return self.go_path(path, go_id)
        if path.suffix == ".controller":
            return f"state {self.state_name(path, doc.file_id)}"
        return "?"

    def read_triggers(self) -> None:
        files = sorted(_walk(self.root / "Assets", TRIGGER_SUFFIXES), key=lambda p: _rel(p, self.root))
        for path in files:
            docs = self.docs_of(path)
            for doc in docs.values():
                if doc.cls == 114 and not doc.stripped:
                    m = _SCRIPT_LINE.search(doc.text)
                    script = self.scripts.get(m.group(1).lower()) if m else None
                    if script is None or "  _eventMap:" not in doc.text:
                        continue
                    body = doc.body()
                    _, map_guid = _ref(body.get("_eventMap"))
                    ids = [(f, str(body.get(f, ""))) for f in ENTRY_FIELDS if str(body.get(f, ""))]
                    if not ids:
                        continue
                    owner = self.owner(path, doc)
                    if not map_guid:
                        self.warn(f"{_rel(path, self.root)}: {owner} ({script}) has an entry id but no Event Map")
                        continue
                    for f, entry_id in ids:
                        self.refs.append(_Ref(map_guid.lower(), entry_id, self.where(path, owner, script, f, body)))
                elif doc.cls == 1001 and "propertyPath: _e" in doc.text:
                    self.read_overrides(path, doc)

    def read_overrides(self, path: Path, inst: _Doc) -> None:
        """Prefab overrides of ``_eventMap`` / ``_entryId*`` in a scene or prefab."""
        targets: dict[tuple[str, str | None], dict[str, Any]] = {}
        for mod in self.modifications(inst):
            prop = mod.get("propertyPath")
            if prop in ENTRY_FIELDS or prop == "_eventMap" or prop == "_gainMultiplier":
                key = _ref(mod.get("target"))
                targets.setdefault(key, {})[prop] = (
                    _ref(mod.get("objectReference"))[1] if prop == "_eventMap" else mod.get("value", "")
                )
        for (tid, tguid), over in targets.items():
            fields = [f for f in ENTRY_FIELDS if str(over.get(f, ""))]
            if not fields:
                continue
            prefab = self.guid_paths.get((tguid or "").lower())
            source = self.docs_of(prefab).get(tid) if prefab is not None and prefab.suffix == ".prefab" else None
            body = dict(source.body()) if source is not None else {}
            body.update({k: v for k, v in over.items() if k != "_eventMap"})
            map_guid = over.get("_eventMap") or _ref(body.get("_eventMap"))[1]
            m = _SCRIPT_LINE.search(source.text) if source is not None else None
            script = (self.scripts.get(m.group(1).lower()) if m else None) or "trigger"
            owner = "?"
            if source is not None and prefab is not None:
                owner = self.go_path(prefab, _ref(source.line_value("m_GameObject"))[0])
            owner = f"{owner} in prefab {prefab.stem if prefab is not None else '?'}"
            if not map_guid:
                self.warn(f"{_rel(path, self.root)}: prefab override of {', '.join(fields)} on {owner}: Event Map not found")
                continue
            for f in fields:
                self.refs.append(_Ref(
                    map_guid.lower(), str(over[f]), self.where(path, owner, script, f, body, "prefab override"),
                ))

    # ── entries ──

    def wav_of(self, entry: dict, label: str) -> str | None:
        clip_id, clip_guid = _ref(entry.get("streamClip"))
        if clip_id == "0" or not clip_guid:
            self.warn(f"{label}: no streamClip")
            return None
        path = self.guid_paths.get(clip_guid.lower())
        if path is None:
            self.warn(f"{label}: streamClip guid {clip_guid} not found in the project")
            return None
        if path.suffix.lower() != ".wav":
            self.warn(f"{label}: streamClip {_rel(path, self.root)} is not a WAV (left out)")
            return None
        return _rel(path, self.root)

    def level_map(self, entry: dict, loop: bool, label: str) -> dict | None:
        bindings = entry.get("bindings")
        bindings = [b for b in bindings if isinstance(b, dict)] if isinstance(bindings, list) else []
        if any(str(b.get("outputParameter")) == OUTPUT_PAN for b in bindings):
            self.warn(f"{label}: has a StreamPan binding (not part of the cue table)")
        gains = [b for b in bindings if str(b.get("outputParameter")) == OUTPUT_GAIN]
        if not gains:
            return None
        if not loop:
            self.warn(f"{label}: StreamGain binding on a one-shot entry (left out)")
            return None
        maps = []
        for b in gains:
            lm, reason = binding_level_map(b)
            if lm is None:
                self.warn(f"{label}: StreamGain binding left out of levelMap: {reason}")
                return None
            maps.append(lm)
        if any(json.dumps(m, sort_keys=True) != json.dumps(maps[0], sort_keys=True) for m in maps):
            self.warn(f"{label}: its StreamGain bindings differ; levelMap left out")
            return None
        return maps[0]

    def events(self) -> tuple[list[dict], list[dict]]:
        referenced = {r.map_guid for r in self.refs}
        known = {m["guid"]: m for m in self.maps}
        for guid in sorted(referenced - set(known)):
            path = self.guid_paths.get(guid)
            where = sorted({r.where for r in self.refs if r.map_guid == guid})
            self.warn(
                f"a trigger names an Event Map that was not found ({_rel(path, self.root) if path else guid}): "
                + "; ".join(where)
            )
        maps_out, events = [], []
        for m in self.maps:
            is_ref = m["guid"] in referenced
            maps_out.append({"path": m["path"], "id": m["guid"], "referenced": is_ref})
            if not is_ref:
                continue
            refs = [r for r in self.refs if r.map_guid == m["guid"]]
            ids = set()
            for i, entry in enumerate(m["entries"]):
                if not isinstance(entry, dict):
                    continue
                label = f"{m['name']} entry {i} ({entry.get('displayName') or entry.get('eventName') or '?'})"
                entry_id = str(entry.get("_id", ""))
                if not entry_id:
                    self.warn(f"{label}: empty _id (the entry gets one when it is first used in the editor); left out")
                    continue
                if entry_id in ids:
                    self.warn(f"{label}: entry id {entry_id} is twice in {m['path']}; the second is left out")
                    continue
                ids.add(entry_id)
                source = f"unity:{m['guid']}/{entry_id}"
                stream = str(entry.get("mode", "0")) == MODE_STREAM_CLIP
                display, event_name = str(entry.get("displayName", "")), str(entry.get("eventName", ""))
                name = (display or event_name) if stream else (event_name or display)
                if not name:
                    self.warn(f"{label}: no displayName / eventName; its cue name comes from the entry id")
                    name = "-"  # normalizes to cue_<id> (section 10.2)
                loop = str(entry.get("loop", "0")) == "1"
                ev: dict[str, Any] = {
                    "source": source, "map": m["guid"], "name": name, "loop": loop, "target": str(entry.get("target", "")),
                    "gain": _num(_float(entry.get("gain"), 1.0)),
                }
                if stream:
                    wav = self.wav_of(entry, label)
                    if wav:
                        ev["wav"] = wav
                clip_gain = _float(entry.get("_cachedManifestClipGain"), -1.0)
                if 0 <= clip_gain <= 1:
                    ev["clip_gain"] = _num(clip_gain)
                elif clip_gain >= 0:
                    self.warn(f"{label}: _cachedManifestClipGain {clip_gain} is not 0..1 (left out)")
                lm = self.level_map(entry, loop, label)
                if lm is not None:
                    ev["levelMap"] = lm
                ev["where"] = sorted({r.where for r in refs if r.entry_id == entry_id})
                if not stream:
                    category = str(entry.get("category", ""))
                    event_id = f"{category}.{event_name}" if category and event_name else event_name
                    ev["note"] = f"Command entry: the device plays Kit event {event_id or '(none)'}"
                events.append(ev)
            for r in refs:
                if r.entry_id not in ids:
                    self.warn(f"{r.where}: entry id {r.entry_id} is not in {m['path']}")
        return maps_out, events


def binding_level_map(binding: dict) -> tuple[dict | None, str]:
    """A StreamGain binding preset -> levelMap (cue-table section 5.6) over the normalized input 0..1.

    The binding maps its source to 0..1 (clamped), applies the curve and lerps outputMin..outputMax.
    Linear / EaseIn / EaseOut are exact; a Custom curve is exact when every segment's tangents give one
    of linear / easeIn / easeOut / easeInOut (cubic Hermite), the same for all segments, optionally with
    a last linear segment that continues the previous slope (how HandDemoHapticImport.cs extends a map
    to input 1). Anything else returns (None, reason).
    """
    if abs(_float(binding.get("inputMax"), 1) - _float(binding.get("inputMin"), 0)) <= 1e-4:
        return None, "its input range is empty"
    lo, hi = _float(binding.get("outputMin"), 0.0), _float(binding.get("outputMax"), 1.0)
    kind = CURVE_TYPES.get(str(binding.get("curveType", "0")))
    if kind in ("linear", "easeIn", "easeOut"):
        nodes, curve = [(0.0, 0.0), (1.0, 1.0)], kind
    elif kind == "custom":
        nodes, curve, reason = _custom_curve(binding.get("customCurve"))
        if nodes is None:
            return None, reason
    else:
        return None, f"curve type {kind or binding.get('curveType')} has no levelMap shape"
    values = [(t, lo + v * (hi - lo)) for t, v in nodes]
    if any(not 0 <= y <= 4 for _, y in values):
        return None, "outputs outside 0..4"
    lm: dict[str, Any] = {"points": [[_num(t), _num(y)] for t, y in values[1:]]}
    if _num(values[0][1]) != 0:
        lm["intercept"] = _num(values[0][1])
    lm["curve"] = curve
    if not is_level_map(lm):
        return None, "the curve does not give 1-16 increasing points"
    return lm, ""


def _segment_shapes(m0: float, m1: float, slope: float) -> set[str]:
    """The section 8 shapes a cubic Hermite segment with end tangents m0, m1 matches."""
    if abs(slope) < 1e-9:
        ok = abs(m0) < TANGENT_TOLERANCE and abs(m1) < TANGENT_TOLERANCE
        return {"linear", "easeIn", "easeOut", "easeInOut"} if ok else set()
    a, b = m0 / slope, m1 / slope
    shapes = {"linear": (1, 1), "easeIn": (0, 2), "easeOut": (2, 0), "easeInOut": (0, 0)}
    return {n for n, (x, y) in shapes.items() if abs(a - x) < TANGENT_TOLERANCE and abs(b - y) < TANGENT_TOLERANCE}


def _custom_curve(curve: Any) -> tuple[list | None, str, str]:
    """AnimationCurve keys -> (nodes [(t, v)], shape, reason)."""
    keys = curve.get("m_Curve") if isinstance(curve, dict) else None
    if not isinstance(keys, list) or len(keys) < 2 or not all(isinstance(k, dict) for k in keys):
        return None, "", "the custom curve has fewer than 2 keys"
    if any(str(k.get("weightedMode", "0")) != "0" for k in keys):
        return None, "", "the custom curve has weighted tangents"
    pts = [(_float(k.get("time"), 0), _float(k.get("value"), 0), _float(k.get("inSlope"), 0), _float(k.get("outSlope"), 0)) for k in keys]
    if abs(pts[0][0]) > 1e-6 or abs(pts[-1][0] - 1) > 1e-6:
        return None, "", "the custom curve does not run from time 0 to 1"
    if any(b[0] <= a[0] for a, b in zip(pts, pts[1:])):
        return None, "", "the custom curve's key times do not increase"
    segs = []
    for (t0, v0, _, out0), (t1, v1, in1, _) in zip(pts, pts[1:]):
        slope = (v1 - v0) / (t1 - t0)
        segs.append((_segment_shapes(out0, in1, slope), slope))
    nodes = [(t, v) for t, v, _, _ in pts]
    common = set.intersection(*(s for s, _ in segs))
    if not common and len(segs) >= 2 and "linear" in segs[-1][0]:
        # A last linear segment that continues the previous one end to end (levelMap's extension).
        rest = set.intersection(*(s for s, _ in segs[:-1]))
        (t0, v0), (t1, v1) = nodes[-3], nodes[-2]
        if rest and abs(segs[-1][1] - (v1 - v0) / (t1 - t0)) < TANGENT_TOLERANCE * max(1, abs(segs[-1][1])):
            common, nodes = rest, nodes[:-1]
    if not common:
        return None, "", "the custom curve's segments do not share one levelMap curve"
    for shape in ("linear", "easeIn", "easeOut", "easeInOut"):
        if shape in common:
            return nodes, shape, ""
    return None, "", "no levelMap curve"


# ── output ───────────────────────────────────────────────────


def _project_name(root: Path) -> str | None:
    name = re.sub(r"[^a-z0-9-]+", "-", root.name.lower()).strip("-")
    return name if re.match(r"^[a-z][a-z0-9-]*$", name) else None


def wiring_markdown(inv: dict) -> str:
    """Per entry: what fires it (scene / prefab / controller, GameObject, trigger), and what nothing fires."""
    lines = [f"# Event wiring: {inv.get('project', '(project)')}", ""]
    lines.append("Event Maps:")
    lines.append("")
    for m in inv.get("maps", []):
        state = "referenced" if m["referenced"] else "not referenced by any scene / prefab / controller (left out)"
        lines.append(f"- `{m['path']}` ({m['id']}): {state}")
    lines += ["", "## Entries", ""]
    unfired = []
    for ev in inv["events"]:
        cue = ev.get("cue") or normalize_name(ev["name"], ev["source"])
        lines.append(f"### {ev['name'] or '(no name)'} -> `{cue}`")
        lines.append("")
        lines.append(f"- source: `{ev['source']}`")
        lines.append(
            f"- target: `{ev['target'] or '(everyone)'}`, loop: {str(ev['loop']).lower()}, gain: {ev['gain']}"
            + (f", clip_gain: {ev['clip_gain']}" if "clip_gain" in ev else "")
        )
        lines.append(f"- wav: `{ev['wav']}`" if ev.get("wav") else "- wav: (none)")
        if ev.get("levelMap"):
            lines.append(f"- levelMap: `{json.dumps(ev['levelMap'])}`")
        if ev.get("note"):
            lines.append(f"- note: {ev['note']}")
        if ev.get("where"):
            lines.append("- fired by:")
            lines += [f"  - {w}" for w in ev["where"]]
        else:
            lines.append("- fired by: nothing in the scenes / prefabs / controllers (code may fire it)")
            unfired.append(ev)
        lines.append("")
    lines += ["## Entries nothing references", ""]
    if unfired:
        lines += [f"- {ev['name'] or '(no name)'} (`{ev['source']}`)" for ev in unfired]
    else:
        lines.append("- (none)")
    if inv.get("warnings"):
        lines += ["", "## Warnings", ""]
        lines += [f"- {w}" for w in inv["warnings"]]
    return "\n".join(lines) + "\n"


def scan(project_dir: Path, sdk_root: Path | None = None) -> ScanResult:
    """Read a Unity project into an inventory (cue-table section 10) and its wiring.md text.

    Raises BinaryAssetError for a binary scene / prefab / controller / ScriptableObject, and
    UnityScanError when the project or the SDK's scripts are not found.
    """
    root = Path(project_dir).resolve()
    if not (root / "Assets").is_dir():
        raise UnityScanError(f"{root} is not a Unity project (no Assets folder)")
    sdk = find_sdk(root, Path(sdk_root).resolve() if sdk_root else None)
    if sdk is None:
        raise UnityScanError(
            f"Hapbeat SDK ({SDK_PACKAGE}) not found: looked at Packages/manifest.json, Packages/{SDK_PACKAGE}, "
            f"Library/PackageCache/{SDK_PACKAGE}@*" + (f" and {sdk_root}" if sdk_root else "; pass --sdk-root")
        )
    sc = _Scanner(root, sdk)
    editor = root / "ProjectSettings" / "EditorSettings.asset"
    if editor.is_file():
        m = re.search(r"m_SerializationMode:\s*(\d+)", editor.read_text(encoding="utf-8", errors="replace"))
        if m and m.group(1) != FORCE_TEXT:
            sc.warn("Asset Serialization is not Force Text: binary assets cannot be read")
    sc.index_guids()
    sc.read_maps()
    sc.read_triggers()
    maps, events = sc.events()
    inv: dict[str, Any] = {"schema_version": 1, "engine": "unity"}
    project = _project_name(root)
    if project:
        inv["project"] = project
    inv["maps"] = maps
    inv["events"] = events
    inv["warnings"] = sc.warnings
    return ScanResult(inventory=inv, wiring=wiring_markdown(inv), warnings=sc.warnings)
