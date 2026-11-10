import json
from dataclasses import dataclass

from .document import Document


@dataclass(frozen=True)
class Finding:
    """One value the crawler extracted from a page or from the site as a whole."""

    group: str
    key: str
    value: str
    extra: dict | None = None


def parse_page(html: str, url: str) -> Document:
    return Document(url).parse(html.encode("utf-8"))


def doc_markup(doc: Document) -> str:
    """Rebuild searchable markup (start tags and script bodies) from a parsed document."""
    tags = (
        "<{} {}>".format(
            item["tag"],
            " ".join(
                f'{name}="{value or ""}"' for name, value in item["attrs"].items()
            ),
        )
        for item in doc.elements
    )
    return "\n".join([*tags, *doc.script_blocks])


def json_ld_items(doc: Document) -> list[dict]:
    """Return the top-level JSON-LD objects, flattening lists and ``@graph``; malformed blocks are skipped."""
    items: list[dict] = []
    for block in doc.script_blocks:
        try:
            data = json.loads(block)
        except (ValueError, RecursionError):
            continue
        pending = data if isinstance(data, list) else [data]
        for node in pending:
            if not isinstance(node, dict):
                continue
            items.append(node)
            graph = node.get("@graph")
            if isinstance(graph, list):
                items.extend(entry for entry in graph if isinstance(entry, dict))
    return items


def schema_types(node: dict) -> list[str]:
    value = node.get("@type")
    values = value if isinstance(value, list) else [value]
    return [item for item in values if isinstance(item, str) and item]
