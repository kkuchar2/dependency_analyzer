"""Data model shared by every stage of the analysis.

Three layers of data exist:

* **Raw** (``RawProject``, ``RawDependency``, ``RawPlugin``) - exactly what a
  single ``pom.xml`` says, strings not yet interpolated.
* **Effective** (``EffectiveProject``, ``EffectiveDependency``,
  ``EffectivePlugin``) - after parent inheritance, property interpolation and
  ``dependencyManagement`` / ``pluginManagement`` have been applied, using the
  *local* copies of the parent POMs.
* **Resolved** (``TreeNode``, ``MavenData``) - optional data produced by
  running real Maven (full transitive tree, all build plugins, bytecode usage).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

DEFAULT_PLUGIN_GROUP = "org.apache.maven.plugins"

TEST_SCOPES = {"test"}
SHIPPED_SCOPES = {"compile", "runtime", "system"}  # end up in the runtime classpath


# --------------------------------------------------------------------------- raw


@dataclass
class RawDependency:
    group_id: str
    artifact_id: str
    version: str | None = None
    scope: str | None = None
    type: str = "jar"
    classifier: str | None = None
    optional: bool = False
    exclusions: list[str] = field(default_factory=list)
    profile: str | None = None  # id of the <profile> it was declared in


@dataclass
class RawPlugin:
    group_id: str
    artifact_id: str
    version: str | None = None
    inherited: bool = True
    dependencies: list[RawDependency] = field(default_factory=list)
    profile: str | None = None


@dataclass
class ParentRef:
    group_id: str
    artifact_id: str
    version: str | None
    relative_path: str | None = None

    @property
    def ga(self) -> str:
        return f"{self.group_id}:{self.artifact_id}"


@dataclass
class Profile:
    id: str
    active_by_default: bool = False
    dependencies: list[RawDependency] = field(default_factory=list)
    managed_dependencies: list[RawDependency] = field(default_factory=list)
    plugins: list[RawPlugin] = field(default_factory=list)
    properties: dict[str, str] = field(default_factory=dict)


@dataclass
class RawProject:
    pom_path: Path
    group_id: str
    artifact_id: str
    version: str | None
    packaging: str = "jar"
    parent: ParentRef | None = None
    properties: dict[str, str] = field(default_factory=dict)
    modules: list[str] = field(default_factory=list)
    dependencies: list[RawDependency] = field(default_factory=list)
    managed_dependencies: list[RawDependency] = field(default_factory=list)
    plugins: list[RawPlugin] = field(default_factory=list)
    managed_plugins: list[RawPlugin] = field(default_factory=list)
    profiles: list[Profile] = field(default_factory=list)

    @property
    def ga(self) -> str:
        return f"{self.group_id}:{self.artifact_id}"

    @property
    def gav(self) -> str:
        return f"{self.ga}:{self.version or '?'}"

    @property
    def base_dir(self) -> Path:
        return self.pom_path.parent

    @property
    def name(self) -> str:
        return self.artifact_id


# --------------------------------------------------------------------- effective


class Role(str, Enum):
    PARENT = "parent"
    LIB = "lib"  # inherits from the parent and is consumed by others / managed by parent
    APP = "app"  # inherits from the parent, nobody depends on it
    POM = "pom"  # pom-packaged descendant of parent (intermediate parent / aggregator / BOM)
    STANDALONE = "standalone"  # does not inherit from the selected parent


@dataclass
class EffectiveDependency:
    group_id: str
    artifact_id: str
    type: str
    classifier: str | None
    version: str | None  # effective version (None = unresolved statically)
    version_source: str  # "declared" | "managed by <artifactId>" | "unresolved" | ...
    declared_version: str | None  # the version literally written where it was declared
    scope: str
    optional: bool
    declared_in: str  # artifactId of the POM the <dependency> element lives in
    inherited: bool  # declared in an ancestor, not in the project itself
    internal: bool = False  # points at another scanned project
    profile: str | None = None
    exclusions: list[str] = field(default_factory=list)

    @property
    def ga(self) -> str:
        return f"{self.group_id}:{self.artifact_id}"

    @property
    def key(self) -> str:
        """Maven's uniqueness key for a dependency: g:a:type[:classifier]."""
        base = f"{self.ga}:{self.type}"
        return f"{base}:{self.classifier}" if self.classifier else base

    @property
    def gav(self) -> str:
        return f"{self.ga}:{self.version or '?'}"


