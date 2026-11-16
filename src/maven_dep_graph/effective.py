"""Build a static *effective model* for every scanned project.

This re-implements the parts of Maven's model building that matter for a
dependency inventory, using only the local checkouts:

* parent inheritance (``<dependencies>``, ``<build><plugins>``, properties,
  ``dependencyManagement``, ``pluginManagement``),
* property interpolation in the context of the child project,
* ``scope=import`` BOMs that are themselves scanned projects,
* ``activeByDefault`` profiles.

Parents are matched by ``groupId:artifactId`` **ignoring the version**: children
usually point at a parent version released by CI while the local checkout may be
newer. The mismatch is recorded so the report can flag it.
"""

from __future__ import annotations

import re
from pathlib import Path

from .models import (
    EffectiveDependency,
    EffectivePlugin,
    EffectiveProject,
    ManagedEntry,
    RawDependency,
    RawPlugin,
    RawProject,
)

_PROP_RE = re.compile(r"\$\{([^}]+)\}")


def interpolate(value: str | None, props: dict[str, str]) -> str | None:
    if value is None:
        return None
    for _ in range(10):
        new = _PROP_RE.sub(lambda m: props.get(m.group(1), m.group(0)), value)
        if new == value:
            break
        value = new
    return value


def _resolved(value: str | None) -> str | None:
    return None if value is None or "${" in value else value


