# create_sales_tables.py - Versión corregida completa
import sqlite3
import os

DB_PATH = "data/tustockya.db"

def create_all_tables():
    """Crear todas las tablas necesarias"""
    print("🔧 Creando todas las tablas necesarias...")
    
    conn = sqlite3.connect(DB_PATH)
    
    # PRIMERO: Tablas básicas (usuarios y ubicaciones)
    print("📋 Creando tablas básicas...")
    
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
    
    print("✅ Tablas básicas creadas")
    
    # SEGUNDO: Tablas del módulo vendedor
    print("🛍️ Creando tablas del módulo vendedor...")
    
    # Tabla de ventas
    conn.execute('''
        CREATE TABLE IF NOT EXISTS sales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER NOT NULL,
            location_id INTEGER NOT NULL,
            total_amount DECIMAL(10, 2) NOT NULL,
            receipt_image TEXT,
            sale_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            status TEXT DEFAULT 'completed',
            notes TEXT,
            FOREIGN KEY (seller_id) REFERENCES users (id),
            FOREIGN KEY (location_id) REFERENCES locations (id)
        )
    ''')
    
    # Tabla de items de venta
    conn.execute('''
        CREATE TABLE IF NOT EXISTS sale_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sale_id INTEGER NOT NULL,
            sneaker_reference_code TEXT NOT NULL,
            brand TEXT NOT NULL,
            model TEXT NOT NULL,
            color TEXT,
            size TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            unit_price DECIMAL(10, 2) NOT NULL,
            subtotal DECIMAL(10, 2) NOT NULL,
            FOREIGN KEY (sale_id) REFERENCES sales (id)
        )
    ''')
    
    # Tabla de gastos
    conn.execute('''
        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            location_id INTEGER NOT NULL,
            concept TEXT NOT NULL,
            amount DECIMAL(10, 2) NOT NULL,
            receipt_image TEXT,
            expense_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            notes TEXT,
            FOREIGN KEY (user_id) REFERENCES users (id),
            FOREIGN KEY (location_id) REFERENCES locations (id)
        )
    ''')
    
    # Tabla de solicitudes de transferencia
    conn.execute('''
        CREATE TABLE IF NOT EXISTS transfer_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            requester_id INTEGER NOT NULL,
            source_location_id INTEGER NOT NULL,
            destination_location_id INTEGER NOT NULL,
            sneaker_reference_code TEXT NOT NULL,
            brand TEXT NOT NULL,
            model TEXT NOT NULL,
            size TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            purpose TEXT NOT NULL,
            pickup_type TEXT NOT NULL,
            courier_id INTEGER,
            warehouse_keeper_id INTEGER,
            status TEXT DEFAULT 'pending',
            requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            accepted_at TIMESTAMP NULL,
            picked_up_at TIMESTAMP NULL,
            delivered_at TIMESTAMP NULL,
            notes TEXT,
            FOREIGN KEY (requester_id) REFERENCES users (id),
            FOREIGN KEY (source_location_id) REFERENCES locations (id),
            FOREIGN KEY (destination_location_id) REFERENCES locations (id),
            FOREIGN KEY (courier_id) REFERENCES users (id),
            FOREIGN KEY (warehouse_keeper_id) REFERENCES users (id)
        )
    ''')
    
    # Tabla de solicitudes de descuento
    conn.execute('''
        CREATE TABLE IF NOT EXISTS discount_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER NOT NULL,
            amount DECIMAL(10, 2) NOT NULL,
            reason TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            administrator_id INTEGER,
            requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            reviewed_at TIMESTAMP NULL,
            admin_comments TEXT,
            FOREIGN KEY (seller_id) REFERENCES users (id),
            FOREIGN KEY (administrator_id) REFERENCES users (id)
        )
    ''')
    
    # Tabla de devoluciones
    conn.execute('''
        CREATE TABLE IF NOT EXISTS return_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            original_transfer_id INTEGER NOT NULL,
            requester_id INTEGER NOT NULL,
            source_location_id INTEGER NOT NULL,
            destination_location_id INTEGER NOT NULL,
            sneaker_reference_code TEXT NOT NULL,
            size TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            courier_id INTEGER,
            warehouse_keeper_id INTEGER,
            status TEXT DEFAULT 'pending',
            requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP NULL,
            notes TEXT,
            FOREIGN KEY (original_transfer_id) REFERENCES transfer_requests (id),
            FOREIGN KEY (requester_id) REFERENCES users (id),
            FOREIGN KEY (source_location_id) REFERENCES locations (id),
            FOREIGN KEY (destination_location_id) REFERENCES locations (id),
            FOREIGN KEY (courier_id) REFERENCES users (id),
            FOREIGN KEY (warehouse_keeper_id) REFERENCES users (id)
        )
    ''')
    
    conn.commit()
    print("✅ Tablas del módulo vendedor creadas")
    
    # TERCERO: Datos iniciales
    create_initial_data(conn)
    
    conn.close()

