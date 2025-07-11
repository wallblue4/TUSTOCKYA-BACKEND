# main_standalone.py - Versión completa con todos los requerimientos del seller
import sys
import os
import sqlite3
import tempfile
import random
import asyncio
import httpx
from datetime import datetime, timedelta

from fastapi import FastAPI, HTTPException, status, File, UploadFile, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from passlib.context import CryptContext
from jose import jwt
import cloudinary
import cloudinary.uploader
from cloudinary.exceptions import Error as CloudinaryError
import io
from PIL import Image

# ==================== CONFIGURACIÓN PARA RAILWAY ====================

# Variables de entorno para Railway
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./data/tustockya.db")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
SECRET_KEY = os.getenv("SECRET_KEY", "super-secret-key-cambia-en-produccion")
PORT = int(os.getenv("PORT", "10000"))  # Render usa puerto 10000 por defecto



cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET"),
    secure=True
)


# Configuración de base de datos
# Detectar si estamos en Render (siempre usar PostgreSQL)
if os.getenv("RENDER") or os.getenv("DATABASE_URL", "").startswith("postgresql"):
    # Estamos en Render - usar PostgreSQL
    DB_PATH = DATABASE_URL
    USE_POSTGRESQL = True
    print(f"💾 Usando PostgreSQL: {DATABASE_URL[:50]}...")
elif DATABASE_URL.startswith("sqlite"):
    # Desarrollo local - usar SQLite
    DB_PATH = DATABASE_URL.replace("sqlite:///", "")
    os.makedirs(os.path.dirname(DB_PATH) if os.path.dirname(DB_PATH) else "data", exist_ok=True)
    USE_POSTGRESQL = False
    print(f"💾 Usando SQLite: {DB_PATH}")
else:
    # Fallback a PostgreSQL
    DB_PATH = DATABASE_URL
    USE_POSTGRESQL = True
    print(f"💾 Usando PostgreSQL: {DATABASE_URL[:50]}...")

# Crear directorio de uploads
upload_dir = os.getenv("UPLOAD_DIR", "data/uploads")
os.makedirs(upload_dir, exist_ok=True)

ALGORITHM = "HS256"

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBearer()

try:
    import psycopg2
    import psycopg2.extras
    PSYCOPG2_AVAILABLE = True
except ImportError:
    PSYCOPG2_AVAILABLE = False
    print("⚠️ psycopg2 no disponible - solo SQLite funcionará")

# ==================== SCHEMAS ====================

class UserLogin(BaseModel):
    email: str
    password: str

# Schemas para métodos de pago
class PaymentMethod(BaseModel):
    type: str  # 'efectivo', 'tarjeta', 'transferencia', 'mixto'
    amount: float
    reference: str = None  # Número de tarjeta (últimos 4), referencia transferencia, etc.

# Schemas para módulo seller completo
class SaleCreateComplete(BaseModel):
    items: list
    total_amount: float
    payment_methods: list[PaymentMethod]  # Puede ser múltiples métodos
    receipt_image: str = None  # Foto del comprobante
    notes: str = None
    requires_confirmation: bool = False  # Si necesita confirmación posterior

class SaleConfirmation(BaseModel):
    sale_id: int
    confirmed: bool
    confirmation_notes: str = None

class ExpenseCreate(BaseModel):
    concept: str
    amount: float
    receipt_image: str = None  # Foto del comprobante
    notes: str = None

class TransferRequestComplete(BaseModel):
    source_location_id: int
    sneaker_reference_code: str
    brand: str
    model: str
    size: str
    quantity: int
    purpose: str  # 'exhibition' o 'sale'
    pickup_type: str  # 'seller' o 'corredor'
    destination_type: str  # 'bodega' o 'exhibicion' - donde se guardará
    notes: str = None

class DiscountRequestCreate(BaseModel):
    amount: float
    reason: str

class ReturnRequestCreate(BaseModel):
    original_transfer_id: int
    notes: str = None

class ReturnNotification(BaseModel):
    transfer_request_id: int
    returned_to_location: str
    returned_at: str
    notes: str = None

# ==================== FUNCIONES DE SEGURIDAD ====================

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)

def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=10080)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def decode_token(token: str):
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except:
        return None

# ==================== DEPENDENCIAS ====================

async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials
    payload = decode_token(token)
    
    if not payload:
        raise HTTPException(status_code=401, detail="Token inválido")
    
    user_id = payload.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Token inválido")
    
    if USE_POSTGRESQL:
        # Usar PostgreSQL
        import psycopg2
        import psycopg2.extras
        
        conn = psycopg2.connect(DB_PATH)
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute(
            "SELECT * FROM users WHERE id = %s AND is_active = TRUE", (user_id,)
        )
        user = cursor.fetchone()
        conn.close()
    else:
        # Usar SQLite
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            "SELECT * FROM users WHERE id = ? AND is_active = 1", (user_id,)
        )
        user = cursor.fetchone()
        conn.close()
    
    if not user:
        raise HTTPException(status_code=401, detail="Usuario no encontrado")
    
    return dict(user)

async def upload_receipt_to_cloudinary(
    file: UploadFile, 
    receipt_type: str,  # 'sale' o 'expense'
    user_id: int,
    record_id: str = None  # ID de venta o gasto
) -> str:
    """
    Subir comprobante a Cloudinary y retornar solo la URL
    """
    try:
        # Leer y validar archivo
        content = await file.read()
        
        if len(content) > MAX_IMAGE_SIZE:
            raise HTTPException(status_code=413, detail="Imagen muy grande (máximo 10MB)")
        
        if not file.content_type or file.content_type not in ALLOWED_IMAGE_FORMATS:
            raise HTTPException(status_code=400, detail="Formato no válido")
        
        # Optimizar imagen
        img = Image.open(io.BytesIO(content))
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        
        # Redimensionar si es muy grande
        if img.width > 1920:
            ratio = 1920 / img.width
            new_height = int(img.height * ratio)
            img = img.resize((1920, new_height), Image.Resampling.LANCZOS)
        
        # Guardar optimizada en memoria
        output = io.BytesIO()
        img.save(output, format='JPEG', quality=85, optimize=True)
        optimized_content = output.getvalue()
        
        # Generar ID único
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        unique_id = str(uuid.uuid4())[:8]
        public_id = f"{CLOUDINARY_FOLDER}/receipts/{receipt_type}/{timestamp}_{user_id}_{unique_id}"
        
        # Subir a Cloudinary
        upload_result = cloudinary.uploader.upload(
            optimized_content,
            public_id=public_id,
            tags=[
                "tustockya",
                receipt_type,
                f"user_{user_id}",
                f"record_{record_id}" if record_id else f"temp_{unique_id}"
            ],
            folder=f"{CLOUDINARY_FOLDER}/receipts/{receipt_type}",
            resource_type="image",
            format="jpg",
            quality="auto:good",
            transformation=[
                {"width": 1920, "height": 1920, "crop": "limit"},
                {"quality": "auto:good"}
            ]
        )
        
        return upload_result["secure_url"]
        
    except CloudinaryError as e:
        raise HTTPException(status_code=500, detail=f"Error subiendo imagen: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error procesando imagen: {str(e)}")


# ==================== CONFIGURACIÓN FASTAPI ====================

app = FastAPI(
    title="TuStockYa Backend - Railway Ready",
    version="1.0.0",
    docs_url="/docs",
    description="Sistema completo para gestión de inventario de tenis con módulo seller completo - Railway Compatible"
)

# CORS mejorado para Railway
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:8080",
        "https://*.railway.app",
        "https://*.up.railway.app",
        "*"  # Para desarrollo, en producción ser más específico
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==================== ENDPOINTS BÁSICOS ====================

@app.get("/")
async def root():
    environment = "Railway" if os.getenv("RAILWAY_ENVIRONMENT") else "Local"
    return {
        "message": "🚀 TuStockYa Backend API - Railway Ready",
        "version": "1.0.0",
        "environment": environment,
        "database": "SQLite" if DATABASE_URL.startswith("sqlite") else "PostgreSQL",
        "status": "working",
        "port": PORT,
        "features": [
            "Escaneo de tenis con CLIP simulado",
            "Ventas con múltiples métodos de pago",
            "Confirmación de ventas",
            "Gestión de gastos con comprobantes",
            "Solicitudes de transferencia con ubicación específica",
            "Solicitudes de descuento",
            "Notificaciones de devolución",
            "Dashboard completo del seller"
        ]
    }

@app.get("/health")
async def health():
    try:
        if USE_POSTGRESQL:
            import psycopg2
            conn = psycopg2.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM users")
            user_count = cursor.fetchone()[0]
            cursor.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
            tables = [row[0] for row in cursor.fetchall()]
            conn.close()
        else:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.execute("SELECT COUNT(*) FROM users")
            user_count = cursor.fetchone()[0]
            cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [row[0] for row in cursor.fetchall()]
            conn.close()
        
        db_status = "connected"
    except Exception as e:
        user_count = 0
        tables = []
        db_status = f"error: {e}"
    
    environment = "Railway" if os.getenv("RAILWAY_ENVIRONMENT") else "Local"
    
    return {
        "status": "healthy",
        "environment": environment,
        "database": f"SQLite ({db_status})" if not USE_POSTGRESQL else f"PostgreSQL ({db_status})",
        "users": user_count,
        "tables": len(tables),
        "table_list": tables,
        "port": PORT,
        "upload_dir": upload_dir,
        "redis_available": bool(os.getenv("REDIS_URL")),
        "modules": [
            "Autenticación",
            "Clasificación con CLIP",
            "Módulo seller Completo",
            "Gestión de Inventario",
            "Transferencias y Devoluciones"
        ]
    }

# ==================== AUTENTICACIÓN ====================

@app.post("/api/v1/auth/login")
async def login(credentials: UserLogin):
    """Login de usuario"""
    
    if USE_POSTGRESQL:
        # Usar PostgreSQL
        import psycopg2
        import psycopg2.extras
        
        conn = psycopg2.connect(DB_PATH)
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        cursor.execute(
            '''SELECT u.*, l.name as location_name 
               FROM users u 
               LEFT JOIN locations l ON u.location_id = l.id
               WHERE u.email = %s AND u.is_active = TRUE''',  # ✅ TRUE en lugar de 1
            (credentials.email,)
        )
        user = cursor.fetchone()
        conn.close()
    else:
        # Usar SQLite
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        
        cursor = conn.execute(
            '''SELECT u.*, l.name as location_name 
               FROM users u 
               LEFT JOIN locations l ON u.location_id = l.id
               WHERE u.email = ? AND u.is_active = 1''',  # ✅ 1 para SQLite
            (credentials.email,)
        )
        user = cursor.fetchone()
        conn.close()
    
    if not user or not verify_password(credentials.password, user['password_hash']):
        raise HTTPException(status_code=401, detail="Email o contraseña incorrectos")
    
    access_token = create_access_token(data={"user_id": user['id']})
    
    user_dict = dict(user)
    
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "user": {
            "id": user_dict['id'],
            "email": user_dict['email'],
            "first_name": user_dict['first_name'],
            "last_name": user_dict['last_name'],
            "role": user_dict['role'],
            "location_id": user_dict['location_id'],
            "is_active": bool(user_dict['is_active']),
            "location_name": user_dict.get('location_name')
        }
    }

