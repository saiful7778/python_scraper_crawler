"""Google Maps URLs, DOM selectors, locale texts, and page scripts.

Everything here is pure data (or data derived once at import time). It has
no dependency on a browser tab, so it can be imported and diffed freely,
and it is the single place to edit when Google changes markup, adds a
locale, or renames an endpoint.

The JavaScript snippets are stored as strings so they can be sent to the
tab verbatim with ``BrowserTab.evaluate``; keeping them here (rather than
inline in the collector) makes them greppable and testable.
"""

import json
import re
from typing import Final

# ---------------------------------------------------------------------------
# Network endpoints.
# ---------------------------------------------------------------------------

#: Matches the batch JSON requests the feed issues while scrolling.
BATCH_URL_PATTERN: Final = re.compile(
    r"^https://www\.google\.[a-z.]+/search\?.*tbm=map"
)

#: Matches the JSON that backs a single place page.
LEAD_PANEL_URL_PATTERN: Final = re.compile(
    r"^https://www\.google\.[a-z.]+/maps/preview/place\?"
)

# ---------------------------------------------------------------------------
# Feed end-of-list detection.
# ---------------------------------------------------------------------------

#: Class Google puts on the last element of the feed. Stable for years.
END_CLASS: Final[str] = "HlvSq"

#: Texts the end-of-list element carries, across locales. Used in case
#: the class above is renamed. (Checked live on 2026-10-31 for the first
#: three; the rest are best-effort.)
END_MARKERS: Final[tuple[str, ...]] = (
    "You've reached the end of the list",
    "reached the end",
    "Vous êtes arrivé à la fin de la liste",
    "Has llegado al final de la lista",
    "Du hast das Ende der Liste erreicht",
    "Hai raggiunto la fine dell'elenco",
    "Você chegou ao fim da lista",
    "Chegou ao fim da lista",
    "Je hebt het einde van de lijst bereikt",
    "Listenin sonuna ulaştınız",
    "Dotarłeś do końca listy",
    "Du har nått slutet av listan",
)

# ---------------------------------------------------------------------------
# Scroll timing.
# ---------------------------------------------------------------------------

#: After a scroll: seconds to wait for the page to request the next
#: batch (Google asks at once or never), and seconds to wait for that
#: batch once requested (seen live up to 40 s on a busy tile).
SETTLE_SECONDS: Final[float] = 6.0
SLOW_BATCH_SECONDS: Final[float] = 45.0

#: The feed renders in small steps; while its scrollHeight grows, the
#: bottom has not really been reached, so the settle clock restarts.
FEED_GROWTH_POLL_SECONDS: Final[float] = 0.4

# ---------------------------------------------------------------------------
# Consent interstitial.
# ---------------------------------------------------------------------------

#: Cookies that skip Google's consent interstitial (EU and elsewhere).
CONSENT_COOKIES: Final[dict[str, str]] = {
    "CONSENT": "YES+cb.20210720-07-p0.en+FX+410",
    "SOCS": "CAISHAgBEhJnd3NfMjAyMzA4MTAtMF9SQzIaAmVuIAEaBgiAo_CmBg",
}

#: Button texts on the consent page, across locales. The "Reject all"
#: form posts ``set_eom=true`` to ``consent.google.com/save``; the
#: ``JS_DISMISS_CONSENT`` snippet prefers that form and falls back to
#: these words.
CONSENT_WORDS: Final[tuple[str, ...]] = (
    "reject all",
    "accept all",
    "alle ablehnen",
    "alle akzeptieren",
    "tout refuser",
    "tout accepter",
    "rechazar todo",
    "aceptar todo",
    "rifiuta tutto",
    "accetta tutto",
    "rejeitar tudo",
    "aceitar tudo",
    "alles afwijzen",
    "alles accepteren",
    "tümünü reddet",
    "tümünü kabul et",
    "odrzuć wszystko",
    "zaakceptuj wszystko",
    "avvisa alla",
    "godkänn alla",
    "afvis alle",
    "accepter alle",
)

# ---------------------------------------------------------------------------
# Href parsing.
# ---------------------------------------------------------------------------

#: Regexes lifted from place URLs, in the order they are tried.
CID_RE: Final = re.compile(r"!1s(0x[0-9a-f]+:0x[0-9a-f]+)")
LAT_LON_RE: Final = re.compile(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)")
PLACE_ID_RE: Final = re.compile(r"!19s(ChIJ[\w-]+)")

# ---------------------------------------------------------------------------
# In-page JavaScript snippets.
# ---------------------------------------------------------------------------

#: Dismiss the consent interstitial, preferring the "Reject all" form.
#: Returns true if a button was clicked.
JS_DISMISS_CONSENT: Final[str] = f"""(() => {{
  const forms = [...document.querySelectorAll('form[action*="consent"]')];
  const reject = forms.find(f => f.querySelector('input[name="set_eom"][value="true"]'));
  let b = reject && reject.querySelector('button, input[type="submit"]');
  const words = {json.dumps(CONSENT_WORDS, ensure_ascii=False)};
  if (!b) b = [...document.querySelectorAll('button')].find(x => words.some(w => x.innerText.toLowerCase().includes(w)));
  if (b) {{ b.click(); return true; }} return false;
}})()"""

#: True once the results feed has rendered.
JS_FEED_PRESENT: Final[str] = "!!document.querySelector('div[role=\"feed\"]')"

#: Every place anchor in the feed, with href and label.
JS_FEED_LINKS: Final[str] = """(() => {
  const out = [];
  for (const a of document.querySelectorAll('div[role="feed"] a[href*="/maps/place/"]'))
    out.push({href: a.href, label: a.getAttribute('aria-label') || ''});
  return out;
})()"""

#: Scroll the feed to its bottom; returns the new scrollHeight, or null
#: if the feed is gone.
JS_SCROLL_FEED_TO_BOTTOM: Final[str] = """(() => {
  const f = document.querySelector('div[role="feed"]');
  if (!f) return null;
  f.scrollTo(0, f.scrollHeight);
  return f.scrollHeight;
})()"""

#: Inspect the feed's end-of-list marker.
JS_FEED_END_STATE: Final[str] = f"""(() => {{
  const f = document.querySelector('div[role="feed"]');
  return f ? {{cls: !!f.querySelector('.{END_CLASS}'), text: f.innerText}} : null;
}})()"""
