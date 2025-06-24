# app/api/deps.py
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from typing import Optional

from app.core.database import get_db
from app.core.security import decode_token
from app.models.user import User, UserRole

security = HTTPBearer()

async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db)
) -> User:
    """Obtener usuario actual desde token JWT"""
    
    token = credentials.credentials
    payload = decode_token(token)
    
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token inválido",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    user_id = payload.get("user_id")
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token inválido"
        )
    
    user = db.query(User).filter(User.id == user_id, User.is_active == True).first()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Usuario no encontrado"
        )
    
    return user

def require_role(allowed_roles: list[UserRole]):
    """Decorator para requerir roles específicos"""
    def role_checker(current_user: User = Depends(get_current_user)) -> User:
        if current_user.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Acceso denegado. Roles permitidos: {[role.value for role in allowed_roles]}"
            )
        return current_user
    return role_checker

# Shortcuts para roles
def get_vendedor(current_user: User = Depends(require_role([UserRole.vendedor, UserRole.administrador]))):
    return current_user

def get_bodeguero(current_user: User = Depends(require_role([UserRole.bodeguero, UserRole.administrador]))):
    return current_user

def get_corredor(current_user: User = Depends(require_role([UserRole.corredor, UserRole.administrador]))):
    return current_user

def get_admin(current_user: User = Depends(require_role([UserRole.administrador]))):
    return current_user