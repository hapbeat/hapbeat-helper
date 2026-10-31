"""The AI-agent guide and log templates of ``hapbeat-helper scene guide`` / ``scene template``.

``guide.md`` is the procedure (hapbeat-contracts ``specs/cue-table.md`` section 10 and
``specs/scene-recording.md`` sections 5-7), ``engines/<engine>.md`` the engine notes, and
``templates/<engine>/`` the files that write the recording log (copied into the game).
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

ENGINES = ("unity", "unreal", "js", "other")
TEMPLATE_ENGINES = ("unity", "unreal", "js")


def _root():
    return resources.files(__name__)


def guide(engine: str | None = None) -> str:
    """The guide, followed by the notes of ``engine`` (or of every engine)."""
    root = _root()
    parts = [root.joinpath("guide.md").read_text(encoding="utf-8")]
    for name in (engine,) if engine else ENGINES:
        parts.append(root.joinpath("engines", f"{name}.md").read_text(encoding="utf-8"))
    return "\n".join(p.rstrip("\n") + "\n" for p in parts)


def template_files(engine: str) -> dict[str, str]:
    """``{file name: text}`` of an engine's templates."""
    folder = _root().joinpath("templates", engine)
    return {f.name: f.read_text(encoding="utf-8") for f in sorted(folder.iterdir(), key=lambda f: f.name) if f.is_file()}


def write_templates(engine: str, out: Path, force: bool = False) -> tuple[list[Path], list[Path]]:
    """Copy an engine's templates into ``out``: ``(written, skipped)``. An existing file is skipped
    unless ``force`` (the copy in the game may have been edited)."""
    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    skipped: list[Path] = []
    for name, text in template_files(engine).items():
        target = out / name
        if target.exists() and not force:
            skipped.append(target)
            continue
        target.write_text(text, encoding="utf-8", newline="\n")
        written.append(target)
    return written, skipped
