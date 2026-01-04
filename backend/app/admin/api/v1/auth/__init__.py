from fastapi import APIRouter

from backend.app.admin.api.v1.auth.auth import router as auth_router

router = APIRouter(prefix='/auth')

router.include_router(auth_router, tags=['授权'])
