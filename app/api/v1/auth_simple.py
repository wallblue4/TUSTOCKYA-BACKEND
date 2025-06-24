# app/api/v1/auth_simple.py
from fastapi import APIRouter, HTTPException, status
from app.services.auth_service import auth_service
from app.schemas.user import UserLogin, Token
from app.core.security import create_access_token

router = APIRouter()

@router.post("/login")
async def login(credentials: UserLogin):
    """Login simplificado"""
    
    user = auth_service.authenticate_user(credentials.email, credentials.password)
    
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email o contraseña incorrectos"
        )
    
    access_token = create_access_token(data={"user_id": user['id']})
    
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "user": {
            "id": user['id'],
            "email": user['email'],
            "first_name": user['first_name'],
            "last_name": user['last_name'],
            "role": user['role'],
            "location_id": user['location_id'],
            "is_active": user['is_active'],
            "location_name": user.get('location_name')
        }
    }