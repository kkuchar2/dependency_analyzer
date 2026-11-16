"""Reduction rules: turn the collected data into actionable findings.

Every rule is a function ``(ctx) -> list[Finding]``. Rules prefer the most
reliable evidence available for a project:

``maven-analyze`` (bytecode) > ``static-usage`` (sources vs. jar packages) > ``pom``.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from .knowledge import LOW_PRIORITY_GROUPS, is_test_library, overlap_members
from .models import (
    SHIPPED_SCOPES,
    AnalysisResult,
    AnalyzeResult,
    EffectiveDependency,
    EffectiveProject,
    Finding,
    Severity,
    TreeNode,
    Usage,
)


@dataclass
class Ctx:
    result: AnalysisResult

    @property
    def projects(self) -> list[EffectiveProject]:
        return self.result.projects

    def tree(self, project: EffectiveProject) -> TreeNode | None:
        return self.result.maven.trees.get(project.ga) if self.result.maven else None

    def analyze(self, project: EffectiveProject) -> AnalyzeResult | None:
        if not self.result.maven or not self.result.options.get("maven_analyze"):
            return None
        return self.result.maven.analyze.get(project.name)

    def tree_node(self, project: EffectiveProject, ga: str) -> TreeNode | None:
        tree = self.tree(project)
        if tree is None:
            return None
        return next((c for c in tree.children if c.ga == ga), None)

    def removable(self, project: EffectiveProject, ga: str, scopes: set[str] | None = None) -> int:
        """How many artifacts leave the classpath if the direct dependency ``ga`` goes.

        Exact when a (verbose) Maven tree is available: artifacts still reachable
        through other dependencies are not counted. Otherwise 1.
        """
        tree = self.tree(project)
        if tree is None:
            return 1
        return len(tree.removable_with(ga, scopes))

    def descendants(self, project: EffectiveProject) -> list[EffectiveProject]:
        """Projects inheriting from ``project`` (itself included) or importing it as a BOM."""
        result = []
        for p in self.projects:
            if p is project or any(a.pom_path == project.raw.pom_path for a in p.ancestors):
                result.append(p)
            elif any(e.imported_from == project.ga for e in p.managed.values()):
                result.append(p)
        return result

    def used_through(self, project: EffectiveProject, ga: str, analyze: AnalyzeResult) -> list[str]:
        """Used-undeclared artifacts that arrive through ``ga`` (aggregator pattern)."""
        node = self.tree_node(project, ga)
        if node is None:
            return []
        undeclared = {_ga(x) for x in analyze.used_undeclared}
        return sorted({n.ga for n in node.walk(include_omitted=True)} & undeclared)

    def unused_status(self, project: EffectiveProject, dep: EffectiveDependency) -> tuple[bool | None, str]:
        """(unused?, evidence). ``None`` when nothing is known."""
        analyze = self.analyze(project)
        if analyze is not None:
            unused = {_ga(x) for x in analyze.unused_declared}
            if dep.ga in unused and self.used_through(project, dep.ga, analyze):
                return False, "maven-analyze"  # aggregator: used via its transitive deps
            return dep.ga in unused, "maven-analyze"
        info = self.result.usage.get(project.ga, {}).get(dep.key)
        if info is None or info.usage in (Usage.UNKNOWN, Usage.NOT_APPLICABLE, Usage.RUNTIME):
            return None, "static-usage"
        return info.usage == Usage.UNUSED, "static-usage"


def _ga(coord: str) -> str:
    parts = coord.split(":")
    return f"{parts[0]}:{parts[1]}" if len(parts) >= 2 else coord


def _scope_of(coord: str) -> str:
    return coord.rsplit(":", 1)[-1]


def _sev_for_scope(scope: str) -> Severity:
    if scope in ("compile", "system"):
        return Severity.HIGH
    if scope in ("runtime", "provided"):
        return Severity.MEDIUM
    return Severity.LOW


def _unused_severity(ctx: Ctx, p: EffectiveProject, scope: str, gain: int) -> tuple[Severity, str]:
    """Unused deps that still arrive transitively do not change exposure -> low."""
    if ctx.tree(p) is not None and gain == 0:
        return Severity.LOW, " It still arrives transitively, so removing it only tidies the POM."
    return _sev_for_scope(scope), ""


# ----------------------------------------------------------------- rules


def rule_unused(ctx: Ctx) -> list[Finding]:
    """Directly declared dependencies that the code never references."""
    out: list[Finding] = []
    for p in ctx.projects:
        own = [d for d in p.dependencies if not d.inherited]
        analyze = ctx.analyze(p)
        if analyze is not None:
            for coord in analyze.unused_declared:
                ga = _ga(coord)
                dep = next((d for d in own if d.ga == ga), None)
                if dep is None:
                    continue  # inherited -> rule_inherited_unused
                scope = _scope_of(coord)
                via = ctx.used_through(p, ga, analyze)
                if via:
                    out.append(Finding(
                        "aggregator-dependency", Severity.LOW, p.name, ga,
                        f"{ga} is not used directly; the code uses its transitive {', '.join(via)}.",
                        f"Declare {', '.join(via)} directly and drop {ga} if the rest of what it brings is not needed.",
                        "maven-analyze", 0,
                    ))
                    continue
                if scope == "runtime":
                    out.append(Finding(
                        "unused-dependency", Severity.LOW, p.name, ga,
                        f"{ga} (runtime) is not referenced by bytecode - runtime deps are often loaded reflectively.",
                        "Verify it is still needed at runtime (driver, logging backend, SPI impl...); remove if not.",
                        "maven-analyze", ctx.removable(p, ga),
                    ))
                    continue
                gain = ctx.removable(p, ga)
                sev, note = _unused_severity(ctx, p, scope, gain)
                out.append(Finding(
                    "unused-dependency", sev, p.name, ga,
                    f"{ga} ({scope}) is declared but never used by compiled classes.{note}",
                    f"Remove the <dependency> on {ga} from {p.name}/pom.xml.",
                    "maven-analyze", gain,
                ))
            continue

        infos = ctx.result.usage.get(p.ga)
        if not infos:
            continue
        for dep in own:
            info = infos.get(dep.key)
            if info is None or info.usage != Usage.UNUSED:
                continue
            gain = ctx.removable(p, dep.ga)
            sev, note = _unused_severity(ctx, p, dep.scope, gain)
            out.append(Finding(
                "unused-dependency", sev, p.name, dep.ga,
                f"{dep.ga} ({dep.scope}) - no reference to its packages in sources/resources (static scan).{note}",
                f"Remove the <dependency> on {dep.ga} from {p.name}/pom.xml (run with --maven-analyze to confirm).",
                "static-usage", gain,
            ))
    return out


def rule_test_scope(ctx: Ctx) -> list[Finding]:
    """Test-only libraries that end up on the shipped (compile/runtime) classpath."""
    seen: set[tuple[str, str]] = set()
    out: list[Finding] = []

    def add(p: EffectiveProject, ga: str, scope: str, why: str, source: str) -> None:
        if (p.name, ga) in seen:
            return
        seen.add((p.name, ga))
        dep = next((d for d in p.dependencies if d.ga == ga), None)
        where = dep.declared_in if dep else p.name
        out.append(Finding(
            "test-dependency-in-shipped-scope", Severity.HIGH, p.name, ga,
            f"{ga} has scope '{scope}' but {why} - it is shipped with the artifact and leaks to every consumer.",
            f"Set <scope>test</scope> on {ga} in {where}/pom.xml.",
            source, ctx.removable(p, ga, SHIPPED_SCOPES),
        ))

    for p in ctx.projects:
        analyze = ctx.analyze(p)
        if analyze is not None:
            for coord in analyze.non_test_scoped_test_only:
                add(p, _ga(coord), _scope_of(coord), "it is used only by test classes", "maven-analyze")
        infos = ctx.result.usage.get(p.ga, {})
        for dep in p.dependencies:
            if dep.scope not in SHIPPED_SCOPES:
                continue
            info = infos.get(dep.key)
            if analyze is None and info is not None and info.usage == Usage.TEST_ONLY:
                add(p, dep.ga, dep.scope, "it is referenced only from src/test", "static-usage")
            elif is_test_library(dep.ga):
                add(p, dep.ga, dep.scope, "it is a test library", "pom")
    return out


def _elimination_note(ctx: Ctx, ga: str) -> str:
    e = ctx.result.elimination.get(ga)
    if e is None or e.effort in ("none", "unknown"):
        return ""
    if e.effort == "keep":
        return f" Elimination: keep ({e.keep_reason})."
    return (f" Elimination effort: {e.effort} ({len(e.classes)} class(es), {e.references} reference(s) "
            f"in {e.files} file(s)) - see eliminate-dependency.")


def rule_inherited_unused(ctx: Ctx) -> list[Finding]:
    """Dependencies declared in a parent's <dependencies> are forced on every child.

    Redistributing such a dependency to the children that use it does not
    reduce exposure (the library is still shipped), so the recommendation is
    either to drop it (nobody uses it) or to eliminate it entirely
    (see :func:`rule_eliminate`).
    """
    out: list[Finding] = []
    declared: dict[tuple[str, str], list[tuple[EffectiveProject, EffectiveDependency]]] = defaultdict(list)
    for p in ctx.projects:
        for dep in p.dependencies:
            if dep.inherited:
                declared[(dep.declared_in, dep.key)].append((p, dep))

    for (owner, key), heirs in sorted(declared.items()):
        dep0 = heirs[0][1]
        unused, unknown, used = [], [], []
        evidence = set()
        for p, dep in heirs:
            if p.packaging == "pom":
                continue
            status, source = ctx.unused_status(p, dep)
            evidence.add(source)
            (unknown if status is None else unused if status else used).append(p.name)
        heir_names = [p.name for p, _ in heirs if p.packaging != "pom"]
        if not heir_names:
            continue
        gain = sum(ctx.removable(p, dep0.ga) for p, _ in heirs if p.name in unused)
        shipped = dep0.scope in SHIPPED_SCOPES
        note = _elimination_note(ctx, dep0.ga)
        if unused and not used and not unknown:
            out.append(Finding(
                "inherited-unused", Severity.HIGH if shipped else Severity.MEDIUM, owner, dep0.ga,
                f"{dep0.ga} ({dep0.scope}) is forced on {len(heir_names)} project(s) by {owner} and none uses it.",
                f"Delete {dep0.ga} from <dependencies> of {owner}/pom.xml (and from its dependencyManagement "
                "if nothing else needs the managed version).",
                "+".join(sorted(evidence)), gain,
            ))
        elif unused:
            e = ctx.result.elimination.get(dep0.ga)
            cheap = e is not None and e.effort in ("trivial", "low")
            out.append(Finding(
                "inherited-unused",
                (Severity.HIGH if shipped else Severity.MEDIUM) if cheap else Severity.LOW,
                owner, dep0.ga,
                f"{dep0.ga} ({dep0.scope}) is forced on {len(heir_names)} project(s) by {owner}; used only by "
                f"{', '.join(sorted(used))}, unused in {', '.join(sorted(unused))}"
                + (f", unknown for {', '.join(sorted(unknown))}" if unknown else "") + "." + note,
                (f"Eliminate {dep0.ga}: rewrite its few usages in {', '.join(sorted(used))} and delete it from "
                 f"{owner}/pom.xml entirely." if cheap else
                 f"Exposure only drops if {dep0.ga} disappears entirely; moving it into the children does not "
                 "help. Keep it in the parent unless its elimination is planned."),
                "+".join(sorted(evidence)), gain,
            ))
        elif unknown:
            out.append(Finding(
                "inherited-dependency", Severity.INFO, owner, dep0.ga,
                f"{dep0.ga} ({dep0.scope}) is forced on {len(heir_names)} project(s) via {owner}/<dependencies>; "
                f"usage unknown for {', '.join(sorted(unknown))}.{note}",
                "Run with --maven-analyze or make sources/jars available to decide whether it can be eliminated.",
                "pom", 0,
            ))
    return out


def rule_eliminate(ctx: Ctx) -> list[Finding]:
    """Libraries whose whole-workspace footprint is small enough to rewrite away."""
    out = []
    for ga, e in ctx.result.elimination.items():
        if e.effort not in ("trivial", "low", "medium"):
            continue
        users = sorted(set(e.used_by_main) | set(e.used_by_test))
        test_only = not e.used_by_main
        if e.effort == "trivial":
            sev = Severity.HIGH if e.shipped and not test_only else Severity.MEDIUM
        elif e.effort == "low":
            sev = Severity.MEDIUM if e.shipped and not test_only else Severity.LOW
        else:
            sev = Severity.INFO
        top = ", ".join(f"{m} x{n}" for m, n in list(e.members.items())[:6]) or ", ".join(
            c.rsplit(".", 1)[1] for c in e.classes[:6])
        brings = f"; removes {e.brings} artifact(s) from the workspace" if e.brings else ""
        out.append(Finding(
            "eliminate-dependency", sev, "*", ga,
            f"{ga} footprint: {len(e.classes)} class(es), {e.references} reference(s) in {e.files} file(s) "
            f"across {', '.join(users)}" + (" (tests only)" if test_only else "") + f". Used: {top}.",
            f"Effort {e.effort}: replace the usages"
            + (f" ({e.hint})" if e.hint else " with JDK code or a small in-house helper")
            + f", then delete {ga} from {', '.join(e.declared_in)}" + brings + ".",
            "static-usage", e.brings or 1,
        ))
    return out


def rule_duplicates(ctx: Ctx) -> list[Finding]:
    return [
        Finding(
            "duplicate-declaration", Severity.LOW, p.name, key,
            f"{key} is declared more than once in {p.name}/pom.xml (last one wins).",
            "Delete the duplicate <dependency> element.",
            "pom",
        )
        for p in ctx.projects
        for key in p.duplicate_keys
    ]


def rule_version_override(ctx: Ctx) -> list[Finding]:
    out = []
    for p in ctx.projects:
        for dep in p.dependencies:
            if dep.inherited or not dep.declared_version:
                continue
            entry = p.managed.get(dep.key)
            if entry and entry.version and entry.version != dep.version:
                out.append(Finding(
                    "version-override", Severity.MEDIUM, p.name, dep.ga,
                    f"{dep.ga} pinned to {dep.declared_version} although {entry.declared_in} manages {entry.version}.",
                    f"Drop the <version> in {p.name}/pom.xml (or update the managed version) so one version is used.",
                    "pom",
                ))
    return out


def rule_version_drift(ctx: Ctx) -> list[Finding]:
    out = []
    direct: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for p in ctx.projects:
        for dep in p.dependencies:
            if not dep.internal and dep.version:
                direct[dep.ga][dep.version].add(p.name)
    for ga, versions in sorted(direct.items()):
        if len(versions) > 1:
            desc = "; ".join(f"{v}: {', '.join(sorted(ps))}" for v, ps in sorted(versions.items()))
            out.append(Finding(
                "version-drift", Severity.MEDIUM, "*", ga,
                f"{ga} is declared with {len(versions)} different versions - {desc}.",
                "Manage one version in the parent's dependencyManagement and remove local <version>s.",
                "pom", len(versions) - 1,
            ))
    if ctx.result.maven:
        resolved: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
        internal = {p.ga for p in ctx.projects}
        for p in ctx.projects:
            tree = ctx.tree(p)
            if tree is None:
                continue
            for node in tree.walk():
                if node.ga not in internal:
                    resolved[node.ga][node.version].add(p.name)
        for ga, versions in sorted(resolved.items()):
            if len(versions) > 1 and ga not in direct:
                desc = "; ".join(f"{v}: {', '.join(sorted(ps))}" for v, ps in sorted(versions.items()))
                out.append(Finding(
                    "transitive-version-drift", Severity.LOW, "*", ga,
                    f"{ga} resolves to {len(versions)} versions across projects (transitively) - {desc}.",
                    "Pin it once in the parent's dependencyManagement so every project gets the same (patched) version.",
                    "maven-tree", len(versions) - 1,
                ))
    return out


def rule_overlap(ctx: Ctx) -> list[Finding]:
    out = []
    users: dict[str, set[str]] = defaultdict(set)
    for p in ctx.projects:
        for dep in p.dependencies:
            if not dep.internal:
                users[dep.ga].add(p.name)
    for group, hits in overlap_members(set(users)).items():
        members = sorted({ga for gas in hits.values() for ga in gas})
        desc = "; ".join(f"{ga} ({', '.join(sorted(users[ga]))})" for ga in members)
        sev = Severity.LOW if group in LOW_PRIORITY_GROUPS else Severity.MEDIUM
        out.append(Finding(
            "overlapping-libraries", sev, "*", ", ".join(members),
            f"{group}: {len(hits)} libraries doing the same job are declared - {desc}.",
            f"Standardise on one {group.lower()} library across projects and migrate the others.",
            "pom", len(hits) - 1,
        ))
    return out


def rule_unused_managed(ctx: Ctx) -> list[Finding]:
    out = []
    has_trees = bool(ctx.result.maven and ctx.result.maven.trees)
    for owner in ctx.projects:
        own_entries = [e for e in owner.managed.values() if e.declared_in == owner.name and not e.imported_from]
        if not own_entries:
            continue
        family = ctx.descendants(owner)
        direct = {d.ga for p in family for d in [*p.dependencies, *p.profile_dependencies]}
        transitive: set[str] = set()
        for p in family:
            tree = ctx.tree(p)
            if tree is not None:
                transitive |= {n.ga for n in tree.walk()}
        for entry in own_entries:
            if entry.ga in direct or entry.ga in transitive:
                continue
            if has_trees:
                msg = f"{entry.ga} is managed in {owner.name} but no project uses it, not even transitively."
                act = f"Delete the entry from {owner.name}'s dependencyManagement."
            else:
                msg = f"{entry.ga} is managed in {owner.name} but no scanned project declares it."
                act = ("Delete it from dependencyManagement unless it pins a transitive version "
                       "(re-run with --maven to check).")
            internal = any(p.ga == entry.ga for p in ctx.projects)
            if internal:
                msg += " (internal artifact - may be consumed outside the scanned directory)"
            out.append(Finding(
                "unused-managed-dependency", Severity.LOW, owner.name, entry.ga, msg, act,
                "maven-tree" if has_trees else "pom",
            ))
    return out


def rule_plugins(ctx: Ctx) -> list[Finding]:
    out = []
    maven = ctx.result.maven
    for owner in ctx.projects:
        own = [m for m in owner.managed_plugins.values() if m.declared_in == owner.name]
        if own:
            family = ctx.descendants(owner)
            used = {pl.ga for p in family for pl in p.plugins}
            if maven:
                used |= {pl.ga for p in family for pl in maven.plugins.get(p.ga, [])}
            for mp in own:
                if mp.ga not in used:
                    out.append(Finding(
                        "unused-managed-plugin", Severity.LOW, owner.name, mp.ga,
                        f"Plugin {mp.ga} is in {owner.name}'s pluginManagement but no project uses it.",
                        "Delete the pluginManagement entry (fewer build-time artifacts to keep patched).",
                        "maven-plugins" if maven else "pom",
                    ))
        unpinned = sorted(pl.ga for pl in owner.plugins if pl.version_source.startswith("Maven default"))
        if unpinned:
            out.append(Finding(
                "unpinned-plugin", Severity.LOW, owner.name, ", ".join(unpinned),
                f"Plugins without a version: {', '.join(unpinned)} - Maven picks a default that changes between "
                "Maven versions (often old, unpatched).",
                "Pin plugin versions in the parent's pluginManagement.",
                "pom",
            ))
    return out


def rule_heavy(ctx: Ctx) -> list[Finding]:
    out = []
    threshold = int(ctx.result.options.get("heavy_threshold", 10))
    flagged = {(f.project, f.artifact) for f in ctx.result.findings if f.rule == "unused-dependency"}
    internal = {p.ga for p in ctx.projects}
    for p in ctx.projects:
        tree = ctx.tree(p)
        if tree is None:
            continue
        for child in tree.children:
            if child.omitted or child.scope not in SHIPPED_SCOPES or (p.name, child.ga) in flagged:
                continue
            if child.ga in internal:
                continue
            size = len(tree.removable_with(child.ga, SHIPPED_SCOPES))
            if size < threshold:
                continue
            out.append(Finding(
                "heavy-dependency", Severity.INFO, p.name, child.ga,
                f"{child.ga} alone brings {size} artifacts into the shipped classpath "
                f"({child.subtree_size()} in its subtree).",
                "Check whether a lighter alternative exists or exclude the transitive parts you do not use.",
                "maven-tree", size,
            ))
    return out


def rule_used_undeclared(ctx: Ctx) -> list[Finding]:
    out = []
    for p in ctx.projects:
        analyze = ctx.analyze(p)
        if analyze is None:
            continue
        gas = sorted({_ga(c) for c in analyze.used_undeclared})
        if gas:
            out.append(Finding(
                "used-undeclared", Severity.INFO, p.name, ", ".join(gas),
                f"Code uses {', '.join(gas)} only via transitive dependencies.",
                "Declare them explicitly before removing anything they arrive through.",
                "maven-analyze",
            ))
    return out


def rule_structure(ctx: Ctx) -> list[Finding]:
    out = []
    r = ctx.result
    for cycle in r.cycles:
        out.append(Finding(
            "internal-cycle", Severity.HIGH, cycle[0], " -> ".join(cycle),
            f"Dependency cycle between internal projects: {' -> '.join(cycle)}.",
            "Break the cycle (move shared code into a lower-level lib).",
            "pom",
        ))
    by_ga = {p.ga: p for p in ctx.projects}
    for p in ctx.projects:
        for dep in p.dependencies:
            target = by_ga.get(dep.ga)
            if target is not None and dep.type == "pom" and target.packaging == "pom" and not dep.inherited:
                n = len(target.dependencies)
                out.append(Finding(
                    "depends-on-pom-artifact", Severity.MEDIUM, p.name, dep.ga,
                    f"{p.name} depends on the POM artifact {dep.ga}: its {n} <dependencies> become transitive "
                    f"dependencies of {p.name} and of everyone consuming {p.name}.",
                    "Remove this dependency (inheritance via <parent> is enough) or import it as a BOM "
                    "in dependencyManagement.",
                    "pom", n,
                ))
        if p.parent_version_mismatch:
            out.append(Finding(
                "parent-version-mismatch", Severity.INFO, p.name, r.parent.ga,
                f"{p.name} builds against {r.parent.name} {p.parent_version_declared}, the local checkout is "
                f"{p.parent_version_local}. Static results use the local parent.",
                "Use --maven to resolve the released parent, or align the versions.",
                "pom",
            ))
        if p.external_parent:
            out.append(Finding(
                "external-parent", Severity.INFO, p.name, p.external_parent,
                f"{p.name} inherits from {p.external_parent} which is not in the scanned directory - "
                "versions managed there are unknown statically.",
                "Use --maven for complete data.",
                "pom",
            ))
    return out


RULES = [
    rule_unused,
    rule_test_scope,
    rule_inherited_unused,
    rule_eliminate,
    rule_duplicates,
    rule_version_override,
    rule_version_drift,
    rule_overlap,
    rule_unused_managed,
    rule_plugins,
    rule_used_undeclared,
    rule_structure,
]


def run_all(result: AnalysisResult) -> list[Finding]:
    ctx = Ctx(result)
    findings: list[Finding] = []
    for rule in RULES:
        findings.extend(rule(ctx))
    result.findings = findings
    findings.extend(rule_heavy(ctx))  # needs the unused findings above
    findings.sort(key=lambda f: (f.severity.rank, f.rule, f.project, f.artifact))
    return findings
