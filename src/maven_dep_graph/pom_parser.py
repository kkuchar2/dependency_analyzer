"""Parse a single ``pom.xml`` into a :class:`RawProject`.

Nothing is interpolated or inherited here - values are kept as written so the
effective-model stage can interpolate them in the context of the *child*
project, which is what Maven does (``${project.version}`` inside a parent's
``dependencyManagement`` means the child's version).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from .models import DEFAULT_PLUGIN_GROUP, ParentRef, Profile, RawDependency, RawPlugin, RawProject


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _child(parent: ET.Element | None, name: str) -> ET.Element | None:
    if parent is None:
        return None
    for child in parent:
        if isinstance(child.tag, str) and _local(child.tag) == name:
            return child
    return None


def _children(parent: ET.Element | None, name: str) -> list[ET.Element]:
    if parent is None:
        return []
    return [c for c in parent if isinstance(c.tag, str) and _local(c.tag) == name]


def _path(root: ET.Element | None, *names: str) -> ET.Element | None:
    current = root
    for name in names:
        current = _child(current, name)
    return current


def _text(el: ET.Element | None, default: str | None = None) -> str | None:
    if el is None or el.text is None:
        return default
    value = el.text.strip()
    return value or default


def _dependency(el: ET.Element, profile: str | None) -> RawDependency | None:
    group_id = _text(_child(el, "groupId"))
    artifact_id = _text(_child(el, "artifactId"))
    if not group_id or not artifact_id:
        return None
    exclusions = []
    for ex in _children(_child(el, "exclusions"), "exclusion"):
        exclusions.append(f"{_text(_child(ex, 'groupId'), '*')}:{_text(_child(ex, 'artifactId'), '*')}")
    return RawDependency(
        group_id=group_id,
        artifact_id=artifact_id,
        version=_text(_child(el, "version")),
        scope=_text(_child(el, "scope")),
        type=_text(_child(el, "type"), "jar") or "jar",
        classifier=_text(_child(el, "classifier")),
        optional=(_text(_child(el, "optional"), "") or "").lower() == "true",
        exclusions=exclusions,
        profile=profile,
    )


def _dependencies(container: ET.Element | None, profile: str | None) -> list[RawDependency]:
    deps = []
    for el in _children(_child(container, "dependencies"), "dependency"):
        dep = _dependency(el, profile)
        if dep:
            deps.append(dep)
    return deps


def _plugins(plugins_el: ET.Element | None, profile: str | None) -> list[RawPlugin]:
    result = []
    for el in _children(plugins_el, "plugin"):
        artifact_id = _text(_child(el, "artifactId"))
        if not artifact_id:
            continue
        result.append(
            RawPlugin(
                group_id=_text(_child(el, "groupId"), DEFAULT_PLUGIN_GROUP) or DEFAULT_PLUGIN_GROUP,
                artifact_id=artifact_id,
                version=_text(_child(el, "version")),
                inherited=(_text(_child(el, "inherited"), "true") or "true").lower() != "false",
                dependencies=_dependencies(el, profile),
                profile=profile,
            )
        )
    return result


def _properties(container: ET.Element | None) -> dict[str, str]:
    props: dict[str, str] = {}
    props_el = _child(container, "properties")
    for child in props_el if props_el is not None else []:
        if isinstance(child.tag, str):
            props[_local(child.tag)] = (child.text or "").strip()
    return props


def _profile(el: ET.Element) -> Profile:
    profile_id = _text(_child(el, "id"), "default") or "default"
    active = (_text(_path(el, "activation", "activeByDefault"), "false") or "").lower() == "true"
    build = _child(el, "build")
    return Profile(
        id=profile_id,
        active_by_default=active,
        dependencies=_dependencies(el, profile_id),
        managed_dependencies=_dependencies(_child(el, "dependencyManagement"), profile_id),
        plugins=_plugins(_child(build, "plugins"), profile_id),
        properties=_properties(el),
    )


def parse_pom(pom_path: Path) -> RawProject:
    root = ET.parse(pom_path).getroot()
    if _local(root.tag) != "project":
        raise ValueError("root element is not <project>")

    parent: ParentRef | None = None
    parent_el = _child(root, "parent")
    if parent_el is not None and _text(_child(parent_el, "artifactId")):
        parent = ParentRef(
            group_id=_text(_child(parent_el, "groupId"), "") or "",
            artifact_id=_text(_child(parent_el, "artifactId"), "") or "",
            version=_text(_child(parent_el, "version")),
            relative_path=_text(_child(parent_el, "relativePath")),
        )

    artifact_id = _text(_child(root, "artifactId"))
    if not artifact_id:
        raise ValueError("missing <artifactId>")
    group_id = _text(_child(root, "groupId")) or (parent.group_id if parent else None) or "unknown"
    version = _text(_child(root, "version")) or (parent.version if parent else None)

    build = _child(root, "build")
    return RawProject(
        pom_path=pom_path.resolve(),
        group_id=group_id,
        artifact_id=artifact_id,
        version=version,
        packaging=_text(_child(root, "packaging"), "jar") or "jar",
        parent=parent,
        properties=_properties(root),
        modules=[m.text.strip() for m in _children(_child(root, "modules"), "module") if m.text and m.text.strip()],
        dependencies=_dependencies(root, None),
        managed_dependencies=_dependencies(_child(root, "dependencyManagement"), None),
        plugins=_plugins(_child(build, "plugins"), None),
        managed_plugins=_plugins(_path(build, "pluginManagement", "plugins"), None),
        profiles=[_profile(p) for p in _children(_child(root, "profiles"), "profile")],
    )
