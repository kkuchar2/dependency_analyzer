"""End-to-end tests on the bundled sample workspace (static mode, fake local repository)."""

from __future__ import annotations

import json

from conftest import SAMPLES

from maven_dep_graph.analyzer import Options, analyze
from maven_dep_graph.cli import main
from maven_dep_graph.models import Role, Severity, Usage


def _result(fake_repo):
    return analyze(Options(root=SAMPLES, parent=str(SAMPLES / "parent-bom"), local_repo=fake_repo),
                   progress=lambda *_: None)


def _find(result, rule, project=None, artifact=None):
    return [f for f in result.findings if f.rule == rule
            and (project is None or f.project == project)
            and (artifact is None or artifact in f.artifact)]


def test_roles(fake_repo):
    r = _result(fake_repo)
    roles = {p.name: p.role for p in r.projects}
    assert roles == {
        "parent-bom": Role.PARENT, "lib-core": Role.LIB, "lib-utils": Role.LIB,
        "app-service": Role.APP, "standalone-tool": Role.STANDALONE,
    }


def test_static_usage(fake_repo):
    r = _result(fake_repo)
    core = r.usage["com.example:lib-core"]
    assert core["org.slf4j:slf4j-api:jar"].usage == Usage.USED
    assert core["com.google.guava:guava:jar"].usage == Usage.UNUSED
    assert core["org.assertj:assertj-core:jar"].usage == Usage.TEST_ONLY
    # aggregator jar resolved through its pom
    assert core["org.junit.jupiter:junit-jupiter:jar"].usage == Usage.TEST_ONLY
    utils = r.usage["com.example:lib-utils"]
    assert utils["org.mockito:mockito-core:jar"].usage == Usage.TEST_ONLY
    assert utils["com.example:lib-core:jar"].usage == Usage.USED  # internal, via sources
    tool = r.usage["com.example.tools:standalone-tool"]
    assert tool["ch.qos.logback:logback-classic:jar"].usage == Usage.RUNTIME


def test_findings(fake_repo):
    r = _result(fake_repo)
    assert _find(r, "unused-dependency", "lib-core", "guava")[0].severity == Severity.HIGH
    assert _find(r, "unused-dependency", "lib-utils", "jackson-databind")
    assert _find(r, "unused-dependency", "app-service", "lib-core")
    assert _find(r, "test-dependency-in-shipped-scope", "lib-utils", "mockito-core")
    inherited = _find(r, "inherited-unused", "parent-bom", "commons-lang3")[0]
    assert "app-service" in inherited.message and "lib-utils" in inherited.message
    # never "move it to the children" - either eliminate it or keep it
    assert "Eliminate org.apache.commons:commons-lang3" in inherited.action
    assert "declare" not in inherited.action
    assert _find(r, "inherited-unused", "parent-bom", "junit-jupiter")  # app-service has no tests
    assert _find(r, "version-override", "lib-utils", "slf4j-api")
    assert _find(r, "version-drift", "*", "slf4j-api")
    assert _find(r, "overlapping-libraries", "*", "gson")
    assert _find(r, "overlapping-libraries", "*", "logback-classic")
    assert _find(r, "unused-managed-dependency", "parent-bom", "commons-io")
    assert _find(r, "unused-managed-plugin", "parent-bom", "maven-shade-plugin")
    assert not _find(r, "unused-managed-plugin", "parent-bom", "maven-surefire-plugin")
    assert not r.cycles
    # sorted by severity
    ranks = [f.severity.rank for f in r.findings]
    assert ranks == sorted(ranks)


def test_cli_writes_reports(fake_repo, tmp_path, capsys):
    out = tmp_path / "out"
    rc = main(["-d", str(SAMPLES), "-p", str(SAMPLES / "parent-bom"), "-o", str(out),
               "--local-repo", str(fake_repo), "--non-interactive"])
    assert rc == 0
    for name in ("report.md", "report.json", "inventory.csv", "findings.csv"):
        assert (out / name).is_file()
    data = json.loads((out / "report.json").read_text())
    assert data["parent"] == "com.example:parent-bom"
    assert {p["artifactId"] for p in data["projects"]} >= {"lib-core", "app-service"}
    md = (out / "report.md").read_text()
    assert "## Action plan" in md and "```mermaid" in md
    assert "Top findings" in capsys.readouterr().out


def test_cli_bad_parent(tmp_path, capsys):
    rc = main(["-d", str(SAMPLES), "-p", "does-not-exist", "-o", str(tmp_path), "--non-interactive"])
    assert rc == 1
    assert "neither an existing path" in capsys.readouterr().err


def test_elimination_footprint(fake_repo):
    r = _result(fake_repo)
    lang = r.elimination["org.apache.commons:commons-lang3"]
    assert lang.effort == "trivial"
    assert lang.classes == ["org.apache.commons.lang3.StringUtils"]
    assert lang.members == {"StringUtils.capitalize": 1}
    assert lang.used_by_main == {"lib-core": 1}  # one call site
    assert lang.declared_in == ["parent-bom"]
    assert set(lang.forced_on) == {"lib-core", "lib-utils", "app-service"}
    assert "JDK" in lang.hint
    assert r.elimination["com.google.guava:guava"].effort == "none"
    assert r.elimination["org.slf4j:slf4j-api"].effort == "keep"
    assert r.elimination["org.mockito:mockito-core"].used_by_test == {"lib-utils": 2}
    elim = _find(r, "eliminate-dependency", "*", "commons-lang3")[0]
    assert elim.severity == Severity.HIGH and "StringUtils.capitalize x1" in elim.message
    gson = _find(r, "eliminate-dependency", "*", "gson")[0]
    assert "JSON library already standard" in gson.action
