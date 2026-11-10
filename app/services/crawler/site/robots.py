from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class RobotTxtPolicy:
    disallow: tuple[str, ...]
    allow: tuple[str, ...]
    sitemaps: list[str]

    @classmethod
    def from_text(cls, text: str, user_agent: str = "*"):
        groups = []
        current_agents = []
        current_allow = []
        current_disallow = []
        sitemaps = []
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            key, value = (part.strip() for part in line.split(":", 1))
            low = key.lower()
            if low == "user-agent":
                if current_agents and (current_allow or current_disallow):
                    groups.append((current_agents, current_allow, current_disallow))
                    current_allow, current_disallow = [], []
                current_agents.append(value.lower())
            elif low == "allow" and current_agents:
                current_allow.append(value)
            elif low == "disallow" and current_agents and value:
                current_disallow.append(value)
            elif low == "sitemap" and value:
                sitemaps.append(value)
        if current_agents:
            groups.append((current_agents, current_allow, current_disallow))
        selected = [g for g in groups if user_agent.lower() in g[0]] or [
            g for g in groups if "*" in g[0]
        ]
        if not selected:
            return cls((), (), sitemaps)
        _, allows, disallows = selected[0]
        return cls(tuple(disallows), tuple(allows), sitemaps)

    def is_allowed(self, url: str) -> bool:
        path = urlsplit(url).path or "/"
        matches = [
            (len(rule), False) for rule in self.disallow if path.startswith(rule)
        ]
        matches += [(len(rule), True) for rule in self.allow if path.startswith(rule)]
        return not matches or max(matches)[1]
