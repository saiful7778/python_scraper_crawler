import pytest
from tortoise.exceptions import IntegrityError

from app.models import (
    SerperMapJobModel,
    SerperMapJobStatus,
    SerperMapRequestModel,
)


async def test_a_job_stores_its_config_and_counts(db):
    job = await SerperMapJobModel.create(
        name="plumbers @ Austin",
        status=SerperMapJobStatus.RUNNING,
        config={"keywords": ["plumbers"], "city": "Austin", "depth": 2, "grid": 3},
        config_key="k1",
        key_count=4,
        total_requests=18,
    )
    await SerperMapRequestModel.create(
        job_id=job.id,
        keyword="plumbers",
        page=1,
        grid_x=0,
        grid_y=0,
        lat=30.1,
        lng=-97.7,
        zoom=13,
    )
    assert job.status == "running"
    assert job.done_requests == 0 and job.credits_spent == 0
    requests = await SerperMapRequestModel.filter(job_id=job.id)
    assert [r.status for r in requests] == ["pending"]
    assert requests[0].attempts == 0 and requests[0].key_hint is None


async def test_a_request_is_unique_per_job_keyword_page_and_grid(db):
    job = await SerperMapJobModel.create(
        name="j",
        status=SerperMapJobStatus.RUNNING,
        config={},
        config_key="k",
        total_requests=1,
    )
    fields = {
        "job_id": job.id,
        "keyword": "plumbers",
        "page": 2,
        "grid_x": 1,
        "grid_y": 0,
        "lat": 30.1,
        "lng": -97.7,
        "zoom": 13,
    }
    await SerperMapRequestModel.create(**fields)
    with pytest.raises(IntegrityError):
        await SerperMapRequestModel.create(**fields)


async def test_the_hours_table_stamps_every_row(db):
    """Every other profile table has timestamps; the hours table must too.

    The live table has had NOT NULL created_at/updated_at since the
    add_timestamps_to_hours migration, so a model without them fails to insert.
    """
    from app.models import (
        GoogleMapProfileHourModel,
        GoogleMapProfileModel,
        LeadModel,
    )

    lead = await LeadModel.create(title="Cafe")
    profile = await GoogleMapProfileModel.create(lead_id=lead.id)
    hour = await GoogleMapProfileHourModel.create(
        google_map_id=profile.id, day="Monday", position=0, span="9-5"
    )
    assert hour.created_at is not None
    assert hour.updated_at is not None
