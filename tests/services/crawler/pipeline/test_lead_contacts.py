from app.helpers.http_fetcher import FetchResult
from app.models import CrawlWebsiteModel, LeadEmailModel, LeadModel, LeadSocialModel
from app.services.crawler.run import crawl_one
from app.services.crawler.pipeline.stage import CRAWL_OPTIONS
from tests.services.crawler.pipeline.conftest import in_database

HTML = (
    b"<html><head><title>A</title></head><body>"
    b'<a href="mailto:hi@a.de">mail</a>'
    b'<a href="https://www.instagram.com/a_shop/">instagram</a>'
    b"</body></html>"
)


def contact_fetch(url):
    return FetchResult(
        url=url,
        final_url=url,
        status=200,
        headers={"Content-Type": "text/html; charset=utf-8"},
        body=HTML,
        redirect_chain=(),
        error=None,
    )


async def _create_lead():
    lead = await LeadModel.create(
        title="A", website="https://www.a.de/", website_domain=None
    )
    return lead.id


def test_crawl_links_the_lead_and_saves_its_contacts(fake_redis, postgres):
    lead_id = in_database(postgres, _create_lead)

    crawl_one(
        "a.de",
        redis=fake_redis,
        postgres=postgres,
        options=CRAWL_OPTIONS,
        fetch=contact_fetch,
    )

    website_leads = in_database(
        postgres, lambda: CrawlWebsiteModel.all().values_list("lead_id", flat=True)
    )
    emails = in_database(
        postgres,
        lambda: LeadEmailModel.filter(lead_id=lead_id).values_list("email", flat=True),
    )
    socials = in_database(
        postgres,
        lambda: LeadSocialModel.filter(lead_id=lead_id).values_list("platform", "url"),
    )
    assert website_leads == [lead_id]
    assert emails == ["hi@a.de"]
    assert dict(socials) == {"instagram": "https://www.instagram.com/a_shop"}
