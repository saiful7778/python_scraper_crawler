import json

import pytest

from app.services.google_map_scraper.google_map import (
    BATCH_URL_PATTERN,
    CID_RE,
    CONSENT_COOKIES,
    CONSENT_WORDS,
    END_CLASS,
    END_MARKERS,
    FEED_GROWTH_POLL_SECONDS,
    JS_DISMISS_CONSENT,
    JS_FEED_END_STATE,
    JS_FEED_LINKS,
    JS_FEED_PRESENT,
    JS_SCROLL_FEED_TO_BOTTOM,
    LAT_LON_RE,
    LEAD_PANEL_URL_PATTERN,
    PLACE_ID_RE,
    SETTLE_SECONDS,
    SLOW_BATCH_SECONDS,
)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.google.com/search?q=cafe&tbm=map",
        "https://www.google.co.uk/search?tbm=map&start=20",
    ],
)
def test_batch_pattern_matches_map_search_requests(url):
    assert BATCH_URL_PATTERN.match(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.google.com/search?q=cafe",
        "http://www.google.com/search?tbm=map",
        "https://www.google.com/maps/preview/place?authuser=0",
    ],
)
def test_batch_pattern_rejects_other_requests(url):
    assert not BATCH_URL_PATTERN.match(url)


def test_lead_panel_pattern_matches_place_preview_json():
    url = "https://www.google.com/maps/preview/place?authuser=0&hl=en"

    assert LEAD_PANEL_URL_PATTERN.match(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.google.com/maps/preview/place/extra?x=1",
        "https://www.google.com/search?tbm=map",
    ],
)
def test_lead_panel_pattern_rejects_other_urls(url):
    assert not LEAD_PANEL_URL_PATTERN.match(url)


def test_cid_regex_extracts_data_id():
    match = CID_RE.search("...!1s0x89c25:0x123abc!8m2...")

    assert match is not None
    assert match.group(1) == "0x89c25:0x123abc"


def test_lat_lon_regex_extracts_signed_coordinates():
    match = LAT_LON_RE.search("!3d-33.8688!4d151.2093")

    assert match is not None
    assert match.groups() == ("-33.8688", "151.2093")


def test_place_id_regex_extracts_place_id():
    match = PLACE_ID_RE.search("!19sChIJabc-123_x?authuser=0")

    assert match is not None
    assert match.group(1) == "ChIJabc-123_x"


def test_end_markers_are_non_empty_and_unique():
    assert END_MARKERS
    assert len(set(END_MARKERS)) == len(END_MARKERS)


def test_consent_words_are_lowercase_because_js_lowercases_text():
    assert all(word == word.lower() for word in CONSENT_WORDS)


def test_consent_cookies_skip_the_interstitial():
    assert set(CONSENT_COOKIES) == {"CONSENT", "SOCS"}


def test_dismiss_consent_script_embeds_locale_words():
    assert json.dumps(CONSENT_WORDS, ensure_ascii=False) in JS_DISMISS_CONSENT


def test_dismiss_consent_script_prefers_reject_form():
    assert 'input[name="set_eom"][value="true"]' in JS_DISMISS_CONSENT


def test_feed_end_state_script_checks_end_class():
    assert f".{END_CLASS}" in JS_FEED_END_STATE


def test_feed_scripts_target_the_results_feed():
    for script in (
        JS_FEED_PRESENT,
        JS_FEED_LINKS,
        JS_SCROLL_FEED_TO_BOTTOM,
        JS_FEED_END_STATE,
    ):
        assert 'div[role="feed"]' in script or 'div[role=\\"feed\\"]' in script


def test_timing_constants_are_ordered_sensibly():
    assert FEED_GROWTH_POLL_SECONDS < SETTLE_SECONDS
    assert SETTLE_SECONDS < SLOW_BATCH_SECONDS
