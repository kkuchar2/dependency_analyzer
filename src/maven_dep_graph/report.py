"""Render an :class:`AnalysisResult` as console text, Markdown, JSON and CSV."""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from . import __version__
from .models import (
    EFFORT_ORDER,
    SHIPPED_SCOPES,
    AnalysisResult,
    EffectiveProject,
    Finding,
    Severity,
    TreeNode,
)

SEV_ICON = {"high": "🔴", "medium": "🟠", "low": "🟡", "info": "🔵"}


# --------------------------------------------------------------- shared views


def _analyze_status(result: AnalysisResult, project: EffectiveProject, ga: str) -> str:
    if not result.maven or not result.options.get("maven_analyze"):
        return ""
    a = result.maven.analyze.get(project.name)
    if a is None:
        return "n/a"
    gas = lambda coords: {":".join(c.split(":")[:2]) for c in coords}  # noqa: E731
    if ga in gas(a.unused_declared):
        tree = result.maven.trees.get(project.ga)
        node = next((c for c in tree.children if c.ga == ga), None) if tree else None
        if node and {n.ga for n in node.walk()} & gas(a.used_undeclared):
            return "used via transitive"
        return "unused"
    if ga in gas(a.non_test_scoped_test_only):
        return "test-only"
    return "used"


def _usage(result: AnalysisResult, project: EffectiveProject, key: str) -> str:
    info = result.usage.get(project.ga, {}).get(key)
    return info.usage.value if info else ""


def workspace_inventory(result: AnalysisResult) -> list[dict]:
    """One row per external g:a across the workspace (direct + transitive)."""
    internal = {p.ga for p in result.projects}
    rows: dict[str, dict] = {}

    def row(ga: str) -> dict:
        return rows.setdefault(ga, {
            "ga": ga, "versions": set(), "scopes": set(), "direct_in": set(),
            "transitive_in": set(), "via": set(),
        })

    for p in result.projects:
        for dep in p.dependencies:
            if dep.internal:
                continue
            r = row(dep.ga)
            if dep.version:
                r["versions"].add(dep.version)
            r["scopes"].add(dep.scope)
            r["direct_in"].add(p.name)
        tree = result.maven.trees.get(p.ga) if result.maven else None
        if tree is None:
            continue
        for top in tree.children:
            for node in [top, *top.walk()]:
                if node.ga in internal:
                    continue
                r = row(node.ga)
                r["versions"].add(node.version)
                r["scopes"].add(node.scope)
                if node is not top:
                    r["transitive_in"].add(p.name)
                    r["via"].add(top.ga)
    out = []
    for ga, r in sorted(rows.items()):
        out.append({
            "ga": ga,
            "versions": sorted(r["versions"]),
            "scopes": sorted(r["scopes"]),
            "shipped": bool(r["scopes"] & SHIPPED_SCOPES),
            "direct_in": sorted(r["direct_in"]),
            "transitive_in": sorted(r["transitive_in"] - r["direct_in"]),
            "via": sorted(r["via"]),
        })
    return out


def plugin_inventory(result: AnalysisResult) -> list[dict]:
    rows: dict[str, dict] = {}
    for p in result.projects:
        resolved = result.maven.plugins.get(p.ga) if result.maven else None
        if resolved is not None:
            for pl in resolved:
                r = rows.setdefault(pl.ga, {"ga": pl.ga, "versions": set(), "projects": set(), "deps": set(),
                                            "declared": set()})
                r["versions"].add(pl.version)
                r["projects"].add(p.name)
                r["deps"].update(d.rsplit(":", 1)[0] for d in pl.dependencies)
        for pl in p.plugins:
            r = rows.setdefault(pl.ga, {"ga": pl.ga, "versions": set(), "projects": set(), "deps": set(),
                                        "declared": set()})
            if pl.version and resolved is None:
                r["versions"].add(pl.version)
            r["projects"].add(p.name)
            r["declared"].add(pl.declared_in)
    return [
        {
            "ga": ga,
            "versions": sorted(r["versions"]),
            "projects": sorted(r["projects"]),
            "declared_in": sorted(r["declared"]),
            "implicit": not r["declared"],
            "plugin_dependencies": len(r["deps"]),
        }
        for ga, r in sorted(rows.items())
    ]


