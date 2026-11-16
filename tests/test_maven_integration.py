"""Runs real Maven on a copy of the samples. Opt-in: ``MDG_MAVEN_TESTS=1 pytest``."""

from __future__ import annotations

import os
import shutil

import pytest
from conftest import SAMPLES

from maven_dep_graph.analyzer import Options, analyze

pytestmark = pytest.mark.skipif(
    os.environ.get("MDG_MAVEN_TESTS") != "1" or shutil.which("mvn") is None,
    reason="set MDG_MAVEN_TESTS=1 and have mvn on PATH (needs network or a warm ~/.m2)",
)


def test_maven_analyze_end_to_end(tmp_path):
    ws = tmp_path / "ws"
    shutil.copytree(SAMPLES, ws, ignore=shutil.ignore_patterns("target"))
    r = analyze(Options(root=ws, parent=str(ws / "parent-bom"), maven=True, maven_analyze=True,
                        log_dir=tmp_path / "logs"), progress=lambda *_: None)
    assert r.maven is not None and not r.maven.failed_projects
    assert "com.example:app-service" in r.maven.trees
    by = {(f.rule, f.project, f.artifact) for f in r.findings}
    assert ("unused-dependency", "lib-core", "com.google.guava:guava") in by
    assert ("test-dependency-in-shipped-scope", "lib-utils", "org.mockito:mockito-core") in by
    # junit-jupiter is an aggregator: used through junit-jupiter-api in lib-core/lib-utils
    jupiter = next(f for f in r.findings if f.rule == "inherited-unused" and "junit-jupiter" in f.artifact)
    assert "used only by lib-core, lib-utils, unused in app-service" in jupiter.message
    assert "moving it into the children does not help" in jupiter.action  # junit = keep
    # lib-core is unused in app-service but still arrives through lib-utils -> nothing removable, low
    core = next(f for f in r.findings if f.rule == "unused-dependency" and f.project == "app-service")
    assert core.removable_artifacts == 0 and core.severity.value == "low"
    assert r.maven.plugins["com.example:app-service"]
    # removing guava everywhere drops guava + 5 of its deps; error_prone_annotations stays (gson needs it)
    assert r.elimination["com.google.guava:guava"].brings == 6
    lang = r.elimination["org.apache.commons:commons-lang3"]
    assert lang.effort == "trivial" and lang.brings == 1
