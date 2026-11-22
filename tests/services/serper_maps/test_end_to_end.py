"""A grid run through the service: 4 keys, one dry, dedupe across tiles."""


from app.models import LeadModel, SerperMapRequestModel
from app.repositories.serper_map_repository import SerperMapPage
from app.services.google_map_scraper.google_map.geo import (
    BoundingBox,
    build_grid_cells,
)
from app.services.serper.maps import serper_map_service as service_module
from app.services.serper.maps.serper_map_config import SerperMapConfig
from app.services.serper.maps.serper_map_errors import SerperNoCreditsError
from app.services.serper.maps.serper_map_plan import SerperMapPlan
from app.services.serper.maps.serper_map_service import SerperMapService
from app.services.serper.maps.serper_map_type import Place, SearchResult

CITY_BOX: BoundingBox = (30.09, 30.51, -97.93, -97.56)


def _config(**overrides):
    values = {"keywords": ("plumbers",), "city": "Austin", "state": "TX", "country": "US"}
    values.update(overrides)
    return SerperMapConfig(**values)


def _grid_plan(config: SerperMapConfig) -> SerperMapPlan:
    """The same tiling build_plan produces, without the network geocode."""
    pages = [
        SerperMapPage(
            keyword=keyword,
            page=page,
            grid_x=index % config.grid,
            grid_y=index // config.grid,
            lat=cell["lat"],
            lng=cell["lon"],
            zoom=cell["zoom"],
        )
        for keyword in config.keywords
        for index, cell in enumerate(build_grid_cells(CITY_BOX, config.grid))
        for page in range(1, config.depth + 1)
    ]
    return SerperMapPlan(
        config=config, center=(30.2649, -97.7474), bounding_box=CITY_BOX, pages=pages
    )


def _places(count: int = 20) -> list[Place]:
    places = []
    for index in range(count):
        places.append(
            Place(
                position=index + 1,
                title=f"Plumber {index}",
                latitude=30.26 + (index % 5) * 0.01,
                longitude=-97.74 + (index % 4) * 0.01,
                placeId=f"place-{index}",
                fid=f"0x1:0x{index}",
                phoneNumber=f"(512) 555-01{index:02d}",
                website=f"https://plumber{index}.example",
                type="Plumber",
                types=["Plumber"],
                rating=4.5,
                ratingCount=10 + index,
            )
        )
    return places


class ScriptedClient:
    """Answers every page with the same 20 places, or runs dry."""

    def __init__(self, index: int, *, dry: bool):
        self.index = index
        self.dry = dry
        self.calls = 0

    async def search(self, keyword, lat, lon, zoom, page):
        self.calls += 1
        if self.dry:
            raise SerperNoCreditsError("no credits")
        return SearchResult(ll=f"@{lat},{lon},{zoom}z", places=_places(), credits=3)

    async def balance(self) -> int:
        return 0 if self.dry else 100

    async def aclose(self):
        pass


async def test_a_grid_run_with_one_dry_key_saves_each_business_once(db, monkeypatch):
    config = _config(depth=2, grid=3, parallel=5)
    plan = _grid_plan(config)
    assert plan.page_count == 18

    async def fake_plan(given):
        return plan

    monkeypatch.setattr(service_module, "build_plan", fake_plan)
    clients = {
        "k1": ScriptedClient(0, dry=True),
        "k2": ScriptedClient(1, dry=False),
        "k3": ScriptedClient(2, dry=False),
        "k4": ScriptedClient(3, dry=False),
    }
    service = SerperMapService(
        config, api_keys=list(clients), client_factory=lambda key: clients[key]
    )

    result = await service.run(await service.start())

    assert result.status == "finished"
    assert result.requests_done == 18
    assert result.requests_pending == 0
    assert result.places_found == 18 * 20
    assert result.places_saved == 20  # every page repeats the same 20 businesses
    assert result.credits_spent == 18 * 3
    assert await LeadModel.all().count() == 20
    assert clients["k1"].calls == 0  # the dry key was retired at startup


async def test_every_request_row_ends_done_or_failed(db, monkeypatch):
    config = _config(depth=1, grid=2)
    plan = _grid_plan(config)

    async def fake_plan(given):
        return plan

    monkeypatch.setattr(service_module, "build_plan", fake_plan)
    client = ScriptedClient(0, dry=False)
    service = SerperMapService(
        config, api_keys=["k"], client_factory=lambda key: client
    )
    job_id = await service.start()
    await service.run(job_id)

    statuses = await SerperMapRequestModel.filter(job_id=job_id).values_list(
        "status", flat=True
    )
    assert set(statuses) == {"done"}
    assert len(statuses) == plan.page_count


async def test_a_resumed_exhausted_job_finishes_without_refetching(db, monkeypatch):
    config = _config(depth=1, grid=2)
    plan = _grid_plan(config)

    async def fake_plan(given):
        return plan

    monkeypatch.setattr(service_module, "build_plan", fake_plan)

    class Dry:
        def __init__(self):
            self.calls = 0

        async def search(self, keyword, lat, lon, zoom, page):
            self.calls += 1
            raise SerperNoCreditsError("no credits")

        async def balance(self) -> int:
            return 100

        async def aclose(self) -> None:
            pass

    dry = Dry()
    failing = SerperMapService(
        config, api_keys=["k"], client_factory=lambda key: dry
    )
    job_id = await failing.start()
    first = await failing.run(job_id)
    assert first.status == "exhausted"
    assert first.requests_pending == plan.page_count

    healthy = ScriptedClient(0, dry=False)
    resumed = SerperMapService(
        config, api_keys=["k"], client_factory=lambda key: healthy
    )
    second = await resumed.run(job_id)
    assert second.status == "finished"
    assert second.requests_done == plan.page_count
    assert healthy.calls == plan.page_count
    await failing.aclose()
    await resumed.aclose()
