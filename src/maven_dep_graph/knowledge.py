"""Small curated knowledge base used by the reduction rules.

Extend freely - matching is by ``groupId`` prefix or exact ``groupId:artifactId``.
"""

from __future__ import annotations

# Libraries that only make sense on the test classpath.
TEST_LIBRARIES = [
    "junit:junit",
    "org.junit.jupiter:",
    "org.junit.vintage:",
    "org.junit.platform:",
    "org.testng:testng",
    "org.mockito:",
    "org.easymock:",
    "org.jmock:",
    "io.mockk:",
    "org.powermock:",
    "org.assertj:",
    "org.hamcrest:",
    "com.google.truth:",
    "org.spockframework:",
    "org.testcontainers:",
    "com.github.tomakehurst:wiremock",
    "org.wiremock:",
    "io.rest-assured:",
    "org.springframework.boot:spring-boot-starter-test",
    "org.springframework:spring-test",
    "org.awaitility:",
    "com.h2database:h2",
    "org.xmlunit:",
    "org.skyscreamer:jsonassert",
    "com.jayway.jsonpath:json-path-assert",
    "net.javacrumbs.json-unit:",
    "io.cucumber:",
    "org.dbunit:",
    "org.mock-server:",
    "com.tngtech.archunit:",
    "org.jacoco:",
]

# Families of libraries that do the same job. Using several members of one
# family multiplies the attack surface without adding capability.
OVERLAP_GROUPS: dict[str, list[str]] = {
    "JSON binding": [
        "com.fasterxml.jackson.core:jackson-databind",
        "com.google.code.gson:gson",
        "org.json:json",
        "com.alibaba:fastjson",
        "com.alibaba.fastjson2:",
        "jakarta.json.bind:",
        "javax.json.bind:",
        "org.eclipse:yasson",
        "com.squareup.moshi:",
        "net.minidev:json-smart",
        "com.googlecode.json-simple:",
        "com.jsoniter:",
    ],
    "Logging backend": [
        "ch.qos.logback:logback-classic",
        "org.apache.logging.log4j:log4j-core",
        "log4j:log4j",
        "org.slf4j:slf4j-simple",
        "org.slf4j:slf4j-jdk14",
        "org.slf4j:slf4j-reload4j",
        "org.slf4j:slf4j-log4j12",
        "ch.qos.reload4j:reload4j",
        "org.tinylog:tinylog-impl",
    ],
    "Logging facade": [
        "commons-logging:commons-logging",
        "org.jboss.logging:jboss-logging",
        "org.apache.logging.log4j:log4j-api",
        "org.slf4j:slf4j-api",
        "com.google.flogger:",
    ],
    "HTTP client": [
        "org.apache.httpcomponents:httpclient",
        "org.apache.httpcomponents.client5:",
        "com.squareup.okhttp3:okhttp",
        "com.squareup.okhttp:okhttp",
        "org.eclipse.jetty:jetty-client",
        "io.netty:netty-codec-http",
        "org.asynchttpclient:",
        "com.konghq:unirest-java",
        "com.mashape.unirest:",
        "commons-httpclient:commons-httpclient",
    ],
    "Mocking": ["org.mockito:", "org.easymock:", "org.jmock:", "org.powermock:", "io.mockk:"],
    "Assertions": ["org.assertj:", "org.hamcrest:", "com.google.truth:"],
    "Test framework": ["junit:junit", "org.junit.jupiter:", "org.testng:testng", "org.spockframework:"],
    "Utility grab-bag": [
        "com.google.guava:guava",
        "org.apache.commons:commons-lang3",
        "commons-lang:commons-lang",
        "org.apache.commons:commons-collections4",
        "commons-collections:commons-collections",
        "org.eclipse.collections:",
        "io.vavr:",
    ],
    "Commons Lang 2 vs 3": ["commons-lang:commons-lang", "org.apache.commons:commons-lang3"],
    "Commons Collections 3 vs 4": [
        "commons-collections:commons-collections",
        "org.apache.commons:commons-collections4",
    ],
    "XML binding": ["javax.xml.bind:", "jakarta.xml.bind:", "com.sun.xml.bind:", "org.glassfish.jaxb:"],
    "YAML": ["org.yaml:snakeyaml", "com.fasterxml.jackson.dataformat:jackson-dataformat-yaml", "org.snakeyaml:"],
    "Bytecode generation": ["cglib:", "net.bytebuddy:byte-buddy", "org.javassist:javassist", "org.ow2.asm:asm"],
    "Connection pool": ["com.zaxxer:HikariCP", "com.mchange:c3p0", "org.apache.commons:commons-dbcp2", "commons-dbcp:"],
}

# Overlaps that are common and often legitimate (bridges, partial use) - reported as low.
LOW_PRIORITY_GROUPS = {"Logging facade", "Utility grab-bag", "Bytecode generation", "Assertions"}


def matches(ga: str, pattern: str) -> bool:
    if pattern.endswith(":") or pattern.endswith("."):
        return ga.startswith(pattern)
    return ga == pattern or ga.startswith(pattern + ":")


def is_test_library(ga: str) -> bool:
    return any(matches(ga, p) for p in TEST_LIBRARIES)


def overlap_members(gas: set[str]) -> dict[str, dict[str, list[str]]]:
    """``{group: {pattern: [matching GAs]}}`` for groups where 2+ distinct patterns match."""
    found: dict[str, dict[str, list[str]]] = {}
    for group, patterns in OVERLAP_GROUPS.items():
        hits: dict[str, list[str]] = {}
        for pattern in patterns:
            members = sorted(ga for ga in gas if matches(ga, pattern))
            if members:
                hits[pattern] = members
        if len(hits) >= 2:
            found[group] = hits
    return found


