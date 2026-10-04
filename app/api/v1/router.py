from fastapi import APIRouter

from app.api.v1 import auth, availability, bookings, plans, sessions, stations

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(plans.router)
api_router.include_router(bookings.router)
api_router.include_router(sessions.router)
api_router.include_router(stations.router)
api_router.include_router(availability.router)
