from html.parser import HTMLParser
from urllib.parse import urljoin


class Document(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.elements = []
        self.text = []
        self.heading_text = []
        self.script_blocks = []
        self._stack = []
        self._buffers = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        record = {"tag": tag, "attrs": dict(attrs), "text": ""}
        self.elements.append(record)
        self._stack.append(record)
        if tag in {"script", "style", "noscript", "template"}:
            self._buffers.append([])
        else:
            self._buffers.append(None)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if not self._stack:
            return
        index = len(self._stack) - 1
        while index >= 0 and self._stack[index]["tag"] != tag:
            index -= 1
        if index < 0:
            return
        record = self._stack[index]
        value = " ".join(record["text"].split())
        record["text"] = value
        if tag.startswith("h") and len(tag) == 2 and value:
            self.heading_text.append((tag, value))
        if tag == "script":
            self.script_blocks.append("".join(self._buffers[index] or []))
        del self._stack[index:]
        del self._buffers[index:]

    def handle_data(self, data):
        if self._stack:
            current = self._stack[-1]
            current["text"] += data
            if self._buffers[-1] is not None:
                self._buffers[-1].append(data)
        if not any(
            item["tag"] in {"script", "style", "noscript", "template"}
            for item in self._stack
        ):
            value = " ".join(data.split())
            if value:
                self.text.append(value)

    def parse(self, html):
        self.feed(html[:5_000_000].decode("utf-8", "replace"))
        return self

    def attr(self, tag, name):
        return [
            item["attrs"].get(name)
            for item in self.elements
            if item["tag"] == tag and item["attrs"].get(name)
        ]


def absolute(base, value):
    return urljoin(base, value.strip())
