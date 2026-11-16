"""Command line entry point: ``maven-dep-graph``."""

from __future__ import annotations

import argparse
import shlex
import sys
from pathlib import Path

from . import __version__
from .analyzer import AnalysisError, Options, analyze
from .report import render_console, write_reports


def _prompt(message: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        value = input(f"{message}{suffix}: ").strip()
        if value:
            return value
        if default is not None:
            return default
        print("  Please enter a value.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="maven-dep-graph",
        description=(
            "Inventory every dependency and plugin of a directory full of Maven projects that share a "
            "parent POM, detect unused / misplaced / duplicated dependencies and produce an action plan "
            "to shrink the dependency (and vulnerability) surface."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("-d", "--directory", help="Directory containing all Maven projects (searched recursively)")
    parser.add_argument(
        "-p", "--parent",
        help="Path to the parent project (directory or pom.xml). An artifactId or groupId:artifactId of a "
             "scanned project is accepted as well.",
    )
    parser.add_argument("-o", "--output", default="dependency-report",
                        help="Output directory for report.md, report.json, inventory.csv, findings.csv")
    parser.add_argument("--maven", action="store_true",
                        help="Run Maven to resolve full transitive trees and all build plugins (needs mvn + network/repo)")
    parser.add_argument("--maven-analyze", action="store_true",
                        help="Like --maven, plus test-compile and dependency:analyze (bytecode-level unused detection)")
    parser.add_argument("--mvn", help="Maven executable (default: mvn on PATH)")
    parser.add_argument("--maven-args", default="",
                        help="Extra arguments for Maven, e.g. \"-o -s ~/.m2/work-settings.xml -Pci\"")
    parser.add_argument("--maven-timeout", type=int, default=None, help="Timeout in seconds for each Maven run")
    parser.add_argument("--no-static-usage", action="store_true",
                        help="Skip the static source scan for unused dependencies")
    parser.add_argument("--local-repo", help="Local Maven repository (default: ~/.m2/repository or settings.xml)")
    parser.add_argument("--heavy-threshold", type=int, default=10,
                        help="Report direct dependencies pulling at least this many transitive artifacts")
    parser.add_argument("--top", type=int, default=25, help="Number of findings printed to the console")
    parser.add_argument("--non-interactive", action="store_true",
                        help="Fail instead of prompting when --directory / --parent are missing")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    directory, parent = args.directory, args.parent
    if not directory or not parent:
        if args.non_interactive or not sys.stdin.isatty():
            parser.error("--directory and --parent are required (non-interactive)")
        print("Maven dependency reduction analyzer")
        print("-----------------------------------")
        if not directory:
            directory = _prompt("Directory with all projects", ".")
        if not parent:
            parent = _prompt("Path to the parent project (or its artifactId)")

    root = Path(directory).expanduser()
    if not root.is_dir():
        print(f"Error: not a directory: {root}", file=sys.stderr)
        return 2

    out_dir = Path(args.output).expanduser()
    opts = Options(
        root=root,
        parent=parent,
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
    progress = lambda msg: print(msg, file=sys.stderr)  # noqa: E731
    try:
        result = analyze(opts, progress=progress)
    except AnalysisError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    paths = write_reports(result, out_dir)
    print(render_console(result, limit=args.top))
    print("Reports:")
    for kind, path in paths.items():
        print(f"  {kind:<16} {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
