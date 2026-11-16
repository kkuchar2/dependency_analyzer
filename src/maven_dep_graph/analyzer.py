"""Orchestrates scanning, effective models, usage detection, Maven and rules."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from . import rules
from .effective import ModelBuilder
from .maven_runner import MavenError, reactor_roots, run_maven
from .models import (
    SHIPPED_SCOPES,
    AnalysisResult,
    EffectiveProject,
    InternalEdge,
    MavenData,
    ProjectStats,
    Role,
    TreeNode,
)
from .scanner import module_children, resolve_parent, scan_projects
from .elimination import analyze_elimination
from .usage import JarIndex, analyze_usage, default_local_repo


class AnalysisError(RuntimeError):
    pass


@dataclass
class Options:
    root: Path
    parent: str
    static_usage: bool = True
    maven: bool = False
    maven_analyze: bool = False
    mvn: str | None = None
    maven_args: list[str] = field(default_factory=list)
    maven_timeout: int | None = None
    local_repo: Path | None = None
    log_dir: Path | None = None
    heavy_threshold: int = 10


# ------------------------------------------------------------------ graph


def build_internal_edges(projects: list[EffectiveProject], builder: ModelBuilder) -> list[InternalEdge]:
    by_ga = {p.ga: p for p in projects}
    edges: dict[tuple[str, str, str], InternalEdge] = {}

    def add(source: str, target: str, via: str, scope: str, version: str | None) -> None:
        if source != target:
            edges.setdefault((source, target, via), InternalEdge(source, target, via, scope, version))

    for project in projects:
        parent = builder.local_parent(project.raw)
        if parent is not None:
            add(project.name, parent.artifact_id, "parent", "-", project.raw.parent.version if project.raw.parent else None)
        for dep in project.dependencies:
            target = by_ga.get(dep.ga)
            if target is not None:
                add(project.name, target.name, "dependency", dep.scope, dep.version)
        for entry in project.managed.values():
            target = by_ga.get(entry.ga)
            if target is not None and entry.declared_in == project.name and not entry.imported_from:
                add(project.name, target.name, "dependencyManagement", entry.scope or "-", entry.version)
    return sorted(edges.values(), key=lambda e: (e.via, e.source, e.target))


def find_cycles(edges: list[InternalEdge]) -> list[list[str]]:
    """Simple cycles over parent + dependency edges (dependencyManagement is not a real dependency)."""
    graph: dict[str, set[str]] = defaultdict(set)
    for edge in edges:
        if edge.via != "dependencyManagement":
            graph[edge.source].add(edge.target)

    cycles: list[list[str]] = []
    keys: set[tuple[str, ...]] = set()
    path: list[str] = []
    on_path: set[str] = set()
    visited: set[str] = set()

    def dfs(node: str) -> None:
        visited.add(node)
        path.append(node)
        on_path.add(node)
        for nxt in sorted(graph.get(node, ())):
            if nxt in on_path:
                body = path[path.index(nxt):]
                start = min(range(len(body)), key=lambda i: body[i])
                key = tuple(body[start:] + body[:start])
                if key not in keys:
                    keys.add(key)
                    cycles.append([*key, key[0]])
            elif nxt not in visited:
                dfs(nxt)
        path.pop()
        on_path.discard(node)

    for node in sorted(graph):
        if node not in visited:
            dfs(node)
    return cycles


# ------------------------------------------------------------------ tree stats


def tree_artifacts(tree: TreeNode, scopes: set[str] | None = None) -> set[str]:
    return {n.ga for n in tree.walk() if scopes is None or n.scope in scopes}


def _assign_roles(projects: list[EffectiveProject], parent: EffectiveProject, edges: list[InternalEdge]) -> None:
    depended_on = {e.target for e in edges if e.via == "dependency"}
    managed_by_parent = {e.target for e in edges if e.via == "dependencyManagement" and e.source == parent.name}
    for project in projects:
        if project is parent:
            project.role = Role.PARENT
        elif project.packaging == "pom" and project.inherits_from_parent:
            project.role = Role.POM
        elif project.name in depended_on or project.name in managed_by_parent:
            project.role = Role.LIB
        elif project.inherits_from_parent:
            project.role = Role.APP
        else:
            project.role = Role.STANDALONE


def _link_parent(projects: list[EffectiveProject], parent: EffectiveProject, builder: ModelBuilder) -> None:
    for project in projects:
        if project is parent:
            continue
        lineage = [project.raw, *project.ancestors]
        for child, ancestor in zip(lineage, lineage[1:]):
            if ancestor.pom_path == parent.raw.pom_path:
                project.inherits_from_parent = True
                project.parent_version_declared = child.parent.version if child.parent else None
                project.parent_version_local = parent.raw.version
                break


def _stats(result: AnalysisResult) -> list[ProjectStats]:
    fan_in: dict[str, set[str]] = defaultdict(set)
    fan_out: dict[str, set[str]] = defaultdict(set)
    for edge in result.edges:
        if edge.via == "dependency":
            fan_out[edge.source].add(edge.target)
            fan_in[edge.target].add(edge.source)
    findings_per: dict[str, int] = defaultdict(int)
    unused_per: dict[str, int] = defaultdict(int)
    for f in result.findings:
        findings_per[f.project] += 1
        if f.rule == "unused-dependency":
            unused_per[f.project] += 1

    stats = []
    for p in result.projects:
        by_scope: dict[str, int] = defaultdict(int)
        for dep in p.dependencies:
            by_scope[dep.scope] += 1
        s = ProjectStats(
            artifact_id=p.name,
            role=p.role,
            packaging=p.packaging,
            direct=len(p.dependencies),
            inherited=sum(1 for d in p.dependencies if d.inherited),
            by_scope=dict(by_scope),
            internal=sum(1 for d in p.dependencies if d.internal),
            external=sum(1 for d in p.dependencies if not d.internal),
            plugins=len(p.plugins),
            unused=unused_per[p.name],
            fan_in=len(fan_in[p.name]),
            fan_out=len(fan_out[p.name]),
            findings=findings_per[p.name],
        )
        if result.maven:
            tree = result.maven.trees.get(p.ga)
            if tree is not None:
                shipped = tree_artifacts(tree, SHIPPED_SCOPES)
                s.transitive_shipped = len(shipped)
                s.transitive_test = len(tree_artifacts(tree) - shipped)
            resolved = result.maven.plugins.get(p.ga)
            if resolved is not None:
                arts = {pl.ga for pl in resolved}
                for pl in resolved:
                    arts.update(d.rsplit(":", 1)[0] for d in pl.dependencies)
                s.plugin_artifacts = len(arts)
        stats.append(s)
    stats.sort(key=lambda s: (s.role != Role.PARENT, -(s.transitive_shipped or 0), -s.direct, s.artifact_id))
    return stats


# ------------------------------------------------------------------ entry


def analyze(opts: Options, progress=print) -> AnalysisResult:
    warnings: list[str] = []
    root = opts.root.expanduser().resolve()
    raws = scan_projects(root, warnings)
    if not raws:
        raise AnalysisError(f"No pom.xml files found under {root}")
    try:
        parent_raw = resolve_parent(raws, opts.parent, warnings)
    except (LookupError, ValueError, OSError) as exc:
        raise AnalysisError(str(exc)) from exc

    builder = ModelBuilder(raws)
    projects = [builder.build(p) for p in raws]
    parent = next(p for p in projects if p.raw is parent_raw)

    internal_gas = {p.ga for p in projects}
    for project in projects:
        for dep in [*project.dependencies, *project.profile_dependencies]:
            dep.internal = dep.ga in internal_gas

    _link_parent(projects, parent, builder)
    edges = build_internal_edges(projects, builder)
    cycles = find_cycles(edges)
    _assign_roles(projects, parent, edges)

    repo = opts.local_repo or default_local_repo()
    usage = analyze_usage(projects, repo) if opts.static_usage else {}
    if opts.static_usage and repo is None:
        warnings.append("No local Maven repository found - static usage detection limited to internal projects.")

    maven: MavenData | None = None
    if opts.maven or opts.maven_analyze:
        roots = reactor_roots(raws, module_children(raws), warnings)
        try:
            maven = run_maven(
                roots,
                analyze=opts.maven_analyze,
                mvn=opts.mvn,
                extra_args=opts.maven_args,
                log_dir=opts.log_dir or Path.cwd(),
                timeout=opts.maven_timeout,
                progress=progress,
            )
        except MavenError as exc:
            warnings.append(str(exc))
        except Exception as exc:  # noqa: BLE001 - e.g. timeout; static results stay useful
            warnings.append(f"Maven run failed: {exc}")
        if maven is not None:
            maven.failed_projects = sorted(p.name for p in projects if p.ga not in maven.trees)
            if maven.failed_projects:
                warnings.append(
                    "Maven produced no dependency tree for: " + ", ".join(maven.failed_projects)
                    + f" (see {', '.join(str(f) for f in maven.log_files)})"
                )

    result = AnalysisResult(
        root_dir=root,
        parent=parent,
        projects=sorted(projects, key=lambda p: p.name),
        usage=usage,
        maven=maven,
        edges=edges,
        cycles=cycles,
        stats=[],
        findings=[],
        warnings=warnings,
        options={
            "static_usage": opts.static_usage,
            "maven": bool(maven),
            "maven_analyze": bool(maven and opts.maven_analyze),
            "local_repo": str(repo) if repo else None,
            "heavy_threshold": opts.heavy_threshold,
        },
    )
    if opts.static_usage:
        result.elimination = analyze_elimination(result.projects, JarIndex(repo), maven)
    result.findings = rules.run_all(result)
    result.stats = _stats(result)
    return result