class ModelBuilder:
    def __init__(self, projects: list[RawProject]):
        self.projects = projects
        self.by_ga: dict[str, list[RawProject]] = {}
        for project in projects:
            self.by_ga.setdefault(project.ga, []).append(project)
        self._cache: dict[Path, EffectiveProject] = {}
        self._building: set[Path] = set()

    # ------------------------------------------------------------- ancestry

    def local_parent(self, project: RawProject) -> RawProject | None:
        ref = project.parent
        if ref is None:
            return None
        candidates = self.by_ga.get(ref.ga, [])
        if not candidates:
            return None
        if len(candidates) > 1:
            rel = ref.relative_path if ref.relative_path is not None else "../pom.xml"
            target = (project.base_dir / rel)
            target = (target if target.name.endswith(".xml") else target / "pom.xml").resolve()
            for candidate in candidates:
                if candidate.pom_path == target:
                    return candidate
            for candidate in candidates:
                if candidate.version == ref.version:
                    return candidate
        return candidates[0]

    def ancestors(self, project: RawProject) -> tuple[list[RawProject], str | None]:
        """Local ancestors nearest-first, plus the g:a:v of a non-local root parent."""
        chain: list[RawProject] = []
        seen = {project.pom_path}
        current = project
        while current.parent is not None:
            parent = self.local_parent(current)
            if parent is None:
                ref = current.parent
                return chain, f"{ref.ga}:{ref.version or '?'}"
            if parent.pom_path in seen:  # malformed cycle
                break
            seen.add(parent.pom_path)
            chain.append(parent)
            current = parent
        return chain, None

    # ------------------------------------------------------------- building

    def build(self, project: RawProject) -> EffectiveProject:
        cached = self._cache.get(project.pom_path)
        if cached:
            return cached
        self._building.add(project.pom_path)
        try:
            eff = self._build(project)
        finally:
            self._building.discard(project.pom_path)
        self._cache[project.pom_path] = eff
        return eff

    def _properties(self, project: RawProject, chain: list[RawProject]) -> dict[str, str]:
        props: dict[str, str] = {}
        for p in reversed([project, *chain]):  # root first, child overrides
            props.update(p.properties)
            for profile in p.profiles:
                if profile.active_by_default:
                    props.update(profile.properties)
        builtins = {
            "groupId": project.group_id,
            "artifactId": project.artifact_id,
            "version": project.version or "",
            "packaging": project.packaging,
            "basedir": str(project.base_dir),
        }
        for key, value in builtins.items():
            props[f"project.{key}"] = value
            props[f"pom.{key}"] = value
        props["basedir"] = str(project.base_dir)
        if project.parent:
            props["project.parent.groupId"] = project.parent.group_id
            props["project.parent.artifactId"] = project.parent.artifact_id
            props["project.parent.version"] = project.parent.version or ""
        # Interpolate property values themselves (e.g. <a>${b}</a>).
        return {k: interpolate(v, props) or "" for k, v in props.items()}

    def _build(self, project: RawProject) -> EffectiveProject:
        chain, external_parent = self.ancestors(project)
        props = self._properties(project, chain)
        ip = lambda v: interpolate(v, props)  # noqa: E731

        lineage = [project, *chain]  # nearest first

        # --- dependencyManagement (child wins, then imports in declaration order)
        managed: dict[str, ManagedEntry] = {}
        imports: list[tuple[RawDependency, RawProject]] = []
        for p in lineage:
            entries = list(p.managed_dependencies)
            for profile in p.profiles:
                if profile.active_by_default:
                    entries.extend(profile.managed_dependencies)
            for raw in entries:
                scope = ip(raw.scope)
                if scope == "import" and raw.type == "pom":
                    imports.append((raw, p))
                    continue
                entry = ManagedEntry(
                    group_id=ip(raw.group_id) or raw.group_id,
                    artifact_id=ip(raw.artifact_id) or raw.artifact_id,
                    type=ip(raw.type) or "jar",
                    classifier=ip(raw.classifier),
                    version=_resolved(ip(raw.version)),
                    scope=scope,
                    declared_in=p.artifact_id,
                )
                managed.setdefault(entry.key, entry)

        external_boms: list[str] = []
        for raw, p in imports:
            ga = f"{ip(raw.group_id)}:{ip(raw.artifact_id)}"
            version = ip(raw.version) or "?"
            bom = self.by_ga.get(ga)
            if bom and bom[0].pom_path not in self._building:
                bom_eff = self.build(bom[0])
                for key, entry in bom_eff.managed.items():
                    if key not in managed:
                        managed[key] = ManagedEntry(**{**entry.__dict__, "imported_from": ga})
                external_boms.extend(bom_eff.external_boms)
            else:
                external_boms.append(f"{ga}:{version}")

        # --- dependencies (root first, child overrides by key)
        merged: dict[str, tuple[RawDependency, RawProject]] = {}
        duplicates: list[str] = []
        own_keys: set[str] = set()
        for p in reversed(lineage):
            raws = list(p.dependencies)
            for profile in p.profiles:
                if profile.active_by_default:
                    raws.extend(profile.dependencies)
            for raw in raws:
                key = self._dep_key(raw, ip)
                if p is project:
                    if key in own_keys:
                        duplicates.append(key)
                    own_keys.add(key)
                merged[key] = (raw, p)

        dependencies = [
            self._effective_dep(raw, p, project, managed, external_boms, external_parent, ip)
            for raw, p in merged.values()
        ]

        profile_deps = []
        for p in lineage:
            for profile in p.profiles:
                if profile.active_by_default:
                    continue
                for raw in profile.dependencies:
                    profile_deps.append(
                        self._effective_dep(raw, p, project, managed, external_boms, external_parent, ip)
                    )

        # --- plugins
        managed_plugins: dict[str, EffectivePlugin] = {}
        for p in lineage:
            for raw in p.managed_plugins:
                plugin = self._effective_plugin(raw, p, project, ip)
                managed_plugins.setdefault(plugin.ga, plugin)

        plugins: dict[str, EffectivePlugin] = {}
        for p in reversed(lineage):
            raws: list[RawPlugin] = list(p.plugins)
            for profile in p.profiles:
                if profile.active_by_default:
                    raws.extend(profile.plugins)
            for raw in raws:
                if p is not project and not raw.inherited:
                    continue
                plugin = self._effective_plugin(raw, p, project, ip)
                previous = plugins.get(plugin.ga)
                if previous and plugin.version is None:
                    plugin.version = previous.version
                    plugin.version_source = previous.version_source
                if previous:
                    plugin.dependencies = sorted(set(previous.dependencies) | set(plugin.dependencies))
                plugins[plugin.ga] = plugin
        for plugin in plugins.values():
            if plugin.version is None:
                mp = managed_plugins.get(plugin.ga)
                if mp and mp.version:
                    plugin.version = mp.version
                    plugin.version_source = f"pluginManagement in {mp.declared_in}"
                    plugin.dependencies = sorted(set(plugin.dependencies) | set(mp.dependencies))
                elif external_parent:
                    plugin.version_source = f"external parent {external_parent} (unknown)"
                else:
                    plugin.version_source = "Maven default (unpinned)"

        return EffectiveProject(
            raw=project,
            ancestors=chain,
            group_id=project.group_id,
            artifact_id=project.artifact_id,
            version=_resolved(ip(project.version)) or project.version,
            packaging=ip(project.packaging) or "jar",
            properties=props,
            dependencies=dependencies,
            managed=managed,
            external_boms=sorted(set(external_boms)),
            plugins=list(plugins.values()),
            managed_plugins=managed_plugins,
            profile_dependencies=profile_deps,
            external_parent=external_parent,
            duplicate_keys=sorted(set(duplicates)),
        )

    # ------------------------------------------------------------- helpers

    @staticmethod
    def _dep_key(raw: RawDependency, ip) -> str:
        base = f"{ip(raw.group_id)}:{ip(raw.artifact_id)}:{ip(raw.type) or 'jar'}"
        classifier = ip(raw.classifier)
        return f"{base}:{classifier}" if classifier else base

    @staticmethod
    def _effective_dep(
        raw: RawDependency,
        declared_in: RawProject,
        project: RawProject,
        managed: dict[str, ManagedEntry],
        external_boms: list[str],
        external_parent: str | None,
        ip,
    ) -> EffectiveDependency:
        group_id = ip(raw.group_id) or raw.group_id
        artifact_id = ip(raw.artifact_id) or raw.artifact_id
        dep_type = ip(raw.type) or "jar"
        classifier = ip(raw.classifier)
        declared_version = ip(raw.version)
        key = f"{group_id}:{artifact_id}:{dep_type}" + (f":{classifier}" if classifier else "")
        entry = managed.get(key)

        version = _resolved(declared_version)
        if declared_version:
            source = "declared" if version else f"declared, unresolved ({declared_version})"
        elif entry and entry.version:
            version = entry.version
            source = (
                f"managed by BOM {entry.imported_from}" if entry.imported_from else f"managed by {entry.declared_in}"
            )
        elif external_boms or external_parent:
            source = "managed externally (" + ", ".join(external_boms or [external_parent or ""]) + ")"
        else:
            source = "unresolved"

        scope = ip(raw.scope) or (entry.scope if entry and entry.scope else None) or "compile"
        return EffectiveDependency(
            group_id=group_id,
            artifact_id=artifact_id,
            type=dep_type,
            classifier=classifier,
            version=version,
            version_source=source,
            declared_version=declared_version,
            scope=scope,
            optional=raw.optional,
            declared_in=declared_in.artifact_id,
            inherited=declared_in is not project,
            profile=raw.profile,
            exclusions=list(raw.exclusions),
        )

    @staticmethod
    def _effective_plugin(raw: RawPlugin, declared_in: RawProject, project: RawProject, ip) -> EffectivePlugin:
        version = _resolved(ip(raw.version))
        deps = [f"{ip(d.group_id)}:{ip(d.artifact_id)}:{ip(d.version) or '?'}" for d in raw.dependencies]
        return EffectivePlugin(
            group_id=ip(raw.group_id) or raw.group_id,
            artifact_id=ip(raw.artifact_id) or raw.artifact_id,
            version=version,
            version_source="declared" if version else "",
            declared_in=declared_in.artifact_id,
            inherited=declared_in is not project,
            dependencies=deps,
            profile=raw.profile,
        )


def build_effective(projects: list[RawProject]) -> list[EffectiveProject]:
    builder = ModelBuilder(projects)
    return [builder.build(p) for p in projects]
