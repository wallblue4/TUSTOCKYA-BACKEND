# create_enhanced_sales_tables.py
import sqlite3
import os

DB_PATH = "data/tustockya.db"

def update_sales_tables():
    """Actualizar tablas de ventas con campos faltantes"""
    print("🔧 Actualizando tablas de ventas...")
    
    conn = sqlite3.connect(DB_PATH)
    
    # Agregar columnas a la tabla sales si no existen
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
    
    # Crear tabla de métodos de pago
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
    
    # Agregar columna destination_type a transfer_requests
    try:
        conn.execute('ALTER TABLE transfer_requests ADD COLUMN destination_type TEXT DEFAULT "bodega"')
        print("✅ Columna destination_type agregada a transferencias")
    except:
        print("ℹ️ Columna destination_type ya existe")
    
    # Crear tabla de notificaciones de devolución
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
    
    conn.commit()
    conn.close()
    print("✅ Tablas actualizadas con funcionalidades faltantes")

if __name__ == "__main__":
    update_sales_tables()