"""Static, Maven-free detection of unused dependencies.

How it works:

1. Collect every dotted Java name referenced by the project: ``import``
   statements and fully-qualified names in ``src/{main,test}/{java,kotlin,groovy,scala}``,
   plus fully-qualified class names that appear in text resources
   (``META-INF/services``, Spring XML / ``spring.factories``, ``logback.xml``,
   ``application.yml``, ...).
2. Find the packages each dependency provides:
   * internal dependency (another scanned project) -> packages of its sources,
   * external dependency -> class entries of its jar in the local Maven
     repository (``~/.m2/repository``), using the resolved version when
     available and any cached version otherwise,
   * jar without classes (an "aggregator" such as ``junit-jupiter`` or a Spring
     Boot starter) -> the packages of the artifacts its own POM depends on.
3. A dependency is *used* when any referenced name lies in one of its packages.

This is a heuristic - reflection, ``Class.forName`` with computed names or
annotation processors can hide real usages. Prefer ``--maven-analyze`` (bytecode
analysis by ``maven-dependency-plugin``) when the projects can be compiled.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .models import EffectiveDependency, EffectiveProject, Usage, UsageInfo

SOURCE_LANGS = ("java", "kotlin", "groovy", "scala")
SOURCE_EXT = {".java", ".kt", ".kts", ".groovy", ".scala"}
RESOURCE_EXT = {
    ".xml", ".properties", ".yml", ".yaml", ".json", ".conf", ".factories",
    ".imports", ".txt", ".xsd", ".vm", ".ftl", ".sql", ".jsp", ".html",
}
MAX_RESOURCE_BYTES = 2_000_000

_IMPORT_RE = re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+)(?:\.\*)?\s*;?", re.MULTILINE)
_FQN_RE = re.compile(r"\b((?:[a-z_][a-z0-9_]*\.){1,}[A-Za-z_$][\w$]*)")
_PACKAGE_RE = re.compile(r"^\s*package\s+([\w.]+)", re.MULTILINE)


@dataclass
class References:
    main: set[str] = field(default_factory=set)
    test: set[str] = field(default_factory=set)
    own_packages: set[str] = field(default_factory=set)
    has_sources: bool = False


def _read(path: Path) -> str:
    try:
        if path.stat().st_size > MAX_RESOURCE_BYTES:
            return ""
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def _scan_tree(base: Path, refs: set[str], own: set[str], resources: bool) -> bool:
    found = False
    if not base.is_dir():
        return False
    for path in base.rglob("*"):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        in_services = "META-INF" in path.parts and "services" in path.parts
        if not resources and suffix in SOURCE_EXT:
            text = _read(path)
            found = True
            refs.update(_IMPORT_RE.findall(text))
            refs.update(_FQN_RE.findall(text))
            own.update(_PACKAGE_RE.findall(text))
        elif resources and (suffix in RESOURCE_EXT or in_services):
            text = _read(path)
            refs.update(_FQN_RE.findall(text))
            if in_services:
                refs.add(path.name)  # the file name is the service interface FQN
    return found


def collect_references(project_dir: Path) -> References:
    refs = References()
    for kind, bucket in (("main", refs.main), ("test", refs.test)):
        for lang in SOURCE_LANGS:
            if _scan_tree(project_dir / "src" / kind / lang, bucket, refs.own_packages, resources=False):
                refs.has_sources = True
        _scan_tree(project_dir / "src" / kind / "resources", bucket, refs.own_packages, resources=True)
    return refs


def source_packages(project_dir: Path) -> set[str]:
    packages: set[str] = set()
    for lang in SOURCE_LANGS:
        base = project_dir / "src" / "main" / lang
        if base.is_dir():
            for path in base.rglob("*"):
                if path.suffix in SOURCE_EXT:
                    packages.update(_PACKAGE_RE.findall(_read(path)))
    return packages


def _prefixes(name: str) -> list[str]:
    parts = name.split(".")
    return [".".join(parts[:i]) for i in range(1, len(parts))]


def _matches(refs: set[str], packages: set[str]) -> bool:
    if not packages:
        return False
    for ref in refs:
        for prefix in _prefixes(ref):
            if prefix in packages:
                return True
    return False


class JarIndex:
    """Lazily maps g:a(:v) to the Java packages contained in the jar in ~/.m2."""

    def __init__(self, repo: Path | None):
        self.repo = repo
        self._cache: dict[str, set[str] | None] = {}

    def _artifact_dir(self, group_id: str, artifact_id: str) -> Path | None:
        if not self.repo:
            return None
        return self.repo.joinpath(*group_id.split("."), artifact_id)

    def _pick_file(self, group_id: str, artifact_id: str, version: str | None, ext: str,
                   classifier: str | None = None) -> Path | None:
        base = self._artifact_dir(group_id, artifact_id)
        if base is None or not base.is_dir():
            return None
        suffix = f"-{classifier}" if classifier else ""
        if version:
            exact = base / version / f"{artifact_id}-{version}{suffix}{ext}"
            if exact.is_file():
                return exact
        for vdir in sorted((d for d in base.iterdir() if d.is_dir()), key=lambda d: d.stat().st_mtime, reverse=True):
            candidate = vdir / f"{artifact_id}-{vdir.name}{suffix}{ext}"
            if candidate.is_file():
                return candidate
        return None

    def classes(self, group_id: str, artifact_id: str, version: str | None,
                classifier: str | None = None, depth: int = 0) -> set[str] | None:
        """Top-level class FQNs provided by the artifact; ``None`` when not available locally.

        A jar without classes (aggregator such as ``junit-jupiter`` or a Spring Boot
        starter) or a pom-only artifact contributes the classes of its own
        non-test, non-optional dependencies (two levels deep).
        """
        key = f"{group_id}:{artifact_id}:{version}:{classifier}"
        if key in self._cache:
            return self._cache[key]
        self._cache[key] = None  # guards recursion
        jar = self._pick_file(group_id, artifact_id, version, ".jar", classifier)
        result: set[str] | None = None
        if jar is not None:
            result = set()
            try:
                with zipfile.ZipFile(jar) as zf:
                    for name in zf.namelist():
                        if not name.endswith(".class") or "/" not in name:
                            continue
                        if name.startswith("META-INF/versions/"):
                            name = name.split("/", 3)[-1]
                        fqn = name[:-6].replace("/", ".")
                        if "$" in fqn or fqn.endswith("module-info") or fqn.endswith("package-info"):
                            continue
                        result.add(fqn)
            except (zipfile.BadZipFile, OSError):
                result = None
        if (result is not None and not result) or (result is None and jar is None):
            if depth < 2:
                pom = self._pick_file(group_id, artifact_id, version, ".pom")
                if pom is not None:
                    agg: set[str] = set()
                    for g, a, v in _pom_compile_deps(pom):
                        agg |= self.classes(g, a, v, None, depth + 1) or set()
                    if agg or result is not None:
                        result = agg
        self._cache[key] = result
        return result

    def packages(self, group_id: str, artifact_id: str, version: str | None,
                 classifier: str | None = None) -> set[str] | None:
        classes = self.classes(group_id, artifact_id, version, classifier)
        if classes is None:
            return None
        return {c.rsplit(".", 1)[0] for c in classes if "." in c}


def _pom_compile_deps(pom: Path) -> list[tuple[str, str, str | None]]:
    try:
        root = ET.parse(pom).getroot()
    except (ET.ParseError, OSError):
        return []
    ns = root.tag[: root.tag.index("}") + 1] if root.tag.startswith("{") else ""
    deps = []
    for dep in root.findall(f"{ns}dependencies/{ns}dependency"):
        scope = (dep.findtext(f"{ns}scope") or "compile").strip()
        optional = (dep.findtext(f"{ns}optional") or "").strip() == "true"
        if scope in ("test", "provided") or optional:
            continue
        g = (dep.findtext(f"{ns}groupId") or "").strip()
        a = (dep.findtext(f"{ns}artifactId") or "").strip()
        v = (dep.findtext(f"{ns}version") or "").strip() or None
        if v and "${" in v:
            v = None
        if g and a and "${" not in g:
            deps.append((g, a, v))
    return deps


def classify(
    dep: EffectiveDependency,
    refs: References,
    jars: JarIndex,
    internal_packages: dict[str, set[str]],
) -> UsageInfo:
    if dep.type == "pom" or dep.scope == "import":
        return UsageInfo(Usage.NOT_APPLICABLE, "pom-type dependency (brings only its dependencies)")
    if dep.scope == "system":
        return UsageInfo(Usage.UNKNOWN, "system scope")

    if dep.internal:
        packages = internal_packages.get(dep.ga, set())
        if not packages:
            return UsageInfo(Usage.UNKNOWN, "internal project without sources")
    else:
        packages = jars.packages(dep.group_id, dep.artifact_id, dep.version, dep.classifier)
        if packages is None:
            return UsageInfo(Usage.UNKNOWN, "artifact not found in local Maven repository")
        if not packages:
            return UsageInfo(Usage.UNKNOWN, "artifact contains no classes")
    packages = packages - refs.own_packages if not dep.internal else packages

    in_main = _matches(refs.main, packages)
    in_test = _matches(refs.test, packages)
    if in_main:
        return UsageInfo(Usage.USED, "referenced from main code/resources")
    if in_test:
        return UsageInfo(Usage.TEST_ONLY, "referenced only from tests")
    if dep.scope == "runtime":
        return UsageInfo(Usage.RUNTIME, "runtime scope - not referenced, loaded reflectively?")
    return UsageInfo(Usage.UNUSED, "no reference to any of its packages found")


def analyze_usage(
    projects: list[EffectiveProject],
    repo: Path | None,
) -> dict[str, dict[str, UsageInfo]]:
    """Return ``{project GA: {dependency key: UsageInfo}}`` for projects that have sources."""
    jars = JarIndex(repo)
    internal_packages = {p.ga: source_packages(p.base_dir) for p in projects}
    result: dict[str, dict[str, UsageInfo]] = {}
    for project in projects:
        if project.packaging == "pom":
            continue
        refs = collect_references(project.base_dir)
        if not refs.has_sources:
            continue
        result[project.ga] = {dep.key: classify(dep, refs, jars, internal_packages) for dep in project.dependencies}
    return result


def default_local_repo() -> Path | None:
    """``~/.m2/repository`` unless ``~/.m2/settings.xml`` overrides ``localRepository``."""
    home = Path.home()
    settings = home / ".m2" / "settings.xml"
    if settings.is_file():
        try:
            root = ET.parse(settings).getroot()
            ns = root.tag[: root.tag.index("}") + 1] if root.tag.startswith("{") else ""
            custom = (root.findtext(f"{ns}localRepository") or "").strip()
            if custom and "${" not in custom:
                return Path(custom).expanduser()
        except (ET.ParseError, OSError):
            pass
    repo = home / ".m2" / "repository"
    return repo if repo.is_dir() else None
