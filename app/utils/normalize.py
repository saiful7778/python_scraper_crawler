from typing import Any

_TRANS = str.maketrans(
    {
        "\u00a0": " ",
        "\u202f": " ",
        "\u2007": " ",
        "\u2009": " ",
        "\u200b": "",
        "\ufeff": "",
    }
)


def normalize_unicode_text(v: Any) -> str:
    return v.translate(_TRANS).strip() if isinstance(v, str) else ""
