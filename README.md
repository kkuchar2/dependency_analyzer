# maven-dep-graph

Shrink the dependency (and vulnerability) surface of a **workspace of Maven
projects that share a parent POM**.

The tool reads every `pom.xml` in a directory, rebuilds each project's
*effective* model (parent inheritance, properties, `dependencyManagement`,
`pluginManagement`, BOM imports), optionally asks real Maven for the full
transitive picture and bytecode usage, and produces:

- a **complete inventory** – runtime, test, provided, transitive dependencies,
  build plugins and the plugins' own dependencies, per project and workspace-wide;
- **unused / misplaced dependency detection** – declared-but-unused, test
  libraries in compile scope, parent `<dependencies>` forced on children that
  never use them, dead `dependencyManagement` / `pluginManagement` entries;
- an **elimination analysis** – for every external library: could it disappear
  from the *whole* workspace, how big is its footprint in the code (classes,
  members, call sites, files, projects), how many artifacts would leave the
  classpath with it, and how hard rewriting it would be
  (`none / trivial / low / medium / high / keep`);
- a prioritised **action plan** where every line is a concrete POM or code change.

Pure Python standard library (3.10+). Maven is only needed for the optional
`--maven` / `--maven-analyze` modes.

---

## Assumed workspace

```
workspace/                    <- --directory
├── company-parent/           <- --parent (path); may be at a newer local SNAPSHOT
│   └── pom.xml                  dependencyManagement also manages the libs' versions
├── lib-a/   (parent = company-parent:1.4.0, a version released by CI)
├── lib-b/   (parent = company-parent:1.4.0, depends on lib-a)
├── service-x/ (parent = company-parent:1.3.2, depends on lib-a, lib-b)
└── some-tool/ (unrelated, no parent)  <- still inventoried, role "standalone"
```

- The parent is identified by **path** (`-p ../workspace/company-parent`); an
  `artifactId` / `groupId:artifactId` also works.
- Children usually reference a parent version **built remotely** (CI release),
  so parents are matched by `groupId:artifactId` **ignoring the version**. The
  static analysis uses the local parent checkout; projects whose declared parent
  version differs are listed (`parent-version-mismatch`). `--maven` resolves the
  real released parent.
- Libs may inherit from the parent *and* be managed by it ("the parent ingests
  them") – both edges are shown in the internal graph.
- Projects are classified as `parent`, `lib` (consumed by other scanned
  projects or managed by the parent), `app` (inherits the parent, nobody depends
  on it), `pom` (pom-packaged descendant) or `standalone`.

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .            # or: pip install -e '.[dev]' for pytest
```

Without installing: `PYTHONPATH=src python3 -m maven_dep_graph ...`

## Usage

```bash
# fast, offline: POM model + static source usage scan
maven-dep-graph -d ~/work -p ~/work/company-parent

# + full transitive trees and every build plugin (needs mvn and repo access)
maven-dep-graph -d ~/work -p ~/work/company-parent --maven

# + compile and bytecode-level unused detection (most accurate, slowest)
maven-dep-graph -d ~/work -p ~/work/company-parent --maven-analyze \
    --maven-args "-s ~/.m2/company-settings.xml -T 1C"

# try it on the bundled sample workspace
maven-dep-graph -d samples -p samples/parent-bom --maven-analyze
```

### Interactive mode

Run with no arguments:

```bash
maven-dep-graph
```

It asks for the projects directory, lets you pick the parent from the
pom-packaged projects it finds, and asks for the mode (fast / maven /
maven-analyze). After the analysis it writes the reports and opens a menu:

```
=== Menu ===
  1) Summary
  2) Findings
  3) Project details
  4) Libraries that could be removed (elimination)
  5) Report files
  q) Quit
