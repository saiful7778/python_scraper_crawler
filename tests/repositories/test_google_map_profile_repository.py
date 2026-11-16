import json

import pytest

from app.models import (
    GoogleMapKeyword,
    GoogleMapProfileHourModel,
    GoogleMapProfileImageModel,
    GoogleMapProfileModel,
    GoogleMapProfileReviewModel,
    KeywordModel,
    LeadModel,
)
from app.repositories.google_map_profile_repository import GoogleMapProfileRepository
from app.repositories.lead_repository import LeadRepository


@pytest.fixture
def repo():
    return GoogleMapProfileRepository(LeadRepository())


async def test_saving_the_same_row_twice_creates_one_profile(db, repo):
    row = {"place_id": "p1", "title": "Cafe A", "cid": "9"}
    await repo.save_row(row, scrape_id="s1")
    await repo.save_row(row, scrape_id="s1")
    assert await GoogleMapProfileModel.filter(place_id="p1").count() == 1


async def test_saving_the_same_row_twice_creates_one_lead(db, repo):
    row = {"place_id": "p1", "title": "Cafe A", "website": "https://a.com"}
    await repo.save_row(row, scrape_id="s1")
    await repo.save_row(row, scrape_id="s1")
    assert await LeadModel.all().count() == 1
    assert await GoogleMapProfileModel.filter(lead_id__isnull=False).count() == 1


async def test_saving_a_row_without_place_id_twice_creates_one_profile_and_lead(
    db, repo
):
    row = {"title": "Cafe A", "website": "https://a.com"}
    await repo.save_row(row, scrape_id="s1")
    await repo.save_row(row, scrape_id="s1")
    assert await GoogleMapProfileModel.all().count() == 1
    assert await LeadModel.all().count() == 1


async def test_a_second_business_on_a_taken_domain_gets_its_own_lead(db, repo):
    await repo.save_row(
        {"place_id": "p1", "title": "Cafe A", "website": "https://a.com"}, "s1"
    )
    await repo.save_row(
        {"place_id": "p2", "title": "Cafe B", "website": "https://a.com"}, "s1"
    )
    assert await LeadModel.all().count() == 2
    assert await GoogleMapProfileModel.filter(lead__website_domain=None).count() == 1


async def test_rows_without_place_id_and_different_titles_stay_separate(db, repo):
    await repo.save_row({"title": "Cafe A", "website": "https://a.com"}, "s1")
    await repo.save_row({"title": "Cafe B", "website": "https://b.com"}, "s1")
    assert await GoogleMapProfileModel.all().count() == 2


async def test_images_and_reviews_are_saved_to_their_own_tables_once(db, repo):
    row = {
        "place_id": "p1",
        "title": "Cafe A",
        "images": json.dumps([{"title": "front", "image": "https://img/1"}]),
        "user_reviews": json.dumps(
            [{"Rating": 5, "When": "2 days ago", "Description": "Great"}]
        ),
        "review_star_5_count": "13",
    }
    await repo.save_row(row, scrape_id="s1")
    await repo.save_row(row, scrape_id="s1")
    profile = await GoogleMapProfileModel.get(place_id="p1")
    assert profile.review_star_5_count == 13
    images = await GoogleMapProfileImageModel.filter(google_map_id=profile.id)
    assert [(i.url, i.title) for i in images] == [("https://img/1", "front")]
    reviews = await GoogleMapProfileReviewModel.filter(google_map_id=profile.id)
    assert [(r.rating, r.when_text, r.description) for r in reviews] == [
        (5, "2 days ago", "Great")
    ]


async def test_saving_a_row_stores_its_crawlability(db, repo):
    await repo.save_row(
        {
            "place_id": "p1",
            "title": "Cafe A",
            "website": "https://a.com",
            "is_crawlable": True,
        },
        scrape_id="s1",
    )

    lead = await LeadModel.get(website_domain="a.com")
    assert lead.is_crawlable is True


async def test_saving_the_same_lead_with_a_new_crawlability_updates_it(db, repo):
    row = {
        "place_id": "p1",
        "title": "Cafe A",
        "website": "https://a.com",
        "is_crawlable": False,
    }
    await repo.save_row(row, scrape_id="s1")
    await repo.save_row({**row, "is_crawlable": True}, scrape_id="s1")

    lead = await LeadModel.get(website_domain="a.com")
    assert lead.is_crawlable is True


