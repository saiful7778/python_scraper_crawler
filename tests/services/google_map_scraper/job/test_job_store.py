import pytest

from app.services.google_map_scraper.job.job_store import JobStore
from app.services.google_map_scraper.job.job_types import LeadRow, SettingsData


def _settings(**overrides) -> SettingsData:
    settings: dict = {
        "keywords": ["bakery", "cafe"],
        "lang": "de",
        "country": "de",
        "lat": 52.5,
        "lon": 13.4,
        "zoom": 15,
        "depth": 2,
        "max_time": 600,
        "fast_mode": True,
        "email": True,
        "socials": False,
        "browser_sites": True,
        "proxies": ["http://proxy-a:8080", "http://proxy-b:8080"],
        "limit": 50,
    }
    settings.update(overrides)
    return settings  # type: ignore[return-value]


def _lead(title: str, **extra: str) -> LeadRow:
    row = {key: "" for key in LeadRow.__required_keys__}
    row["latitude"] = None
    row["longitude"] = None
    row["title"] = title
    row.update(extra)
    return row  # type: ignore[return-value]


@pytest.fixture
def store(tmp_path):
    job_store = JobStore(str(tmp_path))
    yield job_store
    job_store.close()


def test_settings_round_trip(store):
    settings = _settings()
    job_id = store.create_job("nightly", settings)
    assert store.get_job(job_id)["Data"] == settings


def test_unset_optional_settings_come_back_as_stored(store):
    settings = _settings(lat=None, lon=None, limit=None, proxies=[], keywords=["bar"])
    job_id = store.create_job("plain", settings)
    assert store.get_job(job_id)["Data"] == settings


def test_keyword_and_proxy_order_is_kept(store):
    settings = _settings(keywords=["c", "a", "b"], proxies=["http://z", "http://y"])
    job_id = store.create_job("ordered", settings)
    data = store.get_job(job_id)["Data"]
    assert data["keywords"] == ["c", "a", "b"]
    assert data["proxies"] == ["http://z", "http://y"]


def test_list_jobs_includes_settings(store):
    store.create_job("first", _settings(keywords=["one"]))
    store.create_job("second", _settings(keywords=["two"]))
    names = {job["Name"]: job["Data"]["keywords"] for job in store.list_jobs()}
    assert names == {"first": ["one"], "second": ["two"]}


def test_leads_round_trip_with_optional_keys(store):
    job_id = store.create_job("leads", _settings())
    plain = _lead("Plain")
    with_socials = _lead("Socials", instagram="@plain", website_fetch_status="ok")
    store.append_rows(job_id, [plain, with_socials])
    assert store.read_rows(job_id) == [plain, with_socials]


def test_replace_and_truncate_rows(store):
    job_id = store.create_job("rows", _settings())
    store.append_rows(job_id, [_lead("a"), _lead("b"), _lead("c")])
    store.truncate_rows(job_id, 2)
    assert [row["title"] for row in store.read_rows(job_id)] == ["a", "b"]
    store.replace_rows(job_id, [_lead("z")])
    assert [row["title"] for row in store.read_rows(job_id)] == ["z"]


def test_delete_job_removes_settings_and_leads(store):
    job_id = store.create_job("gone", _settings())
    store.append_rows(job_id, [_lead("x")])
    assert store.delete_job(job_id)
    assert store.get_job(job_id) is None
    assert store.read_rows(job_id) == []