@app.get("/api/v1/auth/me")
async def get_me(current_user = Depends(get_current_user)):
    """Información del usuario actual"""
    return {
        "id": current_user['id'],
        "email": current_user['email'],
        "first_name": current_user['first_name'],
        "last_name": current_user['last_name'],
        "role": current_user['role'],
        "location_id": current_user['location_id']
    }

# ==================== CLASIFICACIÓN ====================

def validate_stock_availability(items, location_id):
    """Validar que hay stock suficiente para todos los items"""
    if USE_POSTGRESQL:
        conn = psycopg2.connect(DB_PATH)
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
    
    stock_issues = []
    
    for item in items:
        if USE_POSTGRESQL:
            cursor.execute('''
                SELECT ps.quantity 
                FROM product_sizes ps
                JOIN products p ON ps.product_id = p.id
                WHERE p.reference_code = %s 
                AND ps.size = %s 
                AND p.location_name = (SELECT name FROM locations WHERE id = %s)
            ''', (item['sneaker_reference_code'], item['size'], location_id))
        else:
            cursor.execute('''
                SELECT ps.quantity 
                FROM product_sizes ps
                JOIN products p ON ps.product_id = p.id
                WHERE p.reference_code = ? 
                AND ps.size = ? 
                AND p.location_name = (SELECT name FROM locations WHERE id = ?)
            ''', (item['sneaker_reference_code'], item['size'], location_id))
        
        result = cursor.fetchone()
        available_qty = result['quantity'] if result else 0

        print(available_qty)
        
        if available_qty < item['quantity']:
            stock_issues.append({
                "reference": item['sneaker_reference_code'],
                "size": item['size'],
                "requested": item['quantity'],
                "available": available_qty
            })
    
    conn.close()
    return stock_issues

def get_db_connection_inventory():
    """Obtener conexión para inventario real"""
    if DATABASE_URL.startswith("sqlite"):
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn, "sqlite"
    else:
        if not PSYCOPG2_AVAILABLE:
            raise Exception("psycopg2 no está disponible para PostgreSQL")
        
        conn = psycopg2.connect(DATABASE_URL)
        return conn, "postgresql"

def update_stock_after_sale(items, location_id):
    """Descontar stock después de confirmar venta"""
    if USE_POSTGRESQL:
        conn = psycopg2.connect(DB_PATH)
        cursor = conn.cursor()
    else:
        conn = sqlite3.connect(DB_PATH)
    
    try:
        for item in items:
            if USE_POSTGRESQL:
                cursor.execute('''
                    UPDATE product_sizes 
                    SET quantity = quantity - %s
                    WHERE product_id = (
                        SELECT p.id FROM products p 
                        WHERE p.reference_code = %s 
                        AND p.location_name = (SELECT name FROM locations WHERE id = %s)
                    ) 
                    AND size = %s
                ''', (item['quantity'], item['sneaker_reference_code'], location_id, item['size']))
            else:
                conn.execute('''
                    UPDATE product_sizes 
                    SET quantity = quantity - ?
                    WHERE product_id = (
                        SELECT p.id FROM products p 
                        WHERE p.reference_code = ? 
                        AND p.location_name = (SELECT name FROM locations WHERE id = ?)
                    ) 
                    AND size = ?
                ''', (item['quantity'], item['sneaker_reference_code'], location_id, item['size']))
        
        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

def search_products_in_real_inventory(model_name: str, limit: int = 5):
    """Buscar productos en el inventario real basado en el model_name del microservicio"""
    try:
        conn, db_type = get_db_connection_inventory()
        
        if db_type == "sqlite":
            cursor = conn.execute('''
                SELECT p.*, 
                       GROUP_CONCAT(ps.size || '/' || ps.quantity) as sizes_stock,
                       SUM(ps.quantity) as total_available,
                       SUM(ps.quantity_exhibition) as total_exhibition
                FROM products p
                LEFT JOIN product_sizes ps ON p.id = ps.product_id
                WHERE p.description LIKE ? OR p.brand LIKE ? OR p.model LIKE ?
                AND p.is_active = 1
                GROUP BY p.id
                ORDER BY total_available DESC
                LIMIT ?
            ''', (f'%{model_name}%', f'%{model_name}%', f'%{model_name}%', limit))
            
            products = [dict(row) for row in cursor.fetchall()]
        else:
            cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cursor.execute('''
                SELECT p.*, 
                       STRING_AGG(ps.size || '/' || ps.quantity, ',') as sizes_stock,
                       SUM(ps.quantity) as total_available,
                       SUM(ps.quantity_exhibition) as total_exhibition
                FROM products p
                LEFT JOIN product_sizes ps ON p.id = ps.product_id
                WHERE p.description ILIKE %s OR p.brand ILIKE %s OR p.model ILIKE %s
                AND p.is_active = 1
                GROUP BY p.id, p.reference_code, p.description, p.brand, p.model, 
                         p.color_info, p.video_url, p.image_url, p.total_quantity, 
                         p.location_name, p.unit_price, p.box_price, p.created_at, p.updated_at
                ORDER BY SUM(ps.quantity) DESC
                LIMIT %s
            ''', (f'%{model_name}%', f'%{model_name}%', f'%{model_name}%', limit))
            
            products = [dict(row) for row in cursor.fetchall()]
            cursor.close()
        
        conn.close()
        
        # Procesar datos para formato del API
        for product in products:
            # Parsear tallas
            if product.get('sizes_stock'):
                size_pairs = product['sizes_stock'].split(',')
                stock_by_size = []
                for pair in size_pairs:
                    if '/' in pair:
                        size, qty = pair.split('/')
                        stock_by_size.append({
                            "size": size,
                            "quantity_stock": int(qty),
                            "quantity_exhibition": 0,  # Se puede mejorar
                            "location": product['location_name']
                        })
                product['parsed_stock'] = stock_by_size
            else:
                product['parsed_stock'] = []
        
        return products
        
    except Exception as e:
        print(f"Error buscando en inventario real: {e}")
        return []

async def call_real_classification_service(image_content: bytes, filename: str):
    """Llamar a tu microservicio real de clasificación"""
    try:
        # Tu endpoint real
        classification_url = "https://sneaker-api-v2.onrender.com/api/v2/classify"
        
        # Preparar archivo para upload
        files = {
            "image": (filename, image_content, "image/jpeg")
        }
        
        # Llamada al microservicio
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(classification_url, files=files)
            response.raise_for_status()
            
            classification_result = response.json()
            print(f"🤖 Respuesta del microservicio: {classification_result.get('total_matches_found', 0)} matches")
            
            return classification_result
            
    except httpx.TimeoutException:
        print("⏰ Timeout en microservicio de clasificación")
        return None
    except httpx.HTTPStatusError as e:
        print(f"❌ Error HTTP en microservicio: {e.response.status_code}")
        return None
    except Exception as e:
        print(f"❌ Error llamando microservicio: {e}")
        return None

def merge_classification_with_inventory(classification_result, user_location_id):
    """Combinar resultados de clasificación con inventario real"""
    if not classification_result or not classification_result.get('results'):
        return []
    
    merged_results = []
    
    for rank, result in enumerate(classification_result['results'][:3], 1):  # Top 3
        model_name = result.get('model_name', '')
        
        # Buscar en inventario real
        real_products = search_products_in_real_inventory(model_name, limit=2)
        
        if real_products:
            # Usar datos reales si se encuentran
            for real_product in real_products:
                merged_result = {
                    "rank": rank,
                    "similarity_score": result.get('similarity_score', 0.0),
                    "confidence_percentage": result.get('confidence_percentage', 0.0),
                    "confidence_level": result.get('confidence_level', 'low'),
                    "reference": {
                        "code": real_product['reference_code'],
                        "brand": real_product['brand'],
                        "model": real_product['model'] or "Classic",
                        "color": real_product['color_info'] or "Varios",
                        "description": real_product['description'],
                        "photo": real_product['image_url'] or f"https://via.placeholder.com/300x300?text={real_product['brand']}"
                    },
                    "inventory": {
                        "local_info": {
                            "location_number": user_location_id,
                            "location_name": real_product['location_name']
                        },
                        "pricing": {
                            "unit_price": float(real_product['unit_price'] or 0),
                            "box_price": float(real_product['box_price'] or 0)
                        },
                        "stock_by_size": real_product['parsed_stock'],
                        "total_stock": int(real_product['total_available'] or 0),
                        "total_exhibition": int(real_product['total_exhibition'] or 0),
                        "available_sizes": [s['size'] for s in real_product['parsed_stock'] if s['quantity_stock'] > 0],
                        "other_locations": []  # Se puede expandir
                    },
                    "availability": {
                        "in_stock": int(real_product['total_available'] or 0) > 0,
                        "can_sell": int(real_product['total_available'] or 0) > 0,
                        "can_request_from_other_locations": True,
                        "recommended_action": "Venta disponible en stock local" if int(real_product['total_available'] or 0) > 0 else "Sin stock disponible"
                    },
                    "classification_source": "real_microservice",
                    "inventory_source": "real_database",
                    "original_db_id": result.get('original_db_id'),
                    "image_path": result.get('image_path')
                }
                merged_results.append(merged_result)
                break  # Solo el primer match por resultado de clasificación
        else:
            # Fallback a datos mock si no se encuentra en inventario
            merged_result = {
                "rank": rank,
                "similarity_score": result.get('similarity_score', 0.0),
                "confidence_percentage": result.get('confidence_percentage', 0.0),
                "confidence_level": result.get('confidence_level', 'low'),
                "reference": {
                    "code": f"UNKNOWN-{rank:03d}",
                    "brand": result.get('brand', 'Unknown'),
                    "model": model_name,
                    "color": result.get('color', 'Unknown'),
                    "description": model_name,
                    "photo": f"https://via.placeholder.com/300x300?text={model_name.replace(' ', '+')}"
                },
                "inventory": {
                    "local_info": {
                        "location_number": user_location_id,
                        "location_name": f"Local #{user_location_id}"
                    },
                    "pricing": {
                        "unit_price": float(result.get('price', 0.0)),
                        "box_price": float(result.get('price', 0.0)) * 0.9
                    },
                    "stock_by_size": [],
                    "total_stock": 0,
                    "total_exhibition": 0,
                    "available_sizes": [],
                    "other_locations": []
                },
                "availability": {
                    "in_stock": False,
                    "can_sell": False,
                    "can_request_from_other_locations": True,
                    "recommended_action": "Producto no encontrado en inventario local"
                },
                "classification_source": "real_microservice",
                "inventory_source": "not_found",
                "original_db_id": result.get('original_db_id'),
                "image_path": result.get('image_path')
            }
            merged_results.append(merged_result)
    
    return merged_results

# main_standalone.py - REEMPLAZAR ENDPOINT DE ESCANEO

