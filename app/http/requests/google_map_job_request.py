from pydantic import ConfigDict, Field

from app.services.google_map_scraper.job.job_settings import CleanText, JobSettings


class GoogleMapJobRequest(JobSettings):
    """Body of a REST request that creates a Google Maps job: the job settings plus a name.

    Unknown fields are rejected, so a typo in a field name is an error, not a silent default.
    """

    model_config = ConfigDict(extra="forbid")

    name: CleanText = Field(
        max_length=200, description="Job name shown in the job list."
    )