def create_initial_data(conn):
    """Crear datos iniciales"""
    print("🏗️ Creando datos iniciales...")
    
    # Verificar si ya existen usuarios
    cursor = conn.execute("SELECT COUNT(*) FROM users")
    if cursor.fetchone()[0] > 0:
        print("ℹ️ Los usuarios ya existen, solo agregando ubicaciones...")
        create_additional_locations(conn)
        return
    
    # Crear ubicaciones
    locations = [
        ("Local Principal", "local", "Dirección principal"),
        ("Local Norte", "local", "Av. Norte 123"),
        ("Local Sur", "local", "Calle Sur 456"),
        ("Bodega Central", "bodega", "Industrial 789"),
        ("Bodega Norte", "bodega", "Industrial Norte 321")
    ]
    
    for name, loc_type, address in locations:
        conn.execute(
            'INSERT OR IGNORE INTO locations (name, type, address) VALUES (?, ?, ?)',
            (name, loc_type, address)
        )
    
    conn.commit()
    print("✅ Ubicaciones creadas")
    
    # Crear usuarios si no existen
    from passlib.context import CryptContext
    pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
    
    # Obtener ID del Local Principal
    cursor = conn.execute("SELECT id FROM locations WHERE name = ?", ("Local Principal",))
    location_id = cursor.fetchone()[0]
    
    users = [
        ("admin@tustockya.com", "admin123", "Admin", "TuStockYa", "administrador"),
        ("vendedor@test.com", "test123", "Vendedor", "Prueba", "vendedor"),
        ("bodeguero@test.com", "test123", "Bodeguero", "Prueba", "bodeguero"),
        ("corredor@test.com", "test123", "Corredor", "Prueba", "corredor")
    ]
    
    for email, password, first_name, last_name, role in users:
        try:
            password_hash = pwd_context.hash(password)
            conn.execute(
                '''INSERT OR IGNORE INTO users (email, password_hash, first_name, last_name, role, location_id)
                   VALUES (?, ?, ?, ?, ?, ?)''',
                (email, password_hash, first_name, last_name, role, location_id)
            )
            print(f"✅ Usuario: {email} / {password}")
        except Exception as e:
            print(f"⚠️ Usuario {email}: {e}")
    
    conn.commit()
    print("✅ Usuarios creados")

def create_additional_locations(conn):
    """Crear ubicaciones adicionales para testing"""
    additional_locations = [
        ("Local Norte", "local", "Av. Norte 123"),
        ("Local Sur", "local", "Calle Sur 456"),
        ("Bodega Central", "bodega", "Industrial 789"),
        ("Bodega Norte", "bodega", "Industrial Norte 321")
    ]
    
    for name, loc_type, address in additional_locations:
        conn.execute(
            'INSERT OR IGNORE INTO locations (name, type, address) VALUES (?, ?, ?)',
            (name, loc_type, address)
        )
    
    conn.commit()
    print("✅ Ubicaciones adicionales agregadas")

if __name__ == "__main__":
    # Crear directorio si no existe
    os.makedirs("data", exist_ok=True)
    create_all_tables()
    
    # Mostrar resumen
    conn = sqlite3.connect(DB_PATH)
    
    cursor = conn.execute("SELECT COUNT(*) FROM users")
    user_count = cursor.fetchone()[0]
    
    cursor = conn.execute("SELECT COUNT(*) FROM locations")
    location_count = cursor.fetchone()[0]
    
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [row[0] for row in cursor.fetchall()]
    
    conn.close()
    
    print("\n" + "="*50)
    print("📊 RESUMEN DE LA BASE DE DATOS")
    print("="*50)
    print(f"👥 Usuarios: {user_count}")
    print(f"🏪 Ubicaciones: {location_count}")
    print(f"📋 Tablas creadas: {len(tables)}")
    print(f"📝 Tablas: {', '.join(tables)}")
    print("="*50)
    print("✅ Base de datos lista para usar!")