@app.post("/api/v1/classify/scan")
async def scan_sneaker_integrated(
    image: UploadFile = File(...),
    current_user = Depends(get_current_user)
):
    """Escanear tenis usando microservicio real + inventario real"""
    
    start_time = datetime.now()
    
    if not image.content_type or not image.content_type.startswith('image/'):
        raise HTTPException(status_code=400, detail="El archivo debe ser una imagen")
    
    content = await image.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Archivo muy grande (máximo 10MB)")
    
    print(f"🔍 Iniciando escaneo con microservicio real...")
    
    # Llamar al microservicio real de clasificación
    classification_result = await call_real_classification_service(content, image.filename)
    
    if classification_result and classification_result.get('success'):
        print(f"✅ Microservicio respondió: {classification_result.get('total_matches_found', 0)} matches")
        
        # Combinar con inventario real
        merged_results = merge_classification_with_inventory(classification_result, current_user['location_id'])
        
        processing_time = (datetime.now() - start_time).total_seconds() * 1000
        
        return {
            "success": True,
            "scan_timestamp": datetime.now().isoformat(),
            "scanned_by": {
                "user_id": current_user['id'],
                "email": current_user['email'],
                "name": f"{current_user['first_name']} {current_user['last_name']}",
                "role": current_user['role'],
                "location_id": current_user['location_id']
            },
            "user_location": f"Local #{current_user['location_id']}",
            "best_match": merged_results[0] if merged_results else None,
            "alternative_matches": merged_results[1:] if len(merged_results) > 1 else [],
            "total_matches_found": len(merged_results),
            "processing_time_ms": round(processing_time, 2),
            "image_info": {
                "filename": image.filename,
                "size_bytes": len(content),
                "content_type": image.content_type
            },
            "classification_service": {
                "service": "real_microservice",
                "url": "https://sneaker-api-v2.onrender.com/api/v2/classify",
                "model": classification_result.get('model_info', {}).get('model', 'jina-clip-v2'),
                "total_database_matches": classification_result.get('total_matches_found', 0)
            },
            "inventory_service": {
                "source": "real_database",
                "products_found": len([r for r in merged_results if r.get('inventory_source') == 'real_database']),
                "locations_checked": [current_user['location_id']]
            }
        }
    else:
        # Fallback a mock si el microservicio falla
        print("⚠️ Microservicio no disponible, usando datos mock")
        
        # Tu código mock actual aquí como fallback
        mock_results = [
            {
                "rank": 1,
                "similarity_score": 0.50,
                "confidence_percentage": 50.0,
                "confidence_level": "medium",
                "reference": {
                    "code": "FALLBACK-001",
                    "brand": "Unknown",
                    "model": "Sistema en mantenimiento",
                    "color": "N/A",
                    "description": "Microservicio de clasificación temporalmente no disponible",
                    "photo": "https://via.placeholder.com/300x300?text=Sistema+en+Mantenimiento"
                },
                "inventory": {
                    "local_info": {
                        "location_number": current_user['location_id'],
                        "location_name": f"Local #{current_user['location_id']}"
                    },
                    "pricing": {"unit_price": 0.0, "box_price": 0.0},
                    "stock_by_size": [],
                    "total_stock": 0,
                    "total_exhibition": 0,
                    "available_sizes": []
                },
                "availability": {
                    "in_stock": False,
                    "can_sell": False,
                    "can_request_from_other_locations": False,
                    "recommended_action": "Sistema de clasificación en mantenimiento"
                }
            }
        ]
        
        processing_time = (datetime.now() - start_time).total_seconds() * 1000
        
        return {
            "success": True,
            "scan_timestamp": datetime.now().isoformat(),
            "scanned_by": {
                "user_id": current_user['id'],
                "email": current_user['email'],
                "name": f"{current_user['first_name']} {current_user['last_name']}",
                "role": current_user['role'],
                "location_id": current_user['location_id']
            },
            "user_location": f"Local #{current_user['location_id']}",
            "best_match": mock_results[0],
            "alternative_matches": [],
            "total_matches_found": 1,
            "processing_time_ms": round(processing_time, 2),
            "image_info": {
                "filename": image.filename,
                "size_bytes": len(content),
                "content_type": image.content_type
            },
            "classification_service": {
                "service": "fallback_mock",
                "status": "microservice_unavailable",
                "message": "Usando datos mock como respaldo"
            }
        }

@app.get("/api/v1/classify/health")
async def classification_health():
    return {
        "service": "classification",
        "status": "healthy",
        "mode": "simulation",
        "model": "Mock CLIP ViT-L/14",
        "features": [
            "Detección de marca y modelo",
            "Información de inventario por ubicación",
            "Precios unitarios y por caja",
            "Stock por talla",
            "Disponibilidad en otros locales"
        ]
    }

# ==================== MÓDULO seller COMPLETO ====================

# DASHBOARD COMPLETO DEL seller
@app.get("/api/v1/vendor/dashboard")
async def get_vendor_dashboard_complete(current_user = Depends(get_current_user)):
    """Dashboard completo del seller con todas las funcionalidades según requerimientos"""
    
    if current_user['role'] not in ['seller', 'administrador']:
        raise HTTPException(status_code=403, detail="Acceso denegado")
    
    if USE_POSTGRESQL:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(DB_PATH)
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        # Ventas del día (confirmadas y pendientes) - PostgreSQL
        cursor.execute(
            '''SELECT 
                 COUNT(*) as total_sales,
                 COALESCE(SUM(CASE WHEN confirmed = TRUE THEN total_amount ELSE 0 END), 0) as confirmed_amount,
                 COALESCE(SUM(CASE WHEN confirmed = FALSE AND requires_confirmation = TRUE THEN total_amount ELSE 0 END), 0) as pending_amount,
                 COUNT(CASE WHEN confirmed = FALSE AND requires_confirmation = TRUE THEN 1 END) as pending_confirmations
               FROM sales 
               WHERE DATE(sale_date) = CURRENT_DATE AND seller_id = %s''',
            (current_user['id'],)
        )
        sales_today = dict(cursor.fetchone())
        
        # Métodos de pago del día - PostgreSQL
        cursor.execute(
            '''SELECT sp.payment_type, SUM(sp.amount) as total_amount, COUNT(*) as count
               FROM sale_payments sp
               JOIN sales s ON sp.sale_id = s.id
               WHERE DATE(s.sale_date) = CURRENT_DATE AND s.seller_id = %s AND s.confirmed = TRUE
               GROUP BY sp.payment_type
               ORDER BY total_amount DESC''',
            (current_user['id'],)
        )
        payment_methods = [dict(row) for row in cursor.fetchall()]
        
        # Gastos del día - PostgreSQL
        cursor.execute(
            '''SELECT COUNT(*) as count, COALESCE(SUM(amount), 0) as total
               FROM expenses 
               WHERE DATE(expense_date) = CURRENT_DATE AND user_id = %s''',
            (current_user['id'],)
        )
        expenses_today = dict(cursor.fetchone())
        
        # Solicitudes pendientes - PostgreSQL
        cursor.execute(
            '''SELECT 
                 COUNT(CASE WHEN status = 'pending' THEN 1 END) as pending,
                 COUNT(CASE WHEN status = 'in_transit' THEN 1 END) as in_transit,
                 COUNT(CASE WHEN status = 'delivered' THEN 1 END) as delivered
               FROM transfer_requests WHERE requester_id = %s''',
            (current_user['id'],)
        )
        transfer_stats = dict(cursor.fetchone())
        
        cursor.execute(
            '''SELECT 
                 COUNT(CASE WHEN status = 'pending' THEN 1 END) as pending,
                 COUNT(CASE WHEN status = 'approved' THEN 1 END) as approved,
                 COUNT(CASE WHEN status = 'rejected' THEN 1 END) as rejected
               FROM discount_requests WHERE seller_id = %s''',
            (current_user['id'],)
        )
        discount_stats = dict(cursor.fetchone())
        
        # Notificaciones de devolución no leídas - PostgreSQL
        cursor.execute(
            '''SELECT COUNT(*) as count 
               FROM return_notifications rn
               JOIN transfer_requests tr ON rn.transfer_request_id = tr.id
               WHERE tr.requester_id = %s AND rn.read_by_requester = FALSE''',
            (current_user['id'],)
        )
        unread_returns = cursor.fetchone()['count'] 
        
    else:
        # SQLite (código original)
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        
        # Ventas del día (confirmadas y pendientes)
        cursor = conn.execute(
            '''SELECT 
                 COUNT(*) as total_sales,
                 COALESCE(SUM(CASE WHEN confirmed = 1 THEN total_amount ELSE 0 END), 0) as confirmed_amount,
                 COALESCE(SUM(CASE WHEN confirmed = 0 AND requires_confirmation = 1 THEN total_amount ELSE 0 END), 0) as pending_amount,
                 COUNT(CASE WHEN confirmed = 0 AND requires_confirmation = 1 THEN 1 END) as pending_confirmations
               FROM sales 
               WHERE DATE(sale_date) = DATE('now') AND seller_id = ?''',
            (current_user['id'],)
        )
        sales_today = dict(cursor.fetchone())
        
        # Métodos de pago del día
        cursor = conn.execute(
            '''SELECT sp.payment_type, SUM(sp.amount) as total_amount, COUNT(*) as count
               FROM sale_payments sp
               JOIN sales s ON sp.sale_id = s.id
               WHERE DATE(s.sale_date) = DATE('now') AND s.seller_id = ? AND s.confirmed = 1
               GROUP BY sp.payment_type
               ORDER BY total_amount DESC''',
            (current_user['id'],)
        )
        payment_methods = [dict(row) for row in cursor.fetchall()]
        
        # Gastos del día
        cursor = conn.execute(
            '''SELECT COUNT(*) as count, COALESCE(SUM(amount), 0) as total
               FROM expenses 
               WHERE DATE(expense_date) = DATE('now') AND user_id = ?''',
            (current_user['id'],)
        )
        expenses_today = dict(cursor.fetchone())
        
        # Solicitudes pendientes
        cursor = conn.execute(
            '''SELECT 
                 COUNT(CASE WHEN status = "pending" THEN 1 END) as pending,
                 COUNT(CASE WHEN status = "in_transit" THEN 1 END) as in_transit,
                 COUNT(CASE WHEN status = "delivered" THEN 1 END) as delivered
               FROM transfer_requests WHERE requester_id = ?''',
            (current_user['id'],)
        )
        transfer_stats = dict(cursor.fetchone())
        
        cursor = conn.execute(
            '''SELECT 
                 COUNT(CASE WHEN status = "pending" THEN 1 END) as pending,
                 COUNT(CASE WHEN status = "approved" THEN 1 END) as approved,
                 COUNT(CASE WHEN status = "rejected" THEN 1 END) as rejected
               FROM discount_requests WHERE seller_id = ?''',
            (current_user['id'],)
        )
        discount_stats = dict(cursor.fetchone())
        
        # Notificaciones de devolución no leídas
        cursor = conn.execute(
            '''SELECT COUNT(*) as count 
            FROM return_notifications rn
            JOIN transfer_requests tr ON rn.transfer_request_id = tr.id
            WHERE tr.requester_id = ? AND rn.read_by_requester = 0''',
            (current_user['id'],)
        )
        unread_returns = cursor.fetchone()['count'] 
    
    conn.close()
    
    return {
        "success": True,
        "dashboard_timestamp": datetime.now().isoformat(),
        "vendor_info": {
            "name": f"{current_user['first_name']} {current_user['last_name']}",
            "email": current_user['email'],
            "role": current_user['role'],
            "location_id": current_user['location_id'],
            "location_name": f"Local #{current_user['location_id']}"
        },
        "today_summary": {
            "date": datetime.now().date().isoformat(),
            "sales": {
                "total_count": sales_today['total_sales'],
                "confirmed_amount": float(sales_today['confirmed_amount']),
                "pending_amount": float(sales_today['pending_amount']),
                "pending_confirmations": sales_today['pending_confirmations'],
                "total_amount": float(sales_today['confirmed_amount']) + float(sales_today['pending_amount'])
            },
            "payment_methods_breakdown": payment_methods,
            "expenses": {
                "count": expenses_today['count'],
                "total_amount": float(expenses_today['total'])
            },
            "net_income": float(sales_today['confirmed_amount']) - float(expenses_today['total'])
        },
        "pending_actions": {
            "sale_confirmations": sales_today['pending_confirmations'],
            "transfer_requests": {
                "pending": transfer_stats['pending'],
                "in_transit": transfer_stats['in_transit'],
                "delivered": transfer_stats['delivered']
            },
            "discount_requests": {
                "pending": discount_stats['pending'],
                "approved": discount_stats['approved'],
                "rejected": discount_stats['rejected']
            },
            "return_notifications": unread_returns
        },
        "quick_actions": [
            "Escanear nuevo tenis",
            "Registrar venta",
            "Registrar gasto",
            "Solicitar transferencia",
            "Ver ventas del día"
        ]
    }

