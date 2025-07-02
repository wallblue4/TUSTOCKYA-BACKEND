# app/services/auth_service.py
import sqlite3
from typing import Optional
from app.core.database_sqlite import get_db
from app.core.security import verify_password, get_password_hash

class AuthService:
    
    def authenticate_user(self, email: str, password: str) -> Optional[dict]:
        """Autenticar usuario"""
        with get_db() as conn:
            cursor = conn.execute(
                '''SELECT u.id, u.email, u.password_hash, u.first_name, u.last_name, 
                          u.role, u.location_id, u.is_active, l.name as location_name
                   FROM users u 
                   LEFT JOIN locations l ON u.location_id = l.id
                   WHERE u.email = ? AND u.is_active = 1''',
                (email,)
            )
            user = cursor.fetchone()
            
            if not user or not verify_password(password, user['password_hash']):
                return None
            
            return dict(user)
    
    def get_user_by_id(self, user_id: int) -> Optional[dict]:
        """Obtener usuario por ID"""
        with get_db() as conn:
            cursor = conn.execute(
                '''SELECT u.*, l.name as location_name 
                   FROM users u 
                   LEFT JOIN locations l ON u.location_id = l.id 
                   WHERE u.id = ? AND u.is_active = 1''',
                (user_id,)
            )
            user = cursor.fetchone()
            return dict(user) if user else None
    
    def create_user(self, email: str, password: str, first_name: str, last_name: str, role: str = "seller", location_id: int = None) -> dict:
        """Crear nuevo usuario"""
        password_hash = get_password_hash(password)
        
        with get_db() as conn:
            cursor = conn.execute(
                '''INSERT INTO users (email, password_hash, first_name, last_name, role, location_id)
                   VALUES (?, ?, ?, ?, ?, ?)''',
                (email, password_hash, first_name, last_name, role, location_id)
            )
            conn.commit()
            
            return self.get_user_by_id(cursor.lastrowid)

auth_service = AuthService()