async def test_saving_a_row_with_place_id_and_no_website_twice_creates_one_lead(
    db, repo
):
    row = {"place_id": "p1", "title": "Cafe A"}
    await repo.save_row(row, scrape_id="s1")
    await repo.save_row(row, scrape_id="s1")
    assert await GoogleMapProfileModel.all().count() == 1
    assert await LeadModel.all().count() == 1


async def test_saving_a_row_links_its_keyword_to_the_profile(db, repo):
    row = {"place_id": "p1", "title": "Cafe A", "input_id": "Coffee Shops"}
    await repo.save_row(row, scrape_id="s1")
    await repo.save_row({**row, "input_id": "coffee shops"}, scrape_id="s2")
    links = await GoogleMapKeyword.all().prefetch_related("keyword")
    assert len(links) == 1
    assert links[0].keyword.name == "Coffee Shops"
    assert links[0].scrape_id == "s1"
    assert await KeywordModel.all().count() == 1


async def test_saving_a_row_stores_one_hour_per_time_range(db, repo):
    hours = {"Monday": ["9 AM-5 PM", "6 PM-9 PM"], "Tuesday": ["Closed"]}
    row = {"place_id": "p1", "title": "Cafe A", "open_hours": json.dumps(hours)}
    await repo.save_row(row, scrape_id="s1")
    await repo.save_row(row, scrape_id="s1")
    saved = await GoogleMapProfileHourModel.all().order_by("day", "position")
    assert [(h.day, h.position, h.span) for h in saved] == [
        ("Monday", 0, "9 AM-5 PM"),
        ("Monday", 1, "6 PM-9 PM"),
        ("Tuesday", 0, "Closed"),
    ]


async def test_save_rows_writes_every_row_and_its_children(db, repo):
    rows = [
        {
            "place_id": "p1",
            "title": "Cafe A",
            "input_id": "coffee",
            "open_hours": json.dumps({"Mon": ["9-5"]}),
            "images": json.dumps([{"image": "https://img/1.jpg", "title": "front"}]),
            "user_reviews": json.dumps([{"Rating": 5, "Description": "great"}]),
        },
        {"place_id": "p2", "title": "Cafe B", "input_id": "coffee"},
    ]
    assert await repo.save_rows(rows, scrape_id="s1") == 2
    assert await GoogleMapProfileModel.all().count() == 2
    assert await GoogleMapKeyword.all().count() == 2
    assert await KeywordModel.filter(name_key="coffee").count() == 1
    assert await GoogleMapProfileHourModel.all().count() == 1
    assert await GoogleMapProfileImageModel.all().count() == 1
    assert await GoogleMapProfileReviewModel.all().count() == 1


async def test_save_rows_twice_creates_nothing_new(db, repo):
    rows = [
        {"place_id": "p1", "title": "Cafe A", "input_id": "coffee"},
        {"place_id": "p2", "title": "Cafe B", "input_id": "coffee"},
    ]
    await repo.save_rows(rows, scrape_id="s1")
    await repo.save_rows(rows, scrape_id="s1")
    assert await GoogleMapProfileModel.all().count() == 2
    assert await GoogleMapKeyword.all().count() == 2
    assert await LeadModel.all().count() == 2


async def test_save_rows_keeps_the_rows_before_a_failing_one(db, repo):
    rows = [
        {"place_id": "p1", "title": "Cafe A"},
        {"place_id": "p2", "title": "Cafe B", "cid": "7"},
        {"place_id": "p3", "title": None, "website": None},
    ]
    real = repo._save_parent
    calls = []

    async def failing_on_second(row):
        calls.append(row["place_id"])
        if row["place_id"] == "p2":
            raise ValueError("bad row")
        return await real(row)

    repo._save_parent = failing_on_second
    assert await repo.save_rows(rows, scrape_id="s1") == 1
    assert calls == ["p1", "p2"]
    assert await GoogleMapProfileModel.filter(place_id="p1").count() == 1
    assert await GoogleMapProfileModel.filter(place_id="p2").count() == 0