# UBICACIONES
@app.get("/api/v1/locations")
async def get_locations(current_user = Depends(get_current_user)):
    """Obtener todas las ubicaciones disponibles para transferencias"""
    
    if USE_POSTGRESQL:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(DB_PATH)
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        cursor.execute(
            '''SELECT *, 
               CASE 
                 WHEN id = %s THEN 1 
                 ELSE 0 
               END as is_current_location
               FROM locations 
               WHERE is_active = TRUE
               ORDER BY is_current_location DESC, name''',
            (current_user['location_id'],)
        )
        locations = [dict(row) for row in cursor.fetchall()]
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        
        cursor = conn.execute(
            '''SELECT *, 
               CASE 
                 WHEN id = ? THEN 1 
                 ELSE 0 
               END as is_current_location
               FROM locations 
               WHERE is_active = 1
               ORDER BY is_current_location DESC, name''',
            (current_user['location_id'],)
        )
        locations = [dict(row) for row in cursor.fetchall()]
    
    conn.close()
    
    return {
        "success": True,
        "current_location_id": current_user['location_id'],
        "locations": locations,
        "available_for_transfer": [loc for loc in locations if not loc['is_current_location']]
    }

# VENTAS COMPLETAS CON MÉTODOS DE PAGO
@app.post("/api/v1/sales/create")
async def create_sale_complete(
    # Datos del formulario
    items: str,  # JSON string de items
    total_amount: float,
    payment_methods: str,  # JSON string de métodos de pago
    notes: str = "",
    requires_confirmation: bool = False,
    # Archivo de imagen (opcional)
    receipt_image: UploadFile = File(None),
    current_user = Depends(get_current_user)
):
    """Registrar venta completa - CON IMAGEN EN EL MISMO ENDPOINT"""
    
    if current_user['role'] not in ['seller', 'administrador']:
        raise HTTPException(status_code=403, detail="Solo vendedores pueden registrar ventas")
    
    try:
        # Parsear datos JSON
        import json
        items_data = json.loads(items)
        payment_methods_data = json.loads(payment_methods)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Datos JSON inválidos")
    
    # Validar que los métodos de pago sumen el total
    total_payments = sum(payment['amount'] for payment in payment_methods_data)
    if abs(total_payments - total_amount) > 0.01:
        raise HTTPException(
            status_code=400, 
            detail=f"Los métodos de pago (${total_payments:.2f}) no coinciden con el total (${total_amount:.2f})"
        )
    
    # Subir imagen a Cloudinary si existe
    receipt_url = None
    if receipt_image and receipt_image.filename:
        print(f"📸 Subiendo comprobante de venta...")
        receipt_url = await upload_receipt_to_cloudinary(
            receipt_image, 
            "sale", 
            current_user['id']
        )
        print(f"✅ Comprobante subido: {receipt_url}")
    
    # Guardar en base de datos
    if USE_POSTGRESQL:
        import psycopg2
        conn = psycopg2.connect(DB_PATH)
        cursor = conn.cursor()
    else:
        conn = sqlite3.connect(DB_PATH)
    
    try:
        sale_timestamp = datetime.now().isoformat()
        
        if USE_POSTGRESQL:
            cursor.execute(
                '''INSERT INTO sales (seller_id, location_id, total_amount, receipt_image, notes, 
                                    requires_confirmation, confirmed, confirmed_at, sale_date)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id''',
                (current_user['id'], current_user['location_id'], total_amount, 
                 receipt_url, notes, requires_confirmation,
                 not requires_confirmation,
                 None if requires_confirmation else sale_timestamp,
                 sale_timestamp)
            )
            sale_id = cursor.fetchone()[0]
        else:
            cursor = conn.execute(
                '''INSERT INTO sales (seller_id, location_id, total_amount, receipt_image, notes, 
                                    requires_confirmation, confirmed, confirmed_at, sale_date)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (current_user['id'], current_user['location_id'], total_amount, 
                 receipt_url, notes, requires_confirmation,
                 not requires_confirmation,
                 None if requires_confirmation else sale_timestamp,
                 sale_timestamp)
            )
            sale_id = cursor.lastrowid
        
        # Crear los métodos de pago
        for payment in payment_methods_data:
            if USE_POSTGRESQL:
                cursor.execute(
                    '''INSERT INTO sale_payments (sale_id, payment_type, amount, reference)
                       VALUES (%s, %s, %s, %s)''',
                    (sale_id, payment['type'], payment['amount'], payment.get('reference'))
                )
            else:
                conn.execute(
                    '''INSERT INTO sale_payments (sale_id, payment_type, amount, reference)
                       VALUES (?, ?, ?, ?)''',
                    (sale_id, payment['type'], payment['amount'], payment.get('reference'))
                )
        
        # Crear los items de la venta
        for item in items_data:
            subtotal = item['quantity'] * item['unit_price']
            if USE_POSTGRESQL:
                cursor.execute(
                    '''INSERT INTO sale_items (sale_id, sneaker_reference_code, brand, model, color, 
                                             size, quantity, unit_price, subtotal)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)''',
                    (sale_id, item['sneaker_reference_code'], item['brand'], item['model'], 
                     item.get('color'), item['size'], item['quantity'], item['unit_price'], subtotal)
                )
            else:
                conn.execute(
                    '''INSERT INTO sale_items (sale_id, sneaker_reference_code, brand, model, color, 
                                             size, quantity, unit_price, subtotal)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                    (sale_id, item['sneaker_reference_code'], item['brand'], item['model'], 
                     item.get('color'), item['size'], item['quantity'], item['unit_price'], subtotal)
                )
        
        conn.commit()
        
        # Actualizar stock si no requiere confirmación
        if not requires_confirmation:
            try:
                update_stock_after_sale(items_data, current_user['location_id'])
            except Exception as e:
                print(f"⚠️ Error actualizando stock: {e}")
        
        return {
            "success": True,
            "sale_id": sale_id,
            "message": "Venta registrada exitosamente",
            "sale_timestamp": sale_timestamp,
            "total_amount": total_amount,
            "items_count": len(items_data),
            "payment_methods_count": len(payment_methods_data),
            "receipt_info": {
                "has_receipt": bool(receipt_url),
                "receipt_url": receipt_url,
                "stored_in": "Cloudinary CDN" if receipt_url else None
            },
            "status": "pending_confirmation" if requires_confirmation else "confirmed"
        }
        
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Error registrando venta: {str(e)}")
    finally:
        conn.close()

@app.post("/api/v1/sales/confirm")
async def confirm_sale(
    confirmation: SaleConfirmation,
    current_user = Depends(get_current_user)
):
    """Confirmar una venta pendiente - Confirmación de la venta según requerimientos"""
    
    if current_user['role'] not in ['seller', 'administrador']:
        raise HTTPException(status_code=403, detail="Solo selleres pueden confirmar ventas")
    
    if USE_POSTGRESQL:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(DB_PATH)
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        # Verificar que la venta existe y pertenece al seller
        cursor.execute(
            'SELECT * FROM sales WHERE id = %s AND seller_id = %s AND requires_confirmation = TRUE',
            (confirmation.sale_id, current_user['id'])
        )
        sale = cursor.fetchone()
    else:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.execute(
            'SELECT * FROM sales WHERE id = ? AND seller_id = ? AND requires_confirmation = 1',
            (confirmation.sale_id, current_user['id'])
        )
        sale = cursor.fetchone()
    
    if not sale:
        conn.close()
        raise HTTPException(status_code=404, detail="Venta no encontrada o ya confirmada")
    
    # Actualizar confirmación de la venta
    confirmation_timestamp = datetime.now().isoformat()
    
    if USE_POSTGRESQL:
        cursor.execute(
            '''UPDATE sales 
               SET confirmed = %s, confirmed_at = %s, notes = COALESCE(notes, '') || %s 
               WHERE id = %s''',
            (confirmation.confirmed, 
             confirmation_timestamp if confirmation.confirmed else None,
             f"\nConfirmación ({confirmation_timestamp}): {confirmation.confirmation_notes}" if confirmation.confirmation_notes else "",
             confirmation.sale_id)
        )
    else:
        conn.execute(
            '''UPDATE sales 
               SET confirmed = ?, confirmed_at = ?, notes = COALESCE(notes, '') || ? 
               WHERE id = ?''',
            (confirmation.confirmed, 
             confirmation_timestamp if confirmation.confirmed else None,
             f"\nConfirmación ({confirmation_timestamp}): {confirmation.confirmation_notes}" if confirmation.confirmation_notes else "",
             confirmation.sale_id)
        )
    
    conn.commit()
    conn.close()

    if confirmation.confirmed:
        # Obtener items de la venta
        sale_items = get_sale_items(confirmation.sale_id)
        try:
            update_stock_after_sale(sale_items, current_user['location_id'])
        except Exception as e:
            raise HTTPException(status_code=500, detail="Error actualizando stock")
    
    return {
        "success": True,
        "sale_id": confirmation.sale_id,
        "confirmed": confirmation.confirmed,
        "message": "Venta confirmada exitosamente" if confirmation.confirmed else "Venta marcada como no confirmada",
        "confirmation_timestamp": confirmation_timestamp,
        "confirmed_by": f"{current_user['first_name']} {current_user['last_name']}"
    }

