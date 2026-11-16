"""Optional ground truth from real Maven.

All scanned projects are built in **one reactor** through a throw-away
aggregator POM (written to a temporary directory, nothing is written into your
projects except the usual ``target/`` output when compiling). Inside one reactor
sibling projects resolve each other without being installed first.

Goals executed (``maven-dependency-plugin`` version pinned for stable output):

* ``dependency:tree`` - full transitive dependency tree per project,
* ``dependency:resolve-plugins`` - every build plugin (including the implicit
  lifecycle ones such as ``maven-clean-plugin``) and the plugins' own deps,
* with ``analyze=True``: ``test-compile`` + ``dependency:analyze-only`` -
  bytecode-level detection of unused / used-but-undeclared dependencies.

Parents referenced by a version that only exists in a remote repository (e.g.
released by CI) are downloaded by Maven as usual, so the transitive data
reflects what the projects are really built with.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .models import AnalyzeResult, MavenData, RawProject, ResolvedPlugin, TreeNode

DEPENDENCY_PLUGIN = "org.apache.maven.plugins:maven-dependency-plugin:3.8.1"
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_ANALYZE_HEADER_RE = re.compile(r"--- (?:maven-)?dependency(?:-plugin)?:[^:\s]+:analyze-only \([^)]*\) @ (\S+) ---")
_SECTIONS = {
    "Used undeclared dependencies found:": "used_undeclared",
    "Unused declared dependencies found:": "unused_declared",
    "Non-test scoped test only dependencies found:": "non_test_scoped_test_only",
}


class MavenError(RuntimeError):
    pass


def find_mvn(explicit: str | None) -> str:
    candidates = [explicit] if explicit else ["mvn", "mvn.cmd"]
    for name in candidates:
        if name and (shutil.which(name) or Path(name).is_file()):
            return shutil.which(name) or name
    raise MavenError("Maven executable not found. Install Maven or pass --mvn /path/to/mvn.")


def write_aggregator(projects: list[RawProject], work_dir: Path) -> Path:
    agg_dir = work_dir / "aggregator"
    agg_dir.mkdir(parents=True, exist_ok=True)
    modules = []
    for project in projects:
        try:
            rel = os.path.relpath(project.base_dir, agg_dir)
        except ValueError:  # different drive on Windows
            rel = str(project.base_dir)
        modules.append(f"    <module>{rel}</module>")
    pom = agg_dir / "pom.xml"
    pom.write_text(
        "<project xmlns=\"http://maven.apache.org/POM/4.0.0\">\n"
        "  <modelVersion>4.0.0</modelVersion>\n"
        "  <groupId>maven-dep-graph.tmp</groupId>\n"
        "  <artifactId>aggregator</artifactId>\n"
        "  <version>0</version>\n"
        "  <packaging>pom</packaging>\n"
        "  <modules>\n" + "\n".join(modules) + "\n  </modules>\n"
        "</project>\n",
        encoding="utf-8",
    )
    return pom


# --------------------------------------------------------------------- parsing


def parse_coordinate(token: str) -> TreeNode | None:
    """Parse ``g:a:type[:classifier]:version[:scope]`` as printed by Maven."""
    parts = token.split(":")
    if len(parts) == 4:  # root line: g:a:packaging:version
        g, a, t, v = parts
        return TreeNode(g, a, t, None, v, "")
    if len(parts) == 5:
        g, a, t, v, s = parts
        return TreeNode(g, a, t, None, v, s)
    if len(parts) == 6:
        g, a, t, c, v, s = parts
        return TreeNode(g, a, t, c, v, s)
    return None


_TREE_PREFIX_RE = re.compile(r"^[|+\\\- ]*")


def parse_tree(text: str) -> TreeNode | None:
    """Parse the text output of ``dependency:tree`` (one project, verbose or not)."""
    root: TreeNode | None = None
    stack: list[tuple[int, TreeNode]] = []
    for raw_line in text.splitlines():
        line = _ANSI_RE.sub("", raw_line).rstrip()
        if not line.strip():
            continue
        indent = _TREE_PREFIX_RE.match(line).end()
        body = line[indent:]
        omitted = body.startswith("(")
        token = body.lstrip("(").split(" ", 1)[0].rstrip(")")
        node = parse_coordinate(token)
        if node is None:
            continue
        node.optional = "(optional)" in body
        node.omitted = omitted
        if indent == 0:
            if root is None:
                root = node
                stack = [(0, node)]
            continue
        if root is None:
            continue
        depth = indent // 3
        while stack and stack[-1][0] >= depth:
            stack.pop()
        if not stack:
            stack = [(0, root)]
        stack[-1][1].children.append(node)
        stack.append((depth, node))
    return root


def parse_plugins(text: str) -> list[ResolvedPlugin]:
    """Parse ``dependency:resolve-plugins`` output (plugins indented 3, their deps 6)."""
    plugins: list[ResolvedPlugin] = []
    for raw_line in text.splitlines():
        line = _ANSI_RE.sub("", raw_line).rstrip()
        stripped = line.strip()
        if not stripped or ":" not in stripped or " " in stripped:
            continue
        indent = len(line) - len(line.lstrip())
        parts = stripped.split(":")
        if indent <= 4 and len(parts) >= 4 and parts[2] == "maven-plugin":
            plugins.append(ResolvedPlugin(parts[0], parts[1], parts[3]))
        elif indent > 4 and plugins and len(parts) >= 4:
            g, a = parts[0], parts[1]
            v = parts[-1]
            if (g, a) != (plugins[-1].group_id, plugins[-1].artifact_id):
                plugins[-1].dependencies.append(f"{g}:{a}:{v}")
    return plugins


def parse_analyze(log: str) -> dict[str, AnalyzeResult]:
    """Parse ``dependency:analyze-only`` warnings from a Maven log, keyed by artifactId."""
    results: dict[str, AnalyzeResult] = {}
    current: AnalyzeResult | None = None
    section: str | None = None
    for raw_line in log.splitlines():
        line = _ANSI_RE.sub("", raw_line).rstrip()
        header = _ANALYZE_HEADER_RE.search(line)
        if header:
            current = results.setdefault(header.group(1), AnalyzeResult())
            section = None
            continue
        if current is None:
            continue
        if line.startswith("[INFO] ---") or line.startswith("[INFO] ----"):
            current, section = None, None
            continue
        content = re.sub(r"^\[(WARNING|INFO|ERROR)\]", "", line)
        stripped = content.strip()
        if stripped in _SECTIONS:
            section = _SECTIONS[stripped]
            continue
        if section and content.startswith("    ") and stripped.count(":") >= 3:
            getattr(current, section).append(stripped.split(" ", 1)[0])
        elif stripped and not content.startswith("    "):
            section = None
    return results


# --------------------------------------------------------------------- running


def _run(cmd: list[str], log_path: Path, timeout: int | None) -> int:
    with log_path.open("w", encoding="utf-8") as log:
        log.write("$ " + " ".join(cmd) + "\n\n")
        log.flush()
        proc = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, timeout=timeout, check=False)
    return proc.returncode


def _invoke(
    executable: str,
    pom: Path,
    *,
    analyze: bool,
    extra_args: list[str],
    tree_dir: Path,
    plugin_dir: Path,
    log_dir: Path,
    tag: str,
    timeout: int | None,
    data: MavenData,
    progress,
) -> None:
    """Run the tree (+analyze) and the resolve-plugins invocations for one POM."""
    base = [executable, "-B", "-fae", "-f", str(pom), *extra_args]
    goals = ["test-compile", f"{DEPENDENCY_PLUGIN}:analyze-only"] if analyze else []
    goals.append(f"{DEPENDENCY_PLUGIN}:tree")
    runs = [
        (
            f"maven-tree-{tag}.log",
            f"{'compile + analyze + ' if analyze else ''}dependency tree",
            [*base, *goals, "-Dverbose", "-DfailOnWarning=false", "-DskipTests",
             f"-DoutputFile={tree_dir}/${{project.groupId}}__${{project.artifactId}}.txt"],
        ),
        (
            f"maven-plugins-{tag}.log",
            "build plugins",
            [*base, f"{DEPENDENCY_PLUGIN}:resolve-plugins", "-DexcludeTransitive=false",
             f"-DoutputFile={plugin_dir}/${{project.groupId}}__${{project.artifactId}}.txt"],
        ),
    ]
    for log_name, what, cmd in runs:
        log = log_dir / log_name
        progress(f"Maven [{tag}]: {what}... (log: {log})")
        rc = _run(cmd, log, timeout)
        data.command_lines.append(" ".join(cmd))
        data.log_files.append(log)
        if rc:
            progress(f"Maven [{tag}] exited with {rc}; continuing with what was resolved.")
        if analyze and log_name.startswith("maven-tree"):
            for artifact_id, res in parse_analyze(log.read_text(encoding="utf-8", errors="ignore")).items():
                data.analyze.setdefault(artifact_id, res)


def _collect(tree_dir: Path, plugin_dir: Path, data: MavenData) -> None:
    for file in tree_dir.glob("*.txt"):
        node = parse_tree(file.read_text(encoding="utf-8", errors="ignore"))
        if node and node.group_id != "maven-dep-graph.tmp":
            data.trees.setdefault(node.ga, node)
    for file in plugin_dir.glob("*.txt"):
        ga = file.stem.replace("__", ":", 1)
        if not ga.startswith("maven-dep-graph.tmp:"):
            data.plugins.setdefault(ga, parse_plugins(file.read_text(encoding="utf-8", errors="ignore")))


def run_maven(
    projects: list[RawProject],
    *,
    analyze: bool,
    mvn: str | None,
    extra_args: list[str],
    log_dir: Path,
    timeout: int | None = None,
    progress=print,
) -> MavenData:
    """Run Maven over all ``projects`` (pass only reactor roots, see :func:`reactor_roots`).

    First one reactor containing everything (siblings resolve each other). A
    project whose POM cannot be built (e.g. its parent version is not available)
    makes Maven reject the whole reactor, so every root still missing a tree
    afterwards is retried on its own.
    """
    executable = find_mvn(mvn)
    data = MavenData()
    log_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="maven-dep-graph-") as tmp:
        work = Path(tmp)
        tree_dir, plugin_dir = work / "trees", work / "plugins"
        tree_dir.mkdir()
        plugin_dir.mkdir()
        common = dict(analyze=analyze, extra_args=extra_args, tree_dir=tree_dir, plugin_dir=plugin_dir,
                      log_dir=log_dir, timeout=timeout, data=data, progress=progress)

        _invoke(executable, write_aggregator(projects, work), tag="reactor", **common)
        _collect(tree_dir, plugin_dir, data)

        missing = [p for p in projects if p.ga not in data.trees]
        if missing and len(projects) > 1:
            progress(f"Retrying {len(missing)} project(s) individually: {', '.join(p.artifact_id for p in missing)}")
            for project in missing:
                _invoke(executable, project.pom_path, tag=project.artifact_id, **common)
            _collect(tree_dir, plugin_dir, data)
    return data


def reactor_roots(projects: list[RawProject], module_children: set[Path], warnings: list[str]) -> list[RawProject]:
    """Projects to list in the aggregator: skip <module>s (their aggregator builds them) and duplicate GAs."""
    seen: dict[str, Path] = {}
    roots = []
    for project in projects:
        if project.pom_path in module_children:
            continue
        if project.ga in seen:
            warnings.append(
                f"Maven: skipping {project.pom_path} - {project.ga} already provided by {seen[project.ga]}"
            )
            continue
        seen[project.ga] = project.pom_path
        roots.append(project)
    return roots


__all__ = [
    "MavenError",
    "parse_analyze",
    "parse_plugins",
    "parse_tree",
    "reactor_roots",
    "run_maven",
]
