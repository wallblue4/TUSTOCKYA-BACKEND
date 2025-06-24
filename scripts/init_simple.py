# scripts/init_simple.py
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.database_sqlite import get_db, init_database
from app.services.auth_service import auth_service

def create_initial_data():
    """Crear datos iniciales"""
    
    # Primero inicializar las tablas
    init_database()
    
    # Crear ubicación
    with get_db() as conn:
        cursor = conn.execute("SELECT id FROM locations WHERE name = ?", ("Local Principal",))
        if not cursor.fetchone():
            conn.execute(
                "INSERT INTO locations (name, type, address) VALUES (?, ?, ?)",
                ("Local Principal", "local", "Dirección principal")
            )
            conn.commit()
            print("✅ Ubicación creada")
        
        cursor = conn.execute("SELECT id FROM locations WHERE name = ?", ("Local Principal",))
        location_id = cursor.fetchone()[0]
    
    # Crear usuarios
    try:
        admin = auth_service.create_user(
            email="admin@tustockya.com",
            password="admin123",
            first_name="Admin",
            last_name="TuStockYa",
            role="administrador",
            location_id=location_id
        )
        print("✅ Usuario admin creado: admin@tustockya.com / admin123")
    except Exception as e:
        print(f"⚠️ Usuario admin: {e}")
    
    try:
        vendedor = auth_service.create_user(
            email="vendedor@test.com",
            password="test123",
            first_name="Vendedor",
            last_name="Prueba",
            role="vendedor",
            location_id=location_id
        )
        print("✅ Usuario vendedor creado: vendedor@test.com / test123")
    except Exception as e:
        print(f"⚠️ Usuario vendedor: {e}")

if __name__ == "__main__":
    create_initial_data()