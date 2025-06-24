# create_complete_tables.py
import sqlite3
import os

DB_PATH = "data/tustockya.db"

def create_complete_tables():
    """Crear todas las tablas necesarias para el módulo vendedor completo"""
    print("🔧 Creando tablas completas para módulo vendedor...")
    
    conn = sqlite3.connect(DB_PATH)
    
    # Tabla sale_payments (si no existe)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS sale_payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sale_id INTEGER NOT NULL,
            payment_type TEXT NOT NULL,
            amount DECIMAL(10, 2) NOT NULL,
            reference TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (sale_id) REFERENCES sales (id)
        )
    ''')
    
    # Tabla return_notifications (si no existe)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS return_notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            transfer_request_id INTEGER NOT NULL,
            returned_to_location TEXT NOT NULL,
            returned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            notes TEXT,
            read_by_requester BOOLEAN DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (transfer_request_id) REFERENCES transfer_requests (id)
        )
    ''')
    
    # Agregar columnas faltantes a sales
    try:
        conn.execute('ALTER TABLE sales ADD COLUMN requires_confirmation BOOLEAN DEFAULT 0')
        print("✅ Columna requires_confirmation agregada")
    except:
        print("ℹ️ Columna requires_confirmation ya existe")
    
    try:
        conn.execute('ALTER TABLE sales ADD COLUMN confirmed BOOLEAN DEFAULT 1')
        print("✅ Columna confirmed agregada")
    except:
        print("ℹ️ Columna confirmed ya existe")
    
    try:
        conn.execute('ALTER TABLE sales ADD COLUMN confirmed_at TIMESTAMP NULL')
        print("✅ Columna confirmed_at agregada")
    except:
        print("ℹ️ Columna confirmed_at ya existe")
    
    # Agregar columna destination_type a transfer_requests
    try:
        conn.execute('ALTER TABLE transfer_requests ADD COLUMN destination_type TEXT DEFAULT "bodega"')
        print("✅ Columna destination_type agregada")
    except:
        print("ℹ️ Columna destination_type ya existe")
    
    conn.commit()
    conn.close()
    print("✅ Todas las tablas del módulo vendedor están listas")

if __name__ == "__main__":
    create_complete_tables()