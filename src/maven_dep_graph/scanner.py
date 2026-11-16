"""Find and parse every ``pom.xml`` below the workspace directory."""

from __future__ import annotations

import os
from pathlib import Path

from .models import RawProject
from .pom_parser import parse_pom

SKIP_DIRS = {
    ".git",
    ".idea",
    ".svn",
    ".hg",
    ".vscode",
    "node_modules",
    "target",
    "build",
    "out",
    "bin",
    ".gradle",
    ".mvn",
    "__pycache__",
    ".venv",
    "venv",
    "src",  # a pom.xml under src/ is a test fixture / archetype resource, not a project
}


def find_pom_files(root: Path) -> list[Path]:
    poms: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        if "pom.xml" in filenames:
            poms.append(Path(dirpath) / "pom.xml")
    return sorted(p.resolve() for p in poms)


def scan_projects(root: Path, warnings: list[str]) -> list[RawProject]:
    projects: list[RawProject] = []
    for pom in find_pom_files(root):
        try:
            projects.append(parse_pom(pom))
        except Exception as exc:  # noqa: BLE001 - report and continue with the rest
            warnings.append(f"Could not parse {pom}: {exc}")
    return projects


def _as_pom(path: Path) -> Path:
    path = path.expanduser()
    return path if path.name.endswith(".xml") and path.is_file() else path / "pom.xml"


def resolve_parent(
    projects: list[RawProject],
    parent_spec: str,
    warnings: list[str],
) -> RawProject:
    """Find the parent project.

    ``parent_spec`` is preferably a path to the parent project directory (or its
    ``pom.xml``). For convenience an ``artifactId`` or ``groupId:artifactId`` of a
    scanned project is accepted too. A parent outside the scanned directory is
    parsed and appended to ``projects``.
    """
    candidate = _as_pom(Path(parent_spec))
    if candidate.is_file():
        pom = candidate.resolve()
        for project in projects:
            if project.pom_path == pom:
                return project
        project = parse_pom(pom)
        projects.append(project)
        warnings.append(f"Parent {pom} lies outside the scanned directory; added it to the analysis.")
        return project

    lowered = parent_spec.lower()
    matches = [p for p in projects if lowered in (p.artifact_id.lower(), p.ga.lower())]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        paths = ", ".join(str(m.pom_path) for m in matches)
        raise LookupError(f"Parent '{parent_spec}' is ambiguous, pass a path instead: {paths}")
    known = ", ".join(sorted(p.artifact_id for p in projects)[:40])
    raise LookupError(
        f"Parent '{parent_spec}' is neither an existing path nor a scanned artifactId.\n"
        f"Scanned artifactIds: {known}"
    )


def module_children(projects: list[RawProject]) -> set[Path]:
    """POM paths that are <module>s of another scanned POM (built through their aggregator)."""
    by_path = {p.pom_path: p for p in projects}
    children: set[Path] = set()
    for project in projects:
        for module in project.modules:
            pom = _as_pom(project.base_dir / module).resolve()
            if pom in by_path and pom != project.pom_path:
                children.add(pom)
    return children
