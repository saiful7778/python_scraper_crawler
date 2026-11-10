from fastapi_startkit.fastapi import Router

from app.http.controllers.home_controller import HomeController
from app.http.controllers.lead_controller import LeadController

homeController = HomeController()
leadController = LeadController()

router = Router()
router.get("/", homeController.index, response_model=None)
router.get("/health", homeController.health)

router.get("/leads", leadController.index)
router.get("/leads/{lead_id}", leadController.show)
router.delete("/leads/{lead_id}", leadController.destroy)
