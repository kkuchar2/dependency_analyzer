from __future__ import annotations

from pathlib import Path

from conftest import SAMPLES, pom, write

from maven_dep_graph.effective import ModelBuilder, interpolate
from maven_dep_graph.pom_parser import parse_pom
from maven_dep_graph.scanner import find_pom_files, resolve_parent, scan_projects


def _build(root: Path):
    warnings: list[str] = []
    raws = scan_projects(root, warnings)
    builder = ModelBuilder(raws)
    return {p.artifact_id: builder.build(p) for p in raws}, raws, builder


def test_parse_pom_reads_plugins_and_management():
    raw = parse_pom(SAMPLES / "parent-bom" / "pom.xml")
    assert raw.packaging == "pom"
    assert {p.artifact_id for p in raw.managed_plugins} == {
        "maven-compiler-plugin", "maven-surefire-plugin", "maven-shade-plugin"}
    assert [p.artifact_id for p in raw.plugins] == ["maven-compiler-plugin"]
    assert raw.plugins[0].group_id == "org.apache.maven.plugins"
    assert any(d.scope == "import" for d in raw.managed_dependencies)


def test_scanner_skips_build_dirs(tmp_path):
    write(tmp_path / "a" / "pom.xml", pom("<groupId>g</groupId><artifactId>a</artifactId><version>1</version>"))
    write(tmp_path / "a" / "target" / "pom.xml", pom("<artifactId>x</artifactId>"))
    write(tmp_path / "a" / "src" / "test" / "resources" / "pom.xml", pom("<artifactId>y</artifactId>"))
    assert [p.parent.name for p in find_pom_files(tmp_path)] == ["a"]


def test_inheritance_and_management_on_samples():
    eff, _, _ = _build(SAMPLES)
    core = eff["lib-core"]
    deps = {d.ga: d for d in core.dependencies}
    # inherited from parent <dependencies>
    assert deps["org.apache.commons:commons-lang3"].inherited
    assert deps["org.apache.commons:commons-lang3"].version == "3.17.0"
    assert deps["org.junit.jupiter:junit-jupiter"].scope == "test"
    # version from parent's dependencyManagement via property
    assert deps["org.slf4j:slf4j-api"].version == "2.0.17"
    assert deps["org.slf4j:slf4j-api"].version_source == "managed by parent-bom"
    # junit-bom is external -> unknown version, recorded
    assert "org.junit:junit-bom:5.11.4" in core.external_boms
    # plugin version from pluginManagement, inherited from parent build/plugins
    compiler = next(p for p in core.plugins if p.artifact_id == "maven-compiler-plugin")
    assert compiler.version == "3.13.0" and compiler.inherited


def test_child_overrides_and_project_version_interpolated_in_child_context(tmp_path):
    write(tmp_path / "parent" / "pom.xml", pom("""
      <groupId>g</groupId><artifactId>parent</artifactId><version>2.0.0-SNAPSHOT</version><packaging>pom</packaging>
      <dependencyManagement><dependencies>
        <dependency><groupId>g</groupId><artifactId>lib</artifactId><version>${project.version}</version></dependency>
      </dependencies></dependencyManagement>"""))
    write(tmp_path / "lib" / "pom.xml", pom("""
      <parent><groupId>g</groupId><artifactId>parent</artifactId><version>1.5.0</version></parent>
      <artifactId>lib</artifactId>"""))
    write(tmp_path / "app" / "pom.xml", pom("""
      <parent><groupId>g</groupId><artifactId>parent</artifactId><version>1.5.0</version></parent>
      <artifactId>app</artifactId><version>3.0.0</version>
      <dependencies><dependency><groupId>g</groupId><artifactId>lib</artifactId></dependency></dependencies>"""))
    eff, raws, _ = _build(tmp_path)
    # parent found locally despite version mismatch (CI-released 1.5.0 vs local 2.0.0-SNAPSHOT)
    assert [a.artifact_id for a in eff["app"].ancestors] == ["parent"]
    # ${project.version} in the parent's DM resolves to the *child's* version, like Maven does
    assert eff["app"].dependencies[0].version == "3.0.0"
    assert eff["lib"].version == "1.5.0"  # inherited from <parent><version>


def test_local_bom_import(tmp_path):
    write(tmp_path / "bom" / "pom.xml", pom("""
      <groupId>g</groupId><artifactId>bom</artifactId><version>1</version><packaging>pom</packaging>
      <dependencyManagement><dependencies>
        <dependency><groupId>x</groupId><artifactId>y</artifactId><version>9.9</version></dependency>
      </dependencies></dependencyManagement>"""))
    write(tmp_path / "app" / "pom.xml", pom("""
      <groupId>g</groupId><artifactId>app</artifactId><version>1</version>
      <dependencyManagement><dependencies>
        <dependency><groupId>g</groupId><artifactId>bom</artifactId><version>1</version>
          <type>pom</type><scope>import</scope></dependency>
      </dependencies></dependencyManagement>
      <dependencies><dependency><groupId>x</groupId><artifactId>y</artifactId></dependency></dependencies>"""))
    eff, _, _ = _build(tmp_path)
    dep = eff["app"].dependencies[0]
    assert dep.version == "9.9"
    assert dep.version_source == "managed by BOM g:bom"
    assert eff["app"].external_boms == []


def test_external_parent_and_duplicates(tmp_path):
    write(tmp_path / "app" / "pom.xml", pom("""
      <parent><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter-parent</artifactId>
        <version>3.4.0</version></parent>
      <artifactId>app</artifactId>
      <dependencies>
        <dependency><groupId>a</groupId><artifactId>b</artifactId></dependency>
        <dependency><groupId>a</groupId><artifactId>b</artifactId></dependency>
      </dependencies>"""))
    eff, _, _ = _build(tmp_path)
    app = eff["app"]
    assert app.external_parent == "org.springframework.boot:spring-boot-starter-parent:3.4.0"
    assert app.duplicate_keys == ["a:b:jar"]
    assert app.dependencies[0].version is None
    assert "managed externally" in app.dependencies[0].version_source


def test_resolve_parent_by_path_or_name():
    warnings: list[str] = []
    raws = scan_projects(SAMPLES, warnings)
    assert resolve_parent(raws, str(SAMPLES / "parent-bom"), warnings).artifact_id == "parent-bom"
    assert resolve_parent(raws, str(SAMPLES / "parent-bom" / "pom.xml"), warnings).artifact_id == "parent-bom"
    assert resolve_parent(raws, "com.example:parent-bom", warnings).artifact_id == "parent-bom"


def test_interpolate_nested():
    assert interpolate("${a}", {"a": "${b}", "b": "1"}) == "1"
    assert interpolate("${missing}", {}) == "${missing}"
