from app.models import ScrapeModel


async def test_scrape_round_trips_settings_and_keywords(db):
    scrape = await ScrapeModel.create(
        name="cafe @ Austin",
        status="pending",
        created_order=1,
        settings={"lang": "en", "max_time": 600},
        keywords=["cafe"],
    )
    fetched = await ScrapeModel.get(id=scrape.id)
    assert fetched.keywords == ["cafe"]
    assert fetched.settings["max_time"] == 600
    assert fetched.progress == {}
    assert fetched.resumes == 0
    assert fetched.error == ""
