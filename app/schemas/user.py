# app/schemas/user.py
from pydantic import BaseModel, EmailStr
from typing import Optional

class UserLogin(BaseModel):
    email: str  # Cambiar de EmailStr a str para simplificar
    password: str

class UserResponse(BaseModel):
    id: int
    email: str
    first_name: str
    last_name: str
    role: str
    location_id: Optional[int] = None
    location_name: Optional[str] = None
    is_active: bool

class Token(BaseModel):
    access_token: str
    token_type: str
    user: UserResponse