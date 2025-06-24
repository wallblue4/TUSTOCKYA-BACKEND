# app/api/v1/auth.py
from datetime import timedelta
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import verify_password, create_access_token
from app.core.config import settings
from app.models.user import User
from app.schemas.user import UserLogin, UserResponse, Token
from app.api.deps import get_current_user

router = APIRouter()

@router.post("/login", response_model=Token)
async def login(
    credentials: UserLogin,
    db: Session = Depends(get_db)
):
    """Login con email y password"""
    
    # Buscar usuario por email
    user = db.query(User).filter(
        User.email == credentials.email,
        User.is_active == True
    ).first()
    
    if not user or not verify_password(credentials.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email o contraseña incorrectos",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # Crear token
    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"user_id": user.id}, 
        expires_delta=access_token_expires
    )
    
    # Respuesta
    user_response = UserResponse(
        id=user.id,
        email=user.email,
        first_name=user.first_name,
        last_name=user.last_name,
        role=user.role,
        location_id=user.location_id,
        is_active=user.is_active,
        location_name=user.location.name if user.location else None
    )
    
    return Token(
        access_token=access_token,
        token_type="bearer",
        user=user_response
    )

@router.get("/me", response_model=UserResponse)
async def get_current_user_info(current_user: User = Depends(get_current_user)):
    """Información del usuario actual"""
    
    return UserResponse(
        id=current_user.id,
        email=current_user.email,
        first_name=current_user.first_name,
        last_name=current_user.last_name,
        role=current_user.role,
        location_id=current_user.location_id,
        is_active=current_user.is_active,
        location_name=current_user.location.name if current_user.location else None
    )

@router.post("/logout")
async def logout(current_user: User = Depends(get_current_user)):
    """Logout"""
    return {"message": "Logout exitoso", "user": current_user.email}