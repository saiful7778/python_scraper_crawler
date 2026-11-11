import pytest

from app.models import LeadModel
from app.repositories.lead_repository import LeadRepository
from tests.models.conftest import db  # noqa: F401


@pytest.mark.usefixtures("db")
async def test_finds_lead_by_website_domain():
    lead = await LeadModel.create(title="A", website="https://a.de", website_domain="a.de")
    assert (await LeadRepository().find_by_host("a.de")).id == lead.id


@pytest.mark.usefixtures("db")
async def test_finds_lead_by_website_when_domain_is_empty():
    lead = await LeadModel.create(title="A", website="https://www.a.de/", website_domain=None)
    assert (await LeadRepository().find_by_host("a.de")).id == lead.id


@pytest.mark.usefixtures("db")
async def test_returns_none_when_no_lead_has_the_host():
    await LeadModel.create(title="B", website="https://ab.de", website_domain=None)
    assert await LeadRepository().find_by_host("a.de") is None