@dataclass
class ManagedEntry:
    group_id: str
    artifact_id: str
    type: str
    classifier: str | None
    version: str | None
    scope: str | None
    declared_in: str
    imported_from: str | None = None  # BOM GA when it came from a scope=import BOM

    @property
    def ga(self) -> str:
        return f"{self.group_id}:{self.artifact_id}"

    @property
    def key(self) -> str:
        base = f"{self.ga}:{self.type}"
        return f"{base}:{self.classifier}" if self.classifier else base


@dataclass
class EffectivePlugin:
    group_id: str
    artifact_id: str
    version: str | None
    version_source: str  # "declared" | "pluginManagement in <artifactId>" | "maven default"
    declared_in: str
    inherited: bool
    dependencies: list[str] = field(default_factory=list)  # g:a:v of <plugin><dependencies>
    profile: str | None = None

    @property
    def ga(self) -> str:
        return f"{self.group_id}:{self.artifact_id}"


@dataclass
class EffectiveProject:
    raw: RawProject
    ancestors: list[RawProject]  # local ancestors, nearest first
    group_id: str
    artifact_id: str
    version: str | None
    packaging: str
    properties: dict[str, str]
    dependencies: list[EffectiveDependency]
    managed: dict[str, ManagedEntry]  # key -> entry (only explicit + local BOM imports)
    external_boms: list[str]  # g:a:v of scope=import BOMs not available locally
    plugins: list[EffectivePlugin]
    managed_plugins: dict[str, EffectivePlugin]  # ga -> entry
    profile_dependencies: list[EffectiveDependency]  # from non-default profiles
    role: Role = Role.STANDALONE
    inherits_from_parent: bool = False
    parent_version_declared: str | None = None  # version in <parent> (may be a CI release)
    parent_version_local: str | None = None  # version of the local checkout of that parent
    external_parent: str | None = None  # g:a:v of a <parent> not found locally
    duplicate_keys: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.artifact_id

    @property
    def ga(self) -> str:
        return f"{self.group_id}:{self.artifact_id}"

    @property
    def base_dir(self) -> Path:
        return self.raw.base_dir

    @property
    def parent_version_mismatch(self) -> bool:
        return bool(
            self.parent_version_declared
            and self.parent_version_local
            and self.parent_version_declared != self.parent_version_local
        )


# ---------------------------------------------------------------------- resolved


@dataclass
class TreeNode:
    group_id: str
    artifact_id: str
    type: str
    classifier: str | None
    version: str
    scope: str
    optional: bool = False
    omitted: bool = False  # verbose tree: "(... omitted for duplicate/conflict)" - a reference only
    children: list[TreeNode] = field(default_factory=list)

    @property
    def ga(self) -> str:
        return f"{self.group_id}:{self.artifact_id}"

    @property
    def gav(self) -> str:
        return f"{self.ga}:{self.version}"

    def walk(self, include_omitted: bool = False):
        """Descendants (depth first). Omitted references are skipped unless asked for."""
        for child in self.children:
            if child.omitted and not include_omitted:
                continue
            yield child
            yield from child.walk(include_omitted)

    def subtree_size(self) -> int:
        return sum(1 for _ in self.walk())

    def edges(self, scopes: set[str] | None = None):
        """GA-level edges ``(parent_ga, child_ga)`` of the full resolution graph,
        including omitted duplicates, optionally restricted to child scopes."""
        for child in self.children:
            if scopes is None or child.scope in scopes:
                yield self.ga, child.ga
                yield from child.edges(scopes)

    def reachable(self, scopes: set[str] | None = None, skip_direct: str | None = None,
                  controlled: set[str] | frozenset[str] = frozenset()) -> set[str]:
        """GAs reachable from this (root) node.

        ``skip_direct`` pretends that dependency was not declared by the root
        **and** by any project in ``controlled`` (GAs of internal projects whose
        POMs we can edit too); edges from third-party artifacts are kept.
        """
        graph: dict[str, set[str]] = {}
        for parent, child in self.edges(scopes):
            if child == skip_direct and parent in controlled:
                continue
            graph.setdefault(parent, set()).add(child)
        start = [c.ga for c in self.children
                 if c.ga != skip_direct and (scopes is None or c.scope in scopes)]
        seen: set[str] = set()
        while start:
            ga = start.pop()
            if ga in seen or ga == self.ga:
                continue
            seen.add(ga)
            start.extend(graph.get(ga, ()))
        return seen

    def removable_with(self, direct_ga: str, scopes: set[str] | None = None) -> set[str]:
        """Artifacts that leave the (scope-filtered) classpath if ``direct_ga`` is removed."""
        return self.reachable(scopes) - self.reachable(scopes, skip_direct=direct_ga)