@app.get("/api/v1/sales/today")
async def get_today_sales(current_user = Depends(get_current_user)):
    """Visualizar todas las ventas del día según requerimientos"""
    
    if current_user['role'] not in ['seller', 'administrador']:
        raise HTTPException(status_code=403, detail="Acceso denegado")
    
    if USE_POSTGRESQL:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(DB_PATH)
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        # Obtener todas las ventas del día
        cursor.execute(
            '''SELECT s.*, u.first_name, u.last_name, l.name as location_name
               FROM sales s
               JOIN users u ON s.seller_id = u.id
               JOIN locations l ON s.location_id = l.id
               WHERE DATE(s.sale_date) = CURRENT_DATE
               AND s.seller_id = %s
               ORDER BY s.sale_date DESC''',
            (current_user['id'],)
        )
        sales = [dict(row) for row in cursor.fetchall()]
        
        # Para cada venta, obtener items y métodos de pago
        for sale in sales:
            # Items de la venta
            cursor.execute(
                'SELECT * FROM sale_items WHERE sale_id = %s',
                (sale['id'],)
            )
            sale['items'] = [dict(row) for row in cursor.fetchall()]
            
            # Métodos de pago
            cursor.execute(
                'SELECT * FROM sale_payments WHERE sale_id = %s',
                (sale['id'],)
            )
            sale['payment_methods'] = [dict(row) for row in cursor.fetchall()]
            
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        
        # Obtener todas las ventas del día
        cursor = conn.execute(
            '''SELECT s.*, u.first_name, u.last_name, l.name as location_name
               FROM sales s
               JOIN users u ON s.seller_id = u.id
               JOIN locations l ON s.location_id = l.id
               WHERE DATE(s.sale_date) = DATE('now', 'localtime')
               AND s.seller_id = ?
               ORDER BY s.sale_date DESC''',
            (current_user['id'],)
        )
        sales = [dict(row) for row in cursor.fetchall()]
        
        # Para cada venta, obtener items y métodos de pago
        for sale in sales:
            # Items de la venta
            cursor = conn.execute(
                'SELECT * FROM sale_items WHERE sale_id = ?',
                (sale['id'],)
            )
            sale['items'] = [dict(row) for row in cursor.fetchall()]
            
            # Métodos de pago
            cursor = conn.execute(
                'SELECT * FROM sale_payments WHERE sale_id = ?',
                (sale['id'],)
            )
            sale['payment_methods'] = [dict(row) for row in cursor.fetchall()]
    
    # Agregar información de estado para todas las ventas
    for sale in sales:
        sale['status_info'] = {
            "is_confirmed": bool(sale['confirmed']),
            "requires_confirmation": bool(sale['requires_confirmation']),
            "has_receipt": bool(sale['receipt_image']),
            "confirmation_pending": bool(sale['requires_confirmation'] and not sale['confirmed'])
        }
    
    # Calcular estadísticas del día
    total_amount = sum(sale['total_amount'] for sale in sales if sale['confirmed'])
    total_items = sum(len(sale['items']) for sale in sales)
    pending_amount = sum(sale['total_amount'] for sale in sales if sale['requires_confirmation'] and not sale['confirmed'])
    
    # Estadísticas por método de pago
    payment_stats = {}
    for sale in sales:
        if sale['confirmed']:
            for payment in sale['payment_methods']:
                if payment['payment_type'] not in payment_stats:
                    payment_stats[payment['payment_type']] = {"count": 0, "amount": 0}
                payment_stats[payment['payment_type']]["count"] += 1
                payment_stats[payment['payment_type']]["amount"] += payment['amount']
    
    conn.close()
    
    return {
        "success": True,
        "date": datetime.now().date().isoformat(),
        "sales": sales,
        "summary": {
            "total_sales": len(sales),
            "confirmed_sales": len([s for s in sales if s['confirmed']]),
            "pending_confirmation": len([s for s in sales if s['requires_confirmation'] and not s['confirmed']]),
            "total_amount": float(total_amount),
            "pending_amount": float(pending_amount),
            "total_items": total_items,
            "average_sale": round(float(total_amount) / len([s for s in sales if s['confirmed']]), 2) if [s for s in sales if s['confirmed']] else 0,
            "payment_methods_stats": payment_stats
        }
    }

@app.get("/api/v1/sales/pending-confirmation")
async def get_pending_confirmation_sales(current_user = Depends(get_current_user)):
    """Obtener ventas pendientes de confirmación"""
    if current_user['role'] not in ['seller', 'administrador']:
        raise HTTPException(status_code=403, detail="Acceso denegado")
    
    if USE_POSTGRESQL:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(DB_PATH)
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        cursor.execute(
            '''SELECT s.*, u.first_name, u.last_name, l.name as location_name
                FROM sales s
                JOIN users u ON s.seller_id = u.id
                JOIN locations l ON s.location_id = l.id
                WHERE s.seller_id = %s AND s.requires_confirmation = TRUE AND s.confirmed = FALSE
                ORDER BY s.sale_date DESC''',
            (current_user['id'],)
        )
        sales = [dict(row) for row in cursor.fetchall()]
        
        # Para cada venta, obtener items y métodos de pago
        for sale in sales:
            cursor.execute('SELECT * FROM sale_items WHERE sale_id = %s', (sale['id'],))
            sale['items'] = [dict(row) for row in cursor.fetchall()]
            
            cursor.execute('SELECT * FROM sale_payments WHERE sale_id = %s', (sale['id'],))
            sale['payment_methods'] = [dict(row) for row in cursor.fetchall()]
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        
        cursor = conn.execute(
            '''SELECT s.*, u.first_name, u.last_name, l.name as location_name
                FROM sales s
                JOIN users u ON s.seller_id = u.id
                JOIN locations l ON s.location_id = l.id
                WHERE s.seller_id = ? AND s.requires_confirmation = 1 AND s.confirmed = 0
                ORDER BY s.sale_date DESC''',
            (current_user['id'],)
        )
        sales = [dict(row) for row in cursor.fetchall()]
        
        # Para cada venta, obtener items y métodos de pago
        for sale in sales:
            cursor = conn.execute('SELECT * FROM sale_items WHERE sale_id = ?', (sale['id'],))
            sale['items'] = [dict(row) for row in cursor.fetchall()]
            
            cursor = conn.execute('SELECT * FROM sale_payments WHERE sale_id = ?', (sale['id'],))
            sale['payment_methods'] = [dict(row) for row in cursor.fetchall()]
    
    conn.close()
    
    return {
        "success": True,
        "pending_sales": sales,
        "count": len(sales),
        "total_pending_amount": sum(sale['total_amount'] for sale in sales)
    }

# GASTOS
@app.post("/api/v1/expenses/create")
async def create_expense(
    # Datos del formulario
    concept: str,
    amount: float,
    notes: str = "",
    # Archivo de imagen (opcional)
    receipt_image: UploadFile = File(None),
    current_user = Depends(get_current_user)
):
    """Registrar gasto - CON IMAGEN EN EL MISMO ENDPOINT"""
    
    if current_user['role'] not in ['seller', 'administrador']:
        raise HTTPException(status_code=403, detail="Solo vendedores pueden registrar gastos")
    
    if amount <= 0:
        raise HTTPException(status_code=400, detail="El monto debe ser mayor a 0")
    
    # Subir imagen a Cloudinary si existe
    receipt_url = None
    if receipt_image and receipt_image.filename:
        print(f"📸 Subiendo comprobante de gasto...")
        receipt_url = await upload_receipt_to_cloudinary(
            receipt_image, 
            "expense", 
            current_user['id']
        )
        print(f"✅ Comprobante subido: {receipt_url}")
    
    # Guardar en base de datos
    if USE_POSTGRESQL:
        import psycopg2
        conn = psycopg2.connect(DB_PATH)
        cursor = conn.cursor()
    else:
        conn = sqlite3.connect(DB_PATH)
    
    expense_timestamp = datetime.now().isoformat()
    
    try:
        if USE_POSTGRESQL:
            cursor.execute(
                '''INSERT INTO expenses (user_id, location_id, concept, amount, receipt_image, notes, expense_date)
                   VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id''',
                (current_user['id'], current_user['location_id'], concept, 
                 amount, receipt_url, notes, expense_timestamp)
            )
            expense_id = cursor.fetchone()[0]
        else:
            cursor = conn.execute(
                '''INSERT INTO expenses (user_id, location_id, concept, amount, receipt_image, notes, expense_date)
                   VALUES (?, ?, ?, ?, ?, ?, ?)''',
                (current_user['id'], current_user['location_id'], concept, 
                 amount, receipt_url, notes, expense_timestamp)
            )
            expense_id = cursor.lastrowid
        
        conn.commit()
        
        return {
            "success": True,
            "expense_id": expense_id,
            "message": "Gasto registrado exitosamente",
            "expense_timestamp": expense_timestamp,
            "expense_details": {
                "concept": concept,
                "amount": amount,
                "has_receipt": bool(receipt_url),
                "receipt_url": receipt_url,
                "stored_in": "Cloudinary CDN" if receipt_url else None,
                "notes": notes
            },
            "registered_by": f"{current_user['first_name']} {current_user['last_name']}"
        }
        
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Error registrando gasto: {str(e)}")
    finally:
        conn.close()

@app.get("/api/v1/expenses/today")
async def get_today_expenses(current_user = Depends(get_current_user)):
    """Obtener gastos del día actual"""
    
    if current_user['role'] not in ['seller', 'administrador']:
        raise HTTPException(status_code=403, detail="Acceso denegado")
    
    if USE_POSTGRESQL:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(DB_PATH)
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        cursor.execute(
            '''SELECT e.*, u.first_name, u.last_name, l.name as location_name
               FROM expenses e
               JOIN users u ON e.user_id = u.id
               JOIN locations l ON e.location_id = l.id
               WHERE DATE(e.expense_date) = CURRENT_DATE 
               AND e.user_id = %s
               ORDER BY e.expense_date DESC''',
            (current_user['id'],)
        )
        expenses = [dict(row) for row in cursor.fetchall()]
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        
        cursor = conn.execute(
            '''SELECT e.*, u.first_name, u.last_name, l.name as location_name
               FROM expenses e
               JOIN users u ON e.user_id = u.id
               JOIN locations l ON e.location_id = l.id
               WHERE DATE(e.expense_date) = DATE('now', 'localtime') 
               AND e.user_id = ?
               ORDER BY e.expense_date DESC''',
            (current_user['id'],)
        )
        expenses = [dict(row) for row in cursor.fetchall()]
    
    conn.close()
    
    # Categorizar gastos por concepto
    expense_categories = {}
    for expense in expenses:
        concept = expense['concept']
        if concept not in expense_categories:
            expense_categories[concept] = {"count": 0, "total_amount": 0}
        expense_categories[concept]["count"] += 1
        expense_categories[concept]["total_amount"] += expense['amount']
    
    total_amount = sum(expense['amount'] for expense in expenses)
    
    return {
        "success": True,
        "date": datetime.now().date().isoformat(),
        "expenses": expenses,
        "summary": {
            "total_expenses": len(expenses),
            "total_amount": float(total_amount),
            "categories": expense_categories,
            "average_expense": round(float(total_amount) / len(expenses), 2) if expenses else 0
        }
    }

