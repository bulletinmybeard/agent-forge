from fastapi import APIRouter

from app.services.vector_service import vector_service

router = APIRouter(tags=["health"])


@router.get("/health")
def health_check() -> dict:
    qdrant_ok = vector_service.check_available()
    return {
        "status": "healthy" if qdrant_ok else "degraded",
        "qdrant": "connected" if qdrant_ok else "unavailable",
    }
