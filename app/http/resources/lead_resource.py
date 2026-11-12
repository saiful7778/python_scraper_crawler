from datetime import datetime
from typing import Any

from app.models import (
    CrawlFindingModel,
    CrawlPageModel,
    CrawlRunModel,
    CrawlWebsiteModel,
    LeadAddressModel,
    LeadModel,
)


class Resource:
    """Turns a model into a JSON-safe dict for an Inertia page prop."""

    def to_dict(self) -> dict[str, Any]:
        raise NotImplementedError

    @staticmethod
    def iso(value: datetime | None) -> str | None:
        return None if value is None else value.isoformat()


class LeadAddressResource(Resource):
    def __init__(self, address: LeadAddressModel):
        self.address = address

    def to_dict(self) -> dict[str, Any]:
        address = self.address
        return {
            "complete_address": address.complete_address,
            "borough": address.borough,
            "street": address.street,
            "city": address.city,
            "state": address.state,
            "postal_code": address.postal_code,
            "country": address.country,
            "latitude": address.latitude,
            "longitude": address.longitude,
            "plus_code": address.plus_code,
        }


class LeadListItemResource(Resource):
    def __init__(self, lead: LeadModel):
        self.lead = lead

    def to_dict(self) -> dict[str, Any]:
        lead = self.lead
        return {
            "id": str(lead.id),
            "counter": lead.counter,
            "title": lead.title,
            "categories": [
                c.name for c in sorted(lead.categories, key=lambda c: c.created_at)
            ],
            "address": lead.address,
            "phone": lead.phone,
            "email": next(
                (e.email for e in sorted(lead.emails, key=lambda e: e.created_at)),
                None,
            ),
            "website": lead.website,
            "status": lead.status,
        }


class LeadDetailResource(LeadListItemResource):
    def __init__(self, lead: LeadModel, address: LeadAddressModel | None):
        super().__init__(lead)
        self.address = address

    def to_dict(self) -> dict[str, Any]:
        lead = self.lead
        return {
            **super().to_dict(),
            "website_domain": lead.website_domain,
            "descriptions": lead.descriptions,
            "owner": lead.owner,
            "timezone": lead.timezone,
            "created_at": self.iso(lead.created_at),
            "updated_at": self.iso(lead.updated_at),
            "emails": [e.email for e in sorted(lead.emails, key=lambda e: e.created_at)],
            "socials": [
                {"platform": s.platform, "url": s.url}
                for s in sorted(lead.socials, key=lambda s: s.created_at)
            ],
            "detailed_address": None
            if self.address is None
            else LeadAddressResource(self.address).to_dict(),
            "crawl_websites": [
                CrawlWebsiteResource(website).to_dict()
                for website in sorted(lead.crawl_websites, key=lambda w: w.created_at)
            ],
        }


class CrawlPageResource(Resource):
    def __init__(self, page: CrawlPageModel):
        self.page = page

    def to_dict(self) -> dict[str, Any]:
        page = self.page
        return {
            "url": page.url,
            "depth": page.depth,
            "status": page.status,
            "http_status": page.http_status,
            "title": page.title,
            "final_url": page.final_url,
            "skip_reason": page.skip_reason,
            "error_message": page.error_message,
        }


class CrawlRunResource(Resource):
    def __init__(self, run: CrawlRunModel):
        self.run = run

    def to_dict(self) -> dict[str, Any]:
        run = self.run
        return {
            "id": str(run.id),
            "requested_url": run.requested_url,
            "status": run.status,
            "error_code": run.error_code,
            "error_message": run.error_message,
            "started_at": self.iso(run.started_at),
            "finished_at": self.iso(run.finished_at),
            "pages": [
                CrawlPageResource(page).to_dict()
                for page in sorted(run.pages, key=lambda p: p.depth)
            ],
        }


class CrawlFindingResource(Resource):
    def __init__(self, finding: CrawlFindingModel):
        self.finding = finding

    def to_dict(self) -> dict[str, Any]:
        return {
            "group": self.finding.group,
            "key": self.finding.key,
            "value": self.finding.value,
        }


class CrawlWebsiteResource(Resource):
    def __init__(self, website: CrawlWebsiteModel):
        self.website = website

    def to_dict(self) -> dict[str, Any]:
        website = self.website
        return {
            "id": str(website.id),
            "host": website.host,
            "registrable_domain": website.registrable_domain,
            "origin": website.origin,
            "status": website.status,
            "error_code": website.error_code,
            "error_message": website.error_message,
            "finished_at": self.iso(website.finished_at),
            "tech": {
                "cms": website.cms,
                "mobile_friendly": website.mobile_friendly,
                "fb_pixel": website.fb_pixel,
                "google_ads_tag": website.google_ads_tag,
                "analytics": website.analytics,
                "bing_ads": website.bing_ads,
                "linkedin_insight": website.linkedin_insight,
                "tiktok_pixel": website.tiktok_pixel,
            },
            "runs": [
                CrawlRunResource(run).to_dict()
                for run in sorted(website.runs, key=lambda r: r.created_at)
            ],
            "findings": [
                CrawlFindingResource(finding).to_dict()
                for finding in sorted(website.findings, key=lambda f: (f.group, f.key))
            ],
        }