# ---------------------------------------------------------------- elimination

# Libraries that are infrastructure rather than helpers: rewriting them yourself
# is never "cheap", the right move is to keep one well-patched choice.
KEEP_CATEGORIES: dict[str, list[str]] = {
    "application framework": [
        "org.springframework", "io.quarkus", "io.micronaut", "io.dropwizard", "io.vertx", "jakarta.", "javax.",
        "org.glassfish", "org.jboss", "org.eclipse.jetty", "org.apache.tomcat", "io.undertow",
    ],
    "persistence / database driver": [
        "org.hibernate", "org.postgresql", "com.mysql", "mysql:", "com.oracle", "org.mariadb", "com.microsoft.sqlserver",
        "org.mongodb", "org.mybatis", "org.jooq", "org.flywaydb", "org.liquibase", "com.zaxxer", "redis.clients",
        "io.lettuce", "org.apache.cassandra", "com.datastax",
    ],
    "messaging / RPC / cloud SDK": [
        "org.apache.kafka", "com.rabbitmq", "io.grpc", "com.google.protobuf", "io.netty", "software.amazon",
        "com.amazonaws", "com.azure", "com.google.cloud", "org.apache.activemq", "org.apache.camel",
    ],
    "serialization framework": ["com.fasterxml.jackson", "org.apache.avro", "com.esotericsoftware"],
    "logging": ["org.slf4j", "ch.qos.logback", "org.apache.logging.log4j", "commons-logging", "org.jboss.logging"],
    "observability": ["io.micrometer", "io.opentelemetry", "io.prometheus", "io.sentry", "co.elastic"],
    "security / crypto": ["org.bouncycastle", "com.nimbusds", "io.jsonwebtoken", "org.keycloak", "com.auth0"],
    "test infrastructure": [
        "org.junit", "junit:", "org.testng", "org.mockito", "org.testcontainers", "org.spockframework",
        "io.rest-assured", "com.github.tomakehurst", "org.wiremock", "org.awaitility",
    ],
}

# Practical replacement hints for common helper libraries.
REPLACEMENT_HINTS: list[tuple[str, str]] = [
    ("org.apache.commons:commons-lang3", "JDK 11+: String.isBlank/strip/repeat, Objects.requireNonNullElse, "
     "Character/Integer helpers; capitalize/abbreviate/join are 1-5 line private helpers"),
    ("commons-lang:commons-lang", "same as commons-lang3: JDK String/Objects methods or tiny private helpers"),
    ("org.apache.commons:commons-text", "String.formatted/text blocks; StringSubstitutor is ~20 lines of regex"),
    ("com.google.guava:guava", "JDK 9+: List/Set/Map.of, Optional, streams, String.join, Objects.requireNonNull "
     "(Preconditions), java.util.concurrent; caches -> Caffeine only if really needed"),
    ("commons-io:commons-io", "java.nio.file.Files (readString/writeString/copy/walk/delete), "
     "InputStream.transferTo/readAllBytes"),
    ("commons-codec:commons-codec", "java.util.Base64, java.util.HexFormat (17+), java.security.MessageDigest"),
    ("org.apache.commons:commons-collections4", "JDK collections + streams"),
    ("commons-collections:commons-collections", "JDK collections + streams"),
    ("commons-beanutils:commons-beanutils", "records / explicit mapping code"),
    ("joda-time:joda-time", "java.time"),
    ("org.apache.httpcomponents", "java.net.http.HttpClient (JDK 11+)"),
    ("com.squareup.okhttp3:okhttp", "java.net.http.HttpClient (JDK 11+)"),
    ("com.google.code.gson:gson", "the JSON library already standard in the workspace (avoid two JSON stacks)"),
    ("org.json:json", "the JSON library already standard in the workspace"),
    ("com.googlecode.json-simple", "the JSON library already standard in the workspace"),
    ("org.projectlombok:lombok", "records (JDK 16+) or generated code (delombok)"),
    ("com.google.code.findbugs:jsr305", "annotations only - drop them or use one annotation set"),
    ("org.jetbrains:annotations", "annotations only - drop them or use one annotation set"),
    ("javax.annotation:", "annotations only - drop them or use one annotation set"),
    ("org.apache.commons:commons-compress", "java.util.zip covers zip/gzip"),
    ("commons-cli:commons-cli", "simple hand-written argument loop for small CLIs"),
    ("org.yaml:snakeyaml", ".properties files, or the JSON library already in use"),
    ("junit:junit", "JUnit 5 (migrate tests, then drop JUnit 4 and vintage engine)"),
    ("org.hamcrest", "one assertion library (e.g. AssertJ or JUnit assertions)"),
    ("org.assertj", "JUnit 5 Assertions (assertEquals/assertTrue/assertThrows) if usage is simple"),
]


def keep_category(ga: str) -> str | None:
    for category, prefixes in KEEP_CATEGORIES.items():
        if any(ga.startswith(p) for p in prefixes):
            return category
    return None


def replacement_hint(ga: str) -> str:
    for prefix, hint in REPLACEMENT_HINTS:
        if ga == prefix or ga.startswith(prefix if prefix.endswith((":", ".")) else prefix + ":") \
                or (":" not in prefix and ga.startswith(prefix)):
            return hint
    return ""
