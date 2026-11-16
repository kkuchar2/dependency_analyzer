from __future__ import annotations

from maven_dep_graph.maven_runner import parse_analyze, parse_plugins, parse_tree
from maven_dep_graph.models import SHIPPED_SCOPES

VERBOSE_TREE = r"""com.example:app-service:jar:1.0.0
+- com.example:lib-utils:jar:1.0.0:compile
|  +- (com.example:lib-core:jar:1.0.0:compile - version managed from 1.0.0; omitted for duplicate)
|  +- com.google.code.gson:gson:jar:2.11.0:compile (version managed from 2.11.0)
|  |  \- com.google.errorprone:error_prone_annotations:jar:2.27.0:compile
|  \- org.mockito:mockito-core:jar:5.14.2:compile
|     +- net.bytebuddy:byte-buddy:jar:1.15.4:compile
|     \- org.objenesis:objenesis:jar:3.3:runtime
+- com.example:lib-core:jar:1.0.0:compile
|  \- com.google.guava:guava:jar:33.4.0-jre:compile
|     +- com.google.guava:failureaccess:jar:1.0.2:compile
|     \- (com.google.errorprone:error_prone_annotations:jar:2.36.0:compile - omitted for conflict with 2.27.0)
+- io.netty:netty-transport-native-epoll:jar:linux-x86_64:4.1.0:compile (optional)
\- org.junit.jupiter:junit-jupiter:jar:5.11.4:test
   \- org.junit.jupiter:junit-jupiter-api:jar:5.11.4:test
"""


def test_parse_verbose_tree():
    root = parse_tree(VERBOSE_TREE)
    assert root.ga == "com.example:app-service"
    assert [c.artifact_id for c in root.children] == [
        "lib-utils", "lib-core", "netty-transport-native-epoll", "junit-jupiter"]
    utils = root.children[0]
    assert utils.children[0].omitted and utils.children[0].artifact_id == "lib-core"
    netty = root.children[2]
    assert netty.classifier == "linux-x86_64" and netty.version == "4.1.0" and netty.optional
    # omitted references are not counted
    assert root.children[1].subtree_size() == 2
    assert {n.ga for n in root.walk()} >= {"org.objenesis:objenesis", "com.google.guava:failureaccess"}


def test_removable_with_accounts_for_other_paths():
    root = parse_tree(VERBOSE_TREE)
    # lib-core is still reachable through lib-utils -> nothing disappears
    assert root.removable_with("com.example:lib-core") == set()
    # removing lib-utils drops its own subtree, but lib-core stays (direct)
    assert root.removable_with("com.example:lib-utils") == {
        "com.example:lib-utils", "com.google.code.gson:gson", "org.mockito:mockito-core",
        "net.bytebuddy:byte-buddy", "org.objenesis:objenesis"}
    # error_prone_annotations still comes via guava (omitted-for-conflict edge)
    assert "com.google.errorprone:error_prone_annotations" not in root.removable_with("com.example:lib-utils")
    # test scope does not affect the shipped classpath
    assert root.removable_with("org.junit.jupiter:junit-jupiter", SHIPPED_SCOPES) == set()


def test_parse_plugins():
    text = """
The following plugins have been resolved:
   org.apache.maven.plugins:maven-clean-plugin:maven-plugin:3.2.0
      org.apache.maven.plugins:maven-clean-plugin:jar:3.2.0
      org.apache.maven.shared:maven-shared-utils:jar:3.3.4
      commons-io:commons-io:jar:2.6
   org.apache.maven.plugins:maven-jar-plugin:maven-plugin:3.4.2
"""
    plugins = parse_plugins(text)
    assert [(p.artifact_id, p.version) for p in plugins] == [("maven-clean-plugin", "3.2.0"), ("maven-jar-plugin", "3.4.2")]
    assert plugins[0].dependencies == ["org.apache.maven.shared:maven-shared-utils:3.3.4", "commons-io:commons-io:2.6"]


def test_parse_analyze_log():
    log = """[INFO] --- dependency:3.8.1:analyze-only (default-cli) @ lib-core ---
[WARNING] Used undeclared dependencies found:
[WARNING]    org.junit.jupiter:junit-jupiter-api:jar:5.11.4:test
[WARNING] Unused declared dependencies found:
[WARNING]    com.google.guava:guava:jar:33.4.0-jre:compile
[INFO]
[INFO] --- dependency:3.8.1:analyze-only (default-cli) @ lib-utils ---
[WARNING] Non-test scoped test only dependencies found:
[WARNING]    org.mockito:mockito-core:jar:5.14.2:compile
[INFO] ------------------------------------------------------------------------
[INFO] --- dependency:3.8.1:analyze-only (default-cli) @ app ---
[INFO] No dependency problems found
"""
    res = parse_analyze(log)
    assert res["lib-core"].used_undeclared == ["org.junit.jupiter:junit-jupiter-api:jar:5.11.4:test"]
    assert res["lib-core"].unused_declared == ["com.google.guava:guava:jar:33.4.0-jre:compile"]
    assert res["lib-utils"].non_test_scoped_test_only == ["org.mockito:mockito-core:jar:5.14.2:compile"]
    assert res["app"].unused_declared == []
