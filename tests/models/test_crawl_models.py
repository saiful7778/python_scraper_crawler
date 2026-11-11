def test_crawl_models_are_registered_with_their_tables():
    from app.models import (
        CrawlEventModel,
        CrawlFindingModel,
        CrawlLinkModel,
        CrawlPageModel,
        CrawlRunModel,
        CrawlWebsiteModel,
    )

    assert CrawlWebsiteModel._meta.db_table == "crawl_websites"
    assert CrawlRunModel._meta.db_table == "crawl_runs"
    assert CrawlPageModel._meta.db_table == "crawl_pages"
    assert CrawlLinkModel._meta.db_table == "crawl_links"
    assert CrawlFindingModel._meta.db_table == "crawl_findings"
    assert CrawlEventModel._meta.db_table == "crawl_events"


def test_crawl_website_is_unique_by_origin():
    from app.models import CrawlWebsiteModel

    assert "scrape_id" not in CrawlWebsiteModel._meta.fields_map
    assert "lead_position" not in CrawlWebsiteModel._meta.fields_map
    assert CrawlWebsiteModel._meta.fields_map["origin"].unique is True


def test_every_crawl_model_has_updated_at():
    from app.models import (
        CrawlEventModel,
        CrawlFindingModel,
        CrawlLinkModel,
        CrawlPageModel,
        CrawlRunModel,
        CrawlWebsiteModel,
    )

    for model in (
        CrawlWebsiteModel,
        CrawlRunModel,
        CrawlPageModel,
        CrawlLinkModel,
        CrawlFindingModel,
        CrawlEventModel,
    ):
        assert "updated_at" in model._meta.fields_map, model.__name__


def test_website_stores_scrape_and_lead_as_plain_values():
    from app.models import CrawlWebsiteModel

    fields_map = CrawlWebsiteModel._meta.fields_map
    assert "scrape_lead" not in fields_map
