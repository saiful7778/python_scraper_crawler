import json

from app.services.serper.maps.serper_map_config import SerperMapConfig
from app.services.serper.maps.serper_map_rows import place_to_row, places_to_rows
from app.services.serper.maps.serper_map_type import OpeningHours, Place

PLACE = Place(
    position=1,
    title="Lalo Plumbing",
    latitude=30.3058,
    longitude=-97.7432,
    rating=5.0,
    ratingCount=23,
    type="Plumber",
    types=["Plumber", "Drainage service"],
    cid="1178220673215833450",
    fid="0x8644cb2ec65763b7:0x1059e175b60d256a",
    placeId="ChIJt2NXxi7LRIYRaiUNtnXhWRA",
    address="3800 N Lamar Blvd #200, Austin, TX 78756",
    website="https://laloplumbing.com/",
    phoneNumber="(737) 300-9817",
    openingHours=OpeningHours(Monday="Open 24 hours"),
    thumbnailUrl="https://lh3.googleusercontent.com/x",
)


def _config(**overrides):
    values = {"keywords": ("plumbers",), "city": "Austin", "state": "TX", "country": "US"}
    values.update(overrides)
    return SerperMapConfig(**values)


def test_the_row_carries_every_dedupe_key_and_field():
    row = place_to_row(PLACE, "plumbers")
    assert row["title"] == "Lalo Plumbing"
    assert row["place_id"] == "ChIJt2NXxi7LRIYRaiUNtnXhWRA"
    assert row["data_id"] == "0x8644cb2ec65763b7:0x1059e175b60d256a"
    assert row["cid"] == "1178220673215833450"
    assert row["review_count"] == "23" and row["review_rating"] == "5.0"
    assert row["categories"] == "Plumber | Drainage service"
    assert row["descriptions"] == "Plumber"
    assert row["input_id"] == "plumbers"
    assert row["latitude"] == 30.3058 and row["longitude"] == -97.7432
    assert row["address"] == "3800 N Lamar Blvd #200, Austin, TX 78756"
    assert row["thumbnail"] == "https://lh3.googleusercontent.com/x"


def test_the_phone_is_formatted_for_the_country():
    row = place_to_row(PLACE, "plumbers")
    from app.services.google_map_scraper.scrape.phone_numbers import format_phone_numbers

    format_phone_numbers([row], "us")
    assert row["phone"] == "+17373009817"


def test_the_link_points_at_the_place():
    row = place_to_row(PLACE, "plumbers")
    assert row["link"].startswith("https://www.google.com/maps/search/?api=1&query=")
    assert "query_place_id=ChIJt2NXxi7LRIYRaiUNtnXhWRA" in row["link"]


def test_open_hours_round_trip_as_json():
    row = place_to_row(PLACE, "plumbers")
    assert json.loads(row["open_hours"]) == {"Monday": ["Open 24 hours"]}


def test_several_ranges_in_one_day_become_several_spans():
    place = Place(
        position=1,
        title="Split day",
        latitude=1.0,
        longitude=2.0,
        openingHours=OpeningHours(Monday="9 AM-12 PM, 1 PM-5 PM"),
    )
    assert json.loads(place_to_row(place, "x")["open_hours"]) == {
        "Monday": ["9 AM-12 PM", "1 PM-5 PM"]
    }


async def test_the_hours_survive_the_hours_table(db):
    from app.models import GoogleMapProfileHourModel
    from app.repositories import GoogleMapProfileRepository, LeadRepository

    row = place_to_row(PLACE, "plumbers")
    await GoogleMapProfileRepository(LeadRepository()).save_row(row, "s1")
    stored = await GoogleMapProfileHourModel.all()
    assert [(hour.day, hour.span) for hour in stored] == [
        ("Monday", "Open 24 hours")
    ]


def test_a_place_without_a_website_or_phone_gets_blank_cells():
    bare = Place(position=1, title="Bare", latitude=1.0, longitude=2.0)
    row = place_to_row(bare, "x")
    assert row["website"] == "" and row["phone"] == ""
    assert row["categories"] == "" and row["open_hours"] == ""
    assert row["link"] == ""
    assert row["plus_code"] != ""


def test_a_place_without_coordinates_keeps_none():
    bare = Place(position=1, title="Bare")
    row = place_to_row(bare, "x")
    assert row["latitude"] == 0.0 and row["plus_code"] != ""


def test_has_phone_drops_rows_without_a_usable_number():
    bare = Place(position=1, title="Bare", latitude=1.0, longitude=2.0)
    kept = places_to_rows([PLACE, bare], "plumbers", _config(has_phone=True))
    assert [row["title"] for row in kept] == ["Lalo Plumbing"]


def test_has_phone_drops_an_unparsable_number():
    broken = Place(
        position=1, title="Broken", latitude=1.0, longitude=2.0, phoneNumber="n/a"
    )
    kept = places_to_rows([broken], "plumbers", _config(has_phone=True))
    assert kept == []


def test_has_website_drops_rows_without_a_website():
    bare = Place(position=1, title="Bare", latitude=1.0, longitude=2.0)
    kept = places_to_rows([PLACE, bare], "plumbers", _config(has_website=True))
    assert [row["title"] for row in kept] == ["Lalo Plumbing"]


def test_filters_off_keep_everything():
    bare = Place(position=1, title="Bare", latitude=1.0, longitude=2.0)
    assert len(places_to_rows([PLACE, bare], "plumbers", _config())) == 2


async def test_the_mapped_row_satisfies_the_repository_contract(db):
    from app.models import GoogleMapProfileModel
    from app.repositories import GoogleMapProfileRepository, LeadRepository

    row = place_to_row(PLACE, "plumbers")
    repo = GoogleMapProfileRepository(LeadRepository())
    await repo.save_row(row, "s1")
    await repo.save_row(row, "s1")
    assert await GoogleMapProfileModel.filter(place_id=PLACE.placeId).count() == 1
