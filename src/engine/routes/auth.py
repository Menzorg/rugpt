"""
Authentication Routes

Endpoints for user authentication.
"""
import jwt
import logging
from datetime import datetime, timedelta
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException, Depends, Header
from pydantic import BaseModel, EmailStr

from ..config import Config
from ..services.engine_service import get_engine_service
from ..services.users_service import UsersService

logger = logging.getLogger("rugpt.routes.auth")
router = APIRouter(prefix="/auth", tags=["auth"])


# ============================================
# Request/Response Models
# ============================================

class LoginRequest(BaseModel):
    """Login request body"""
    email: EmailStr
    password: str


class LoginResponse(BaseModel):
    """Login response"""
    success: bool
    token: Optional[str] = None
    user_id: Optional[str] = None
    org_id: Optional[str] = None
    name: Optional[str] = None
    username: Optional[str] = None
    is_admin: bool = False
    message: Optional[str] = None


class RegisterRequest(BaseModel):
    """Registration request body"""
    org_id: str  # Organization to join
    name: str
    username: str
    email: EmailStr
    password: str


class TokenPayload(BaseModel):
    """JWT token payload"""
    user_id: str
    org_id: str
    exp: datetime


# ============================================
# Helpers
# ============================================

def create_token(user_id: UUID, org_id: UUID, email: str = None, is_admin: bool = False) -> str:
    """Create JWT token for user"""
    expiration = datetime.utcnow() + timedelta(hours=Config.JWT_EXPIRATION_HOURS)
    payload = {
        "user_id": str(user_id),
        "org_id": str(org_id),
        "email": email,
        "is_admin": is_admin,
        "exp": expiration
    }
    return jwt.encode(payload, Config.JWT_SECRET, algorithm=Config.JWT_ALGORITHM)


def verify_token(token: str) -> Optional[dict]:
    """Verify and decode JWT token"""
    try:
        payload = jwt.decode(token, Config.JWT_SECRET, algorithms=[Config.JWT_ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


async def get_current_user(authorization: str = Header(None)) -> dict:
    """Dependency to get current authenticated user"""
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header required")

    # Expect "Bearer <token>"
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="Invalid authorization header format")

    token = parts[1]
    payload = verify_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    return {
        "user_id": UUID(payload["user_id"]),
        "org_id": UUID(payload["org_id"])
    }


# ============================================
# Routes
# ============================================

@router.post("/login", response_model=LoginResponse)
async def login(request: LoginRequest):
    """
    Authenticate user with email and password.

    Returns JWT token on success.
    """
    engine = get_engine_service()
    users_service = UsersService(engine.user_storage)

    # Find user by email
    user = await users_service.get_user_by_email(request.email)
    if not user:
        logger.info(f"Login failed: user not found for {request.email}")
        return LoginResponse(success=False, message="Invalid email or password")

    # Verify password
    if not await users_service.verify_password(user.id, request.password):
        logger.info(f"Login failed: invalid password for {request.email}")
        return LoginResponse(success=False, message="Invalid email or password")

    # Check if user is active
    if not user.is_active:
        logger.info(f"Login failed: user inactive {request.email}")
        return LoginResponse(success=False, message="Account is deactivated")

    # Create token with all user data
    token = create_token(user.id, user.org_id, user.email, user.is_admin)

    # Update last seen
    await users_service.update_last_seen(user.id)

    logger.info(f"User logged in: {user.email}")

    return LoginResponse(
        success=True,
        token=token,
        user_id=str(user.id),
        org_id=str(user.org_id),
        name=user.name,
        username=user.username,
        is_admin=user.is_admin
    )


@router.post("/register", response_model=LoginResponse)
async def register(request: RegisterRequest):
    """
    Register a new user.

    Requires valid organization ID.
    Returns JWT token on success.
    """
    engine = get_engine_service()
    users_service = UsersService(engine.user_storage)

    try:
        org_id = UUID(request.org_id)
    except ValueError:
        return LoginResponse(success=False, message="Invalid organization ID")

    # Verify organization exists
    org = await engine.org_storage.get_by_id(org_id)
    if not org:
        return LoginResponse(success=False, message="Organization not found")

    if not org.is_active:
        return LoginResponse(success=False, message="Organization is inactive")

    try:
        user = await users_service.create_user(
            org_id=org_id,
            name=request.name,
            username=request.username,
            email=request.email,
            password=request.password
        )
    except ValueError as e:
        logger.info(f"Registration failed: {e}")
        return LoginResponse(success=False, message=str(e))

    # Create token with all user data
    token = create_token(user.id, user.org_id, user.email, user.is_admin)

    logger.info(f"User registered: {user.email}")

    return LoginResponse(
        success=True,
        token=token,
        user_id=str(user.id),
        org_id=str(user.org_id),
        name=user.name,
        username=user.username,
        is_admin=user.is_admin
    )


@router.get("/me")
async def get_current_user_info(current_user: dict = Depends(get_current_user)):
    """Get current authenticated user's information"""
    engine = get_engine_service()
    user = await engine.user_storage.get_by_id(current_user["user_id"])

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return user.to_dict()


@router.post("/refresh")
async def refresh_token(current_user: dict = Depends(get_current_user)):
    """Refresh JWT token"""
    token = create_token(current_user["user_id"], current_user["org_id"])
    return {"token": token}