```

Pass `-d`/`-p` to skip the questions; add `--non-interactive` (or pipe stdin)
to just print the summary and exit, e.g. in CI.

| Option | Meaning |
|---|---|
| `-d, --directory` | Directory with all projects (searched recursively; `target/`, `src/`, hidden dirs skipped) |
| `-p, --parent` | Path to the parent project dir or `pom.xml` (or its `artifactId`) |
| `-o, --output` | Report directory (default `dependency-report/`) |
| `--maven` | Run `dependency:tree -Dverbose` and `dependency:resolve-plugins` |
| `--maven-analyze` | Also `test-compile` + `dependency:analyze-only` |
| `--mvn` | Maven executable (default `mvn` on `PATH`) |
| `--maven-args` | Extra Maven args, e.g. `"-o -s settings.xml -Pci"` |
| `--maven-timeout` | Seconds per Maven invocation |
| `--local-repo` | Local repository (default `~/.m2/repository` or `settings.xml`) |
| `--no-static-usage` | Skip the source scan (and with it the elimination analysis) |
| `--heavy-threshold` | Flag direct deps that alone bring at least N artifacts (default 10) |
| `--top` | Findings printed to the console (default 25) |

### How Maven is run

All scanned projects are put into **one reactor** via a throw-away aggregator
POM in a temp directory, so sibling projects resolve each other without
`mvn install`. Nothing is written into your projects except the usual
`target/` when compiling (`--maven-analyze`). If the reactor cannot be built
(e.g. one project's parent version is not downloadable), every project missing
a result is retried on its own; failures are listed in the report. Maven logs
are kept in the output directory.

## Output

| File | Content |
|---|---|
| console | headline numbers, per-project table, elimination candidates, top findings |
| `report.md` | full report: summary, **action plan**, **elimination analysis** + rewrite checklist, all findings, per-project tables with usage status, parent analysis (forced deps, `dependencyManagement`/`pluginManagement` usage), Mermaid graph of internal projects, resolved trees, workspace inventory, plugin inventory |
| `report.json` | everything above, machine readable |
| `inventory.csv` | one row per project × dependency / transitive / managed / plugin / plugin-dependency – pivot it in a spreadsheet |
| `findings.csv` | one row per finding |
| `elimination.csv` | one row per external library with footprint and effort |
| `maven-*.log` | Maven output (Maven modes only) |

## Evidence levels

| Evidence | Source | Reliability |
|---|---|---|
| `maven-analyze` | bytecode analysis by `maven-dependency-plugin` | best; blind to reflection / runtime-only use |
| `maven-tree` | resolved (verbose) dependency graph | exact transitive data and "artifacts removed" counts |
| `static-usage` | imports, fully-qualified names and resource files vs. classes in the jars of `~/.m2` | good heuristic; aggregator jars (e.g. `junit-jupiter`, Spring Boot starters) resolved via their POMs |
| `pom` | structure of the POMs | exact for what it states |

## Findings (rules)

| Rule | Severity | Meaning / action |
|---|---|---|
| `unused-dependency` | high (compile) … low (test, or still arrives transitively) | Declared, never used → remove it |
| `test-dependency-in-shipped-scope` | high | Test library (or used only by tests) in compile/runtime scope → `<scope>test</scope>` |
| `inherited-unused` | high/medium/low | Parent `<dependencies>` entry forced on children that don't use it. If nobody uses it → delete. If some do → **eliminate it entirely when cheap**; otherwise keep (moving it into the children does not lower exposure) |
| `eliminate-dependency` | high (trivial, shipped) … info (medium) | Library with a small footprint – rewrite usages with JDK / own helper (hint given) and delete it everywhere |
| `overlapping-libraries` | medium/low | Two libraries for the same job (JSON, logging backend, HTTP client, mocking, JUnit 4+5…) → standardise |
| `version-drift` / `transitive-version-drift` | medium / low | Same artifact in several versions → manage one in the parent |
| `version-override` | medium | Child pins a version different from the managed one |
| `depends-on-pom-artifact` | medium | A project depends on a POM artifact, dragging all its `<dependencies>` into consumers |
| `internal-cycle` | high | Cycle between internal projects |
| `aggregator-dependency` | low | Only a transitive part of an aggregator is used |
| `unused-managed-dependency` / `unused-managed-plugin` | low | Dead `dependencyManagement` / `pluginManagement` entries |
| `unpinned-plugin` | low | Plugin without version (Maven default, often old) |
| `heavy-dependency` | info | Direct dep that alone brings ≥ N artifacts |
| `used-undeclared` | info | Code relies on a transitive dep – declare it before removing what brings it |
| `parent-version-mismatch`, `external-parent`, `inherited-dependency` | info | Context for interpreting static results |

## Elimination analysis

For each external library declared anywhere (parents included):

1. **Footprint** – library classes referenced (from the jar's class list), members
   called (`StringUtils.capitalize x3`), number of references, files, projects
   (main vs. test).
2. **Artifacts gone** – with `--maven`: how many artifacts leave *every* classpath
   if the library is removed from all scanned POMs (edges from internal projects
   are cut too; edges from third-party libraries are kept).
3. **Effort**

   | Effort | Rule of thumb |
   |---|---|
   | none | no reference – just delete it |
   | trivial | ≤ 2 classes, ≤ 10 references, ≤ 3 files |
   | low | ≤ 5 classes, ≤ 40 references |
   | medium | ≤ 15 classes, ≤ 150 references |
   | high | bigger – keep unless strategic |
   | keep | infrastructure: frameworks, drivers, logging, serialization frameworks, test frameworks… |

4. **Replacement hint** for well-known helper libraries (commons-lang/io/codec/
   collections, Guava, Joda-Time, HTTP clients, Gson/org.json, Lombok, JUnit 4, …).

Thresholds, "keep" categories and hints live in
[`knowledge.py`](src/maven_dep_graph/knowledge.py) and
[`elimination.py`](src/maven_dep_graph/elimination.py) – tune them to your codebase.

## Suggested workflow

1. `maven-dep-graph … --maven-analyze`
2. Work through **Action plan** top-down: unused deps, test-scope leaks, dead
   parent `<dependencies>`.
3. Pick 🟢 trivial/low rows of the **Elimination analysis**, rewrite using the
   checklist, delete the library from all POMs listed in *Declared in*.
4. Standardise overlapping libraries and align versions in the parent.
5. Re-run and compare `report.json` / the *Shipped* column.
6. Run a vulnerability scanner (OWASP dependency-check, `osv-scanner`) on what
   remains – fewer artifacts is half of the job, patched versions the other half.

## Limitations

- Static model: profiles other than `activeByDefault` are listed but not applied;
  `${env.*}`/settings properties stay unresolved; external parents/BOMs (e.g.
  `spring-boot-starter-parent`) are opaque without `--maven`.
- Usage detection can't see reflection with computed class names, and
  runtime-scoped libraries (JDBC drivers, logging backends, SPI implementations)
  are reported as "verify", not "unused".
- Instance-method calls are not attributed to members (`gson.toJson(...)` counts
  as a reference to `Gson`, not as `Gson.toJson`); the footprint is a size
  estimate, not a call graph.

## Development

```bash
pip install -e '.[dev]'
pytest                          # unit + end-to-end tests on samples (no Maven)
MDG_MAVEN_TESTS=1 pytest        # also runs real Maven on a copy of samples/
```

Layout (`src/maven_dep_graph/`):

| Module | Responsibility |
|---|---|
| `scanner.py` | find & parse POMs, resolve the parent |
| `pom_parser.py` | raw POM → `RawProject` (no interpolation) |
| `effective.py` | effective model: inheritance, interpolation, management, BOM imports |
| `usage.py` | static unused detection, local-repo jar index |
| `elimination.py` | per-library footprint, artifacts gone, effort rating |
| `maven_runner.py` | reactor/aggregator Maven runs + parsers for tree / plugins / analyze output |
| `rules.py` | findings |
| `knowledge.py` | test libraries, overlap families, keep categories, replacement hints |
| `report.py` | console / Markdown / JSON / CSV |
| `analyzer.py`, `cli.py` | orchestration, command line |

`samples/` is a small workspace with deliberate problems (unused Guava, Mockito
in compile scope, two JSON libraries, a parent forcing commons-lang3 on everyone,
version drift, dead management entries) used by the tests.