# SOLICITUDES DE TRANSFERENCIA COMPLETAS
@app.post("/api/v1/transfers/request")
async def create_transfer_request_complete(
    transfer_data: TransferRequestComplete,
    current_user = Depends(get_current_user)
):
    """Solicitar tenis de otro local según requerimientos (siguiendo el flujo del escaneo)"""
    
    if current_user['role'] not in ['seller', 'administrador']:
        raise HTTPException(status_code=403, detail="Solo selleres pueden solicitar transferencias")
    
    if USE_POSTGRESQL:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(DB_PATH)
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
    
    request_timestamp = datetime.now().isoformat()
    
    if USE_POSTGRESQL:
        cursor.execute(
            '''INSERT INTO transfer_requests 
               (requester_id, source_location_id, destination_location_id, sneaker_reference_code,
                brand, model, size, quantity, purpose, pickup_type, destination_type, notes, requested_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id''',
            (current_user['id'], transfer_data.source_location_id, current_user['location_id'],
             transfer_data.sneaker_reference_code, transfer_data.brand, transfer_data.model,
             transfer_data.size, transfer_data.quantity, transfer_data.purpose,
             transfer_data.pickup_type, transfer_data.destination_type, transfer_data.notes, request_timestamp)
        )
        request_id = cursor.fetchone()[0]
        
        # Obtener nombre de la ubicación origen
        cursor.execute('SELECT name FROM locations WHERE id = %s', (transfer_data.source_location_id,))
        source_location = cursor.fetchone()
    else:
        cursor = conn.execute(
            '''INSERT INTO transfer_requests 
               (requester_id, source_location_id, destination_location_id, sneaker_reference_code,
                brand, model, size, quantity, purpose, pickup_type, destination_type, notes, requested_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (current_user['id'], transfer_data.source_location_id, current_user['location_id'],
             transfer_data.sneaker_reference_code, transfer_data.brand, transfer_data.model,
             transfer_data.size, transfer_data.quantity, transfer_data.purpose,
             transfer_data.pickup_type, transfer_data.destination_type, transfer_data.notes, request_timestamp)
        )
        request_id = cursor.lastrowid
        
        # Obtener nombre de la ubicación origen
        cursor = conn.execute('SELECT name FROM locations WHERE id = ?', (transfer_data.source_location_id,))
        source_location = cursor.fetchone()
    
    conn.commit()
    conn.close()
    
    return {
        "success": True,
        "transfer_request_id": request_id,
        "message": "Solicitud de transferencia creada exitosamente",
        "request_timestamp": request_timestamp,  # Hora de solicitud según requerimientos
        "transfer_details": {
            "sneaker_info": {
                "reference": transfer_data.sneaker_reference_code,
                "brand": transfer_data.brand,
                "model": transfer_data.model,
                "size": transfer_data.size,
                "quantity": transfer_data.quantity
            },
            "source_location": source_location[0] if source_location else f"Local #{transfer_data.source_location_id}",
            "destination_location": f"Local #{current_user['location_id']}",
            "purpose": "Para exhibición" if transfer_data.purpose == "exhibition" else "Para venta",
            "pickup_arrangement": {
                "type": transfer_data.pickup_type,
                "description": "El mismo seller recogerá" if transfer_data.pickup_type == "seller" else "Un corredor recogerá"
            },
            "destination_storage": "Exhibición" if transfer_data.destination_type == "exhibicion" else "Bodega"
        },
        "status": "pending",
        "next_steps": [
            "Esperando aceptación del bodeguero",
            f"{'seller' if transfer_data.pickup_type == 'seller' else 'Corredor'} será notificado para recolección",
            "Transferencia será registrada al completarse"
        ]
    }

@app.get("/api/v1/transfers/my-requests")
async def get_my_transfer_requests(current_user = Depends(get_current_user)):
    """Obtener mis solicitudes de transferencia"""
    
    if USE_POSTGRESQL:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(DB_PATH)
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        cursor.execute(
            '''SELECT tr.*, 
                      sl.name as source_location_name,
                      dl.name as destination_location_name,
                      c.first_name as courier_first_name,
                      c.last_name as courier_last_name,
                      wk.first_name as warehouse_keeper_first_name,
                      wk.last_name as warehouse_keeper_last_name
               FROM transfer_requests tr
               JOIN locations sl ON tr.source_location_id = sl.id
               JOIN locations dl ON tr.destination_location_id = dl.id
               LEFT JOIN users c ON tr.courier_id = c.id
               LEFT JOIN users wk ON tr.warehouse_keeper_id = wk.id
               WHERE tr.requester_id = %s
               ORDER BY tr.requested_at DESC''',
            (current_user['id'],)
        )
        requests = [dict(row) for row in cursor.fetchall()]
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        
        cursor = conn.execute(
            '''SELECT tr.*, 
                      sl.name as source_location_name,
                      dl.name as destination_location_name,
                      c.first_name as courier_first_name,
                      c.last_name as courier_last_name,
                      wk.first_name as warehouse_keeper_first_name,
                      wk.last_name as warehouse_keeper_last_name
               FROM transfer_requests tr
               JOIN locations sl ON tr.source_location_id = sl.id
               JOIN locations dl ON tr.destination_location_id = dl.id
               LEFT JOIN users c ON tr.courier_id = c.id
               LEFT JOIN users wk ON tr.warehouse_keeper_id = wk.id
               WHERE tr.requester_id = ?
               ORDER BY tr.requested_at DESC''',
            (current_user['id'],)
        )
        requests = [dict(row) for row in cursor.fetchall()]
    
    conn.close()
    
    # Agregar información adicional a cada solicitud
    for request in requests:
        request['status_info'] = {
            "status": request['status'],
            "status_description": {
                "pending": "Esperando aceptación del bodeguero",
                "accepted": "Aceptada, esperando recolección",
                "in_transit": "En camino",
                "delivered": "Entregada",
                "cancelled": "Cancelada"
            }.get(request['status'], "Estado desconocido"),
            "pickup_person": "El mismo seller" if request['pickup_type'] == "seller" else "Corredor",
            "destination": "Exhibición" if request['destination_type'] == "exhibicion" else "Bodega"
        }
    
    return {
        "success": True,
        "transfer_requests": requests,
        "summary": {
            "total_requests": len(requests),
            "pending": len([r for r in requests if r['status'] == 'pending']),
            "accepted": len([r for r in requests if r['status'] == 'accepted']),
            "in_transit": len([r for r in requests if r['status'] == 'in_transit']),
            "delivered": len([r for r in requests if r['status'] == 'delivered']),
            "cancelled": len([r for r in requests if r['status'] == 'cancelled'])
        }
    }

# SOLICITUDES DE DESCUENTO
@app.post("/api/v1/discounts/request")
async def create_discount_request(
    discount_data: DiscountRequestCreate,
    current_user = Depends(get_current_user)
):
    """Gestionar descuento en orden de 5 mil pesos +/- según requerimientos"""
    
    if current_user['role'] not in ['seller', 'administrador']:
        raise HTTPException(status_code=403, detail="Solo selleres pueden solicitar descuentos")
    
    # Validar monto (máximo 5000 según requerimientos)
    if discount_data.amount > 5000:
        raise HTTPException(
            status_code=400, 
            detail="El descuento máximo es de $5,000 pesos. Para descuentos mayores contacte al administrador directamente."
        )
    
    if discount_data.amount <= 0:
        raise HTTPException(
            status_code=400, 
            detail="El monto del descuento debe ser mayor a $0"
        )
    
    if USE_POSTGRESQL:
        import psycopg2
        conn = psycopg2.connect(DB_PATH)
        cursor = conn.cursor()
    else:
        conn = sqlite3.connect(DB_PATH)
    
    request_timestamp = datetime.now().isoformat()
    
    if USE_POSTGRESQL:
        cursor.execute(
            '''INSERT INTO discount_requests (seller_id, amount, reason, requested_at)
               VALUES (%s, %s, %s, %s) RETURNING id''',
            (current_user['id'], discount_data.amount, discount_data.reason, request_timestamp)
        )
        request_id = cursor.fetchone()[0]
    else:
        cursor = conn.execute(
            '''INSERT INTO discount_requests (seller_id, amount, reason, requested_at)
               VALUES (?, ?, ?, ?)''',
            (current_user['id'], discount_data.amount, discount_data.reason, request_timestamp)
        )
        request_id = cursor.lastrowid
    
    conn.commit()
    conn.close()
    
    return {
        "success": True,
        "discount_request_id": request_id,
        "message": "Solicitud de descuento enviada al administrador",
        "request_timestamp": request_timestamp,
        "discount_details": {
            "amount": discount_data.amount,
            "reason": discount_data.reason,
            "max_allowed": 5000,
            "within_limit": discount_data.amount <= 5000
        },
        "status": "pending",
        "next_steps": [
            "El administrador revisará tu solicitud",
            "Recibirás una respuesta (positiva o negativa)",
            "La respuesta será registrada en el sistema"
        ]
    }

@app.get("/api/v1/discounts/my-requests")
async def get_my_discount_requests(current_user = Depends(get_current_user)):
    """Obtener mis solicitudes de descuento"""
    
    if USE_POSTGRESQL:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(DB_PATH)
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        cursor.execute(
            '''SELECT dr.*, 
                      a.first_name as admin_first_name,
                      a.last_name as admin_last_name
               FROM discount_requests dr
               LEFT JOIN users a ON dr.administrator_id = a.id
               WHERE dr.seller_id = %s
               ORDER BY dr.requested_at DESC''',
            (current_user['id'],)
        )
        requests = [dict(row) for row in cursor.fetchall()]
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        
        cursor = conn.execute(
            '''SELECT dr.*, 
                      a.first_name as admin_first_name,
                      a.last_name as admin_last_name
               FROM discount_requests dr
               LEFT JOIN users a ON dr.administrator_id = a.id
               WHERE dr.seller_id = ?
               ORDER BY dr.requested_at DESC''',
            (current_user['id'],)
        )
        requests = [dict(row) for row in cursor.fetchall()]
    
    conn.close()
    
    # Agregar información de estado a cada solicitud
    for request in requests:
        request['status_info'] = {
            "status": request['status'],
            "status_description": {
                "pending": "Esperando revisión del administrador",
                "approved": f"Aprobada por {request.get('admin_first_name', 'Administrador')}",
                "rejected": f"Rechazada por {request.get('admin_first_name', 'Administrador')}"
            }.get(request['status'], "Estado desconocido"),
            "response_available": bool(request['reviewed_at']),
            "admin_responded": bool(request['administrator_id'])
        }
    
    return {
        "success": True,
        "discount_requests": requests,
        "summary": {
            "total_requests": len(requests),
            "pending": len([r for r in requests if r['status'] == 'pending']),
            "approved": len([r for r in requests if r['status'] == 'approved']),
            "rejected": len([r for r in requests if r['status'] == 'rejected']),
            "total_amount_requested": sum(r['amount'] for r in requests),
            "total_amount_approved": sum(r['amount'] for r in requests if r['status'] == 'approved')
        }
    }

# DEVOLUCIONES
@app.post("/api/v1/returns/request")
async def create_return_request(
    return_data: ReturnRequestCreate,
    current_user = Depends(get_current_user)
):
    """Realizar el mismo flujo para la devolución según requerimientos"""
    
    if current_user['role'] not in ['seller', 'administrador']:
        raise HTTPException(status_code=403, detail="Solo selleres pueden solicitar devoluciones")
    
    if USE_POSTGRESQL:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(DB_PATH)
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        # Verificar que la transferencia original existe y fue entregada
        cursor.execute(
            '''SELECT tr.*, sl.name as source_location_name, dl.name as destination_location_name
               FROM transfer_requests tr
               JOIN locations sl ON tr.source_location_id = sl.id
               JOIN locations dl ON tr.destination_location_id = dl.id
               WHERE tr.id = %s AND tr.requester_id = %s AND tr.status = 'delivered' ''',
            (return_data.original_transfer_id, current_user['id'])
        )
        original_transfer = cursor.fetchone()
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        
        # Verificar que la transferencia original existe y fue entregada
        cursor = conn.execute(
            '''SELECT tr.*, sl.name as source_location_name, dl.name as destination_location_name
               FROM transfer_requests tr
               JOIN locations sl ON tr.source_location_id = sl.id
               JOIN locations dl ON tr.destination_location_id = dl.id
               WHERE tr.id = ? AND tr.requester_id = ? AND tr.status = "delivered"''',
            (return_data.original_transfer_id, current_user['id'])
        )
        original_transfer = cursor.fetchone()
    
    if not original_transfer:
        conn.close()
        raise HTTPException(
            status_code=404, 
            detail="Transferencia original no encontrada, no entregada, o no pertenece al usuario actual"
        )
    
    # Crear solicitud de devolución (intercambiando origen y destino)
    return_timestamp = datetime.now().isoformat()
    
    if USE_POSTGRESQL:
        cursor.execute(
            '''INSERT INTO return_requests 
               (original_transfer_id, requester_id, source_location_id, destination_location_id,
                sneaker_reference_code, size, quantity, notes, requested_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id''',
            (return_data.original_transfer_id, current_user['id'], 
             original_transfer['destination_location_id'], original_transfer['source_location_id'],
             original_transfer['sneaker_reference_code'], original_transfer['size'],
             original_transfer['quantity'], return_data.notes, return_timestamp)
        )
        return_id = cursor.fetchone()[0]
    else:
        cursor = conn.execute(
            '''INSERT INTO return_requests 
               (original_transfer_id, requester_id, source_location_id, destination_location_id,
                sneaker_reference_code, size, quantity, notes, requested_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (return_data.original_transfer_id, current_user['id'], 
             original_transfer['destination_location_id'], original_transfer['source_location_id'],
             original_transfer['sneaker_reference_code'], original_transfer['size'],
             original_transfer['quantity'], return_data.notes, return_timestamp)
        )
        return_id = cursor.lastrowid
    
    conn.commit()
    conn.close()
    
    return {
        "success": True,
        "return_request_id": return_id,
        "message": "Solicitud de devolución creada exitosamente",
        "return_timestamp": return_timestamp,
        "return_details": {
            "original_transfer_id": return_data.original_transfer_id,
            "sneaker_info": {
                "reference": original_transfer['sneaker_reference_code'],
                "brand": original_transfer['brand'],
                "model": original_transfer['model'],
                "size": original_transfer['size'],
                "quantity": original_transfer['quantity']
            },
            "return_from": original_transfer['destination_location_name'],
            "return_to": original_transfer['source_location_name'],
            "original_purpose": original_transfer['purpose'],
            "notes": return_data.notes
        },
        "status": "pending",
        "workflow": "Mismo flujo que transferencia original pero en reversa"
    }

# NOTIFICACIONES DE DEVOLUCIÓN
@app.get("/api/v1/notifications/returns")
async def get_return_notifications(current_user = Depends(get_current_user)):
    """Recibir notificación que los tenis fueron devueltos al local solicitado según requerimientos"""
    
    if USE_POSTGRESQL:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(DB_PATH)
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        cursor.execute(
            '''SELECT rn.*, tr.sneaker_reference_code, tr.brand, tr.model, tr.size, tr.quantity,
                      sl.name as source_location_name, dl.name as destination_location_name
               FROM return_notifications rn
               JOIN transfer_requests tr ON rn.transfer_request_id = tr.id
               JOIN locations sl ON tr.source_location_id = sl.id
               JOIN locations dl ON tr.destination_location_id = dl.id
               WHERE tr.requester_id = %s
               ORDER BY rn.created_at DESC''',
            (current_user['id'],)
        )
        notifications = [dict(row) for row in cursor.fetchall()]
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        
        cursor = conn.execute(
            '''SELECT rn.*, tr.sneaker_reference_code, tr.brand, tr.model, tr.size, tr.quantity,
                      sl.name as source_location_name, dl.name as destination_location_name
               FROM return_notifications rn
               JOIN transfer_requests tr ON rn.transfer_request_id = tr.id
               JOIN locations sl ON tr.source_location_id = sl.id
               JOIN locations dl ON tr.destination_location_id = dl.id
               WHERE tr.requester_id = ?
               ORDER BY rn.created_at DESC''',
            (current_user['id'],)
        )
        notifications = [dict(row) for row in cursor.fetchall()]
    
    conn.close()
    
    # Agregar información adicional a cada notificación
    for notification in notifications:
        notification['notification_info'] = {
            "message": f"Los tenis {notification['brand']} {notification['model']} (Talla {notification['size']}) fueron devueltos exitosamente",
            "returned_to": notification['returned_to_location'],
            "return_timestamp": notification['returned_at'],
            "is_read": bool(notification['read_by_requester']),
            "days_ago": (datetime.now() - datetime.fromisoformat(notification['returned_at'])).days
        }
    
    return {
        "success": True,
        "notifications": notifications,
        "summary": {
            "total_notifications": len(notifications),
            "unread_count": len([n for n in notifications if not n['read_by_requester']]),
            "recent_returns": len([n for n in notifications if (datetime.now() - datetime.fromisoformat(n['returned_at'])).days <= 7])
        }
    }

@app.post("/api/v1/notifications/returns/{notification_id}/mark-read")
async def mark_return_notification_read(
    notification_id: int,
    current_user = Depends(get_current_user)
):
    """Marcar notificación de devolución como leída"""
    
    if USE_POSTGRESQL:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Verificar que la notificación pertenece al usuario
        cursor.execute(
            '''SELECT rn.id FROM return_notifications rn
               JOIN transfer_requests tr ON rn.transfer_request_id = tr.id
               WHERE rn.id = %s AND tr.requester_id = %s''',
            (notification_id, current_user['id'])
        )
        
        if not cursor.fetchone():
            conn.close()
            raise HTTPException(status_code=404, detail="Notificación no encontrada")
        
        cursor.execute(
            'UPDATE return_notifications SET read_by_requester = TRUE WHERE id = %s',
            (notification_id,)
        )
    else:
        conn = sqlite3.connect(DB_PATH)
        
        # Verificar que la notificación pertenece al usuario
        cursor = conn.execute(
            '''SELECT rn.id FROM return_notifications rn
               JOIN transfer_requests tr ON rn.transfer_request_id = tr.id
               WHERE rn.id = ? AND tr.requester_id = ?''',
            (notification_id, current_user['id'])
        )
        
        if not cursor.fetchone():
            conn.close()
            raise HTTPException(status_code=404, detail="Notificación no encontrada")
        
        conn.execute(
            'UPDATE return_notifications SET read_by_requester = 1 WHERE id = ?',
            (notification_id,)
        )
    
    conn.commit()
    conn.close()
    
    return {
        "success": True,
        "message": "Notificación marcada como leída",
        "notification_id": notification_id
    }


@app.get("/api/v1/cloudinary/status")
async def cloudinary_status():
    """Verificar estado de Cloudinary"""
    
    config_vars = ["CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET"]
    missing = [var for var in config_vars if not os.getenv(var)]
    
    if missing:
        return {
            "success": False,
            "configured": False,
            "missing_variables": missing,
            "message": "Cloudinary no configurado"
        }
    
    try:
        # Test de conexión
        test_result = cloudinary.api.ping()
        
        return {
            "success": True,
            "configured": True,
            "cloud_name": os.getenv("CLOUDINARY_CLOUD_NAME"),
            "folder": CLOUDINARY_FOLDER,
            "connection": "ok",
            "features": [
                "Upload directo en endpoints de venta/gasto",
                "Optimización automática de imágenes",
                "CDN global",
                "No requiere endpoints separados"
            ]
        }
        
    except Exception as e:
        return {
            "success": False,
            "configured": True,
            "connection": "error",
            "error": str(e)
        }


    # ==================== FUNCIONES AUXILIARES PARA TESTING ====================

@app.post("/api/v1/admin/create-test-data")
async def create_test_data(current_user = Depends(get_current_user)):
    """Crear datos de prueba para testing (solo para desarrollo)"""
    
    if current_user['role'] != 'administrador':
        raise HTTPException(status_code=403, detail="Solo administradores pueden crear datos de prueba")
    
    if USE_POSTGRESQL:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(DB_PATH)
        cursor = conn.cursor()
    else:
        conn = sqlite3.connect(DB_PATH)
    
    try:
        # Crear una venta de prueba
        sale_timestamp = datetime.now().isoformat()
        
        if USE_POSTGRESQL:
            cursor.execute(
                '''INSERT INTO sales (seller_id, location_id, total_amount, notes, sale_date, confirmed)
                   VALUES (%s, %s, %s, %s, %s, %s) RETURNING id''',
                (current_user['id'], current_user['location_id'], 250.0, 
                 "Venta de prueba", sale_timestamp, True)
            )
            sale_id = cursor.fetchone()[0]
        else:
            cursor = conn.execute(
                '''INSERT INTO sales (seller_id, location_id, total_amount, notes, sale_date, confirmed)
                   VALUES (?, ?, ?, ?, ?, ?)''',
                (current_user['id'], current_user['location_id'], 250.0, 
                 "Venta de prueba", sale_timestamp, 1)
            )
            sale_id = cursor.lastrowid
        
        # Items de la venta de prueba
        test_items = [
            ("NK-AM90-WHT-001", "Nike", "Air Max 90", "Blanco/Negro", "9.0", 1, 120.0),
            ("AD-UB22-BLK-001", "Adidas", "Ultraboost 22", "Negro", "9.5", 1, 130.0)
        ]
        
        for item in test_items:
            if USE_POSTGRESQL:
                cursor.execute(
                    '''INSERT INTO sale_items (sale_id, sneaker_reference_code, brand, model, color, 
                                              size, quantity, unit_price, subtotal)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)''',
                    (sale_id, *item, item[6])
                )
            else:
                conn.execute(
                    '''INSERT INTO sale_items (sale_id, sneaker_reference_code, brand, model, color, 
                                              size, quantity, unit_price, subtotal)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                    (sale_id, *item, item[6])
                )
        
        # Métodos de pago de prueba
        test_payments = [
            ("tarjeta", 200.0, "****1234"),
            ("efectivo", 50.0, None)
        ]
        
        for payment in test_payments:
            if USE_POSTGRESQL:
                cursor.execute(
                    '''INSERT INTO sale_payments (sale_id, payment_type, amount, reference)
                       VALUES (%s, %s, %s, %s)''',
                    (sale_id, *payment)
                )
            else:
                conn.execute(
                    '''INSERT INTO sale_payments (sale_id, payment_type, amount, reference)
                       VALUES (?, ?, ?, ?)''',
                    (sale_id, *payment)
                )
        
        # Gasto de prueba
        if USE_POSTGRESQL:
            cursor.execute(
                '''INSERT INTO expenses (user_id, location_id, concept, amount, notes)
                   VALUES (%s, %s, %s, %s, %s)''',
                (current_user['id'], current_user['location_id'], "Almuerzo", 25.0, "Gasto de prueba")
            )
        else:
            conn.execute(
                '''INSERT INTO expenses (user_id, location_id, concept, amount, notes)
                   VALUES (?, ?, ?, ?, ?)''',
                (current_user['id'], current_user['location_id'], "Almuerzo", 25.0, "Gasto de prueba")
            )
        
        conn.commit()
        
        return {
            "success": True,
            "message": "Datos de prueba creados exitosamente",
            "created": {
                "sale_id": sale_id,
                "sale_amount": 250.0,
                "items_count": len(test_items),
                "payments_count": len(test_payments),
                "expense_amount": 25.0
            }
        }
        
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Error creando datos de prueba: {str(e)}")
    finally:
        conn.close()

    # ==================== EJECUTAR APLICACIÓN ====================

# ==================== INICIALIZACIÓN DE BD ====================

def init_database_if_needed():
    """Inicializar base de datos si es necesario"""
    try:
        if USE_POSTGRESQL:
            # PostgreSQL - crear tablas si no existen
            import psycopg2
            import psycopg2.extras
            
            print("🔧 Verificando tablas PostgreSQL...")
            conn = psycopg2.connect(DB_PATH)
            cursor = conn.cursor()
            
            # Verificar si existe la tabla users
            cursor.execute("""
                SELECT EXISTS (
                    SELECT FROM information_schema.tables 
                    WHERE table_name = 'users'
                );
            """)
            table_exists = cursor.fetchone()[0]
            
            if not table_exists:
                print("🔧 Creando tablas PostgreSQL...")
                create_postgresql_tables(conn)
                print("✅ Tablas PostgreSQL creadas")
            else:
                print("✅ Tablas PostgreSQL ya existen")
            
            conn.close()
            
        elif DATABASE_URL.startswith("sqlite"):
            # SQLite - usar el método existente
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='users'")
            if not cursor.fetchone():
                print("🔧 Inicializando base de datos SQLite...")
                conn.close()
                try:
                    from create_sales_tables import create_all_tables
                    create_all_tables()
                    print("✅ Base de datos SQLite inicializada")
                except ImportError:
                    print("⚠️ Script create_sales_tables.py no encontrado")
            else:
                print("✅ Base de datos SQLite ya existe")
                conn.close()
    except Exception as e:
        print(f"⚠️ Error inicializando BD: {e}")

def create_postgresql_tables(conn):
    """Crear todas las tablas para PostgreSQL"""
    cursor = conn.cursor()
    
    # Tabla ubicaciones
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS locations (
            id SERIAL PRIMARY KEY,
            name VARCHAR(255) NOT NULL,
            type VARCHAR(50) NOT NULL,
            address TEXT,
            phone VARCHAR(50),
            is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Tabla usuarios
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            email VARCHAR(255) UNIQUE NOT NULL,
            password_hash VARCHAR(255) NOT NULL,
            first_name VARCHAR(255) NOT NULL,
            last_name VARCHAR(255) NOT NULL,
            role VARCHAR(50) NOT NULL DEFAULT 'seller',
            location_id INTEGER REFERENCES locations(id),
            is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Tabla de ventas
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sales (
            id SERIAL PRIMARY KEY,
            seller_id INTEGER NOT NULL REFERENCES users(id),
            location_id INTEGER NOT NULL REFERENCES locations(id),
            total_amount DECIMAL(10, 2) NOT NULL,
            receipt_image TEXT,
            sale_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            status VARCHAR(50) DEFAULT 'completed',
            notes TEXT,
            requires_confirmation BOOLEAN DEFAULT FALSE,
            confirmed BOOLEAN DEFAULT TRUE,
            confirmed_at TIMESTAMP
        )
    ''')
    
    # Tabla de items de venta
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sale_items (
            id SERIAL PRIMARY KEY,
            sale_id INTEGER NOT NULL REFERENCES sales(id),
            sneaker_reference_code VARCHAR(255) NOT NULL,
            brand VARCHAR(255) NOT NULL,
            model VARCHAR(255) NOT NULL,
            color VARCHAR(255),
            size VARCHAR(50) NOT NULL,
            quantity INTEGER NOT NULL,
            unit_price DECIMAL(10, 2) NOT NULL,
            subtotal DECIMAL(10, 2) NOT NULL
        )
    ''')
    
    # Tabla de métodos de pago
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sale_payments (
            id SERIAL PRIMARY KEY,
            sale_id INTEGER NOT NULL REFERENCES sales(id),
            payment_type VARCHAR(50) NOT NULL,
            amount DECIMAL(10, 2) NOT NULL,
            reference VARCHAR(255),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Tabla de gastos
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS expenses (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            location_id INTEGER NOT NULL REFERENCES locations(id),
            concept VARCHAR(255) NOT NULL,
            amount DECIMAL(10, 2) NOT NULL,
            receipt_image TEXT,
            expense_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            notes TEXT
        )
    ''')
    
    # Tabla de solicitudes de transferencia
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS transfer_requests (
            id SERIAL PRIMARY KEY,
            requester_id INTEGER NOT NULL REFERENCES users(id),
            source_location_id INTEGER NOT NULL REFERENCES locations(id),
            destination_location_id INTEGER NOT NULL REFERENCES locations(id),
            sneaker_reference_code VARCHAR(255) NOT NULL,
            brand VARCHAR(255) NOT NULL,
            model VARCHAR(255) NOT NULL,
            size VARCHAR(50) NOT NULL,
            quantity INTEGER NOT NULL,
            purpose VARCHAR(50) NOT NULL,
            pickup_type VARCHAR(50) NOT NULL,
            destination_type VARCHAR(50) DEFAULT 'bodega',
            courier_id INTEGER REFERENCES users(id),
            warehouse_keeper_id INTEGER REFERENCES users(id),
            status VARCHAR(50) DEFAULT 'pending',
            requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            accepted_at TIMESTAMP,
            picked_up_at TIMESTAMP,
            delivered_at TIMESTAMP,
            notes TEXT
        )
    ''')
    
    # Tabla de solicitudes de descuento
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS discount_requests (
            id SERIAL PRIMARY KEY,
            seller_id INTEGER NOT NULL REFERENCES users(id),
            amount DECIMAL(10, 2) NOT NULL,
            reason TEXT NOT NULL,
            status VARCHAR(50) DEFAULT 'pending',
            administrator_id INTEGER REFERENCES users(id),
            requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            reviewed_at TIMESTAMP,
            admin_comments TEXT
        )
    ''')
    
    # Tabla de devoluciones
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS return_requests (
            id SERIAL PRIMARY KEY,
            original_transfer_id INTEGER NOT NULL REFERENCES transfer_requests(id),
            requester_id INTEGER NOT NULL REFERENCES users(id),
            source_location_id INTEGER NOT NULL REFERENCES locations(id),
            destination_location_id INTEGER NOT NULL REFERENCES locations(id),
            sneaker_reference_code VARCHAR(255) NOT NULL,
            size VARCHAR(50) NOT NULL,
            quantity INTEGER NOT NULL,
            courier_id INTEGER REFERENCES users(id),
            warehouse_keeper_id INTEGER REFERENCES users(id),
            status VARCHAR(50) DEFAULT 'pending',
            requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP,
            notes TEXT
        )
    ''')
    
    # Tabla de notificaciones de devolución
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS return_notifications (
            id SERIAL PRIMARY KEY,
            transfer_request_id INTEGER NOT NULL REFERENCES transfer_requests(id),
            returned_to_location VARCHAR(255) NOT NULL,
            returned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            notes TEXT,
            read_by_requester BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Insertar ubicaciones por defecto
    locations_to_create = [
        ("Local Principal", "local", "Calle Principal 123"),
        ("Local Norte", "local", "Av. Norte 456"),
        ("Local Sur", "local", "Calle Sur 789"),
        ("Bodega Central", "bodega", "Zona Industrial 101"),
        ("Bodega Norte", "bodega", "Zona Industrial Norte 202")
    ]
    
    for location_data in locations_to_create:
        cursor.execute(
            'INSERT INTO locations (name, type, address) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING',
            location_data
        )
        print(f"✅ Ubicación creada: {location_data[0]} ({location_data[1]})")
    
    # Crear usuario admin por defecto
    # Crear usuarios por defecto de diferentes roles
    try:
        from passlib.context import CryptContext
        pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
        
        cursor.execute("SELECT id FROM locations WHERE name = %s", ("Local Principal",))
        location_result = cursor.fetchone()
        if location_result:
            location_id = location_result[0]
            
            # Lista de usuarios a crear
            users_to_create = [
                {
                    "email": "admin@tustockya.com",
                    "password": "admin123",
                    "first_name": "Admin",
                    "last_name": "TuStockYa",
                    "role": "administrador"
                },
                {
                    "email": "seller@tustockya.com",
                    "password": "seller123",
                    "first_name": "Juan",
                    "last_name": "seller",
                    "role": "seller"
                },
                {
                    "email": "seller2@tustockya.com",
                    "password": "seller123",
                    "first_name": "María",
                    "last_name": "González",
                    "role": "seller"
                },
                {
                    "email": "bodeguero@tustockya.com",
                    "password": "bodeguero123",
                    "first_name": "Carlos",
                    "last_name": "Bodeguero",
                    "role": "bodeguero"
                },
                {
                    "email": "corredor@tustockya.com",
                    "password": "corredor123",
                    "first_name": "Luis",
                    "last_name": "Corredor",
                    "role": "corredor"
                }
            ]
            
            for user_data in users_to_create:
                password_hash = pwd_ctx.hash(user_data["password"])
                
                cursor.execute(
                    '''INSERT INTO users (email, password_hash, first_name, last_name, role, location_id)
                       VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (email) DO NOTHING''',
                    (user_data["email"], password_hash, user_data["first_name"], 
                     user_data["last_name"], user_data["role"], location_id)
                )
                print(f"✅ Usuario {user_data['role']}: {user_data['email']} / {user_data['password']}")
                
    except Exception as e:
        print(f"⚠️ Error creando usuarios: {e}")

# ==================== EJECUTAR APLICACIÓN ====================

if __name__ == "__main__":
    import uvicorn
    
    # Inicializar BD si es necesario
    init_database_if_needed()
    
    environment = "Railway" if os.getenv("RAILWAY_ENVIRONMENT") else "Local"
    
    print("🚀 Iniciando TuStockYa Backend")
    print("=" * 60)
    print(f"🌍 Entorno: {environment}")
    print(f"📍 Puerto: {PORT}")
    print(f"💾 Base de datos: {DATABASE_URL[:50]}...")
    print(f"🔄 Redis: {REDIS_URL}")
    print(f"📚 Documentación: http://localhost:{PORT}/docs")
    print(f"📁 Uploads: {upload_dir}")
    print("=" * 60)
    print("🌐 RAILWAY READY - Cambios aplicados!")
    print("=" * 60)
    
    uvicorn.run(
        "main_standalone:app", 
        host="0.0.0.0", 
        port=PORT,  # Usar PORT de variable de entorno
        reload=False  # Desactivar reload en producción
    )