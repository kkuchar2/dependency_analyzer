from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"

# Fake jars for the static usage scan: g:a:v -> class names inside.
FAKE_ARTIFACTS = {
    "org.slf4j:slf4j-api:2.0.17": ["org.slf4j.Logger", "org.slf4j.LoggerFactory"],
    "org.slf4j:slf4j-api:2.0.16": ["org.slf4j.Logger", "org.slf4j.LoggerFactory"],
    "org.slf4j:slf4j-api:2.0.9": ["org.slf4j.Logger", "org.slf4j.LoggerFactory"],
    "com.google.guava:guava:33.4.0-jre": ["com.google.common.collect.ImmutableList"],
    "com.google.code.gson:gson:2.11.0": ["com.google.gson.Gson"],
    "com.fasterxml.jackson.core:jackson-databind:2.18.2": ["com.fasterxml.jackson.databind.ObjectMapper"],
    "org.mockito:mockito-core:5.14.2": ["org.mockito.Mockito"],
    "org.assertj:assertj-core:3.27.0": ["org.assertj.core.api.Assertions"],
    "org.apache.commons:commons-lang3:3.17.0": ["org.apache.commons.lang3.StringUtils"],
    "org.junit.jupiter:junit-jupiter-api:5.11.4": ["org.junit.jupiter.api.Test", "org.junit.jupiter.api.Assertions"],
    "ch.qos.logback:logback-classic:1.5.15": ["ch.qos.logback.classic.Logger"],
    "org.apache.logging.log4j:log4j-core:2.24.3": ["org.apache.logging.log4j.core.Logger"],
    "junit:junit:4.13.2": ["org.junit.Test", "junit.framework.TestCase"],
}


def make_jar(repo: Path, gav: str, classes: list[str]) -> Path:
    g, a, v = gav.split(":")
    d = repo.joinpath(*g.split("."), a, v)
    d.mkdir(parents=True, exist_ok=True)
    jar = d / f"{a}-{v}.jar"
    with zipfile.ZipFile(jar, "w") as zf:
        zf.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")
        for cls in classes:
            zf.writestr(cls.replace(".", "/") + ".class", b"\xca\xfe\xba\xbe")
    return jar


def make_pom(repo: Path, gav: str, deps: list[str]) -> None:
    g, a, v = gav.split(":")
    d = repo.joinpath(*g.split("."), a, v)
    d.mkdir(parents=True, exist_ok=True)
    body = "".join(
        f"<dependency><groupId>{x.split(':')[0]}</groupId><artifactId>{x.split(':')[1]}</artifactId>"
        f"<version>{x.split(':')[2]}</version></dependency>"
        for x in deps
    )
    (d / f"{a}-{v}.pom").write_text(
        f'<project xmlns="http://maven.apache.org/POM/4.0.0"><dependencies>{body}</dependencies></project>'
    )


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "m2"
    for gav, classes in FAKE_ARTIFACTS.items():
        make_jar(repo, gav, classes)
    # junit-jupiter is an aggregator: empty jar + pom pointing at the api.
    make_jar(repo, "org.junit.jupiter:junit-jupiter:5.11.4", [])
    make_pom(repo, "org.junit.jupiter:junit-jupiter:5.11.4", ["org.junit.jupiter:junit-jupiter-api:5.11.4"])
    return repo


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def pom(body: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<project xmlns="http://maven.apache.org/POM/4.0.0">\n'
        "<modelVersion>4.0.0</modelVersion>\n" + body + "\n</project>\n"
    )
