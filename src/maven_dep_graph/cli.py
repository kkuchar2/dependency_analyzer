"""Command line entry point: ``maven-dep-graph``.

Two ways to use it:

* interactive - just run ``maven-dep-graph``: it asks for the directory, the
  parent and the analysis mode, runs the analysis and opens a small menu to
  browse the results;
* scripted - pass ``-d`` and ``-p`` (and ``--non-interactive`` in CI): it runs,
  writes the reports and prints a summary.

All analysis lives in ``analyzer.py``; this file only asks questions and prints.
"""

from __future__ import annotations

import argparse
import shlex
import sys
from pathlib import Path

from . import __version__
from .analyzer import AnalysisError, Options, analyze
from .models import EFFORT_ORDER, AnalysisResult
from .report import headline, render_console, write_reports
from .scanner import scan_projects

MODES = {
    "1": ("fast", "POM model + static source scan (offline, seconds)"),
    "2": ("maven", "+ full transitive trees and build plugins (runs mvn)"),
    "3": ("maven-analyze", "+ compile and bytecode-level unused detection (slowest, most accurate)"),
}


# ------------------------------------------------------------------ small input helpers


def ask(question: str, default: str | None = None) -> str:
    """Ask until a non-empty answer is given (or the default is accepted with Enter)."""
    suffix = f" [{default}]" if default else ""
    while True:
        answer = input(f"{question}{suffix}: ").strip()
        if answer:
            return answer
        if default is not None:
            return default
        print("  Please enter a value.")


def choose(question: str, options: list[str], default: int | None = None) -> int:
    """Show a numbered list and return the 0-based index of the chosen option."""
    for i, option in enumerate(options, 1):
        print(f"  {i}) {option}")
    while True:
        answer = ask(question, str(default + 1) if default is not None else None)
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return int(answer) - 1
        print(f"  Enter a number from 1 to {len(options)}.")


def pause() -> None:
    input("\n(Enter to go back) ")


# ------------------------------------------------------------------ arguments


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="maven-dep-graph",
        description="Find unused, misplaced and duplicated dependencies in a directory of Maven projects "
                    "that share a parent POM. Run without arguments for interactive mode.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("-d", "--directory", help="Directory containing all Maven projects")
    parser.add_argument("-p", "--parent", help="Parent project: directory, pom.xml, or its artifactId")
    parser.add_argument("-o", "--output", default="dependency-report", help="Where to write the reports")
    parser.add_argument("--maven", action="store_true", help="Also run Maven for transitive trees and plugins")
    parser.add_argument("--maven-analyze", action="store_true", help="Like --maven + bytecode unused detection")
    parser.add_argument("--mvn", help="Maven executable (default: mvn on PATH)")
    parser.add_argument("--maven-args", default="", help='Extra Maven args, e.g. "-o -s settings.xml"')
    parser.add_argument("--maven-timeout", type=int, help="Timeout in seconds for each Maven run")
    parser.add_argument("--local-repo", help="Local Maven repository (default: ~/.m2/repository)")
    parser.add_argument("--no-static-usage", action="store_true", help="Skip the static source scan")
    parser.add_argument("--heavy-threshold", type=int, default=10,
                        help="Flag direct deps that bring at least this many artifacts")
    parser.add_argument("--top", type=int, default=25, help="Findings shown in the printed summary")
    parser.add_argument("--non-interactive", action="store_true", help="Never prompt; fail if -d/-p are missing")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


# ------------------------------------------------------------------ interactive setup


def ask_directory() -> Path:
    while True:
        path = Path(ask("Directory with all projects", ".")).expanduser()
        if path.is_dir():
            return path
        print(f"  Not a directory: {path}")


def ask_parent(root: Path) -> str:
    """Offer the pom-packaged projects found under ``root``; any path/artifactId can be typed instead."""
    poms = sorted((p for p in scan_projects(root, []) if p.packaging == "pom"), key=lambda p: p.artifact_id)
    if not poms:
        return ask("Path to the parent project (or its artifactId)")
    print("\nWhich project is the shared parent?")
    labels = [f"{p.artifact_id:<30} {p.pom_path.parent}" for p in poms] + ["other (type a path)"]
    index = choose("Parent", labels, default=0)
    if index == len(poms):
        return ask("Path to the parent project (or its artifactId)")
    return str(poms[index].pom_path)


def ask_mode(args: argparse.Namespace) -> None:
    print("\nAnalysis mode:")
    keys = list(MODES)
    index = choose("Mode", [f"{name:<14} {text}" for name, text in MODES.values()], default=0)
    mode = MODES[keys[index]][0]
    args.maven = mode in ("maven", "maven-analyze")
    args.maven_analyze = mode == "maven-analyze"
    if args.maven and not args.maven_args:
        args.maven_args = input("Extra Maven args (Enter for none): ").strip()


# ------------------------------------------------------------------ result browser


def show_summary(result: AnalysisResult) -> None:
    h = headline(result)
    print(f"\nRoot:     {result.root_dir}")
    print(f"Parent:   {result.parent.ga}:{result.parent.version}")
    print(f"Projects: {h['projects']}  " + ", ".join(f"{k}: {v}" for k, v in sorted(h["roles"].items())))
    print(f"External libraries declared: {h['external_direct_artifacts']} "
          f"({h['external_direct_shipped_artifacts']} shipped)")
    if "external_artifacts_total" in h:
        print(f"Incl. transitive: {h['external_artifacts_total']} ({h['external_artifacts_shipped']} shipped)")
    print("Findings: " + ", ".join(f"{s}: {h['findings'].get(s, 0)}" for s in ("high", "medium", "low", "info")))
    for warning in result.warnings:
        print(f"Warning: {warning}")


