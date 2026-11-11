import pytest

from app.models import GoogleMapProfileModel, GoogleMapProfileToLeadModel, LeadModel
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
    assert await GoogleMapProfileToLeadModel.all().count() == 1


async def test_saving_a_row_without_place_id_twice_creates_one_profile_and_lead(
    db, repo
):
    row = {"title": "Cafe A", "website": "https://a.com"}
    await repo.save_row(row, scrape_id="s1")
    await repo.save_row(row, scrape_id="s1")
    assert await GoogleMapProfileModel.all().count() == 1
    assert await LeadModel.all().count() == 1


async def test_rows_without_place_id_and_different_titles_stay_separate(db, repo):
    await repo.save_row({"title": "Cafe A", "website": "https://a.com"}, "s1")
    await repo.save_row({"title": "Cafe B", "website": "https://b.com"}, "s1")
    assert await GoogleMapProfileModel.all().count() == 2


async def test_saving_a_row_with_place_id_and_no_website_twice_creates_one_lead(
    db, repo
):
    row = {"place_id": "p1", "title": "Cafe A"}
    await repo.save_row(row, scrape_id="s1")
    await repo.save_row(row, scrape_id="s1")
    assert await GoogleMapProfileModel.all().count() == 1
    assert await LeadModel.all().count() == 1
