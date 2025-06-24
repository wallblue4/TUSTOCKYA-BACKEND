# app/core/database_sqlite.py
import sqlite3
import os
from contextlib import contextmanager
from typing import Generator

# Crear directorio para SQLite
os.makedirs("data", exist_ok=True)
SQLITE_DB_PATH = "data/tustockya.db"

def get_db_connection():
    """Obtener conexión SQLite"""
    conn = sqlite3.connect(SQLITE_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

@contextmanager
def get_db() -> Generator[sqlite3.Connection, None, None]:
    """Context manager para BD"""
    conn = get_db_connection()
    try:
        yield conn
    finally:
        conn.close()

def init_database():
    """Crear tablas SQLite"""
    print("🔧 Inicializando base de datos SQLite...")
    
    with get_db() as conn:
        # Tabla ubicaciones (primero porque es referenciada)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS locations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                type TEXT NOT NULL,
                address TEXT,
                phone TEXT,
                is_active BOOLEAN DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Tabla usuarios
        conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                first_name TEXT NOT NULL,
                last_name TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'vendedor',
                location_id INTEGER,
                is_active BOOLEAN DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (location_id) REFERENCES locations (id)
            )
        ''')
        
        # Tabla referencias de tenis
        conn.execute('''
            CREATE TABLE IF NOT EXISTS sneaker_references (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reference_code TEXT UNIQUE NOT NULL,
                brand TEXT NOT NULL,
                model TEXT NOT NULL,
                color TEXT,
                gender TEXT DEFAULT 'unisex',
                image_url TEXT,
                description TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Tabla inventario
        conn.execute('''
            CREATE TABLE IF NOT EXISTS inventory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sneaker_reference_id INTEGER NOT NULL,
                location_id INTEGER NOT NULL,
                size TEXT NOT NULL,
                quantity_stock INTEGER DEFAULT 0,
                quantity_exhibition INTEGER DEFAULT 0,
                unit_price REAL,
                box_price REAL,
                minimum_stock INTEGER DEFAULT 5,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (sneaker_reference_id) REFERENCES sneaker_references (id),
                FOREIGN KEY (location_id) REFERENCES locations (id),
                UNIQUE(sneaker_reference_id, location_id, size)
            )
        ''')
        
        conn.commit()
        print("✅ Tablas SQLite creadas correctamente")

# Redis (opcional)
try:
    import redis
    redis_client = redis.from_url("redis://localhost:6379/0", decode_responses=True)
    redis_client.ping()  # Test conexión
    print("✅ Redis conectado")
except Exception as e:
    redis_client = None
    print(f"⚠️ Redis no disponible: {e}")

def get_redis():
    return redis_client