def show_findings(result: AnalysisResult) -> None:
    levels = ["high", "medium", "low", "info"]
    index = choose("Show findings with severity at least", levels, default=1)
    wanted = set(levels[: index + 1])
    findings = [f for f in result.findings if f.severity.value in wanted]
    print()
    for f in findings:
        print(f"[{f.severity.value.upper():<6}] {f.project}: {f.message}")
        print(f"         -> {f.action}")
    print(f"\n{len(findings)} finding(s).")


def show_project(result: AnalysisResult) -> None:
    projects = result.projects
    index = choose("Project", [f"{p.name:<30} {p.role.value}" for p in projects])
    project = projects[index]
    print(f"\n{project.ga}:{project.version}  ({project.role.value}, {project.packaging})")
    print(f"POM: {project.raw.pom_path}")

    print("\nDependencies:")
    for d in sorted(project.dependencies, key=lambda d: (d.scope, d.ga)):
        usage = result.usage.get(project.ga, {}).get(d.key)
        notes = [n for n in (
            "inherited from " + d.declared_in if d.inherited else "",
            "internal" if d.internal else "",
            usage.usage.value if usage else "",
        ) if n]
        print(f"  {d.scope:<9} {d.ga}:{d.version or '?'}" + (f"   ({', '.join(notes)})" if notes else ""))

    print("\nPlugins:")
    for plugin in project.plugins:
        print(f"  {plugin.ga}:{plugin.version or '?'}")

    own = [f for f in result.findings if f.project == project.name]
    print(f"\nFindings ({len(own)}):")
    for f in own:
        print(f"  [{f.severity.value.upper():<6}] {f.message}")
        print(f"           -> {f.action}")


def show_elimination(result: AnalysisResult) -> None:
    if not result.elimination:
        print("\nNo elimination analysis (static usage scan was skipped).")
        return
    rows = sorted(result.elimination.values(),
                  key=lambda e: (EFFORT_ORDER.index(e.effort) if e.effort in EFFORT_ORDER else 99, e.ga))
    print(f"\n{'effort':<8} {'library':<52} {'refs':>5} {'projects':>8}  hint")
    for e in rows:
        users = set(e.used_by_main) | set(e.used_by_test)
        print(f"{e.effort:<8} {e.ga:<52} {e.references:>5} {len(users):>8}  {e.hint or e.keep_reason}")


def browse(result: AnalysisResult, reports: dict[str, Path]) -> None:
    """Main menu after the analysis has run."""
    menu = {
        "1": ("Summary", lambda: show_summary(result)),
        "2": ("Findings", lambda: show_findings(result)),
        "3": ("Project details", lambda: show_project(result)),
        "4": ("Libraries that could be removed (elimination)", lambda: show_elimination(result)),
        "5": ("Report files", lambda: [print(f"  {k:<16} {p}") for k, p in reports.items()]),
    }
    while True:
        print("\n=== Menu ===")
        for key, (label, _) in menu.items():
            print(f"  {key}) {label}")
        print("  q) Quit")
        answer = input("> ").strip().lower()
        if answer in ("q", "quit", "exit"):
            return
        if answer in menu:
            menu[answer][1]()
            pause()
        else:
            print("  Unknown choice.")


# ------------------------------------------------------------------ main


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    interactive = not args.non_interactive and sys.stdin.isatty()

    # 1. Collect inputs (prompt for whatever is missing).
    if not args.directory or not args.parent:
        if not interactive:
            parser.error("--directory and --parent are required (non-interactive)")
        print("Maven dependency reduction analyzer\n")
        root = Path(args.directory).expanduser() if args.directory else ask_directory()
        args.parent = args.parent or ask_parent(root)
        if not (args.maven or args.maven_analyze):
            ask_mode(args)
    else:
        root = Path(args.directory).expanduser()
    if not root.is_dir():
        print(f"Error: not a directory: {root}", file=sys.stderr)
        return 2

    # 2. Run the analysis.
    out_dir = Path(args.output).expanduser()
    opts = Options(
        root=root,
        parent=args.parent,
        static_usage=not args.no_static_usage,
        maven=args.maven or args.maven_analyze,
        maven_analyze=args.maven_analyze,
        mvn=args.mvn,
        maven_args=shlex.split(args.maven_args),
        maven_timeout=args.maven_timeout,
        local_repo=Path(args.local_repo).expanduser() if args.local_repo else None,
        log_dir=out_dir,
        heavy_threshold=args.heavy_threshold,
    )
    print("\nAnalyzing...", file=sys.stderr)
    try:
        result = analyze(opts, progress=lambda msg: print(msg, file=sys.stderr))
    except AnalysisError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    # 3. Write reports, then print a summary or open the menu.
    reports = write_reports(result, out_dir)
    if interactive:
        show_summary(result)
        print(f"\nReports written to {out_dir}/")
        browse(result, reports)
    else:
        print(render_console(result, limit=args.top))
        print("Reports:")
        for kind, path in reports.items():
            print(f"  {kind:<16} {path}")
    return 0


def run() -> int:
    """Console-script entry: like ``main`` but Ctrl-C / Ctrl-D just exit quietly."""
    try:
        return main()
    except (KeyboardInterrupt, EOFError):
        print("\nBye.")
        return 130


if __name__ == "__main__":
    raise SystemExit(run())
