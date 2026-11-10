from uuid import UUID

from fastapi import HTTPException
from fastapi_startkit.inertia import Inertia
from fastapi_startkit.inertia.inertia import InertiaResponse
from fastapi_startkit.inertia.redirect import InertiaRedirect

from app.http.resources import LeadDetailResource, LeadListItemResource
from app.repositories import LeadRepository


class LeadController:
    async def index(self) -> InertiaResponse:
        leads = await LeadRepository().all(descending=True)
        return Inertia.render(
            "Leads",
            {"leads": [LeadListItemResource(lead).to_dict() for lead in leads]},
        )

    async def show(self, lead_id: UUID) -> InertiaResponse:
        repository = LeadRepository()
        lead = await repository.find_details(lead_id)
        if lead is None:
            raise HTTPException(status_code=404, detail="Lead not found")
        address = await repository.find_address(lead.id)
        return Inertia.render(
            "LeadDetails",
            {"lead": LeadDetailResource(lead, address).to_dict()},
        )

    async def destroy(self, lead_id: UUID) -> InertiaRedirect:
        if not await LeadRepository().delete(lead_id):
            raise HTTPException(status_code=404, detail="Lead not found")
        return Inertia.redirect("/leads", status_code=303)
