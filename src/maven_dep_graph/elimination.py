"""Can a library disappear from the workspace entirely - and how hard is that?

Moving a dependency from the parent into the children does not reduce exposure:
the library is still shipped. The real question is whether the workspace can
stop using it at all. For every external library declared directly anywhere
(including in parent POMs) this module measures its *footprint* in the code:

* which library classes are referenced (imports, static imports, wildcard
  imports, fully-qualified names; resources such as ``logback.xml`` count too),
* which members are called (``StringUtils.isBlank`` x 12 ...),
* number of references, files and projects (main vs. test),

and combines it with the number of artifacts that would leave the classpath
(from the Maven tree, when available) into an effort rating:

=========  ==================================================================
none       no reference at all - delete it
trivial    <= 2 classes, <= 10 references, <= 3 files - rewrite in minutes
low        <= 5 classes, <= 40 references - a small, contained rewrite
medium     <= 15 classes, <= 150 references - a planned refactoring
high       larger footprint - keep unless there is a strategic reason
keep       infrastructure (framework, driver, logging, test framework...)
=========  ==================================================================
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .knowledge import keep_category, replacement_hint
from .models import EffectiveProject, Elimination, MavenData
from .usage import MAX_RESOURCE_BYTES, RESOURCE_EXT, SOURCE_EXT, SOURCE_LANGS, JarIndex

_IMPORT_LINE_RE = re.compile(r"^\s*import\s+(static\s+)?([\w.]+?)(\.\*)?\s*;?\s*$", re.MULTILINE)
_PACKAGE_LINE_RE = re.compile(r"^\s*package\s+[\w.]+\s*;?\s*$", re.MULTILINE)
_FQN_RE = re.compile(r"\b((?:[a-z_][a-z0-9_]*\.)+[A-Z][\w]*)")

TRIVIAL = (2, 10, 3)  # classes, references, files
LOW = (5, 40)
MEDIUM = (15, 150)


@dataclass
class SourceFile:
    project: str
    kind: str  # main | test
    text: str
    resource: bool
    imports: list[tuple[bool, str, bool]] = field(default_factory=list)  # (static, name, wildcard)
    body: str = ""


def _read(path: Path) -> str:
    try:
        if path.stat().st_size > MAX_RESOURCE_BYTES:
            return ""
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def load_sources(project: EffectiveProject) -> list[SourceFile]:
    files: list[SourceFile] = []
    for kind in ("main", "test"):
        for lang in SOURCE_LANGS:
            base = project.base_dir / "src" / kind / lang
            if base.is_dir():
                for path in base.rglob("*"):
                    if path.suffix in SOURCE_EXT and path.is_file():
                        text = _read(path)
                        sf = SourceFile(project.name, kind, text, False)
                        sf.imports = [(bool(m.group(1)), m.group(2), bool(m.group(3)))
                                      for m in _IMPORT_LINE_RE.finditer(text)]
                        sf.body = _PACKAGE_LINE_RE.sub("", _IMPORT_LINE_RE.sub("", text))
                        files.append(sf)
        res = project.base_dir / "src" / kind / "resources"
        if res.is_dir():
            for path in res.rglob("*"):
                if path.is_file() and (path.suffix.lower() in RESOURCE_EXT or "services" in path.parts):
                    text = _read(path) + ("\n" + path.name if "services" in path.parts else "")
                    files.append(SourceFile(project.name, kind, text, True, body=text))
    return files


@dataclass
class Footprint:
    classes: set[str] = field(default_factory=set)
    members: Counter = field(default_factory=Counter)
    files: int = 0
    references: int = 0
    main: Counter = field(default_factory=Counter)
    test: Counter = field(default_factory=Counter)


def footprint(files: list[SourceFile], classes: set[str]) -> Footprint:
    fp = Footprint()
    if not classes:
        return fp
    packages = {c.rsplit(".", 1)[0] for c in classes}
    by_package: dict[str, set[str]] = defaultdict(set)
    for c in classes:
        by_package[c.rsplit(".", 1)[0]].add(c)

    for sf in files:
        used: dict[str, str] = {}  # simple name -> fqn
        static_members: dict[str, str] = {}  # member -> owner fqn
        refs = 0
        if not sf.resource:
            for is_static, name, wildcard in sf.imports:
                if is_static:
                    owner, member = (name, "*") if wildcard else name.rsplit(".", 1)
                    if owner in classes:
                        used[owner.rsplit(".", 1)[1]] = owner
                        if member != "*":
                            static_members[member] = owner
                elif wildcard:
                    if name in packages:
                        for fqn in by_package[name]:
                            simple = fqn.rsplit(".", 1)[1]
                            if re.search(rf"\b{re.escape(simple)}\b", sf.body):
                                used[simple] = fqn
                elif name in classes:
                    used[name.rsplit(".", 1)[1]] = name
        fqn_hits = [m for m in _FQN_RE.findall(sf.body) if m in classes]
        for fqn in fqn_hits:
            fp.classes.add(fqn)
            refs += 1
        for simple, fqn in used.items():
            fp.classes.add(fqn)
            pattern = re.compile(rf"(?<![\w.]){re.escape(simple)}\b(?:\s*\.\s*(\w+))?")
            count = 0
            for m in pattern.finditer(sf.body):
                count += 1
                if m.group(1) and m.group(1) != "class":
                    fp.members[f"{simple}.{m.group(1)}"] += 1
            refs += max(count, 1)  # an import alone still counts as one reference
        for member, owner in static_members.items():
            calls = len(re.findall(rf"(?<![\w.]){re.escape(member)}\s*\(", sf.body))
            if calls:
                fp.members[f"{owner.rsplit('.', 1)[1]}.{member}"] += calls
                refs += calls
        if refs:
            fp.files += 1
            fp.references += refs
            (fp.main if sf.kind == "main" else fp.test)[sf.project] += refs
    return fp


def rate(fp: Footprint, keep: str | None) -> str:
    if fp.references == 0:
        return "none"
    if keep:
        return "keep"
    n_classes, refs, files = len(fp.classes), fp.references, fp.files
    if n_classes <= TRIVIAL[0] and refs <= TRIVIAL[1] and files <= TRIVIAL[2]:
        return "trivial"
    if n_classes <= LOW[0] and refs <= LOW[1]:
        return "low"
    if n_classes <= MEDIUM[0] and refs <= MEDIUM[1]:
        return "medium"
    return "high"


def _brings(ga: str, projects: list[EffectiveProject], maven: MavenData | None) -> int | None:
    """Artifacts that leave the workspace if ``ga`` is removed from every scanned POM.

    Edges from internal projects are cut as well (we can edit those POMs), edges
    from third-party artifacts are not (the library may still come transitively).
    """
    if not maven or not maven.trees:
        return None
    internal = frozenset(p.ga for p in projects)
    before: set[str] = set()
    after: set[str] = set()
    for p in projects:
        tree = maven.trees.get(p.ga)
        if tree is None:
            continue
        before |= tree.reachable()
        after |= tree.reachable(skip_direct=ga, controlled=internal)
    return len(before - after)


def analyze_elimination(
    projects: list[EffectiveProject],
    jars: JarIndex,
    maven: MavenData | None,
) -> dict[str, Elimination]:
    sources = {p.name: load_sources(p) for p in projects if p.packaging != "pom"}
    all_files = [f for files in sources.values() for f in files]

    declared: dict[str, dict] = {}
    for p in projects:
        for dep in p.dependencies:
            if dep.internal or dep.type == "pom" or dep.scope == "import":
                continue
            entry = declared.setdefault(dep.ga, {"scopes": set(), "declared_in": set(), "forced_on": set(),
                                                 "version": dep.version, "classifier": dep.classifier})
            entry["scopes"].add(dep.scope)
            entry["declared_in"].add(dep.declared_in)
            if dep.inherited:
                entry["forced_on"].add(p.name)

    result: dict[str, Elimination] = {}
    for ga, entry in sorted(declared.items()):
        g, a = ga.split(":", 1)
        classes = jars.classes(g, a, entry["version"], entry["classifier"])
        keep = keep_category(ga)
        hint = replacement_hint(ga)
        if classes is None:
            result[ga] = Elimination(
                ga=ga, scopes=sorted(entry["scopes"]), declared_in=sorted(entry["declared_in"]),
                forced_on=sorted(entry["forced_on"]), used_by_main={}, used_by_test={}, classes=[], members={},
                files=0, references=0, brings=_brings(ga, projects, maven), effort="unknown",
                keep_reason=keep or "", hint=hint,
                detail="artifact not in the local Maven repository (run with --maven to download it)",
            )
            continue
        fp = footprint(all_files, classes)
        effort = rate(fp, keep)
        result[ga] = Elimination(
            ga=ga,
            scopes=sorted(entry["scopes"]),
            declared_in=sorted(entry["declared_in"]),
            forced_on=sorted(entry["forced_on"]),
            used_by_main=dict(sorted(fp.main.items())),
            used_by_test=dict(sorted(fp.test.items())),
            classes=sorted(fp.classes),
            members=dict(fp.members.most_common()),
            files=fp.files,
            references=fp.references,
            brings=_brings(ga, projects, maven),
            effort=effort,
            keep_reason=keep or "",
            hint=hint,
            detail="" if classes else "artifact contains no classes",
        )
    return result