def headline(result: AnalysisResult) -> dict:
    inv = workspace_inventory(result)
    sev = Counter(f.severity.value for f in result.findings)
    roles = Counter(p.role.value for p in result.projects)
    direct = {d.ga for p in result.projects for d in p.dependencies if not d.internal}
    direct_shipped = {d.ga for p in result.projects for d in p.dependencies
                      if not d.internal and d.scope in SHIPPED_SCOPES}
    removable = sum(f.removable_artifacts for f in result.findings
                    if f.rule in ("unused-dependency", "test-dependency-in-shipped-scope", "inherited-unused",
                                  "depends-on-pom-artifact"))
    data = {
        "projects": len(result.projects),
        "roles": dict(roles),
        "external_direct_artifacts": len(direct),
        "external_direct_shipped_artifacts": len(direct_shipped),
        "findings": dict(sev),
        "estimated_removable_artifact_occurrences": removable,
    }
    if result.maven:
        data["external_artifacts_total"] = len(inv)
        data["external_artifacts_shipped"] = sum(1 for r in inv if r["shipped"])
        data["build_plugins"] = len(plugin_inventory(result))
    return data


# --------------------------------------------------------------- console


def render_console(result: AnalysisResult, limit: int = 25) -> str:
    h = headline(result)
    lines = [
        "",
        "Maven dependency reduction report",
        "=================================",
        f"Root:    {result.root_dir}",
        f"Parent:  {result.parent.ga}:{result.parent.version}  ({result.parent.raw.pom_path})",
        f"Modes:   static POM model"
        + (" + static usage scan" if result.options.get("static_usage") else "")
        + (" + maven tree/plugins" if result.options.get("maven") else "")
        + (" + maven analyze" if result.options.get("maven_analyze") else ""),
        "",
        f"Projects: {h['projects']}  ({', '.join(f'{k}: {v}' for k, v in sorted(h['roles'].items()))})",
        f"External artifacts declared directly: {h['external_direct_artifacts']} "
        f"({h['external_direct_shipped_artifacts']} in shipped scopes)",
    ]
    if "external_artifacts_total" in h:
        lines.append(
            f"External artifacts incl. transitive: {h['external_artifacts_total']} "
            f"({h['external_artifacts_shipped']} shipped); build plugins: {h['build_plugins']}"
        )
    lines.append("Findings: " + ", ".join(f"{s}: {h['findings'].get(s, 0)}" for s in ("high", "medium", "low", "info")))
    lines.append("")

    header = f"{'project':<28} {'role':<10} {'direct':>6} {'inh':>4} {'test':>5} {'trans':>6} {'plug':>5} {'unused':>6}"
    lines += [header, "-" * len(header)]
    for s in result.stats:
        trans = "" if s.transitive_shipped is None else str(s.transitive_shipped)
        lines.append(
            f"{s.artifact_id[:28]:<28} {s.role.value:<10} {s.direct:>6} {s.inherited:>4} "
            f"{s.by_scope.get('test', 0):>5} {trans:>6} {s.plugins:>5} {s.unused:>6}"
        )
    lines.append("(direct = effective direct deps incl. inherited; inh = inherited from parents; "
                 "trans = shipped artifacts incl. transitive, needs --maven)")
    lines.append("")

    elim = sorted(result.elimination.values(), key=lambda e: (EFFORT_ORDER.index(e.effort)
                  if e.effort in EFFORT_ORDER else 99, -(e.brings or 0), e.ga))
    candidates = [e for e in elim if e.effort in ("none", "trivial", "low")]
    if candidates:
        lines.append("Elimination candidates (can disappear from the whole workspace):")
        for e in candidates[:limit]:
            users = sorted(set(e.used_by_main) | set(e.used_by_test))
            brings = "?" if e.brings is None else str(e.brings)
            lines.append(f"  {e.effort:<8} {e.ga:<52} refs {e.references:>3} in {len(users)} project(s), "
                         f"artifacts gone: {brings}")
        lines.append("")

    important = [f for f in result.findings if f.severity in (Severity.HIGH, Severity.MEDIUM)]
    lines.append(f"Top findings ({len(important)} high/medium):")
    for f in important[:limit]:
        lines.append(f"  [{f.severity.value.upper():<6}] {f.project}: {f.message}")
        lines.append(f"           -> {f.action}")
    if len(important) > limit:
        lines.append(f"  ... {len(important) - limit} more in the Markdown report")
    for w in result.warnings:
        lines.append(f"Warning: {w}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------- markdown


def _md_escape(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _table(headers: list[str], rows: list[list]) -> list[str]:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for row in rows:
        out.append("| " + " | ".join(_md_escape(c) for c in row) + " |")
    return out


def _tree_lines(node: TreeNode, depth: int = 0) -> list[str]:
    lines = []
    for child in node.children:
        extra = f" [{child.scope}]" if child.scope else ""
        extra += " (optional)" if child.optional else ""
        if child.omitted:
            lines.append("  " * depth + f"- <sub>↺ {child.gav}{extra} – resolved elsewhere</sub>")
            continue
        lines.append("  " * depth + f"- {child.gav}{extra}")
        lines.extend(_tree_lines(child, depth + 1))
    return lines


def _mermaid(result: AnalysisResult) -> list[str]:
    ids = {p.name: f"n{i}" for i, p in enumerate(result.projects)}
    lines = ["```mermaid", "graph LR"]
    for p in result.projects:
        shape = f'{ids[p.name]}[["{p.name}"]]' if p is result.parent else f'{ids[p.name]}["{p.name}<br/>({p.role.value})"]'
        lines.append(f"  {shape}")
    styles = {"parent": "-.->|parent|", "dependency": "-->", "dependencyManagement": "-.->|manages|"}
    for e in result.edges:
        if e.source in ids and e.target in ids:
            label = styles[e.via]
            if e.via == "dependency" and e.scope != "compile":
                label = f"-->|{e.scope}|"
            lines.append(f"  {ids[e.source]} {label} {ids[e.target]}")
    lines.append("```")
    return lines


EFFORT_ICON = {"none": "🗑️", "trivial": "🟢", "low": "🟢", "medium": "🟠", "high": "🔴", "keep": "⚓", "unknown": "❔"}


def _sorted_elimination(result: AnalysisResult):
    return sorted(result.elimination.values(), key=lambda e: (
        EFFORT_ORDER.index(e.effort) if e.effort in EFFORT_ORDER else 99, -(e.brings or 0), e.ga))


def render_elimination_md(r: AnalysisResult) -> list[str]:
    if not r.elimination:
        return []
    md = ["## Elimination analysis", "",
          "Can each external library disappear from the **whole workspace**? Moving a library from the parent "
          "into the children that use it does not reduce exposure - only removing it does. The footprint below "
          "is measured in the sources (imports, calls, fully-qualified names, resources); _artifacts gone_ is "
          "how many artifacts (the library plus what only it pulls in) leave every classpath.", "",
          "Effort: 🗑️ none (unused - delete) · 🟢 trivial/low (rewrite in minutes/hours) · 🟠 medium "
          "(planned refactoring) · 🔴 high (keep unless strategic) · ⚓ keep (infrastructure) · ❔ jar not available", ""]
    rows = []
    for e in _sorted_elimination(r):
        users = sorted(set(e.used_by_main) | set(e.used_by_test))
        rows.append([
            f"{EFFORT_ICON.get(e.effort, '')} {e.effort}", e.ga, ", ".join(e.scopes),
            ", ".join(e.declared_in), len(e.forced_on) or "",
            ", ".join(users) + (" (tests only)" if users and not e.used_by_main else ""),
            len(e.classes), e.references, e.files, "?" if e.brings is None else e.brings,
            e.keep_reason or e.hint or e.detail,
        ])
    md += _table(["Effort", "Library", "Scopes", "Declared in", "Forced on", "Used by", "Classes", "Refs",
                  "Files", "Artifacts gone", "Keep reason / replacement"], rows)
    detailed = [e for e in _sorted_elimination(r) if e.effort in ("trivial", "low", "medium") and e.classes]
    if detailed:
        md += ["", "### What exactly is used (rewrite checklist)", ""]
        for e in detailed:
            md.append(f"**{e.ga}** – {e.effort}" + (f" – _{e.hint}_" if e.hint else ""))
            md.append("")
            for cls in e.classes:
                simple = cls.rsplit(".", 1)[1]
                calls = {m: n for m, n in e.members.items() if m.startswith(simple + ".")}
                desc = ", ".join(f"`{m.split('.', 1)[1]}` x{n}" for m, n in calls.items())
                md.append(f"- `{cls}`" + (f": {desc}" if desc else ""))
            per = {**{k: f"{v} main" for k, v in e.used_by_main.items()}}
            for k, v in e.used_by_test.items():
                per[k] = (per[k] + ", " if k in per else "") + f"{v} test"
            md.append("- references per project: " + "; ".join(f"{k}: {v}" for k, v in sorted(per.items())))
            md.append("")
    md.append("")
    return md


def render_markdown(result: AnalysisResult) -> str:
    r = result
    h = headline(r)
    md: list[str] = [
        "# Maven dependency reduction report",
        "",
        f"Generated {datetime.now():%Y-%m-%d %H:%M} by maven-dep-graph {__version__}.",
        "",
        f"- **Workspace:** `{r.root_dir}`",
        f"- **Parent:** `{r.parent.ga}:{r.parent.version}` (`{r.parent.raw.pom_path}`)",
        f"- **Evidence used:** static POM model"
        + (", static source-usage scan" if r.options.get("static_usage") else "")
        + (", Maven dependency tree + plugins" if r.options.get("maven") else "")
        + (", Maven bytecode analysis (`dependency:analyze`)" if r.options.get("maven_analyze") else ""),
        f"- **Local repository:** `{r.options.get('local_repo')}`",
        "",
        "## Summary",
        "",
    ]
    rows = [
        ["Projects", f"{h['projects']} ({', '.join(f'{k}: {v}' for k, v in sorted(h['roles'].items()))})"],
        ["External artifacts declared directly", h["external_direct_artifacts"]],
        ["…of which in shipped scopes (compile/runtime)", h["external_direct_shipped_artifacts"]],
    ]
    if "external_artifacts_total" in h:
        rows += [
            ["External artifacts incl. transitive", h["external_artifacts_total"]],
            ["…of which shipped", h["external_artifacts_shipped"]],
            ["Build plugins (incl. implicit lifecycle plugins)", h["build_plugins"]],
        ]
    rows += [
        ["Findings", ", ".join(f"{SEV_ICON[s]} {s}: {h['findings'].get(s, 0)}" for s in ("high", "medium", "low", "info"))],
        ["Artifact occurrences removable by fixing high/medium usage findings (estimate)",
         h["estimated_removable_artifact_occurrences"]],
    ]
    md += _table(["Metric", "Value"], rows)

    # ---------------------------------------------------------- action plan
    md += ["", "## Action plan", "",
           "Ordered by impact on exposure. Each line is one concrete POM change.", ""]
    by_rule: dict[str, list[Finding]] = defaultdict(list)
    for f in r.findings:
        if f.severity in (Severity.HIGH, Severity.MEDIUM):
            by_rule[f.rule].append(f)
    if not by_rule:
        md.append("No high or medium findings. 🎉")
    for rule, fs in sorted(by_rule.items(), key=lambda kv: (min(f.severity.rank for f in kv[1]), kv[0])):
        md += [f"### {rule} ({len(fs)})", ""]
        for f in fs:
            gain = f" _(−{f.removable_artifacts} artifact(s))_" if f.removable_artifacts else ""
            md.append(f"- {SEV_ICON[f.severity.value]} **{f.project}** – {f.action}{gain}  ")
            md.append(f"  <sub>{_md_escape(f.message)} · evidence: {f.source}</sub>")
        md.append("")

    md += render_elimination_md(r)

    # ---------------------------------------------------------- all findings
    md += ["## All findings", ""]
    md += _table(
        ["Sev", "Rule", "Project", "Artifact", "Finding", "Action", "Evidence"],
        [[f"{SEV_ICON[f.severity.value]} {f.severity.value}", f.rule, f.project, f.artifact, f.message, f.action, f.source]
         for f in r.findings],
    )

    # ---------------------------------------------------------- projects
    md += ["", "## Projects", ""]
    md += _table(
        ["Project", "Role", "Pack", "Direct", "Inherited", "compile", "runtime", "provided", "test",
         "Shipped (transitive)", "Test-only (transitive)", "Plugin artifacts", "Fan-in", "Fan-out", "Findings"],
        [[s.artifact_id, s.role.value, s.packaging, s.direct, s.inherited, s.by_scope.get("compile", 0),
          s.by_scope.get("runtime", 0), s.by_scope.get("provided", 0), s.by_scope.get("test", 0),
          "" if s.transitive_shipped is None else s.transitive_shipped,
          "" if s.transitive_test is None else s.transitive_test,
          "" if s.plugin_artifacts is None else s.plugin_artifacts, s.fan_in, s.fan_out, s.findings]
         for s in r.stats],
    )
    md += ["", "_Direct_ = effective direct dependencies (own + inherited from parent POMs). "
               "_Shipped_ = unique artifacts on the compile/runtime classpath (what is exposed in production).", ""]

    # ---------------------------------------------------------- parent
    p = r.parent
    md += [f"## Parent `{p.name}`", ""]
    mism = [x for x in r.projects if x.parent_version_mismatch]
    children = [x for x in r.projects if x.inherits_from_parent]
    md.append(f"{len(children)} project(s) inherit from it. Local version: `{p.version}`.")
    if mism:
        md.append("")
        md.append("Projects built against a different (released) parent version than the local checkout:")
        md.append("")
        md += _table(["Project", "Declared parent version", "Local parent version"],
                     [[x.name, x.parent_version_declared, x.parent_version_local] for x in mism])
    md += ["", "### Dependencies forced on every child (`<dependencies>` in parent)", ""]
    inherited_rows = []
    for dep in p.dependencies:
        heirs = [c for c in children if any(d.key == dep.key and d.inherited for d in c.dependencies)]
        statuses = Counter()
        for c in heirs:
            cd = next(d for d in c.dependencies if d.key == dep.key)
            st = _analyze_status(r, c, cd.ga) or _usage(r, c, cd.key) or "?"
            statuses[st] += 1
        inherited_rows.append([dep.ga, dep.version or "?", dep.scope, len(heirs),
                               ", ".join(f"{k}: {v}" for k, v in sorted(statuses.items()))])
    md += _table(["Artifact", "Version", "Scope", "Inherited by", "Usage in children"], inherited_rows) \
        if inherited_rows else ["None."]

    md += ["", "### dependencyManagement", ""]
    users: dict[str, set[str]] = defaultdict(set)
    trans_users: dict[str, set[str]] = defaultdict(set)
    for c in r.projects:
        for d in c.dependencies:
            users[d.ga].add(c.name)
        tree = r.maven.trees.get(c.ga) if r.maven else None
        if tree:
            for n in tree.walk():
                trans_users[n.ga].add(c.name)
    dm_rows = []
    for e in sorted(p.managed.values(), key=lambda e: e.ga):
        dm_rows.append([e.ga, e.version or "?", e.scope or "", e.imported_from or e.declared_in,
                        len(users[e.ga]), len(trans_users[e.ga] - users[e.ga]) if r.maven else ""])
    md += _table(["Artifact", "Version", "Scope", "From", "Direct users", "Transitive-only users"], dm_rows) \
        if dm_rows else ["None."]
    if p.external_boms:
        md += ["", "Imported BOMs not available locally (their managed versions are unknown statically): "
               + ", ".join(f"`{b}`" for b in p.external_boms)]

    md += ["", "### pluginManagement", ""]
    pm_rows = []
    for mp in sorted(p.managed_plugins.values(), key=lambda m: m.ga):
        using = sorted(c.name for c in r.projects if any(pl.ga == mp.ga for pl in c.plugins))
        pm_rows.append([mp.ga, mp.version or "?", mp.declared_in, ", ".join(using) or "—"])
    md += _table(["Plugin", "Version", "Declared in", "Used by"], pm_rows) if pm_rows else ["None."]

    # ---------------------------------------------------------- internal graph
    md += ["", "## Internal dependency graph", ""]
    md += _mermaid(r)
    md += ["", "Cycles: " + ("; ".join(" → ".join(c) for c in r.cycles) if r.cycles else "none")]

    # ---------------------------------------------------------- per project
    md += ["", "## Per-project dependencies", ""]
    for proj in r.projects:
        md += [f"### {proj.name} ({proj.role.value})", "", f"`{proj.ga}:{proj.version}` – `{proj.raw.pom_path}`", ""]
        if proj.dependencies:
            tree = r.maven.trees.get(proj.ga) if r.maven else None
            sizes = {c.ga: c.subtree_size() for c in tree.children} if tree else {}
            rows = []
            for d in sorted(proj.dependencies, key=lambda d: (d.scope, d.ga)):
                rows.append([
                    d.ga + (f" ({d.type}{':' + d.classifier if d.classifier else ''})" if d.type != "jar" or d.classifier else ""),
                    d.version or "?", d.version_source, d.scope + (" opt" if d.optional else ""),
                    "inherited from " + d.declared_in if d.inherited else "own",
                    "internal" if d.internal else "external",
                    _usage(r, proj, d.key), _analyze_status(r, proj, d.ga),
                    sizes.get(d.ga, "") if tree else "",
                ])
            md += _table(["Dependency", "Version", "Version source", "Scope", "Declared", "Kind",
                          "Static usage", "Maven analyze", "Transitive"], rows)
        else:
            md.append("No dependencies.")
        if proj.profile_dependencies:
            md += ["", "Profile-only dependencies: " + ", ".join(
                f"`{d.ga}` ({d.profile})" for d in proj.profile_dependencies)]
        if proj.plugins:
            md += ["", "Declared/inherited plugins: " + ", ".join(
                f"`{pl.ga}:{pl.version or '?'}`" for pl in sorted(proj.plugins, key=lambda x: x.ga))]
        tree = r.maven.trees.get(proj.ga) if r.maven else None
        if tree and tree.children:
            md += ["", "<details><summary>Resolved dependency tree "
                   f"({tree.subtree_size()} nodes)</summary>", ""]
            md += _tree_lines(tree)
            md += ["", "</details>"]
        md.append("")

    # ---------------------------------------------------------- inventory
    md += ["## Workspace inventory (external artifacts)", ""]
    inv = workspace_inventory(r)
    md += _table(
        ["Artifact", "Versions", "Scopes", "Shipped", "Direct in", "Transitive in", "Arrives via"],
        [[x["ga"], ", ".join(x["versions"]), ", ".join(x["scopes"]), "yes" if x["shipped"] else "no",
          ", ".join(x["direct_in"]), ", ".join(x["transitive_in"]), ", ".join(x["via"][:5])] for x in inv],
    )
    md += ["", "## Build plugins", ""]
    pinv = plugin_inventory(r)
    md += _table(
        ["Plugin", "Versions", "Used by", "Declared in", "Implicit", "Plugin deps"],
        [[x["ga"], ", ".join(x["versions"]) or "?", len(x["projects"]), ", ".join(x["declared_in"]) or "—",
          "yes" if x["implicit"] else "", x["plugin_dependencies"] if r.maven else ""] for x in pinv],
    )

    if r.warnings:
        md += ["", "## Warnings", ""] + [f"- {w}" for w in r.warnings]
    md += ["", "## How to read this", "",
           "- **Evidence** `maven-analyze` (bytecode) is the most reliable, `static-usage` is a source scan "
           "heuristic, `pom` is structural.",
           "- Runtime-scoped and reflectively loaded libraries (JDBC drivers, logging backends, SPI "
           "implementations) cannot be proven used by either scan - verify before removing.",
           "- Fewer artifacts is only half of exposure: run a vulnerability scanner (e.g. OWASP "
           "dependency-check, `osv-scanner`) on what remains.", ""]
    return "\n".join(md)


# --------------------------------------------------------------- json / csv


def to_dict(result: AnalysisResult) -> dict:
    def tree(node: TreeNode) -> dict:
        return {"gav": node.gav, "type": node.type, "classifier": node.classifier, "scope": node.scope,
                "optional": node.optional, "children": [tree(c) for c in node.children]}

    projects = []
    for p in result.projects:
        projects.append({
            "artifactId": p.name, "groupId": p.group_id, "version": p.version, "packaging": p.packaging,
            "role": p.role.value, "pom": str(p.raw.pom_path),
            "parent": {"declared_version": p.parent_version_declared, "local_version": p.parent_version_local,
                       "inherits_from_selected_parent": p.inherits_from_parent, "external": p.external_parent},
            "dependencies": [{
                "ga": d.ga, "type": d.type, "classifier": d.classifier, "version": d.version,
                "version_source": d.version_source, "scope": d.scope, "optional": d.optional,
                "declared_in": d.declared_in, "inherited": d.inherited, "internal": d.internal,
                "exclusions": d.exclusions, "static_usage": _usage(result, p, d.key),
                "maven_analyze": _analyze_status(result, p, d.ga),
            } for d in p.dependencies],
            "profile_dependencies": [{"ga": d.ga, "version": d.version, "scope": d.scope, "profile": d.profile}
                                     for d in p.profile_dependencies],
            "plugins": [{"ga": pl.ga, "version": pl.version, "version_source": pl.version_source,
                         "declared_in": pl.declared_in, "dependencies": pl.dependencies} for pl in p.plugins],
            "managed": [{"ga": e.ga, "version": e.version, "scope": e.scope, "declared_in": e.declared_in,
                         "imported_from": e.imported_from} for e in p.managed.values()],
            "external_boms": p.external_boms,
            "resolved_tree": tree(result.maven.trees[p.ga]) if result.maven and p.ga in result.maven.trees else None,
            "resolved_plugins": [{"ga": pl.ga, "version": pl.version, "dependencies": pl.dependencies}
                                 for pl in result.maven.plugins.get(p.ga, [])] if result.maven else None,
        })
    return {
        "tool": {"name": "maven-dep-graph", "version": __version__},
        "root": str(result.root_dir),
        "parent": result.parent.ga,
        "options": result.options,
        "summary": headline(result),
        "findings": [{**f.__dict__, "severity": f.severity.value} for f in result.findings],
        "stats": [{**s.__dict__, "role": s.role.value} for s in result.stats],
        "projects": projects,
        "internal_edges": [e.__dict__ for e in result.edges],
        "cycles": result.cycles,
        "elimination": [e.__dict__ for e in _sorted_elimination(result)],
        "inventory": workspace_inventory(result),
        "plugins": plugin_inventory(result),
        "warnings": result.warnings,
    }


CSV_FIELDS = ["project", "role", "kind", "groupId", "artifactId", "type", "classifier", "version",
              "version_source", "scope", "relation", "via", "declared_in", "profile", "static_usage",
              "maven_analyze"]


def inventory_rows(result: AnalysisResult) -> list[dict]:
    rows = []
    for p in result.projects:
        base = {"project": p.name, "role": p.role.value}
        for d in [*p.dependencies, *p.profile_dependencies]:
            rows.append({**base, "kind": "dependency", "groupId": d.group_id, "artifactId": d.artifact_id,
                         "type": d.type, "classifier": d.classifier or "", "version": d.version or "",
                         "version_source": d.version_source, "scope": d.scope,
                         "relation": "inherited" if d.inherited else "direct", "via": "",
                         "declared_in": d.declared_in, "profile": d.profile or "",
                         "static_usage": _usage(result, p, d.key), "maven_analyze": _analyze_status(result, p, d.ga)})
        tree = result.maven.trees.get(p.ga) if result.maven else None
        if tree:
            for top in tree.children:
                for n in top.walk():
                    rows.append({**base, "kind": "dependency", "groupId": n.group_id, "artifactId": n.artifact_id,
                                 "type": n.type, "classifier": n.classifier or "", "version": n.version,
                                 "version_source": "resolved", "scope": n.scope, "relation": "transitive",
                                 "via": top.ga, "declared_in": "", "profile": "", "static_usage": "",
                                 "maven_analyze": ""})
        for e in p.managed.values():
            if e.declared_in == p.name or e.imported_from:
                rows.append({**base, "kind": "managed", "groupId": e.group_id, "artifactId": e.artifact_id,
                             "type": e.type, "classifier": e.classifier or "", "version": e.version or "",
                             "version_source": "dependencyManagement", "scope": e.scope or "",
                             "relation": "managed", "via": e.imported_from or "", "declared_in": e.declared_in,
                             "profile": "", "static_usage": "", "maven_analyze": ""})
        resolved = result.maven.plugins.get(p.ga) if result.maven else None
        declared = {pl.ga: pl for pl in p.plugins}
        plugin_rows = (
            [(pl.group_id, pl.artifact_id, pl.version, "resolved", pl.dependencies,
              declared[pl.ga].declared_in if pl.ga in declared else "(implicit)") for pl in resolved]
            if resolved is not None else
            [(pl.group_id, pl.artifact_id, pl.version or "", pl.version_source, pl.dependencies, pl.declared_in)
             for pl in p.plugins]
        )
        for g, a, v, src, deps, where in plugin_rows:
            rows.append({**base, "kind": "plugin", "groupId": g, "artifactId": a, "type": "maven-plugin",
                         "classifier": "", "version": v, "version_source": src, "scope": "build",
                         "relation": "direct", "via": "", "declared_in": where, "profile": "",
                         "static_usage": "", "maven_analyze": ""})
            for dep in deps:
                parts = dep.split(":")
                rows.append({**base, "kind": "plugin-dependency", "groupId": parts[0], "artifactId": parts[1],
                             "type": "jar", "classifier": "", "version": parts[-1] if len(parts) > 2 else "",
                             "version_source": src, "scope": "build", "relation": "plugin", "via": f"{g}:{a}",
                             "declared_in": where, "profile": "", "static_usage": "", "maven_analyze": ""})
    return rows


def write_reports(result: AnalysisResult, out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "markdown": out_dir / "report.md",
        "json": out_dir / "report.json",
        "csv": out_dir / "inventory.csv",
        "findings_csv": out_dir / "findings.csv",
    }
    paths["markdown"].write_text(render_markdown(result), encoding="utf-8")
    paths["json"].write_text(json.dumps(to_dict(result), indent=2, default=str), encoding="utf-8")
    with paths["csv"].open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(inventory_rows(result))
    if result.elimination:
        paths["elimination_csv"] = out_dir / "elimination.csv"
        with paths["elimination_csv"].open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["effort", "library", "scopes", "declared_in", "forced_on", "used_by_main",
                             "used_by_test", "classes", "references", "files", "artifacts_gone",
                             "keep_reason", "replacement_hint", "members"])
            for e in _sorted_elimination(result):
                writer.writerow([e.effort, e.ga, " ".join(e.scopes), " ".join(e.declared_in),
                                 " ".join(e.forced_on),
                                 " ".join(f"{k}:{v}" for k, v in e.used_by_main.items()),
                                 " ".join(f"{k}:{v}" for k, v in e.used_by_test.items()),
                                 len(e.classes), e.references, e.files, "" if e.brings is None else e.brings,
                                 e.keep_reason, e.hint,
                                 " ".join(f"{m}x{n}" for m, n in e.members.items())])
    with paths["findings_csv"].open("w", newline="", encoding="utf-8") as fh:
        fields = ["severity", "rule", "project", "artifact", "message", "action", "source", "removable_artifacts"]
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for f in result.findings:
            writer.writerow({**{k: getattr(f, k) for k in fields if k != "severity"}, "severity": f.severity.value})
    return paths
