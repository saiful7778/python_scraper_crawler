from fastapi_startkit.fastapi import Router

from app.http.controllers.home_controller import HomeController

controller = HomeController()
router = Router()
router.get("/", controller.index, response_model=None)
router.get("/health", controller.health)
