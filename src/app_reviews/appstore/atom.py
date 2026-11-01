"""Reads the App Store RSS XML (Atom) feed into the JSON feed's entry shape."""

from __future__ import annotations

from typing import Any
from xml.etree import ElementTree

__all__ = ["atom_entries"]

_ATOM = "{http://www.w3.org/2005/Atom}"
_ITUNES = "{http://itunes.apple.com/rss}"

_MAX_DEPTH = 32
"""How deep an entry's elements may nest. Apple's deepest is three levels; the
bound keeps a hostile feed from exhausting the recursion limit."""


def atom_entries(body: str) -> list[dict[str, Any]]:
    """The feed's ``<entry>`` elements, each shaped like a JSON feed entry.

    Raises ``ValueError`` (or ``ElementTree.ParseError``) for a body that is not
    an Atom feed. Documents with a DTD are refused: Apple sends none, and it is
    how entity-expansion attacks arrive.
    """
    if "<!DOCTYPE" in body or "<!ENTITY" in body:
        raise ValueError("the XML feed declares a document type")
    root = ElementTree.fromstring(body)
    if root.tag != f"{_ATOM}feed":
        raise ValueError(f"the root element is {root.tag!r}, not an Atom feed")
    return [_node(entry, depth=1) for entry in root.findall(f"{_ATOM}entry")]


def _node(element: ElementTree.Element, *, depth: int) -> dict[str, Any]:
    """Children keyed as the JSON feed keys them; the first of a repeated key wins.

    The JSON feed has only the text ``content``, so the HTML one is dropped.
    Raises ``ValueError`` when elements nest deeper than ``_MAX_DEPTH``.
    """
    if depth > _MAX_DEPTH:
        raise ValueError(f"the XML feed nests deeper than {_MAX_DEPTH} levels")
    node: dict[str, Any] = {}
    for child in element:
        key = _key(child.tag)
        if key is None or key in node:
            continue
        if key == "content" and child.get("type") == "html":
            continue
        node[key] = _value(child, depth=depth)
    return node


def _value(element: ElementTree.Element, *, depth: int) -> dict[str, Any]:
    """``{"label": text, "attributes": {...}}``, or a nested node."""
    if len(element):
        return _node(element, depth=depth + 1)
    value: dict[str, Any] = {}
    if element.text is not None:
        value["label"] = element.text
    if element.attrib:
        value["attributes"] = dict(element.attrib)
    return value


def _key(tag: str) -> str | None:
    """``title`` for Atom, ``im:rating`` for iTunes, None for anything else."""
    if tag.startswith(_ATOM):
        return tag.removeprefix(_ATOM)
    if tag.startswith(_ITUNES):
        return "im:" + tag.removeprefix(_ITUNES)
    return None