@dataclass
class ResolvedPlugin:
    group_id: str
    artifact_id: str
    version: str
    dependencies: list[str] = field(default_factory=list)  # g:a:v

    @property
    def ga(self) -> str:
        return f"{self.group_id}:{self.artifact_id}"


@dataclass
class AnalyzeResult:
    """Output of ``maven-dependency-plugin:analyze-only`` for one project."""

    used_undeclared: list[str] = field(default_factory=list)  # g:a:type:version:scope
    unused_declared: list[str] = field(default_factory=list)
    non_test_scoped_test_only: list[str] = field(default_factory=list)


@dataclass
class MavenData:
    trees: dict[str, TreeNode] = field(default_factory=dict)  # project GA -> root node
    plugins: dict[str, list[ResolvedPlugin]] = field(default_factory=dict)  # project GA ->
    analyze: dict[str, AnalyzeResult] = field(default_factory=dict)  # artifactId -> result
    failed_projects: list[str] = field(default_factory=list)
    log_files: list[Path] = field(default_factory=list)
    command_lines: list[str] = field(default_factory=list)


# -------------------------------------------------------------------- analysis


class Usage(str, Enum):
    USED = "used"
    TEST_ONLY = "test-only"  # referenced only from src/test
    UNUSED = "unused"
    RUNTIME = "runtime-only"  # runtime scope: cannot be proven by source scanning
    UNKNOWN = "unknown"  # jar not available locally, or not a jar
    NOT_APPLICABLE = "n/a"  # pom / import entries


@dataclass
class UsageInfo:
    usage: Usage
    detail: str = ""


class Severity(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"

    @property
    def rank(self) -> int:
        return {"high": 0, "medium": 1, "low": 2, "info": 3}[self.value]


@dataclass
class Finding:
    rule: str
    severity: Severity
    project: str  # artifactId, or "*" for workspace-wide findings
    artifact: str  # g:a (or comma-separated list)
    message: str
    action: str
    source: str  # pom | static-usage | maven-tree | maven-analyze
    removable_artifacts: int = 0  # artifacts that disappear if the action is taken (estimate)


@dataclass
class InternalEdge:
    source: str
    target: str
    via: str  # "parent" | "dependency" | "dependencyManagement"
    scope: str
    version: str | None


@dataclass
class ProjectStats:
    artifact_id: str
    role: Role
    packaging: str
    direct: int = 0
    inherited: int = 0
    by_scope: dict[str, int] = field(default_factory=dict)
    internal: int = 0
    external: int = 0
    plugins: int = 0
    transitive_shipped: int | None = None  # unique artifacts on compile+runtime classpath
    transitive_test: int | None = None  # extra artifacts only on the test classpath
    plugin_artifacts: int | None = None  # plugins + their deps (build-time)
    unused: int = 0
    fan_in: int = 0
    fan_out: int = 0
    findings: int = 0


EFFORT_ORDER = ["none", "trivial", "low", "medium", "high", "keep"]


@dataclass
class Elimination:
    """Could this external library disappear from the whole workspace, and at what cost?"""

    ga: str
    scopes: list[str]  # effective scopes it is declared with
    declared_in: list[str]  # POMs whose <dependencies> contain it (parents included)
    forced_on: list[str]  # projects that get it only through inheritance
    used_by_main: dict[str, int]  # project -> references from src/main (+ main resources)
    used_by_test: dict[str, int]  # project -> references from src/test
    classes: list[str]  # distinct library classes referenced
    members: dict[str, int]  # "Class.member" -> call count
    files: int
    references: int
    brings: int | None  # artifacts that leave the workspace with it (needs --maven)
    effort: str  # one of EFFORT_ORDER
    keep_reason: str
    hint: str
    detail: str = ""

    @property
    def shipped(self) -> bool:
        return bool(set(self.scopes) & SHIPPED_SCOPES)


@dataclass
class AnalysisResult:
    root_dir: Path
    parent: EffectiveProject
    projects: list[EffectiveProject]
    usage: dict[str, dict[str, UsageInfo]]  # project ga -> dep key -> usage
    maven: MavenData | None
    edges: list[InternalEdge]
    cycles: list[list[str]]
    stats: list[ProjectStats]
    findings: list[Finding]
    warnings: list[str]
    options: dict[str, object]
    elimination: dict[str, Elimination] = field(default_factory=dict)  # ga ->
