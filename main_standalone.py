# main_standalone.py - Versión completa con todos los requerimientos del seller
import sys
import os
import sqlite3
import tempfile
import random
import asyncio
import httpx
import uuid
from datetime import datetime, timedelta

from fastapi import FastAPI, HTTPException, status, File, UploadFile, Depends , Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from passlib.context import CryptContext
from jose import jwt
import cloudinary
import cloudinary.uploader
import cloudinary.api 
import cloudinary.utils
from cloudinary.exceptions import Error as CloudinaryError
import io
from PIL import Image
import json
from typing import List, Optional
from enum import Enum

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

CLOUDINARY_FOLDER = os.getenv("CLOUDINARY_FOLDER", "tustockya")
ALLOWED_IMAGE_FORMATS = {"image/jpeg", "image/png", "image/webp", "image/jpg"}
MAX_IMAGE_SIZE = 10 * 1024 * 1024  # ✅ AGREGAR ESTA LÍNEA: 10MB



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

class RequestPurpose(str, Enum):
    cliente = "cliente"
    restock = "restock"

class ReservationStatus(str, Enum):
    cliente_presente = "cliente_presente"  # 5 minutos
    restock_exhibicion = "restock_exhibicion"  # 1 minuto
    expired = "expired"

class TransferStatus(str, Enum):
    pending = "pending"
    accepted = "accepted"
    in_transit = "in_transit"
    delivered = "delivered"
    cancelled = "cancelled"

class ProductReservation(BaseModel):
    sneaker_reference_code: str
    size: str
    quantity: int
    purpose: RequestPurpose
    location_id: int
    notes: str = None

class TransferAcceptance(BaseModel):
    transfer_request_id: int
    accepted: bool
    estimated_preparation_time: int = 30  # minutos
    notes: str = None

class ProductDelivery(BaseModel):
    transfer_request_id: int
    delivered: bool
    delivery_notes: str = None
    damaged_items: int = 0

class InventoryMovement(BaseModel):
    sneaker_reference_code: str
    size: str
    quantity: int
    from_location_id: int
    to_location_id: int
    movement_type: str  # 'transfer', 'exhibition', 'return'
    notes: str = None

class CourierNotification(BaseModel):
    type: str  # 'new_request', 'pickup_ready', 'delivery_failed'
    transfer_request_id: int
    message: str
    priority: str = "normal"


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
    Subir comprobante a Cloudinary - VERSIÓN CON FIX PARA FORMDATA
    """
    try:
        print(f"📸 [CLOUDINARY] Iniciando upload...")
        print(f"   Archivo: {file.filename}")
        print(f"   Content-Type: {file.content_type}")
        print(f"   Size hint: {getattr(file, 'size', 'unknown')}")
        print(f"   Usuario ID: {user_id}")
        print(f"   Receipt type: {receipt_type}")
        
        # ✅ FIX 1: Verificar que el archivo tiene contenido
        if not file or not file.filename:
            raise Exception("Archivo vacío o sin nombre")
        
        # ✅ FIX 2: Reset file pointer antes de leer
        await file.seek(0)
        
        # Leer contenido del archivo
        print(f"📖 [CLOUDINARY] Leyendo archivo...")
        content = await file.read()
        file_size = len(content)
        
        print(f"   Tamaño leído: {file_size} bytes ({file_size/1024:.1f} KB)")
        
        if file_size == 0:
            raise Exception("Archivo vacío - 0 bytes leídos")
        
        if file_size > MAX_IMAGE_SIZE:
            raise Exception(f"Archivo muy grande: {file_size} bytes (máximo {MAX_IMAGE_SIZE})")
        
        # ✅ FIX 3: Validar content-type más flexible
        valid_types = ["image/jpeg", "image/jpg", "image/png", "image/webp"]
        if file.content_type not in valid_types:
            print(f"⚠️ [CLOUDINARY] Content-type '{file.content_type}' no está en lista válida")
            print(f"   Tipos válidos: {valid_types}")
            # Continuar pero con advertencia
        
        print(f"✅ [CLOUDINARY] Validaciones básicas pasadas")
        
        # ✅ FIX 4: Verificar que es una imagen real
        print(f"🔍 [CLOUDINARY] Verificando formato de imagen...")
        try:
            # Crear copia del contenido para verificación
            content_copy = io.BytesIO(content)
            img_test = Image.open(content_copy)
            img_format = img_test.format
            img_mode = img_test.mode
            img_size = img_test.size
            
            print(f"   Formato detectado: {img_format}")
            print(f"   Modo: {img_mode}")
            print(f"   Dimensiones: {img_size[0]}x{img_size[1]}")
            
            # Cerrar imagen de test
            img_test.close()
            content_copy.close()
            
        except Exception as e:
            print(f"❌ [CLOUDINARY] No es una imagen válida: {e}")
            raise Exception(f"Archivo no es una imagen válida: {str(e)}")
        
        print(f"✅ [CLOUDINARY] Imagen válida detectada")
        
        # Verificar configuración de Cloudinary
        cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME")
        api_key = os.getenv("CLOUDINARY_API_KEY")
        api_secret = os.getenv("CLOUDINARY_API_SECRET")
        
        if not all([cloud_name, api_key, api_secret]):
            missing = [var for var, val in [("cloud_name", cloud_name), ("api_key", api_key), ("api_secret", api_secret)] if not val]
            raise Exception(f"Configuración incompleta de Cloudinary. Faltan: {missing}")
        
        # ✅ FIX 5: Configurar Cloudinary explícitamente para cada upload
        cloudinary.config(
            cloud_name=cloud_name,
            api_key=api_key,
            api_secret=api_secret,
            secure=True
        )
        print(f"✅ [CLOUDINARY] Configuración aplicada")
        
        # ✅ FIX 6: Optimización más robusta
        print(f"🔄 [CLOUDINARY] Optimizando imagen...")
        try:
            # Usar BytesIO para manejar el contenido
            content_io = io.BytesIO(content)
            img = Image.open(content_io)
            
            original_format = img.format
            print(f"   Formato original: {original_format}")
            print(f"   Tamaño original: {img.width}x{img.height}")
            print(f"   Modo original: {img.mode}")
            
            # Convertir a RGB si es necesario
            if img.mode in ("RGBA", "P", "LA"):
                if img.mode == "RGBA":
                    # Para RGBA, crear fondo blanco
                    background = Image.new("RGB", img.size, (255, 255, 255))
                    background.paste(img, mask=img.split()[-1] if img.mode == "RGBA" else None)
                    img = background
                else:
                    img = img.convert("RGB")
                print(f"   Convertido a RGB")
            
            # Redimensionar si es muy grande
            max_dimension = 1920
            if img.width > max_dimension or img.height > max_dimension:
                ratio = min(max_dimension / img.width, max_dimension / img.height)
                new_width = int(img.width * ratio)
                new_height = int(img.height * ratio)
                img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
                print(f"   Redimensionado a: {img.width}x{img.height}")
            
            # Guardar optimizada
            output = io.BytesIO()
            img.save(output, format='JPEG', quality=85, optimize=True)
            optimized_content = output.getvalue()
            
            print(f"   Tamaño optimizado: {len(optimized_content)} bytes ({len(optimized_content)/1024:.1f} KB)")
            print(f"   Compresión: {((len(content) - len(optimized_content)) / len(content) * 100):.1f}%")
            
            # Limpiar
            img.close()
            content_io.close()
            output.close()
            
        except Exception as e:
            print(f"⚠️ [CLOUDINARY] Error optimizando imagen: {e}")
            print(f"   Usando imagen original...")
            optimized_content = content
        
        # ✅ FIX 7: Generar nombres únicos más robustos
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        unique_id = str(uuid.uuid4())[:8]
        
        # Obtener extensión del archivo original
        original_ext = os.path.splitext(file.filename)[1].lower() if file.filename else ""
        safe_filename = "".join(c for c in file.filename if c.isalnum() or c in "._-")[:20] if file.filename else "upload"
        
        public_id = f"{CLOUDINARY_FOLDER}/receipts/{receipt_type}/{timestamp}_{user_id}_{unique_id}_{safe_filename}"
        
        print(f"🆔 [CLOUDINARY] Public ID: {public_id}")
        
        # Tags para organización
        tags = [
            "tustockya",
            receipt_type,
            f"user_{user_id}",
            f"date_{datetime.now().strftime('%Y-%m-%d')}",
            f"original_{original_ext[1:]}" if original_ext else "unknown_format"
        ]
        if record_id:
            tags.append(f"record_{record_id}")
        
        print(f"🏷️ [CLOUDINARY] Tags: {tags}")
        
        # ✅ FIX 8: Upload con parámetros más específicos
        print(f"☁️ [CLOUDINARY] Iniciando upload a Cloudinary...")
        
        upload_params = {
            "public_id": public_id,
            "tags": tags,
            "folder": f"{CLOUDINARY_FOLDER}/receipts/{receipt_type}",
            "resource_type": "image",
            "format": "jpg",  # Forzar JPG
            "quality": "auto:good",
            "transformation": [
                {"width": 1920, "height": 1920, "crop": "limit"},
                {"quality": "auto:good"}
            ],
            "use_filename": False,  # No usar nombre original
            "unique_filename": True,  # Generar nombre único
            "overwrite": False  # No sobrescribir si existe
        }
        
        print(f"📤 [CLOUDINARY] Parámetros de upload: {upload_params}")
        
        upload_result = cloudinary.uploader.upload(
            optimized_content,
            **upload_params
        )
        
        print(f"✅ [CLOUDINARY] Upload exitoso!")
        print(f"   URL: {upload_result['secure_url']}")
        print(f"   Public ID: {upload_result['public_id']}")
        print(f"   Tamaño final: {upload_result.get('bytes', 0)} bytes")
        print(f"   Formato final: {upload_result.get('format', 'unknown')}")
        print(f"   Dimensiones finales: {upload_result.get('width', 0)}x{upload_result.get('height', 0)}")
        print(f"   Asset ID: {upload_result.get('asset_id', 'unknown')}")
        
        return upload_result["secure_url"]
        
    except CloudinaryError as e:
        print(f"❌ [CLOUDINARY] Error específico de Cloudinary:")
        print(f"   Tipo: {type(e).__name__}")
        print(f"   Mensaje: {str(e)}")
        print(f"   HTTP Code: {getattr(e, 'http_code', 'unknown')}")
        print(f"   Error Code: {getattr(e, 'error_code', 'unknown')}")
        raise Exception(f"Error Cloudinary: {str(e)}")
        
    except Exception as e:
        print(f"❌ [CLOUDINARY] Error general:")
        print(f"   Tipo: {type(e).__name__}")
        print(f"   Mensaje: {str(e)}")
        
        # Stack trace para debugging
        try:
            import traceback
            print(f"   Traceback completo:")
            traceback.print_exc()
        except:
            pass
        
        raise Exception(f"Error procesando imagen: {str(e)}")


def validate_cloudinary_config() -> bool:
    """Verificar que Cloudinary está configurado correctamente - VERSIÓN CORREGIDA"""
    required_vars = ["CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET"]
    missing = [var for var in required_vars if not os.getenv(var)]
    
    if missing:
        print(f"⚠️ Variables de Cloudinary faltantes: {missing}")
        return False
    
    try:
        # Configurar primero
        cloudinary.config(
            cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
            api_key=os.getenv("CLOUDINARY_API_KEY"),
            api_secret=os.getenv("CLOUDINARY_API_SECRET"),
            secure=True
        )
        
        # Test básico de conexión - AHORA SÍ FUNCIONA
        result = cloudinary.api.ping()
        print("✅ Cloudinary conectado correctamente")
        print(f"✅ Status: {result.get('status', 'unknown')}")
        return True
        
    except Exception as e:
        print(f"❌ Error conectando a Cloudinary: {e}")
        return False

def get_sale_items(sale_id: int) -> list:
    """Obtener items de una venta específica (función auxiliar)"""
    if USE_POSTGRESQL:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(DB_PATH)
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        cursor.execute(
            'SELECT * FROM sale_items WHERE sale_id = %s',
            (sale_id,)
        )
        items = [dict(row) for row in cursor.fetchall()]
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        
        cursor = conn.execute(
            'SELECT * FROM sale_items WHERE sale_id = ?',
            (sale_id,)
        )
        items = [dict(row) for row in cursor.fetchall()]
    
    conn.close()
    
    # Convertir al formato esperado por update_stock_after_sale
    formatted_items = []
    for item in items:
        formatted_items.append({
            'sneaker_reference_code': item['sneaker_reference_code'],
            'size': item['size'],
            'quantity': item['quantity']
        })
    
    return formatted_items

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


@app.post("/api/v1/admin/fix-unique-constraint")
async def fix_unique_constraint(current_user = Depends(get_current_user)):
    """Corregir constraint UNIQUE problemática"""
    
    if current_user['role'] != 'administrador':
        raise HTTPException(403, "Solo administradores")
    
    if USE_POSTGRESQL:
        import psycopg2
        conn = psycopg2.connect(DB_PATH)
        cursor = conn.cursor()
        
        try:
            # Eliminar constraint problemática
            cursor.execute('''
                ALTER TABLE products 
                DROP CONSTRAINT IF EXISTS products_reference_code_key
            ''')
            
            # Agregar constraint correcta
            cursor.execute('''
                ALTER TABLE products 
                ADD CONSTRAINT products_unique_per_location 
                UNIQUE (reference_code, location_name)
            ''')
            
            conn.commit()
            
            return {
                "success": True,
                "message": "Constraint UNIQUE corregida",
                "change": "reference_code UNIQUE → (reference_code, location_name) UNIQUE",
                "impact": "Ahora permite mismo producto en diferentes ubicaciones"
            }
            
        except Exception as e:
            conn.rollback()
            raise HTTPException(500, f"Error: {str(e)}")
        finally:
            conn.close()
    else:
        # SQLite requiere recrear tabla
        return {
            "success": False,
            "message": "SQLite requiere recrear tabla - usar OPCIÓN B"
        }

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
    """
    Versión COMPLETA que combina simplicidad original + todas las ubicaciones
    """
    if not classification_result or not classification_result.get('results'):
        print("⚠️ [DEBUG] No classification results to process")
        return []
    
    merged_results = []
    
    try:
        for rank, result in enumerate(classification_result['results'][:3], 1):
            model_name = result.get('model_name', '')
            original_brand = result.get('brand', 'Unknown')
            
            print(f"🔍 [DEBUG] Processing result {rank}: original_brand='{original_brand}', model='{model_name}'")
            
            # Extraer marca si es necesario
            if original_brand == 'Unknown' or not original_brand:
                extracted_brand, _ = extract_brand_from_model(model_name)
                display_brand = extracted_brand
                print(f"🔧 [DEBUG] Extracted brand: '{display_brand}' from model: '{model_name}'")
            else:
                display_brand = original_brand
                print(f"✅ [DEBUG] Using original brand: '{display_brand}'")
            
            # ✅ BUSCAR EN TODAS LAS UBICACIONES
            all_locations = search_products_in_all_locations(model_name, display_brand)
            
            if all_locations:
                print(f"✅ [DEBUG] Found product in {len(all_locations)} location(s)")
                
                # ✅ SEPARAR POR UBICACIÓN DEL USUARIO
                current_location = []
                other_locations = []
                
                for location_data in all_locations:
                    if location_data['location_info']['location_id'] == user_location_id:
                        current_location.append(location_data)
                        print(f"📍 [DEBUG] Added to current location: {location_data['location_info']['location_name']}")
                    else:
                        other_locations.append(location_data)
                        print(f"📍 [DEBUG] Added to other locations: {location_data['location_info']['location_name']}")
                
                # ✅ CALCULAR DISPONIBILIDAD COMPLETA
                current_stock = sum(loc['stock_info']['total_stock'] for loc in current_location)
                other_stock = sum(loc['stock_info']['total_stock'] for loc in other_locations)
                total_stock = current_stock + other_stock
                
                print(f"📊 [DEBUG] Stock summary - Current: {current_stock}, Other: {other_stock}, Total: {total_stock}")
                
                # Tallas disponibles en ubicación actual
                current_sizes = []
                for loc in current_location:
                    for size_info in loc['stock_info']['available_sizes']:
                        if size_info['quantity_stock'] > 0:
                            current_sizes.append(size_info['size'])
                
                # Tallas adicionales en otras ubicaciones
                other_sizes = []
                for loc in other_locations:
                    for size_info in loc['stock_info']['available_sizes']:
                        if size_info['quantity_stock'] > 0 and size_info['size'] not in current_sizes:
                            other_sizes.append(size_info['size'])
                
                # Determinar acción recomendada
                if current_stock > 0:
                    recommended_action = "✅ Disponible para venta inmediata"
                    action_type = "sell_immediately"
                elif other_stock > 0:
                    recommended_action = f"📦 Solicitar transferencia ({len(other_locations)} ubicación{'es' if len(other_locations) > 1 else ''} disponible{'s' if len(other_locations) > 1 else ''})"
                    action_type = "request_transfer"
                else:
                    recommended_action = "❌ Sin stock en el sistema"
                    action_type = "out_of_stock"
                
                print(f"🎯 [DEBUG] Recommended action: {recommended_action}")
                
                # ✅ USAR DATOS DEL PRIMER PRODUCTO ENCONTRADO
                first_product = current_location[0] if current_location else all_locations[0]
                
                merged_result = {
                    "rank": rank,
                    "similarity_score": result.get('similarity_score', 0.0),
                    "confidence_percentage": result.get('confidence_percentage', 0.0),
                    "confidence_level": result.get('confidence_level', 'low'),
                    "reference": {
                        "code": first_product['product_info']['reference_code'],  # ✅ CÓDIGO REAL
                        "brand": display_brand,
                        "model": model_name,
                        "color": first_product['product_info']['color'],
                        "description": first_product['product_info']['description'],
                        "photo": first_product['product_info']['image_url']
                    },
                    "availability": {
                        "summary": {
                            "current_location": {
                                "has_stock": current_stock > 0,
                                "total_stock": current_stock,
                                "available_sizes": list(set(current_sizes)),
                                "locations_count": len(current_location)
                            },
                            "other_locations": {
                                "has_stock": other_stock > 0,
                                "total_stock": other_stock,
                                "additional_sizes": list(set(other_sizes)),
                                "locations_count": len(other_locations),
                                "can_request_transfer": len(other_locations) > 0
                            },
                            "total_system": {
                                "total_stock": total_stock,
                                "total_locations": len(all_locations),
                                "all_available_sizes": list(set(current_sizes + other_sizes))
                            }
                        },
                        "recommended_action": recommended_action,
                        "action_type": action_type,
                        "can_sell_now": current_stock > 0,
                        "can_request_transfer": other_stock > 0
                    },
                    "locations": {
                        "current_location": current_location,
                        "other_locations": other_locations,
                        "total_locations_found": len(all_locations)
                    },
                    "pricing": {
                        "unit_price": first_product['product_info']['unit_price'],
                        "box_price": first_product['product_info']['box_price'],
                        "has_pricing": True
                    },
                    "classification_source": "real_microservice",
                    "inventory_source": "found_in_database",
                    "brand_extraction": {
                        "original_brand": original_brand,
                        "final_brand": display_brand,
                        "extraction_method": "enhanced_analysis" if original_brand == "Unknown" else "microservice_direct"
                    },
                    "search_strategy": "simple_effective_complete",
                    "original_db_id": result.get('original_db_id'),
                    "image_path": result.get('image_path')
                }
                
                merged_results.append(merged_result)
                print(f"✅ [DEBUG] Added result {rank} with inventory data")
                
            else:
                # Sin inventario - crear resultado básico
                print(f"⚠️ [DEBUG] No inventory found for result {rank}, creating classification-only result")
                merged_result = create_basic_classification_result(rank, result, display_brand, model_name, original_brand)
                merged_results.append(merged_result)
                print(f"📝 [DEBUG] Added result {rank} as classification-only")
        
        print(f"✅ [DEBUG] Final merged_results count: {len(merged_results)}")
        return merged_results
        
    except Exception as e:
        print(f"❌ [ERROR] merge_classification_with_inventory: {str(e)}")
        import traceback
        traceback.print_exc()
        return []

def create_basic_classification_result(rank, original_result, brand, model_name, original_brand):
    """Crear resultado básico cuando no hay inventario pero sí clasificación"""
    return {
        "rank": rank,
        "similarity_score": original_result.get('similarity_score', 0.0),
        "confidence_percentage": original_result.get('confidence_percentage', 0.0),
        "confidence_level": original_result.get('confidence_level', 'low'),
        "reference": {
            "code": f"CLASSIFIED-{rank:03d}",
            "brand": brand,
            "model": model_name,
            "color": original_result.get('color', 'Varios'),
            "description": f"{brand} {model_name}",
            "photo": f"https://via.placeholder.com/300x300?text={brand.replace(' ', '+')}+{model_name.replace(' ', '+')}"
        },
        "availability": {
            "summary": {
                "current_location": {"has_stock": False, "total_stock": 0, "available_sizes": [], "locations_count": 0},
                "other_locations": {"has_stock": False, "total_stock": 0, "additional_sizes": [], "locations_count": 0, "can_request_transfer": False},
                "total_system": {"total_stock": 0, "total_locations": 0, "all_available_sizes": []}
            },
            "recommended_action": "🔍 Producto identificado - No disponible en inventario actual",
            "action_type": "classified_not_in_inventory",
            "can_sell_now": False,
            "can_request_transfer": False
        },
        "locations": {
            "current_location": [],
            "other_locations": [],
            "total_locations_found": 0
        },
        "pricing": {
            "unit_price": 0.0,
            "box_price": 0.0,
            "has_pricing": False
        },
        "classification_source": "real_microservice",
        "inventory_source": "not_in_current_inventory",
        "brand_extraction": {
            "original_brand": original_brand,
            "final_brand": brand,
            "extraction_method": "model_analysis" if original_brand == "Unknown" else "microservice_direct"
        },
        "suggestions": {
            "can_add_to_inventory": True,
            "can_search_suppliers": True,
            "similar_products_available": False
        },
        "original_db_id": original_result.get('original_db_id'),
        "image_path": original_result.get('image_path')
    }

def create_basic_classification_result(rank, original_result, brand, model_name, original_brand):
    """Crear resultado básico cuando no hay inventario pero sí clasificación"""
    return {
        "rank": rank,
        "similarity_score": original_result.get('similarity_score', 0.0),
        "confidence_percentage": original_result.get('confidence_percentage', 0.0),
        "confidence_level": original_result.get('confidence_level', 'low'),
        "reference": {
            "code": f"CLASSIFIED-{rank:03d}",
            "brand": brand,
            "model": model_name,
            "color": original_result.get('color', 'Unknown'),
            "description": f"{brand} {model_name}",
            "photo": f"https://via.placeholder.com/300x300?text={brand.replace(' ', '+')}+{model_name.replace(' ', '+')}"
        },
        "availability": {
            "summary": {
                "current_location": {"has_stock": False, "total_stock": 0, "available_sizes": [], "locations_count": 0},
                "other_locations": {"has_stock": False, "total_stock": 0, "additional_sizes": [], "locations_count": 0, "can_request_transfer": False},
                "total_system": {"total_stock": 0, "total_locations": 0, "all_available_sizes": []}
            },
            "recommended_action": "🔍 Producto identificado - No disponible en inventario actual",
            "action_type": "classified_not_in_inventory",
            "can_sell_now": False,
            "can_request_transfer": False
        },
        "locations": {
            "current_location": [],
            "other_locations": [],
            "total_locations_found": 0
        },
        "pricing": {
            "unit_price": 0.0,
            "box_price": 0.0,
            "has_pricing": False
        },
        "classification_source": "real_microservice",
        "inventory_source": "not_in_current_inventory",
        "brand_extraction": {
            "original_brand": original_brand,
            "final_brand": brand,
            "extraction_method": "enhanced_analysis" if original_brand == "Unknown" else "microservice_direct"
        },
        "suggestions": {
            "can_add_to_inventory": True,
            "can_search_suppliers": True,
            "similar_products_available": False
        },
        "original_db_id": original_result.get('original_db_id'),
        "image_path": original_result.get('image_path')
    }

def search_products_in_all_locations(model_name: str, brand: str = None):
    """
    Buscar producto en TODAS las ubicaciones (versión completa)
    """
    try:
        if USE_POSTGRESQL:
            import psycopg2
            import psycopg2.extras
            conn = psycopg2.connect(DB_PATH)
            cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            
            # ✅ BUSCAR EN TODAS LAS UBICACIONES POR SEPARADO
            cursor.execute('''
                SELECT p.reference_code, p.brand, p.model, p.description,
                       p.color_info, p.unit_price, p.box_price, p.image_url,
                       ps.size, ps.quantity, ps.quantity_exhibition,
                       l.id as location_id, l.name as location_name,
                       l.type as location_type, l.address as location_address
                FROM products p
                JOIN product_sizes ps ON p.id = ps.product_id
                JOIN locations l ON p.location_name = l.name
                WHERE (p.description ILIKE %s OR p.brand ILIKE %s OR p.model ILIKE %s)
                AND p.is_active = 1
                AND ps.quantity > 0
                ORDER BY l.id, p.reference_code, ps.quantity DESC
            ''', (f'%{model_name}%', f'%{model_name}%', f'%{model_name}%'))
            
            results = [dict(row) for row in cursor.fetchall()]
            
        else:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            
            cursor = conn.execute('''
                SELECT p.reference_code, p.brand, p.model, p.description,
                       p.color_info, p.unit_price, p.box_price, p.image_url,
                       ps.size, ps.quantity, ps.quantity_exhibition,
                       l.id as location_id, l.name as location_name,
                       l.type as location_type, l.address as location_address
                FROM products p
                JOIN product_sizes ps ON p.id = ps.product_id
                JOIN locations l ON p.location_name = l.name
                WHERE (p.description LIKE ? OR p.brand LIKE ? OR p.model LIKE ?)
                AND p.is_active = 1
                AND ps.quantity > 0
                ORDER BY l.id, p.reference_code, ps.quantity DESC
            ''', (f'%{model_name}%', f'%{model_name}%', f'%{model_name}%'))
            
            results = [dict(row) for row in cursor.fetchall()]
        
        conn.close()
        
        print(f"📍 [DEBUG] Found {len(results)} product-location combinations for '{model_name}'")
        
        if not results:
            return []
        
        # ✅ AGRUPAR POR UBICACIÓN Y PRODUCTO
        locations_map = {}
        
        for result in results:
            print(f"📍 [DEBUG] Processing: {result['reference_code']} at {result['location_name']} - Stock: {result['quantity']}")
            
            key = f"{result['location_id']}_{result['reference_code']}"
            
            if key not in locations_map:
                locations_map[key] = {
                    "location_info": {
                        "location_id": result['location_id'],
                        "location_name": result['location_name'],
                        "location_type": result['location_type'],
                        "location_address": result['location_address'] or "Dirección no disponible"
                    },
                    "product_info": {
                        "reference_code": result['reference_code'],
                        "brand": result['brand'],
                        "model": result['model'],
                        "description": result['description'],
                        "color": result['color_info'] or "Varios",
                        "unit_price": float(result['unit_price']) if result['unit_price'] else 0.0,
                        "box_price": float(result['box_price']) if result['box_price'] else 0.0,
                        "image_url": result['image_url'] or f"https://via.placeholder.com/300x200?text={result['brand']}+{result['model']}"
                    },
                    "stock_info": {
                        "available_sizes": [],
                        "total_stock": 0,
                        "total_exhibition": 0,
                        "size_count": 0
                    },
                    "transfer_info": {
                        "can_request_transfer": True,
                        "estimated_transfer_time": "2-4 horas" if result['location_type'] == 'local' else "4-8 horas"
                    }
                }
            
            # Agregar información de talla
            locations_map[key]["stock_info"]["available_sizes"].append({
                "size": result['size'],
                "quantity_stock": result['quantity'],
                "quantity_exhibition": result['quantity_exhibition'] or 0
            })
            
            locations_map[key]["stock_info"]["total_stock"] += result['quantity']
            locations_map[key]["stock_info"]["total_exhibition"] += (result['quantity_exhibition'] or 0)
            locations_map[key]["stock_info"]["size_count"] += 1
        
        final_results = list(locations_map.values())
        print(f"✅ [DEBUG] Processed into {len(final_results)} location-product combinations")
        
        return final_results
        
    except Exception as e:
        print(f"❌ [ERROR] search_products_in_all_locations: {str(e)}")
        import traceback
        traceback.print_exc()
        return []

def extract_brand_from_model(model_name: str):
    """
    Extracción de marca mejorada para casos específicos
    """
    if not model_name:
        return "Generic", model_name
    
    model_lower = model_name.lower()
    
    # Casos especiales primero
    special_cases = {
        'samba': 'Adidas',  # Samba siempre es Adidas
        'jordan': 'Jordan',  # Jordan es su propia marca
        'air max': 'Nike',
        'air force': 'Nike',
        'superstar': 'Adidas',
        'stan smith': 'Adidas',
        'chuck taylor': 'Converse',
        'old skool': 'Vans'
    }
    
    # Buscar casos especiales
    for keyword, brand in special_cases.items():
        if keyword in model_lower:
            # Limpiar el modelo
            clean_model = model_name
            if model_lower.startswith(keyword):
                clean_model = model_name[len(keyword):].strip()
            return brand, clean_model if clean_model else model_name
    
    # Mapeo de marcas conocidas con sus términos
    brand_keywords = {
        'Nike': ['nike', 'zoom', 'dunk', 'react', 'vapormax', 'pegasus', 'air', 'cushlon'],
        'Adidas': ['adidas', 'ultraboost', 'gazelle', 'nmd', 'yeezy', 'wales bonner', 'benito', 'blanco negro'],
        'Puma': ['puma', 'suede', 'clyde', 'rs-x'],
        'Converse': ['converse', 'all star'],
        'Vans': ['vans', 'authentic', 'sk8'],
        'New Balance': ['new balance', 'nb', '990', '574'],
        'Reebok': ['reebok', 'classic', 'club c'],
        'Fila': ['fila', 'disruptor'],
        'Skechers': ['skechers', 'ultra go', 'gowalk', 'sketcher'],
        'Jordan': ['jordan', 'retro', 'aj', 'bota'],
        'Asics': ['asics', 'gel'],
        'Louis Vuitton': ['louis vuitton', 'luis vuitton', 'lv'],
        'Mizuno': ['mizuno', 'wave']
    }
    
    # Buscar marca en el modelo
    for brand, keywords in brand_keywords.items():
        for keyword in keywords:
            if keyword in model_lower:
                # Limpiar el modelo removiendo la marca
                clean_model = model_name
                if model_lower.startswith(keyword):
                    clean_model = model_name[len(keyword):].strip()
                elif keyword != brand.lower():
                    # Mantener el modelo completo si no es la marca exacta
                    clean_model = model_name
                
                return brand, clean_model if clean_model else model_name
    
    # Si no se encuentra marca conocida, usar la primera palabra
    words = model_name.split()
    if len(words) >= 1:
        potential_brand = words[0].title()
        remaining_model = ' '.join(words[1:]) if len(words) > 1 else model_name
        return potential_brand, remaining_model
    
    return "Generic", model_name

def find_all_locations_with_product(reference_code: str, brand: str = None, model: str = None, size: str = None):
    """
    Búsqueda mejorada que incluye descripción y términos parciales
    """
    
    try:
        if USE_POSTGRESQL:
            import psycopg2
            import psycopg2.extras
            conn = psycopg2.connect(DB_PATH)
            cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            
            # ✅ BÚSQUEDA MEJORADA: Incluir descripción y términos parciales
            base_query = '''
                SELECT p.reference_code, p.brand, p.model, p.description,
                       p.color_info, p.unit_price, p.box_price, p.image_url,
                       ps.size, ps.quantity, ps.quantity_exhibition,
                       l.id as location_id, l.name as location_name,
                       l.type as location_type, l.address as location_address
                FROM products p
                JOIN product_sizes ps ON p.id = ps.product_id
                JOIN locations l ON p.location_name = l.name
                WHERE ps.quantity > 0 AND p.is_active = 1
            '''
            
            params = []
            search_conditions = []
            
            if reference_code:
                # Búsqueda exacta por código
                search_conditions.append("p.reference_code = %s")
                params.append(reference_code)
            elif brand and model:
                # ✅ BÚSQUEDA AMPLIADA: Multiple estrategias
                
                # Estrategia 1: Marca exacta + modelo en descripción
                condition1 = "(p.brand ILIKE %s AND p.description ILIKE %s)"
                params.extend([f'%{brand}%', f'%{model}%'])
                
                # Estrategia 2: Términos del modelo en descripción
                model_terms = model.lower().split()
                model_conditions = []
                for term in model_terms:
                    if len(term) > 2:  # Solo términos significativos
                        model_conditions.append("p.description ILIKE %s")
                        params.append(f'%{term}%')
                
                if model_conditions:
                    condition2 = f"(p.brand ILIKE %s AND ({' AND '.join(model_conditions)}))"
                    params.insert(-len(model_conditions), f'%{brand}%')
                else:
                    condition2 = condition1
                
                # Estrategia 3: Modelo parcial en campo model
                condition3 = "(p.brand ILIKE %s AND p.model ILIKE %s)"
                params.extend([f'%{brand}%', f'%{model.split()[0] if model.split() else model}%'])
                
                search_conditions.append(f"({condition1} OR {condition2} OR {condition3})")
                
            elif brand:
                search_conditions.append("p.brand ILIKE %s")
                params.append(f'%{brand}%')
            
            if size:
                search_conditions.append("ps.size = %s")
                params.append(size)
            
            if search_conditions:
                base_query += " AND (" + " AND ".join(search_conditions) + ")"
            
            base_query += '''
                ORDER BY 
                    CASE 
                        WHEN p.description ILIKE %s THEN 1
                        WHEN p.model ILIKE %s THEN 2
                        ELSE 3
                    END,
                    l.type DESC, ps.quantity DESC, l.name ASC
            '''
            
            # Parámetros para ordenamiento
            search_term = f'%{model}%' if model else '%'
            params.extend([search_term, search_term])
            
            print(f"🔍 [DEBUG] Enhanced Query: {base_query}")
            print(f"🔍 [DEBUG] Params: {params}")
            
            cursor.execute(base_query, params)
            results = [dict(row) for row in cursor.fetchall()]
            
            print(f"✅ [DEBUG] Found {len(results)} results with enhanced search")
            
        else:
            # SQLite - lógica similar
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            
            base_query = '''
                SELECT p.reference_code, p.brand, p.model, p.description,
                       p.color_info, p.unit_price, p.box_price, p.image_url,
                       ps.size, ps.quantity, ps.quantity_exhibition,
                       l.id as location_id, l.name as location_name,
                       l.type as location_type, l.address as location_address
                FROM products p
                JOIN product_sizes ps ON p.id = ps.product_id
                JOIN locations l ON p.location_name = l.name
                WHERE ps.quantity > 0 AND p.is_active = 1
            '''
            
            params = []
            search_conditions = []
            
            if reference_code:
                search_conditions.append("p.reference_code = ?")
                params.append(reference_code)
            elif brand and model:
                # Búsqueda por descripción (más amplia)
                condition1 = "(p.brand LIKE ? AND p.description LIKE ?)"
                params.extend([f'%{brand}%', f'%{model}%'])
                
                # Búsqueda por términos
                model_terms = model.lower().split()
                model_conditions = []
                for term in model_terms:
                    if len(term) > 2:
                        model_conditions.append("p.description LIKE ?")
                        params.append(f'%{term}%')
                
                if model_conditions:
                    condition2 = f"(p.brand LIKE ? AND ({' AND '.join(model_conditions)}))"
                    params.insert(-len(model_conditions), f'%{brand}%')
                else:
                    condition2 = condition1
                
                search_conditions.append(f"({condition1} OR {condition2})")
                
            elif brand:
                search_conditions.append("p.brand LIKE ?")
                params.append(f'%{brand}%')
            
            if size:
                search_conditions.append("ps.size = ?")
                params.append(size)
            
            if search_conditions:
                base_query += " AND (" + " AND ".join(search_conditions) + ")"
            
            base_query += '''
                ORDER BY l.type DESC, ps.quantity DESC, l.name ASC
            '''
            
            cursor = conn.execute(base_query, params)
            results = [dict(row) for row in cursor.fetchall()]
        
        conn.close()
        
        if not results:
            print(f"⚠️ [DEBUG] No results found for brand='{brand}', model='{model}', reference='{reference_code}'")
            return []
        
        # Resto del procesamiento igual...
        locations_with_stock = {}
        
        for result in results:
            print(f"📍 [DEBUG] Found product: {result['reference_code']} - {result['description']} - Stock: {result['quantity']}")
            
            location_key = f"{result['location_id']}_{result['reference_code']}"
            
            if location_key not in locations_with_stock:
                locations_with_stock[location_key] = {
                    "location_info": {
                        "location_id": result['location_id'],
                        "location_name": result['location_name'],
                        "location_type": result['location_type'],
                        "location_address": result['location_address'] or "Dirección no disponible"
                    },
                    "product_info": {
                        "reference_code": result['reference_code'],
                        "brand": result['brand'],
                        "model": result['model'],
                        "description": result['description'],
                        "color": result['color_info'] or "Varios",
                        "unit_price": float(result['unit_price']) if result['unit_price'] else 0.0,
                        "box_price": float(result['box_price']) if result['box_price'] else 0.0,
                        "image_url": result['image_url'] or f"https://via.placeholder.com/300x200?text={result['brand']}+{result['model']}"
                    },
                    "stock_info": {
                        "available_sizes": [],
                        "total_stock": 0,
                        "total_exhibition": 0,
                        "size_count": 0
                    },
                    "transfer_info": {
                        "can_request_transfer": True,
                        "estimated_transfer_time": "2-4 horas" if result['location_type'] == 'local' else "4-8 horas"
                    }
                }
            
            # Agregar información de talla
            locations_with_stock[location_key]["stock_info"]["available_sizes"].append({
                "size": result['size'],
                "quantity_stock": result['quantity'],
                "quantity_exhibition": result['quantity_exhibition'] or 0
            })
            
            locations_with_stock[location_key]["stock_info"]["total_stock"] += result['quantity']
            locations_with_stock[location_key]["stock_info"]["total_exhibition"] += (result['quantity_exhibition'] or 0)
            locations_with_stock[location_key]["stock_info"]["size_count"] += 1
        
        # Convertir a lista y ordenar por cantidad de stock
        final_results = list(locations_with_stock.values())
        final_results.sort(key=lambda x: x['stock_info']['total_stock'], reverse=True)
        
        print(f"✅ [DEBUG] Processed {len(final_results)} unique location-product combinations")
        
        return final_results
        
    except Exception as e:
        print(f"❌ [ERROR] find_all_locations_with_product_improved: {str(e)}")
        print(f"❌ [ERROR] Params were: reference_code='{reference_code}', brand='{brand}', model='{model}', size='{size}'")
        import traceback
        traceback.print_exc()
        return []

def separate_locations_by_user(all_locations, current_user_location_id):
    """
    Separar ubicaciones entre la actual del usuario y otras disponibles
    """
    current_location = []
    other_locations = []
    
    for location_data in all_locations:
        if location_data['location_info']['location_id'] == current_user_location_id:
            current_location.append(location_data)
        else:
            other_locations.append(location_data)
    
    return current_location, other_locations

def calculate_availability_summary(current_location, other_locations):
    """
    Calcular resumen de disponibilidad
    """
    current_stock = sum(loc['stock_info']['total_stock'] for loc in current_location)
    other_stock = sum(loc['stock_info']['total_stock'] for loc in other_locations)
    
    # Tallas disponibles en ubicación actual
    current_sizes = []
    for loc in current_location:
        for size_info in loc['stock_info']['available_sizes']:
            if size_info['quantity_stock'] > 0:
                current_sizes.append(size_info['size'])
    
    # Tallas disponibles en otras ubicaciones
    other_sizes = []
    for loc in other_locations:
        for size_info in loc['stock_info']['available_sizes']:
            if size_info['quantity_stock'] > 0 and size_info['size'] not in current_sizes:
                other_sizes.append(size_info['size'])
    
    return {
        "current_location": {
            "has_stock": current_stock > 0,
            "total_stock": current_stock,
            "available_sizes": list(set(current_sizes)),
            "locations_count": len(current_location)
        },
        "other_locations": {
            "has_stock": other_stock > 0,
            "total_stock": other_stock,
            "additional_sizes": list(set(other_sizes)),
            "locations_count": len(other_locations),
            "can_request_transfer": len(other_locations) > 0
        },
        "total_system": {
            "total_stock": current_stock + other_stock,
            "total_locations": len(current_location) + len(other_locations),
            "all_available_sizes": list(set(current_sizes + other_sizes))
        }
    }

@app.post("/api/v1/classify/scan")
async def scan_sneaker_integrated_enhanced(
    image: UploadFile = File(...),
    include_transfer_options: bool = True,
    current_user = Depends(get_current_user)
):
    """Escanear tenis con información completa de ubicaciones disponibles - VERSIÓN FINAL"""
    
    start_time = datetime.now()
    
    if not image.content_type or not image.content_type.startswith('image/'):
        raise HTTPException(status_code=400, detail="El archivo debe ser una imagen")
    
    content = await image.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Archivo muy grande (máximo 10MB)")
    
    print(f"🔍 Iniciando escaneo completo con información de todas las ubicaciones...")
    
    try:
        # Llamar al microservicio real de clasificación
        classification_result = await call_real_classification_service(content, image.filename)
        
        if classification_result and classification_result.get('success'):
            print(f"✅ Microservicio respondió: {classification_result.get('total_matches_found', 0)} matches")
            
            # ✅ USAR LA FUNCIÓN COMPLETA
            try:
                merged_results = merge_classification_with_inventory(
                    classification_result, 
                    current_user['location_id']
                )
                print(f"✅ Merged results: {len(merged_results)} productos procesados")
                
            except Exception as merge_error:
                print(f"❌ Error en merge_classification_with_inventory: {merge_error}")
                # Fallback a resultados básicos
                merged_results = create_fallback_results(classification_result, current_user)
            
            processing_time = (datetime.now() - start_time).total_seconds() * 1000
            
            # Estadísticas con validación
            total_locations_with_stock = 0
            products_in_current_location = 0
            products_requiring_transfer = 0
            products_classified_only = 0
            
            for result in merged_results:
                try:
                    total_locations_with_stock += result.get('locations', {}).get('total_locations_found', 0)
                    
                    action_type = result.get('availability', {}).get('action_type', '')
                    
                    if result.get('availability', {}).get('can_sell_now', False):
                        products_in_current_location += 1
                    elif result.get('availability', {}).get('can_request_transfer', False):
                        products_requiring_transfer += 1
                    elif action_type == 'classified_not_in_inventory':
                        products_classified_only += 1
                        
                except Exception:
                    continue
            
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
                "results": {
                    "best_match": merged_results[0] if merged_results else None,
                    "alternative_matches": merged_results[1:] if len(merged_results) > 1 else [],
                    "total_matches_found": len(merged_results)
                },
                "availability_summary": {
                    "products_available_locally": products_in_current_location,
                    "products_requiring_transfer": products_requiring_transfer,
                    "products_classified_only": products_classified_only,
                    "total_locations_with_stock": total_locations_with_stock,
                    "can_sell_immediately": products_in_current_location > 0,
                    "transfer_options_available": products_requiring_transfer > 0,
                    "classification_successful": len(merged_results) > 0
                },
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
                    "source": "complete_multi_location",
                    "locations_searched": "all_active",
                    "include_transfer_options": include_transfer_options,
                    "search_strategy": "simple_effective_complete"
                }
            }
        else:
            # Microservicio no disponible
            return await handle_classification_fallback(content, image, current_user, start_time)
            
    except Exception as e:
        print(f"❌ Error general en scan_sneaker_integrated_enhanced: {str(e)}")
        import traceback
        traceback.print_exc()
        
        # Retornar error estructurado
        processing_time = (datetime.now() - start_time).total_seconds() * 1000
        
        return {
            "success": False,
            "error": "Error interno del servidor",
            "error_details": str(e),
            "scan_timestamp": datetime.now().isoformat(),
            "processing_time_ms": round(processing_time, 2),
            "fallback_available": True
        }

def create_fallback_results(classification_result, current_user):
    """Crear resultados básicos cuando falla el procesamiento avanzado"""
    fallback_results = []
    
    for rank, result in enumerate(classification_result.get('results', [])[:3], 1):
        fallback_result = {
            "rank": rank,
            "similarity_score": result.get('similarity_score', 0.0),
            "confidence_percentage": result.get('confidence_percentage', 0.0),
            "confidence_level": result.get('confidence_level', 'low'),
            "reference": {
                "code": f"FALLBACK-{rank:03d}",
                "brand": result.get('brand', 'Unknown'),
                "model": result.get('model_name', 'Unknown'),
                "color": result.get('color', 'Varios'),
                "description": f"{result.get('brand', 'Unknown')} {result.get('model_name', 'Unknown')}",
                "photo": f"https://via.placeholder.com/300x300?text=Producto+{rank}"
            },
            "availability": {
                "summary": {
                    "current_location": {"has_stock": False, "total_stock": 0},
                    "other_locations": {"has_stock": False, "total_stock": 0},
                    "total_system": {"total_stock": 0, "total_locations": 0}
                },
                "recommended_action": "Información de inventario no disponible",
                "action_type": "inventory_error",
                "can_sell_now": False,
                "can_request_transfer": False
            },
            "locations": {
                "current_location": [],
                "other_locations": [],
                "total_locations_found": 0
            }
        }
        fallback_results.append(fallback_result)
    
    return fallback_results

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
    # Datos como Form fields - TODOS los parámetros con Form(...)
    items: str = Form(..., description="JSON string con array de items de la venta"),
    total_amount: float = Form(..., description="Monto total de la venta", gt=0),
    payment_methods: str = Form(..., description="JSON string con métodos de pago"),
    notes: str = Form("", description="Notas adicionales sobre la venta"),
    requires_confirmation: bool = Form(False, description="Si la venta requiere confirmación posterior"),
    # Archivo opcional
    receipt_image: Optional[UploadFile] = File(None, description="Imagen del comprobante de venta"),
    current_user = Depends(get_current_user)
):
    """
    Registrar venta completa con comprobante opcional
    
    **Parámetros:**
    - **items**: JSON string con items de la venta
    - **total_amount**: Monto total de la venta (debe ser > 0)
    - **payment_methods**: JSON string con métodos de pago
    - **notes**: Notas adicionales (opcional)
    - **requires_confirmation**: Si requiere confirmación posterior (default: false)
    - **receipt_image**: Archivo de imagen del comprobante (opcional)
    
    **Ejemplo de items JSON:**
    ```json
    [
        {
            "sneaker_reference_code": "NK-AF1-001",
            "brand": "Nike",
            "model": "Air Force 1",
            "color": "Blanco",
            "size": "9.0",
            "quantity": 1,
            "unit_price": 150.00
        }
    ]
    ```
    
    **Ejemplo de payment_methods JSON:**
    ```json
    [
        {
            "type": "efectivo",
            "amount": 100.00,
            "reference": null
        },
        {
            "type": "tarjeta",
            "amount": 50.00,
            "reference": "****1234"
        }
    ]
    ```
    """
    
    print(f"📥 [SALE] Iniciando registro de venta")
    print(f"   Usuario: {current_user['email']} (ID: {current_user['id']})")
    print(f"   Location: {current_user['location_id']}")
    print(f"   Total: ${total_amount}")
    print(f"   Requiere confirmación: {requires_confirmation}")
    print(f"   Imagen: {'Sí (' + receipt_image.filename + ')' if receipt_image and receipt_image.filename else 'No'}")
    
    # Verificar permisos
    if current_user['role'] not in ['seller', 'administrador']:
        raise HTTPException(status_code=403, detail="Solo vendedores pueden registrar ventas")
    
    # Validar monto total
    if total_amount <= 0:
        raise HTTPException(status_code=400, detail="El monto total debe ser mayor a 0")
    
    try:
        # Parsear datos JSON
        print(f"📦 [JSON] Parseando items: {items[:200]}..." if len(items) > 200 else f"📦 [JSON] Items: {items}")
        items_data = json.loads(items)
        
        print(f"💳 [JSON] Parseando payment methods: {payment_methods[:200]}..." if len(payment_methods) > 200 else f"💳 [JSON] Payment methods: {payment_methods}")
        payment_methods_data = json.loads(payment_methods)
        
        print(f"✅ [JSON] Parseado exitoso:")
        print(f"   Items: {len(items_data)} productos")
        print(f"   Métodos de pago: {len(payment_methods_data)} métodos")
        
    except json.JSONDecodeError as e:
        print(f"❌ [JSON] Error parseando JSON: {e}")
        raise HTTPException(status_code=400, detail=f"Datos JSON inválidos: {str(e)}")
    except Exception as e:
        print(f"❌ [JSON] Error inesperado parseando: {e}")
        raise HTTPException(status_code=400, detail=f"Error procesando datos: {str(e)}")
    
    # Validar estructura de items
    try:
        for i, item in enumerate(items_data):
            required_fields = ['sneaker_reference_code', 'brand', 'model', 'size', 'quantity', 'unit_price']
            for field in required_fields:
                if field not in item:
                    raise HTTPException(status_code=400, detail=f"Item {i+1}: falta campo '{field}'")
            
            if item['quantity'] <= 0:
                raise HTTPException(status_code=400, detail=f"Item {i+1}: quantity debe ser mayor a 0")
            
            if item['unit_price'] <= 0:
                raise HTTPException(status_code=400, detail=f"Item {i+1}: unit_price debe ser mayor a 0")
        
        print(f"✅ [VALIDATION] Items validados correctamente")
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error validando items: {str(e)}")
    
    # Validar estructura de métodos de pago
    try:
        for i, payment in enumerate(payment_methods_data):
            required_fields = ['type', 'amount']
            for field in required_fields:
                if field not in payment:
                    raise HTTPException(status_code=400, detail=f"Método de pago {i+1}: falta campo '{field}'")
            
            if payment['amount'] <= 0:
                raise HTTPException(status_code=400, detail=f"Método de pago {i+1}: amount debe ser mayor a 0")
        
        print(f"✅ [VALIDATION] Métodos de pago validados correctamente")
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error validando métodos de pago: {str(e)}")
    
    # Validar que los métodos de pago sumen el total
    total_payments = sum(float(payment['amount']) for payment in payment_methods_data)
    if abs(total_payments - total_amount) > 0.01:  # Tolerancia de 1 centavo
        print(f"❌ [VALIDATION] Métodos de pago no coinciden:")
        print(f"   Total esperado: ${total_amount}")
        print(f"   Total métodos de pago: ${total_payments}")
        raise HTTPException(
            status_code=400, 
            detail=f"Los métodos de pago (${total_payments:.2f}) no coinciden con el total (${total_amount:.2f})"
        )
    
    print(f"✅ [VALIDATION] Totales coinciden: ${total_amount}")
    
    # Validar stock disponible (opcional, descomenta si quieres validar stock)
    """
    try:
        stock_issues = validate_stock_availability(items_data, current_user['location_id'])
        if stock_issues:
            print(f"❌ [STOCK] Issues encontrados: {stock_issues}")
            raise HTTPException(
                status_code=400, 
                detail=f"Stock insuficiente: {stock_issues}"
            )
        print(f"✅ [STOCK] Stock disponible para todos los items")
    except HTTPException:
        raise
    except Exception as e:
        print(f"⚠️ [STOCK] Error validando stock: {e}")
        # Continuar sin validación de stock si hay error
    """
    
    # Subir imagen a Cloudinary si existe
    receipt_url = None
    if receipt_image and receipt_image.filename:
        try:
            print(f"📸 [CLOUDINARY] Subiendo comprobante de venta...")
            print(f"   Archivo: {receipt_image.filename}")
            print(f"   Tipo: {receipt_image.content_type}")
            
            receipt_url = await upload_receipt_to_cloudinary(
                receipt_image, 
                "sale", 
                current_user['id']
            )
            print(f"✅ [CLOUDINARY] Comprobante subido exitosamente:")
            print(f"   URL: {receipt_url}")
            
        except Exception as e:
            print(f"❌ [CLOUDINARY] Error subiendo imagen: {e}")
            # Continuar sin imagen si falla el upload - la venta no debe fallar por esto
            receipt_url = None
    
    # Conectar a base de datos
    if USE_POSTGRESQL:
        import psycopg2
        conn = psycopg2.connect(DB_PATH)
        cursor = conn.cursor()
        print(f"🔗 [DATABASE] Conectado a PostgreSQL")
    else:
        conn = sqlite3.connect(DB_PATH)
        print(f"🔗 [DATABASE] Conectado a SQLite")
    
    try:
        sale_timestamp = datetime.now().isoformat()
        print(f"🕐 [TIMESTAMP] {sale_timestamp}")
        
        # Crear la venta principal
        if USE_POSTGRESQL:
            cursor.execute(
                '''INSERT INTO sales (seller_id, location_id, total_amount, receipt_image, notes, 
                                    requires_confirmation, confirmed, confirmed_at, sale_date)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id''',
                (current_user['id'], current_user['location_id'], total_amount, 
                 receipt_url, notes, requires_confirmation,
                 not requires_confirmation,  # Si no requiere confirmación, ya está confirmada
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
        
        print(f"✅ [DATABASE] Venta creada con ID: {sale_id}")
        
        # Crear los métodos de pago
        for i, payment in enumerate(payment_methods_data):
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
            
            print(f"✅ [DATABASE] Método de pago {i+1}: {payment['type']} ${payment['amount']}")
        
        # Crear los items de la venta
        total_items_value = 0
        for i, item in enumerate(items_data):
            subtotal = float(item['quantity']) * float(item['unit_price'])
            total_items_value += subtotal
            
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
            
            print(f"✅ [DATABASE] Item {i+1}: {item['brand']} {item['model']} - {item['quantity']}x${item['unit_price']} = ${subtotal}")
        
        print(f"✅ [DATABASE] Total items calculado: ${total_items_value}")
        
        # Commit de la transacción
        conn.commit()
        print(f"✅ [DATABASE] Transacción completada exitosamente")
        
        # Actualizar stock si no requiere confirmación
        if not requires_confirmation:
            try:
                print(f"📦 [STOCK] Actualizando stock...")
                update_stock_after_sale(items_data, current_user['location_id'])
                print(f"✅ [STOCK] Stock actualizado correctamente")
            except Exception as e:
                print(f"⚠️ [STOCK] Error actualizando stock: {e}")
                # No fallar la venta por error de stock
        else:
            print(f"⏳ [STOCK] Actualización de stock pendiente de confirmación")
        
        # Preparar respuesta
        response_data = {
            "success": True,
            "sale_id": sale_id,
            "message": "Venta registrada exitosamente",
            "sale_timestamp": sale_timestamp,
            "sale_details": {
                "total_amount": total_amount,
                "items_count": len(items_data),
                "payment_methods_count": len(payment_methods_data),
                "total_items_value": total_items_value
            },
            "payment_breakdown": [
                {
                    "type": p['type'], 
                    "amount": p['amount'], 
                    "reference": p.get('reference')
                } for p in payment_methods_data
            ],
            "items_summary": [
                {
                    "reference": item['sneaker_reference_code'],
                    "brand": item['brand'],
                    "model": item['model'],
                    "size": item['size'],
                    "quantity": item['quantity'],
                    "unit_price": item['unit_price'],
                    "subtotal": item['quantity'] * item['unit_price']
                } for item in items_data
            ],
            "receipt_info": {
                "has_receipt": bool(receipt_url),
                "receipt_url": receipt_url,
                "stored_in": "Cloudinary CDN" if receipt_url else None
            },
            "status_info": {
                "status": "pending_confirmation" if requires_confirmation else "confirmed",
                "requires_confirmation": requires_confirmation,
                "confirmed": not requires_confirmation,
                "confirmed_at": None if requires_confirmation else sale_timestamp
            },
            "seller_info": {
                "seller_id": current_user['id'],
                "seller_name": f"{current_user['first_name']} {current_user['last_name']}",
                "seller_email": current_user['email'],
                "location_id": current_user['location_id']
            }
        }
        
        print(f"🎉 [SUCCESS] Venta {sale_id} registrada exitosamente")
        print(f"   Total: ${total_amount}")
        print(f"   Items: {len(items_data)}")
        print(f"   Métodos de pago: {len(payment_methods_data)}")
        print(f"   Estado: {'Pendiente confirmación' if requires_confirmation else 'Confirmada'}")
        
        return response_data
        
    except Exception as e:
        # Rollback en caso de error
        conn.rollback()
        print(f"❌ [DATABASE] Error en transacción: {e}")
        print(f"❌ [DATABASE] Rollback ejecutado")
        raise HTTPException(status_code=500, detail=f"Error registrando venta: {str(e)}")
        
    finally:
        # Cerrar conexión
        conn.close()
        print(f"🔐 [DATABASE] Conexión cerrada")

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
async def create_expense_corrected(
    # ✅ IMPORTANTE: Usar Form(...) para cada parámetro
    concept: str = Form(..., description="Concepto del gasto"),
    amount: float = Form(..., description="Monto del gasto", gt=0),
    notes: str = Form("", description="Notas adicionales"),
    # ✅ File(None) para archivos opcionales
    receipt_image: Optional[UploadFile] = File(None, description="Imagen del comprobante"),
    current_user = Depends(get_current_user)
):
    """
    Registrar gasto con comprobante opcional
    
    - **concept**: Concepto del gasto (requerido)
    - **amount**: Monto del gasto (requerido, mayor a 0)
    - **notes**: Notas adicionales (opcional)
    - **receipt_image**: Archivo de imagen del comprobante (opcional)
    """
    
    print(f"📥 [EXPENSE] Datos recibidos:")
    print(f"   Concepto: {concept}")
    print(f"   Monto: {amount}")
    print(f"   Notas: {notes}")
    print(f"   Usuario: {current_user['email']}")
    print(f"   Imagen: {'Sí (' + receipt_image.filename + ')' if receipt_image and receipt_image.filename else 'No'}")
    
    if current_user['role'] not in ['seller', 'administrador']:
        raise HTTPException(status_code=403, detail="Solo vendedores pueden registrar gastos")
    
    if amount <= 0:
        raise HTTPException(status_code=400, detail="El monto debe ser mayor a 0")
    
    # Subir imagen a Cloudinary si existe
    receipt_url = None
    if receipt_image and receipt_image.filename:
        try:
            print(f"📸 [CLOUDINARY] Subiendo comprobante de gasto...")
            receipt_url = await upload_receipt_to_cloudinary(
                receipt_image, 
                "expense", 
                current_user['id']
            )
            print(f"✅ [CLOUDINARY] Comprobante subido: {receipt_url}")
        except Exception as e:
            print(f"❌ [CLOUDINARY] Error subiendo imagen: {e}")
            # Continuar sin imagen si falla el upload
            receipt_url = None
    
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
        print(f"✅ [DATABASE] Gasto registrado: ID {expense_id}")
        
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
        print(f"❌ [DATABASE] Error: {e}")
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
        raise HTTPException(status_code=403, detail="Solo sellers pueden solicitar transferencias")
    
    conn = None # Inicializa conn a None
    cursor = None # Inicializa cursor a None
    request_id = None # Inicializa request_id a None
    source_location = None # ¡Importante! Inicializa source_location aquí

    try:
        if USE_POSTGRESQL:
            # Asegúrate de que psycopg2 y psycopg2.extras estén importados al inicio del archivo
            conn = psycopg2.connect(DB_PATH)
            cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            
            cursor.execute(
                '''INSERT INTO transfer_requests
                (requester_id, source_location_id, destination_location_id, sneaker_reference_code,
                    brand, model, size, quantity, purpose, pickup_type, destination_type, notes, requested_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id''',
                (current_user['id'], transfer_data.source_location_id, current_user['location_id'],
                transfer_data.sneaker_reference_code, transfer_data.brand, transfer_data.model,
                transfer_data.size, transfer_data.quantity, transfer_data.purpose,
                transfer_data.pickup_type, transfer_data.destination_type, transfer_data.notes, datetime.now().isoformat())
            )
            returned_row = cursor.fetchone() 
            print(f"DEBUG: Fila devuelta por INSERT: {returned_row}") 
            
            if returned_row is None:
                raise HTTPException(status_code=500, detail="Error: No se pudo obtener el ID después de la inserción de PostgreSQL.")
            
            # Accede al ID usando la clave 'id' del diccionario
            request_id = returned_row['id']  

            # Obtener nombre de la ubicación origen
            cursor.execute('SELECT name FROM locations WHERE id = %s', (transfer_data.source_location_id,))
            source_location = cursor.fetchone() # Esto también devuelve un RealDictRow si se encuentra
            
        else: # Uso de SQLite
            # Asegúrate de que sqlite3 esté importado al inicio del archivo
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row # Para acceder a las columnas por nombre
            
            cursor = conn.execute(
                '''INSERT INTO transfer_requests 
                (requester_id, source_location_id, destination_location_id, sneaker_reference_code,
                    brand, model, size, quantity, purpose, pickup_type, destination_type, notes, requested_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (current_user['id'], transfer_data.source_location_id, current_user['location_id'],
                transfer_data.sneaker_reference_code, transfer_data.brand, transfer_data.model,
                transfer_data.size, transfer_data.quantity, transfer_data.purpose,
                transfer_data.pickup_type, transfer_data.destination_type, transfer_data.notes, datetime.now().isoformat())
            )
            request_id = cursor.lastrowid
            
            # Obtener nombre de la ubicación origen
            cursor = conn.execute('SELECT name FROM locations WHERE id = ?', (transfer_data.source_location_id,))
            source_location = cursor.fetchone() # Esto devolverá una sqlite3.Row si se encuentra
        
        conn.commit() # Confirma los cambios en la base de datos

    except (psycopg2.Error, sqlite3.Error) as e:
        # Captura errores específicos de base de datos para ambos tipos
        if conn:
            conn.rollback() # Revierte la transacción en caso de error
        raise HTTPException(status_code=500, detail=f"Error de base de datos: {str(e)}")
    except Exception as e:
        # Captura cualquier otro error inesperado
        if conn:
            conn.rollback() # Revierte la transacción
        raise HTTPException(status_code=500, detail=f"Ocurrió un error inesperado en el servicio: {str(e)}")
    finally:
        # Este bloque se ejecuta siempre, asegurando que la conexión se cierre
        if conn:
            conn.close()
    
    # Asegúrate de usar la request_timestamp generada al inicio
    request_timestamp = datetime.now().isoformat()

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
            # Manejo unificado para source_location
            "source_location": (
                source_location['name'] if USE_POSTGRESQL and source_location and 'name' in source_location else
                source_location['name'] if not USE_POSTGRESQL and source_location and 'name' in source_location else
                f"Local #{transfer_data.source_location_id}"
            ),
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


@app.post("/api/v1/test/cloudinary-formdata")
async def test_cloudinary_formdata(
    test_image: UploadFile = File(...),
    current_user = Depends(get_current_user)
):
    """Test específico para FormData → Cloudinary"""
    
    print(f"🧪 [TEST FORMDATA] Iniciando test...")
    print(f"   Archivo: {test_image.filename}")
    print(f"   Content-Type: {test_image.content_type}")
    print(f"   Size: {getattr(test_image, 'size', 'unknown')}")
    
    try:
        # Reset file pointer
        await test_image.seek(0)
        
        # Leer contenido
        content = await test_image.read()
        print(f"   Contenido leído: {len(content)} bytes")
        
        # Reset para la función de upload
        await test_image.seek(0)
        
        # Llamar función de upload corregida
        url = await upload_receipt_to_cloudinary(
            test_image,
            "test-formdata",
            current_user['id']
        )
        
        return {
            "success": True,
            "message": "Test FormData → Cloudinary exitoso",
            "uploaded_url": url,
            "file_info": {
                "filename": test_image.filename,
                "content_type": test_image.content_type,
                "size_bytes": len(content)
            }
        }
        
    except Exception as e:
        print(f"❌ [TEST FORMDATA] Error: {e}")
        return {
            "success": False,
            "error": str(e),
            "type": type(e).__name__,
            "file_info": {
                "filename": test_image.filename,
                "content_type": test_image.content_type
            }
        }

@app.get("/api/v1/cloudinary/status")
async def cloudinary_status():
    """Verificar estado de Cloudinary - VERSIÓN CORREGIDA"""
    
    config_vars = ["CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET"]
    missing = [var for var in config_vars if not os.getenv(var)]
    
    if missing:
        return {
            "success": False,
            "configured": False,
            "missing_variables": missing,
            "message": "Cloudinary no configurado - faltan variables de entorno"
        }
    
    try:
        # Asegurar configuración
        cloudinary.config(
            cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
            api_key=os.getenv("CLOUDINARY_API_KEY"),
            api_secret=os.getenv("CLOUDINARY_API_SECRET"),
            secure=True
        )
        
        # Test de conexión mejorado
        ping_result = cloudinary.api.ping()
        
        # Obtener información adicional si es posible
        try:
            usage_info = cloudinary.api.usage()
        except Exception:
            usage_info = None
        
        return {
            "success": True,
            "configured": True,
            "connection": "ok",
            "cloudinary_status": {
                "cloud_name": os.getenv("CLOUDINARY_CLOUD_NAME"),
                "folder": CLOUDINARY_FOLDER,
                "ping_status": ping_result.get("status", "unknown"),
                "api_version": getattr(cloudinary, '__version__', 'unknown')
            },
            "usage_stats": {
                "credits_used": usage_info.get("credits", {}).get("used", 0) if usage_info else "unavailable",
                "storage_used_mb": round(usage_info.get("storage", {}).get("used", 0) / 1024 / 1024, 2) if usage_info else "unavailable",
                "transformations_used": usage_info.get("transformations", {}).get("used", 0) if usage_info else "unavailable"
            } if usage_info else "unavailable",
            "features": [
                "Upload directo en endpoints de venta/gasto",
                "Optimización automática de imágenes", 
                "CDN global",
                "Transformaciones en tiempo real"
            ]
        }
        
    except ImportError as e:
        return {
            "success": False,
            "configured": True,
            "connection": "import_error",
            "error": f"Error de importación: {str(e)}",
            "solution": "Reinstalar cloudinary: pip install cloudinary"
        }
        
    except Exception as e:
        return {
            "success": False,
            "configured": True,
            "connection": "error",
            "error": str(e),
            "cloudinary_config": {
                "cloud_name": os.getenv("CLOUDINARY_CLOUD_NAME", "not_set"),
                "api_key_set": bool(os.getenv("CLOUDINARY_API_KEY")),
                "api_secret_set": bool(os.getenv("CLOUDINARY_API_SECRET"))
            }
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

def create_product_reservation(sneaker_reference_code: str, size: str, quantity: int, 
                             user_id: int, location_id: int, purpose: RequestPurpose):
    """Crear reserva de producto según requerimientos de concurrencia"""
    
    # Determinar duración de reserva según propósito
    if purpose == RequestPurpose.cliente:
        duration_minutes = 5  # Cliente presente
    else:
        duration_minutes = 1  # Restock
    
    reservation_timestamp = datetime.now()
    expires_at = reservation_timestamp + timedelta(minutes=duration_minutes)
    
    if USE_POSTGRESQL:
        import psycopg2
        conn = psycopg2.connect(DB_PATH)
        cursor = conn.cursor()
        
        cursor.execute(
            '''INSERT INTO product_reservations 
               (sneaker_reference_code, size, quantity, user_id, location_id, purpose, 
                reserved_at, expires_at, status)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id''',
            (sneaker_reference_code, size, quantity, user_id, location_id, 
             purpose.value, reservation_timestamp, expires_at, "active")
        )
        reservation_id = cursor.fetchone()[0]
    else:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.execute(
            '''INSERT INTO product_reservations 
               (sneaker_reference_code, size, quantity, user_id, location_id, purpose, 
                reserved_at, expires_at, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (sneaker_reference_code, size, quantity, user_id, location_id, 
             purpose.value, reservation_timestamp.isoformat(), expires_at.isoformat(), "active")
        )
        reservation_id = cursor.lastrowid
    
    conn.commit()
    conn.close()
    
    # Calcular tiempo desde aceptación para priorizar
    for request in requests:
        if request['accepted_at']:
            accepted_time = datetime.fromisoformat(request['accepted_at'])
            time_since_accepted = datetime.now() - accepted_time
            request['priority_score'] = time_since_accepted.total_seconds() / 3600  # horas
        else:
            request['priority_score'] = 0
        
        request['request_info'] = {
            "pickup_location": request['source_location_name'],
            "pickup_address": request['source_address'],
            "delivery_location": request['destination_location_name'],
            "delivery_address": request['destination_address'],
            "product_description": f"{request['brand']} {request['model']} - Talla {request['size']}",
            "urgency": "Cliente presente" if request['purpose'] == 'cliente' else "Restock"
        }
    
    return {
        "success": True,
        "available_requests": requests,
        "count": len(requests),
        "courier_info": {
            "name": f"{current_user['first_name']} {current_user['last_name']}",
            "courier_id": current_user['id']
        }
    }

@app.post("/api/v1/courier/accept-request/{request_id}")
async def accept_courier_request(
    request_id: int,
    current_user = Depends(get_current_user)
):
    """CO002: Aceptar solicitud e iniciar recorrido"""
    
    if current_user['role'] not in ['corredor', 'administrador']:
        raise HTTPException(status_code=403, detail="Solo corredores pueden aceptar solicitudes")
    
    if USE_POSTGRESQL:
        import psycopg2
        conn = psycopg2.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Verificar que la solicitud está disponible
        cursor.execute(
            'SELECT * FROM transfer_requests WHERE id = %s AND status = %s AND courier_id IS NULL',
            (request_id, 'accepted')
        )
        request = cursor.fetchone()
        
        if not request:
            conn.close()
            raise HTTPException(status_code=404, detail="Solicitud no disponible")
        
        # Asignar corredor
        cursor.execute(
            'UPDATE transfer_requests SET courier_id = %s WHERE id = %s',
            (current_user['id'], request_id)
        )
    else:
        conn = sqlite3.connect(DB_PATH)
        
        cursor = conn.execute(
            'SELECT * FROM transfer_requests WHERE id = ? AND status = ? AND courier_id IS NULL',
            (request_id, 'accepted')
        )
        request = cursor.fetchone()
        
        if not request:
            conn.close()
            raise HTTPException(status_code=404, detail="Solicitud no disponible")
        
        conn.execute(
            'UPDATE transfer_requests SET courier_id = ? WHERE id = ?',
            (current_user['id'], request_id)
        )
    
    conn.commit()
    conn.close()
    
    return {
        "success": True,
        "message": "Solicitud de transporte aceptada",
        "request_id": request_id,
        "next_step": "Dirigirse al punto de recolección",
        "courier_assigned": f"{current_user['first_name']} {current_user['last_name']}"
    }

@app.post("/api/v1/courier/confirm-pickup/{request_id}")
async def confirm_pickup(
    request_id: int,
    current_user = Depends(get_current_user)
):
    """CO003: Confirmar recolección en bodega (registrar hora)"""
    
    if current_user['role'] not in ['corredor', 'administrador']:
        raise HTTPException(status_code=403, detail="Solo corredores pueden confirmar recolección")
    
    timestamp = datetime.now().isoformat()
    
    if USE_POSTGRESQL:
        import psycopg2
        conn = psycopg2.connect(DB_PATH)
        cursor = conn.cursor()
        
        cursor.execute(
            '''UPDATE transfer_requests 
               SET status = 'in_transit', picked_up_at = %s
               WHERE id = %s AND courier_id = %s''',
            (timestamp, request_id, current_user['id'])
        )
        
        if cursor.rowcount == 0:
            conn.close()
            raise HTTPException(status_code=404, detail="Solicitud no encontrada o no autorizada")
    else:
        conn = sqlite3.connect(DB_PATH)
        
        cursor = conn.execute(
            '''UPDATE transfer_requests 
               SET status = "in_transit", picked_up_at = ?
               WHERE id = ? AND courier_id = ?''',
            (timestamp, request_id, current_user['id'])
        )
        
        if cursor.rowcount == 0:
            conn.close()
            raise HTTPException(status_code=404, detail="Solicitud no encontrada o no autorizada")
    
    conn.commit()
    conn.close()
    
    return {
        "success": True,
        "message": "Recolección confirmada - Producto en tránsito",
        "request_id": request_id,
        "picked_up_at": timestamp,
        "status": "in_transit",
        "next_step": "Dirigirse al punto de entrega"
    }

@app.post("/api/v1/courier/confirm-delivery/{request_id}")
async def confirm_delivery(
    request_id: int,
    delivery_successful: bool = True,
    notes: str = "",
    current_user = Depends(get_current_user)
):
    """CO004: Confirmar entrega en local (registrar hora)"""
    
    if current_user['role'] not in ['corredor', 'administrador']:
        raise HTTPException(status_code=403, detail="Solo corredores pueden confirmar entrega")
    
    timestamp = datetime.now().isoformat()
    
    if USE_POSTGRESQL:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(DB_PATH)
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        # Obtener información de la solicitud
        cursor.execute(
            'SELECT * FROM transfer_requests WHERE id = %s AND courier_id = %s',
            (request_id, current_user['id'])
        )
        request = cursor.fetchone()
        
        if not request:
            conn.close()
            raise HTTPException(status_code=404, detail="Solicitud no encontrada")
        
        if delivery_successful:
            cursor.execute(
                '''UPDATE transfer_requests 
                   SET status = 'delivered', delivered_at = %s, notes = %s
                   WHERE id = %s''',
                (timestamp, notes, request_id)
            )
        else:
            cursor.execute(
                '''UPDATE transfer_requests 
                   SET status = 'delivery_failed', notes = %s
                   WHERE id = %s''',
                (f"Entrega fallida: {notes}", request_id)
            )
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        
        cursor = conn.execute(
            'SELECT * FROM transfer_requests WHERE id = ? AND courier_id = ?',
            (request_id, current_user['id'])
        )
        request = cursor.fetchone()
        
        if not request:
            conn.close()
            raise HTTPException(status_code=404, detail="Solicitud no encontrada")
        
        if delivery_successful:
            conn.execute(
                '''UPDATE transfer_requests 
                   SET status = "delivered", delivered_at = ?, notes = ?
                   WHERE id = ?''',
                (timestamp, notes, request_id)
            )
        else:
            conn.execute(
                '''UPDATE transfer_requests 
                   SET status = "delivery_failed", notes = ?
                   WHERE id = ?''',
                (f"Entrega fallida: {notes}", request_id)
            )
    
    conn.commit()
    conn.close()
    
    if delivery_successful:
        return {
            "success": True,
            "message": "Entrega confirmada exitosamente",
            "request_id": request_id,
            "delivered_at": timestamp,
            "status": "delivered",
            "next_step": "Vendedor debe confirmar recepción"
        }
    else:
        return {
            "success": True,
            "message": "Entrega marcada como fallida",
            "request_id": request_id,
            "status": "delivery_failed",
            "notes": notes,
            "next_step": "Se activará proceso de reversión"
        }

@app.post("/api/v1/courier/report-incident")
async def report_transport_incident(
    request_id: int,
    incident_type: str,
    description: str,
    current_user = Depends(get_current_user)
):
    """CO005: Reportar incidencias durante el transporte"""
    
    if current_user['role'] not in ['corredor', 'administrador']:
        raise HTTPException(status_code=403, detail="Solo corredores pueden reportar incidencias")
    
    if USE_POSTGRESQL:
        import psycopg2
        conn = psycopg2.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Crear registro de incidencia
        cursor.execute(
            '''INSERT INTO transport_incidents 
               (transfer_request_id, courier_id, incident_type, description, reported_at)
               VALUES (%s, %s, %s, %s, %s) RETURNING id''',
            (request_id, current_user['id'], incident_type, description, datetime.now())
        )
        incident_id = cursor.fetchone()[0]
    else:
        conn = sqlite3.connect(DB_PATH)
        
        cursor = conn.execute(
            '''INSERT INTO transport_incidents 
               (transfer_request_id, courier_id, incident_type, description, reported_at)
               VALUES (?, ?, ?, ?, ?)''',
            (request_id, current_user['id'], incident_type, description, datetime.now().isoformat())
        )
        incident_id = cursor.lastrowid
    
    conn.commit()
    conn.close()
    
    return {
        "success": True,
        "message": "Incidencia reportada exitosamente",
        "incident_id": incident_id,
        "request_id": request_id,
        "incident_type": incident_type,
        "reported_by": f"{current_user['first_name']} {current_user['last_name']}"
    }

@app.get("/api/v1/courier/my-deliveries")
async def get_courier_delivery_history(current_user = Depends(get_current_user)):
    """CO006: Consultar historial de entregas realizadas"""
    
    if current_user['role'] not in ['corredor', 'administrador']:
        raise HTTPException(status_code=403, detail="Solo corredores pueden ver historial")
    
    if USE_POSTGRESQL:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(DB_PATH)
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        cursor.execute('''
            SELECT tr.*, 
                   sl.name as source_location_name,
                   dl.name as destination_location_name,
                   u.first_name as requester_first_name,
                   u.last_name as requester_last_name
            FROM transfer_requests tr
            JOIN locations sl ON tr.source_location_id = sl.id
            JOIN locations dl ON tr.destination_location_id = dl.id
            JOIN users u ON tr.requester_id = u.id
            WHERE tr.courier_id = %s
            ORDER BY tr.delivered_at DESC NULLS LAST, tr.picked_up_at DESC
        ''', (current_user['id'],))
        deliveries = [dict(row) for row in cursor.fetchall()]
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        
        cursor = conn.execute('''
            SELECT tr.*, 
                   sl.name as source_location_name,
                   dl.name as destination_location_name,
                   u.first_name as requester_first_name,
                   u.last_name as requester_last_name
            FROM transfer_requests tr
            JOIN locations sl ON tr.source_location_id = sl.id
            JOIN locations dl ON tr.destination_location_id = dl.id
            JOIN users u ON tr.requester_id = u.id
            WHERE tr.courier_id = ?
            ORDER BY tr.delivered_at DESC, tr.picked_up_at DESC
        ''', (current_user['id'],))
        deliveries = [dict(row) for row in cursor.fetchall()]
    
    conn.close()
    
    # Calcular estadísticas
    total_deliveries = len([d for d in deliveries if d['status'] == 'delivered'])
    failed_deliveries = len([d for d in deliveries if d['status'] == 'delivery_failed'])
    in_transit = len([d for d in deliveries if d['status'] == 'in_transit'])
    
    return {
        "success": True,
        "delivery_history": deliveries,
        "statistics": {
            "total_assigned": len(deliveries),
            "completed_deliveries": total_deliveries,
            "failed_deliveries": failed_deliveries,
            "currently_in_transit": in_transit,
            "success_rate": round((total_deliveries / len(deliveries) * 100), 2) if deliveries else 0
        },
        "courier_info": {
            "name": f"{current_user['first_name']} {current_user['last_name']}",
            "courier_id": current_user['id']
        }
    }

# ==================== ENDPOINTS ADICIONALES PARA VENDEDOR ====================

# ==================== FIX PARA KeyError EN fetchone ====================

@app.post("/api/v1/vendor/confirm-reception/{request_id}")
async def confirm_product_reception_fetchone_fixed(
    request_id: int,
    received_quantity: int = 1,
    condition_ok: bool = True,
    notes: str = "",
    current_user = Depends(get_current_user)
):
    """VE008: Confirmar recepción - FIX fetchone KeyError"""
    
    if current_user['role'] not in ['seller', 'administrador']:
        raise HTTPException(status_code=403, detail="Solo vendedores pueden confirmar recepción")
    
    if USE_POSTGRESQL:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(DB_PATH)
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        # Verificar solicitud
        cursor.execute(
            '''SELECT * FROM transfer_requests 
               WHERE id = %s AND requester_id = %s AND status = 'delivered' ''',
            (request_id, current_user['id'])
        )
        request = cursor.fetchone()
        
        # Obtener información de la ubicación del vendedor
        cursor.execute(
            'SELECT name FROM locations WHERE id = %s',
            (current_user['location_id'],)
        )
        location_info = cursor.fetchone()
        vendor_location_name = location_info['name'] if location_info else f"Local #{current_user['location_id']}"
        
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        
        cursor = conn.execute(
            '''SELECT * FROM transfer_requests 
               WHERE id = ? AND requester_id = ? AND status = "delivered" ''',
            (request_id, current_user['id'])
        )
        request = cursor.fetchone()
        
        # Obtener información de la ubicación del vendedor
        cursor = conn.execute(
            'SELECT name FROM locations WHERE id = ?',
            (current_user['location_id'],)
        )
        location_info = cursor.fetchone()
        vendor_location_name = location_info['name'] if location_info else f"Local #{current_user['location_id']}"
    
    if not request:
        conn.close()
        raise HTTPException(status_code=404, detail="Solicitud no encontrada o no entregada")
    
    timestamp = datetime.now().isoformat()
    
    try:
        if condition_ok and received_quantity > 0:
            
            if USE_POSTGRESQL:
                # PASO 1: Buscar producto existente en el local del vendedor
                cursor.execute('''
                    SELECT p.id, p.reference_code, p.location_name
                    FROM products p 
                    WHERE p.reference_code = %s 
                    AND p.location_name = %s
                ''', (request['sneaker_reference_code'], vendor_location_name))
                
                existing_product = cursor.fetchone()
                
                if existing_product:
                    # CASO 1: Producto YA EXISTE en el local - Actualizar stock
                    
                    cursor.execute('''
                        SELECT id, quantity FROM product_sizes 
                        WHERE product_id = %s AND size = %s
                    ''', (existing_product['id'], request['size']))
                    
                    existing_size = cursor.fetchone()
                    
                    if existing_size:
                        # Actualizar stock existente de esa talla
                        cursor.execute('''
                            UPDATE product_sizes 
                            SET quantity = quantity + %s
                            WHERE id = %s
                        ''', (received_quantity, existing_size['id']))
                        action_taken = "updated_existing_stock"
                        
                    else:
                        # Crear nueva talla para producto existente
                        cursor.execute('''
                            INSERT INTO product_sizes (
                                product_id, size, quantity, quantity_exhibition, location_name, created_at, updated_at
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ''', (
                            existing_product['id'], 
                            request['size'], 
                            received_quantity, 
                            0,
                            vendor_location_name,
                            timestamp,  # ✅ FIX: created_at
                            timestamp   # ✅ FIX: updated_at
                        ))
                        action_taken = "added_new_size_to_existing_product"
                
                else:
                    # CASO 2: Producto NO EXISTE en este local - Crear producto completo
                    
                    # Buscar información del producto en otros locales
                    cursor.execute('''
                        SELECT reference_code, brand, model, description, color_info, 
                               unit_price, box_price, video_url, image_url
                        FROM products 
                        WHERE reference_code = %s 
                        LIMIT 1
                    ''', (request['sneaker_reference_code'],))
                    
                    source_product = cursor.fetchone()
                    
                    if source_product:
                        # ✅ FIX: Manejo seguro del INSERT con RETURNING
                        try:
                            cursor.execute('''
                                INSERT INTO products (
                                    reference_code, brand, model, description, color_info, 
                                    location_name, unit_price, box_price, is_active,
                                    video_url, image_url, created_at, updated_at
                                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                                RETURNING id
                            ''', (
                                source_product['reference_code'],
                                source_product['brand'], 
                                source_product['model'],
                                source_product['description'] or f"{source_product['brand']} {source_product['model']}",
                                source_product['color_info'] or "Varios",
                                vendor_location_name,
                                float(source_product['unit_price']) if source_product['unit_price'] else 0.0,
                                float(source_product['box_price']) if source_product['box_price'] else 0.0,
                                1,
                                source_product['video_url'],
                                source_product['image_url'],
                                timestamp,
                                timestamp
                            ))
                            
                            # ✅ FIX: Manejo seguro del resultado
                            result = cursor.fetchone()
                            if result and 'id' in result:
                                new_product_id = result['id']
                            elif result:
                                # Si result es una tupla o lista
                                new_product_id = result[0] if hasattr(result, '__getitem__') else None
                            else:
                                raise Exception("No se pudo obtener ID del producto creado")
                            
                            if not new_product_id:
                                raise Exception("ID del producto nuevo es None")
                            
                        except Exception as insert_error:
                            raise Exception(f"Error en INSERT de producto: {str(insert_error)}")
                        
                        # Crear stock para el nuevo producto
                        try:
                            cursor.execute('''
                                INSERT INTO product_sizes (
                                    product_id, size, quantity, quantity_exhibition, location_name, created_at, updated_at
                                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                            ''', (
                                new_product_id, 
                                request['size'], 
                                received_quantity, 
                                0,
                                vendor_location_name,
                                timestamp,  # ✅ FIX: created_at
                                timestamp   # ✅ FIX: updated_at
                            ))
                            action_taken = "created_new_product_and_stock"
                            
                        except Exception as size_error:
                            raise Exception(f"Error en INSERT de product_sizes: {str(size_error)}")
                    
                    else:
                        # Producto no existe en ningún lugar - crear desde datos de transferencia
                        product_description = f"{request['brand']} {request['model']}"
                        
                        try:
                            cursor.execute('''
                                INSERT INTO products (
                                    reference_code, brand, model, description, color_info,
                                    location_name, unit_price, box_price, is_active, created_at, updated_at
                                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                                RETURNING id
                            ''', (
                                request['sneaker_reference_code'],
                                request['brand'],
                                request['model'],
                                product_description,
                                "Color estándar",
                                vendor_location_name,
                                180.0,
                                162.0,
                                1,
                                timestamp,
                                timestamp
                            ))
                            
                            # ✅ FIX: Manejo seguro del resultado
                            result = cursor.fetchone()
                            if result and 'id' in result:
                                new_product_id = result['id']
                            elif result:
                                new_product_id = result[0] if hasattr(result, '__getitem__') else None
                            else:
                                raise Exception("No se pudo obtener ID del producto creado desde transfer")
                                
                        except Exception as transfer_insert_error:
                            raise Exception(f"Error en INSERT desde transfer: {str(transfer_insert_error)}")
                        
                        # Crear stock
                        cursor.execute('''
                            INSERT INTO product_sizes (
                                product_id, size, quantity, quantity_exhibition, location_name, created_at, updated_at
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ''', (
                            new_product_id, 
                            request['size'], 
                            received_quantity, 
                            0,
                            vendor_location_name,
                            timestamp,  # ✅ FIX: created_at
                            timestamp   # ✅ FIX: updated_at
                        ))
                        action_taken = "created_product_from_transfer_data"
                
            else:
                # LÓGICA SIMILAR PARA SQLite (más simple, no usa RETURNING)
                cursor = conn.execute('''
                    SELECT p.id, p.reference_code, p.location_name
                    FROM products p 
                    WHERE p.reference_code = ? 
                    AND p.location_name = ?
                ''', (request['sneaker_reference_code'], vendor_location_name))
                
                existing_product = cursor.fetchone()
                
                if existing_product:
                    # Actualizar stock existente
                    cursor = conn.execute('''
                        SELECT id, quantity FROM product_sizes 
                        WHERE product_id = ? AND size = ?
                    ''', (existing_product['id'], request['size']))
                    
                    existing_size = cursor.fetchone()
                    
                    if existing_size:
                        conn.execute('''
                            UPDATE product_sizes 
                            SET quantity = quantity + ?
                            WHERE id = ?
                        ''', (received_quantity, existing_size['id']))
                        action_taken = "updated_existing_stock"
                    else:
                        conn.execute('''
                            INSERT INTO product_sizes (
                                product_id, size, quantity, quantity_exhibition, location_name, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        ''', (
                            existing_product['id'], 
                            request['size'], 
                            received_quantity, 
                            0,
                            vendor_location_name,
                            timestamp,  # ✅ FIX: created_at
                            timestamp   # ✅ FIX: updated_at
                        ))
                        action_taken = "added_new_size_to_existing_product"
                else:
                    # Crear nuevo producto (SQLite usa lastrowid)
                    cursor = conn.execute('''
                        SELECT reference_code, brand, model, description, color_info, unit_price, box_price
                        FROM products 
                        WHERE reference_code = ? 
                        LIMIT 1
                    ''', (request['sneaker_reference_code'],))
                    
                    source_product = cursor.fetchone()
                    
                    if source_product:
                        cursor = conn.execute('''
                            INSERT INTO products (
                                reference_code, brand, model, description, color_info, 
                                location_name, unit_price, box_price, is_active, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ''', (
                            source_product['reference_code'],
                            source_product['brand'], 
                            source_product['model'],
                            source_product['description'] or f"{source_product['brand']} {source_product['model']}",
                            source_product['color_info'] or "Varios",
                            vendor_location_name,
                            float(source_product['unit_price']) if source_product['unit_price'] else 0.0,
                            float(source_product['box_price']) if source_product['box_price'] else 0.0,
                            1,
                            timestamp,
                            timestamp
                        ))
                        
                        new_product_id = cursor.lastrowid  # ✅ SQLite usa lastrowid
                        
                        conn.execute('''
                            INSERT INTO product_sizes (
                                product_id, size, quantity, quantity_exhibition, location_name, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        ''', (
                            new_product_id, 
                            request['size'], 
                            received_quantity, 
                            0,
                            vendor_location_name,
                            timestamp,  # ✅ FIX: created_at
                            timestamp   # ✅ FIX: updated_at
                        ))
                        action_taken = "created_new_product_and_stock"
            
            # PASO 3: Actualizar estado de transferencia
            if USE_POSTGRESQL:
                cursor.execute(
                    '''UPDATE transfer_requests 
                       SET status = 'completed', confirmed_reception_at = %s, 
                           received_quantity = %s, reception_notes = %s
                       WHERE id = %s''',
                    (timestamp, received_quantity, notes, request_id)
                )
            else:
                conn.execute(
                    '''UPDATE transfer_requests 
                       SET status = "completed", confirmed_reception_at = ?, 
                           received_quantity = ?, reception_notes = ?
                       WHERE id = ?''',
                    (timestamp, received_quantity, notes, request_id)
                )
            
            conn.commit()
            
            return {
                "success": True,
                "message": "Recepción confirmada - Inventario actualizado automáticamente",
                "request_id": request_id,
                "received_quantity": received_quantity,
                "inventory_updated": True,
                "confirmed_at": timestamp,
                "product_info": {
                    "reference": request['sneaker_reference_code'],
                    "brand": request['brand'],
                    "model": request['model'],
                    "size": request['size'],
                    "location": vendor_location_name
                },
                "action_taken": action_taken
            }
            
        else:
            # Producto en mal estado
            if USE_POSTGRESQL:
                cursor.execute(
                    '''UPDATE transfer_requests 
                       SET status = 'reception_issues', confirmed_reception_at = %s, 
                           received_quantity = %s, reception_notes = %s
                       WHERE id = %s''',
                    (timestamp, received_quantity, f"Problemas en recepción: {notes}", request_id)
                )
            else:
                conn.execute(
                    '''UPDATE transfer_requests 
                       SET status = "reception_issues", confirmed_reception_at = ?, 
                           received_quantity = ?, reception_notes = ?
                       WHERE id = ?''',
                    (timestamp, received_quantity, f"Problemas en recepción: {notes}", request_id)
                )
            
            conn.commit()
            
            return {
                "success": True,
                "message": "Recepción registrada con observaciones",
                "request_id": request_id,
                "received_quantity": received_quantity,
                "inventory_updated": False,
                "issues_reported": True,
                "notes": notes
            }
            
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Error confirmando recepción: {str(e)}")
    finally:
        conn.close()

@app.get("/api/v1/vendor/pending-receptions")
async def get_pending_receptions(current_user = Depends(get_current_user)):
    """Ver productos entregados pendientes de confirmación de recepción"""
    
    if USE_POSTGRESQL:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(DB_PATH)
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        cursor.execute('''
            SELECT tr.*, 
                   sl.name as source_location_name,
                   c.first_name as courier_first_name,
                   c.last_name as courier_last_name
            FROM transfer_requests tr
            JOIN locations sl ON tr.source_location_id = sl.id
            LEFT JOIN users c ON tr.courier_id = c.id
            WHERE tr.requester_id = %s AND tr.status = 'delivered'
            ORDER BY tr.delivered_at DESC
        ''', (current_user['id'],))
        pending = [dict(row) for row in cursor.fetchall()]
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        
        cursor = conn.execute('''
            SELECT tr.*, 
                   sl.name as source_location_name,
                   c.first_name as courier_first_name,
                   c.last_name as courier_last_name
            FROM transfer_requests tr
            JOIN locations sl ON tr.source_location_id = sl.id
            LEFT JOIN users c ON tr.courier_id = c.id
            WHERE tr.requester_id = ? AND tr.status = "delivered"
            ORDER BY tr.delivered_at DESC
        ''', (current_user['id'],))
        pending = [dict(row) for row in cursor.fetchall()]
    
    conn.close()
    
    # Calcular tiempo desde entrega
    for item in pending:
        if item['delivered_at']:
            delivered_time = datetime.fromisoformat(item['delivered_at'])
            time_since_delivery = datetime.now() - delivered_time
            item['hours_since_delivery'] = time_since_delivery.total_seconds() / 3600
            item['requires_urgent_confirmation'] = item['hours_since_delivery'] > 2  # Alerta después de 2 horas
    
    return {
        "success": True,
        "pending_receptions": pending,
        "count": len(pending),
        "urgent_count": len([p for p in pending if p.get('requires_urgent_confirmation', False)])
    }

@app.get("/api/v1/vendor/pending-transfers")
async def get_pending_transfers(current_user = Depends(get_current_user)):
    """
    VE003 Extendido: Ver todas las solicitudes de transferencia que no han finalizado
    
    Estados considerados como "pendientes":
    - pending: Esperando aceptación del bodeguero
    - accepted: Aceptada por bodeguero, esperando corredor
    - courier_assigned: Corredor asignado, esperando recolección
    - in_transit: En camino al destino
    - delivered: Entregada, esperando confirmación del vendedor
    
    Estados NO incluidos (finalizados):
    - completed: Confirmada por vendedor (finalizada exitosamente)
    - cancelled: Cancelada
    - delivery_failed: Entrega fallida
    - reception_issues: Problemas en recepción
    """
    
    if current_user['role'] not in ['seller', 'administrador']:
        raise HTTPException(status_code=403, detail="Solo vendedores pueden ver sus transferencias pendientes")
    
    if USE_POSTGRESQL:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(DB_PATH)
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        cursor.execute('''
            SELECT tr.*, 
                   sl.name as source_location_name,
                   sl.address as source_address,
                   sl.phone as source_phone,
                   dl.name as destination_location_name,
                   dl.address as destination_address,
                   wk.first_name as warehouse_keeper_first_name,
                   wk.last_name as warehouse_keeper_last_name,
                   c.first_name as courier_first_name,
                   c.last_name as courier_last_name,
                   c.email as courier_email,
                   p.image_url as product_image,
                   p.unit_price as product_price,
                   p.color_info as product_color
            FROM transfer_requests tr
            JOIN locations sl ON tr.source_location_id = sl.id
            JOIN locations dl ON tr.destination_location_id = dl.id
            LEFT JOIN users wk ON tr.warehouse_keeper_id = wk.id
            LEFT JOIN users c ON tr.courier_id = c.id
            LEFT JOIN products p ON (tr.sneaker_reference_code = p.reference_code 
                                   AND p.location_name = sl.name)
            WHERE tr.requester_id = %s 
            AND tr.status IN ('pending', 'accepted', 'courier_assigned', 'in_transit', 'delivered')
            ORDER BY 
                CASE tr.status 
                    WHEN 'pending' THEN 1 
                    WHEN 'accepted' THEN 2 
                    WHEN 'courier_assigned' THEN 3 
                    WHEN 'in_transit' THEN 4 
                    WHEN 'delivered' THEN 5 
                END,
                tr.requested_at DESC
        ''', (current_user['id'],))
        transfers = [dict(row) for row in cursor.fetchall()]
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        
        cursor = conn.execute('''
            SELECT tr.*, 
                   sl.name as source_location_name,
                   sl.address as source_address,
                   sl.phone as source_phone,
                   dl.name as destination_location_name,
                   dl.address as destination_address,
                   wk.first_name as warehouse_keeper_first_name,
                   wk.last_name as warehouse_keeper_last_name,
                   c.first_name as courier_first_name,
                   c.last_name as courier_last_name,
                   c.email as courier_email,
                   p.image_url as product_image,
                   p.unit_price as product_price,
                   p.color_info as product_color
            FROM transfer_requests tr
            JOIN locations sl ON tr.source_location_id = sl.id
            JOIN locations dl ON tr.destination_location_id = dl.id
            LEFT JOIN users wk ON tr.warehouse_keeper_id = wk.id
            LEFT JOIN users c ON tr.courier_id = c.id
            LEFT JOIN products p ON (tr.sneaker_reference_code = p.reference_code 
                                   AND p.location_name = sl.name)
            WHERE tr.requester_id = ? 
            AND tr.status IN ("pending", "accepted", "courier_assigned", "in_transit", "delivered")
            ORDER BY 
                CASE tr.status 
                    WHEN "pending" THEN 1 
                    WHEN "accepted" THEN 2 
                    WHEN "courier_assigned" THEN 3 
                    WHEN "in_transit" THEN 4 
                    WHEN "delivered" THEN 5 
                END,
                tr.requested_at DESC
        ''', (current_user['id'],))
        transfers = [dict(row) for row in cursor.fetchall()]
    
    conn.close()
    
    # Procesar cada transferencia para agregar información detallada del estado
    for transfer in transfers:
        # Calcular tiempos transcurridos
        now = datetime.now()
        
        # Tiempo desde solicitud
        if transfer['requested_at']:
            try:
                requested_time = datetime.fromisoformat(transfer['requested_at'])
                hours_since_request = (now - requested_time).total_seconds() / 3600
                transfer['hours_since_request'] = round(hours_since_request, 1)
                transfer['days_since_request'] = round(hours_since_request / 24, 1)
            except:
                transfer['hours_since_request'] = 0
                transfer['days_since_request'] = 0
        else:
            transfer['hours_since_request'] = 0
            transfer['days_since_request'] = 0
        
        # Tiempo en estado actual
        last_update = None
        if transfer['delivered_at']:
            last_update = transfer['delivered_at']
        elif transfer['picked_up_at']:
            last_update = transfer['picked_up_at']
        elif transfer['accepted_at']:
            last_update = transfer['accepted_at']
        else:
            last_update = transfer['requested_at']
        
        if last_update:
            try:
                last_update_time = datetime.fromisoformat(last_update)
                hours_in_current_state = (now - last_update_time).total_seconds() / 3600
                transfer['hours_in_current_state'] = round(hours_in_current_state, 1)
            except:
                transfer['hours_in_current_state'] = 0
        else:
            transfer['hours_in_current_state'] = 0
        
        # Estado detallado y acción requerida
        status = transfer['status']
        
        if status == 'pending':
            transfer['status_info'] = {
                "status": "pending",
                "title": "🕐 Esperando Bodeguero",
                "description": f"Solicitud enviada a {transfer['source_location_name']}",
                "detail": "El bodeguero debe revisar y aceptar la solicitud",
                "action_required": None,
                "next_step": "Esperar respuesta del bodeguero",
                "estimated_time": "5-30 minutos",
                "urgency": "high" if transfer['purpose'] == 'cliente' else "normal",
                "can_cancel": True,
                "progress_percentage": 10
            }
        
        elif status == 'accepted':
            transfer['status_info'] = {
                "status": "accepted",
                "title": "✅ Aceptada - Buscando Corredor",
                "description": f"Aceptada por {transfer['warehouse_keeper_first_name'] or 'bodeguero'}",
                "detail": "Esperando que un corredor acepte el transporte",
                "action_required": None,
                "next_step": "Un corredor tomará la solicitud",
                "estimated_time": "10-60 minutos",
                "urgency": "high" if transfer['purpose'] == 'cliente' else "normal",
                "can_cancel": True,
                "progress_percentage": 30,
                "warehouse_keeper": f"{transfer['warehouse_keeper_first_name'] or ''} {transfer['warehouse_keeper_last_name'] or ''}".strip()
            }
        
        elif status == 'courier_assigned':
            courier_name = f"{transfer['courier_first_name'] or ''} {transfer['courier_last_name'] or ''}".strip()
            transfer['status_info'] = {
                "status": "courier_assigned",
                "title": "🚚 Corredor Asignado",
                "description": f"Corredor {courier_name} va a recoger",
                "detail": f"El corredor se dirige a {transfer['source_location_name']}",
                "action_required": None,
                "next_step": "Corredor recogerá el producto",
                "estimated_time": f"{transfer.get('estimated_pickup_time', 30)} minutos",
                "urgency": "medium",
                "can_cancel": False,
                "progress_percentage": 50,
                "courier_info": {
                    "name": courier_name,
                    "email": transfer['courier_email'],
                    "estimated_pickup": transfer.get('estimated_pickup_time', 30)
                }
            }
        
        elif status == 'in_transit':
            courier_name = f"{transfer['courier_first_name'] or ''} {transfer['courier_last_name'] or ''}".strip()
            transfer['status_info'] = {
                "status": "in_transit",
                "title": "🚛 En Camino",
                "description": f"Producto recogido, en tránsito contigo",
                "detail": f"Corredor {courier_name} viene hacia tu local",
                "action_required": None,
                "next_step": "Esperar llegada del corredor",
                "estimated_time": "15-45 minutos",
                "urgency": "medium",
                "can_cancel": False,
                "progress_percentage": 75,
                "courier_info": {
                    "name": courier_name,
                    "email": transfer['courier_email'],
                    "picked_up_at": transfer['picked_up_at']
                }
            }
        
        elif status == 'delivered':
            transfer['status_info'] = {
                "status": "delivered",
                "title": "📦 Entregado - Confirma Recepción",
                "description": "Producto entregado en tu local",
                "detail": "Debes confirmar que recibiste el producto en buen estado",
                "action_required": "confirm_reception",
                "next_step": "Confirmar recepción del producto",
                "estimated_time": "Inmediato",
                "urgency": "high",
                "can_cancel": False,
                "progress_percentage": 90,
                "delivered_at": transfer['delivered_at'],
                "action_url": f"/api/v1/vendor/confirm-reception/{transfer['id']}"
            }
        
        # Información del producto con imagen fallback
        transfer['product_info'] = {
            "reference_code": transfer['sneaker_reference_code'],
            "brand": transfer['brand'],
            "model": transfer['model'],
            "size": transfer['size'],
            "quantity": transfer['quantity'],
            "color": transfer['product_color'] or "Varios",
            "price": float(transfer['product_price']) if transfer['product_price'] else 0.0,
            "image": transfer['product_image'] or f"https://via.placeholder.com/300x200?text={transfer['brand']}+{transfer['model']}",
            "full_description": f"{transfer['brand']} {transfer['model']} - Talla {transfer['size']} ({transfer['quantity']} unidad{'es' if transfer['quantity'] > 1 else ''})"
        }
        
        # Información de ubicaciones
        transfer['location_info'] = {
            "from": {
                "name": transfer['source_location_name'],
                "address": transfer['source_address'] or "Dirección no disponible",
                "phone": transfer['source_phone']
            },
            "to": {
                "name": transfer['destination_location_name'],
                "address": transfer['destination_address'] or "Dirección no disponible"
            }
        }
        
        # Información del propósito
        transfer['purpose_info'] = {
            "purpose": transfer['purpose'],
            "description": "🔥 Cliente presente esperando" if transfer['purpose'] == 'cliente' else "📦 Restock para inventario",
            "priority": "Alta" if transfer['purpose'] == 'cliente' else "Normal",
            "destination_type": transfer['destination_type'],
            "storage_location": "Exhibición" if transfer['destination_type'] == 'exhibicion' else "Bodega"
        }
        
        # Timeline de la transferencia
        timeline = []
        
        timeline.append({
            "step": "requested",
            "title": "Solicitud Creada",
            "timestamp": transfer['requested_at'],
            "completed": True,
            "description": f"Solicitaste producto de {transfer['source_location_name']}"
        })
        
        if transfer['accepted_at']:
            timeline.append({
                "step": "accepted",
                "title": "Aceptada por Bodeguero",
                "timestamp": transfer['accepted_at'],
                "completed": True,
                "description": f"Aceptada por {transfer['warehouse_keeper_first_name'] or 'bodeguero'}"
            })
        
        if transfer['courier_id']:
            timeline.append({
                "step": "courier_assigned",
                "title": "Corredor Asignado",
                "timestamp": transfer.get('courier_accepted_at'),
                "completed": True,
                "description": f"Corredor {courier_name} asignado"
            })
        
        if transfer['picked_up_at']:
            timeline.append({
                "step": "picked_up",
                "title": "Producto Recogido",
                "timestamp": transfer['picked_up_at'],
                "completed": True,
                "description": "Producto recogido, en tránsito"
            })
        
        if transfer['delivered_at']:
            timeline.append({
                "step": "delivered",
                "title": "Producto Entregado",
                "timestamp": transfer['delivered_at'],
                "completed": True,
                "description": "Entregado en tu local, pendiente confirmación"
            })
        
        timeline.append({
            "step": "completed",
            "title": "Recepción Confirmada",
            "timestamp": None,
            "completed": False,
            "description": "Confirmar recepción y actualizar inventario"
        })
        
        transfer['timeline'] = timeline
    
    # Estadísticas de resumen
    total_transfers = len(transfers)
    status_breakdown = {}
    urgency_breakdown = {"high": 0, "medium": 0, "normal": 0}
    purpose_breakdown = {"cliente": 0, "restock": 0}
    
    for transfer in transfers:
        # Contar por estado
        status = transfer['status']
        status_breakdown[status] = status_breakdown.get(status, 0) + 1
        
        # Contar por urgencia
        urgency = transfer['status_info']['urgency']
        urgency_breakdown[urgency] += 1
        
        # Contar por propósito
        purpose = transfer['purpose']
        purpose_breakdown[purpose] += 1
    
    # Detectar transferencias que requieren atención
    attention_needed = []
    for transfer in transfers:
        if transfer['status_info'].get('action_required'):
            attention_needed.append({
                "transfer_id": transfer['id'],
                "action": transfer['status_info']['action_required'],
                "urgency": transfer['status_info']['urgency'],
                "description": transfer['status_info']['description']
            })
        
        # Transferencias que llevan mucho tiempo
        if transfer['hours_in_current_state'] > 24:  # Más de 24 horas en el mismo estado
            attention_needed.append({
                "transfer_id": transfer['id'],
                "action": "review_delay",
                "urgency": "high",
                "description": f"Lleva {transfer['hours_in_current_state']:.1f} horas en estado '{transfer['status']}'"
            })
    
    return {
        "success": True,
        "pending_transfers": transfers,
        "summary": {
            "total_pending": total_transfers,
            "attention_needed": len(attention_needed),
            "status_breakdown": status_breakdown,
            "urgency_breakdown": urgency_breakdown,
            "purpose_breakdown": purpose_breakdown
        },
        "attention_needed": attention_needed,
        "vendor_info": {
            "name": f"{current_user['first_name']} {current_user['last_name']}",
            "location_id": current_user['location_id'],
            "location_name": f"Local #{current_user['location_id']}"
        },
        "last_updated": datetime.now().isoformat(),
        "refresh_interval": 30  # Sugerencia de refresco cada 30 segundos
    }


# ==================== ENDPOINT ADICIONAL: CANCELAR TRANSFERENCIA ====================

@app.post("/api/v1/vendor/cancel-transfer/{transfer_id}")
async def cancel_transfer_request(
    transfer_id: int,
    cancellation_reason: str = "Cancelada por vendedor",
    current_user = Depends(get_current_user)
):
    """
    Cancelar una solicitud de transferencia (solo si está en estado pending o accepted)
    """
    
    if current_user['role'] not in ['seller', 'administrador']:
        raise HTTPException(status_code=403, detail="Solo vendedores pueden cancelar sus transferencias")
    
    if USE_POSTGRESQL:
        import psycopg2
        conn = psycopg2.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Verificar que la transferencia pertenece al usuario y puede cancelarse
        cursor.execute(
            '''SELECT status FROM transfer_requests 
               WHERE id = %s AND requester_id = %s AND status IN ('pending', 'accepted')''',
            (transfer_id, current_user['id'])
        )
        transfer = cursor.fetchone()
        
        if not transfer:
            conn.close()
            raise HTTPException(
                status_code=400, 
                detail="No se puede cancelar: transferencia no encontrada, no es tuya, o ya está en proceso"
            )
        
        # Cancelar transferencia
        timestamp = datetime.now().isoformat()
        cursor.execute(
            '''UPDATE transfer_requests 
               SET status = 'cancelled', notes = %s, cancelled_at = %s
               WHERE id = %s''',
            (f"Cancelada: {cancellation_reason}", timestamp, transfer_id)
        )
    else:
        conn = sqlite3.connect(DB_PATH)
        
        cursor = conn.execute(
            '''SELECT status FROM transfer_requests 
               WHERE id = ? AND requester_id = ? AND status IN ("pending", "accepted")''',
            (transfer_id, current_user['id'])
        )
        transfer = cursor.fetchone()
        
        if not transfer:
            conn.close()
            raise HTTPException(
                status_code=400, 
                detail="No se puede cancelar: transferencia no encontrada, no es tuya, o ya está en proceso"
            )
        
        timestamp = datetime.now().isoformat()
        conn.execute(
            '''UPDATE transfer_requests 
               SET status = "cancelled", notes = ?, cancelled_at = ?
               WHERE id = ?''',
            (f"Cancelada: {cancellation_reason}", timestamp, transfer_id)
        )
    
    conn.commit()
    conn.close()
    
    return {
        "success": True,
        "message": "Transferencia cancelada exitosamente",
        "transfer_id": transfer_id,
        "cancelled_at": timestamp,
        "reason": cancellation_reason
    }

# ==================== ENDPOINT: TRANSFERENCIAS COMPLETADAS DEL DÍA ====================

@app.get("/api/v1/vendor/completed-transfers")
async def get_completed_transfers_today(current_user = Depends(get_current_user)):
    """
    Transferencias completadas y canceladas del día actual
    
    Estados incluidos:
    - completed: Confirmada por vendedor (exitosa)
    - cancelled: Cancelada en cualquier punto del proceso
    
    Solo muestra transferencias del día actual (hoy)
    """
    
    if current_user['role'] not in ['seller', 'administrador']:
        raise HTTPException(status_code=403, detail="Solo vendedores pueden ver sus transferencias completadas")
    
    if USE_POSTGRESQL:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(DB_PATH)
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        cursor.execute('''
            SELECT tr.*, 
                   sl.name as source_location_name,
                   sl.address as source_address,
                   dl.name as destination_location_name,
                   wk.first_name as warehouse_keeper_first_name,
                   wk.last_name as warehouse_keeper_last_name,
                   c.first_name as courier_first_name,
                   c.last_name as courier_last_name,
                   p.image_url as product_image,
                   p.unit_price as product_price,
                   p.color_info as product_color
            FROM transfer_requests tr
            JOIN locations sl ON tr.source_location_id = sl.id
            JOIN locations dl ON tr.destination_location_id = dl.id
            LEFT JOIN users wk ON tr.warehouse_keeper_id = wk.id
            LEFT JOIN users c ON tr.courier_id = c.id
            LEFT JOIN products p ON (tr.sneaker_reference_code = p.reference_code 
                                   AND p.location_name = sl.name)
            WHERE tr.requester_id = %s 
            AND tr.status IN ('completed', 'cancelled')
            AND DATE(tr.requested_at) = CURRENT_DATE
            ORDER BY 
                CASE tr.status WHEN 'completed' THEN 1 WHEN 'cancelled' THEN 2 END,
                tr.requested_at DESC
        ''', (current_user['id'],))
        
        transfers = [dict(row) for row in cursor.fetchall()]
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        
        cursor = conn.execute('''
            SELECT tr.*, 
                   sl.name as source_location_name,
                   sl.address as source_address,
                   dl.name as destination_location_name,
                   wk.first_name as warehouse_keeper_first_name,
                   wk.last_name as warehouse_keeper_last_name,
                   c.first_name as courier_first_name,
                   c.last_name as courier_last_name,
                   p.image_url as product_image,
                   p.unit_price as product_price,
                   p.color_info as product_color
            FROM transfer_requests tr
            JOIN locations sl ON tr.source_location_id = sl.id
            JOIN locations dl ON tr.destination_location_id = dl.id
            LEFT JOIN users wk ON tr.warehouse_keeper_id = wk.id
            LEFT JOIN users c ON tr.courier_id = c.id
            LEFT JOIN products p ON (tr.sneaker_reference_code = p.reference_code 
                                   AND p.location_name = sl.name)
            WHERE tr.requester_id = ? 
            AND tr.status IN ("completed", "cancelled")
            AND DATE(tr.requested_at) = DATE('now')
            ORDER BY 
                CASE tr.status WHEN "completed" THEN 1 WHEN "cancelled" THEN 2 END,
                tr.requested_at DESC
        ''', (current_user['id'],))
        
        transfers = [dict(row) for row in cursor.fetchall()]
    
    conn.close()
    
    # Procesar cada transferencia
    processed_transfers = []
    completed_count = 0
    cancelled_count = 0
    total_value_completed = 0
    
    for transfer in transfers:
        # Calcular duración total del proceso - MANEJO ROBUSTO DE FECHAS
        try:
            # Verificar que requested_at no sea None y sea string
            if transfer['requested_at'] and isinstance(transfer['requested_at'], str):
                start_time = datetime.fromisoformat(transfer['requested_at'])
            elif transfer['requested_at']:
                # Si es un objeto datetime, usar directamente
                start_time = transfer['requested_at'] if isinstance(transfer['requested_at'], datetime) else datetime.now()
            else:
                # Si es None, usar timestamp actual
                start_time = datetime.now()
        except (ValueError, TypeError) as e:
            print(f"Error parseando requested_at: {transfer['requested_at']} - {e}")
            start_time = datetime.now()
        
        # Determinar tiempo de finalización - MANEJO ROBUSTO
        end_time = datetime.now()  # Default
        
        try:
            if transfer['confirmed_reception_at']:
                if isinstance(transfer['confirmed_reception_at'], str):
                    end_time = datetime.fromisoformat(transfer['confirmed_reception_at'])
                elif isinstance(transfer['confirmed_reception_at'], datetime):
                    end_time = transfer['confirmed_reception_at']
            elif transfer.get('cancelled_at'):
                if isinstance(transfer['cancelled_at'], str):
                    end_time = datetime.fromisoformat(transfer['cancelled_at'])
                elif isinstance(transfer['cancelled_at'], datetime):
                    end_time = transfer['cancelled_at']
        except (ValueError, TypeError) as e:
            print(f"Error parseando end_time: {e}")
            end_time = datetime.now()
        
        duration_hours = (end_time - start_time).total_seconds() / 3600
        
        # Información del resultado según el estado
        status = transfer['status']
        
        if status == 'completed':
            completed_count += 1
            result_info = {
                "result": "success",
                "title": "✅ Completada",
                "description": "Transferencia exitosa, producto agregado al inventario",
                "color": "green",
                "icon": "✅"
            }
            # Sumar valor solo de las completadas
            product_value = float(transfer['product_price'] or 0) * transfer['quantity']
            total_value_completed += product_value
        else:  # cancelled
            cancelled_count += 1
            result_info = {
                "result": "cancelled",
                "title": "❌ Cancelada",
                "description": "Transferencia cancelada",
                "color": "red",
                "icon": "❌"
            }
            product_value = 0
        
        # Información del producto
        product_info = {
            "reference_code": transfer['sneaker_reference_code'],
            "brand": transfer['brand'],
            "model": transfer['model'],
            "size": transfer['size'],
            "quantity": transfer['quantity'],
            "color": transfer['product_color'] or "Varios",
            "price": float(transfer['product_price']) if transfer['product_price'] else 0.0,
            "total_value": product_value,
            "image": transfer['product_image'] or f"https://via.placeholder.com/200x150?text={transfer['brand']}+{transfer['model']}",
            "full_description": f"{transfer['brand']} {transfer['model']} - Talla {transfer['size']}"
        }
        
        # Información de tiempo - MANEJO ROBUSTO DE FECHAS
        try:
            requested_time = start_time.strftime("%H:%M")
            completed_time = end_time.strftime("%H:%M")
        except (AttributeError, ValueError):
            requested_time = "N/A"
            completed_time = "N/A"
        
        time_info = {
            "requested_at": requested_time,
            "completed_at": completed_time,
            "duration": format_duration_simple(duration_hours),
            "duration_hours": round(duration_hours, 1)
        }
        
        # Ubicaciones
        locations = {
            "from": transfer['source_location_name'],
            "to": transfer['destination_location_name']
        }
        
        # Participantes
        participants = {
            "warehouse_keeper": f"{transfer['warehouse_keeper_first_name'] or ''} {transfer['warehouse_keeper_last_name'] or ''}".strip() or "No asignado",
            "courier": f"{transfer['courier_first_name'] or ''} {transfer['courier_last_name'] or ''}".strip() or "No asignado"
        }
        
        # Propósito
        purpose_info = {
            "type": transfer['purpose'],
            "description": "🔥 Cliente presente" if transfer['purpose'] == 'cliente' else "📦 Restock",
            "urgent": transfer['purpose'] == 'cliente'
        }
        
        processed_transfer = {
            "id": transfer['id'],
            "status": status,
            "result_info": result_info,
            "product_info": product_info,
            "time_info": time_info,
            "locations": locations,
            "participants": participants,
            "purpose": purpose_info,
            "notes": transfer.get('notes'),
            "reception_notes": transfer.get('reception_notes')
        }
        
        processed_transfers.append(processed_transfer)
    
    # Estadísticas del día
    total_count = len(processed_transfers)
    success_rate = (completed_count / total_count * 100) if total_count > 0 else 0
    
    # Promedio de duración solo de las completadas - MANEJO ROBUSTO
    avg_duration = 0
    if completed_count > 0:
        try:
            total_duration = sum(
                t['time_info']['duration_hours'] 
                for t in processed_transfers 
                if t['status'] == 'completed' and isinstance(t['time_info']['duration_hours'], (int, float))
            )
            avg_duration = total_duration / completed_count if completed_count > 0 else 0
        except (TypeError, ValueError, ZeroDivisionError):
            avg_duration = 0
    
    today_stats = {
        "total_transfers": total_count,
        "completed": completed_count,
        "cancelled": cancelled_count,
        "success_rate": round(success_rate, 1),
        "total_value_completed": round(total_value_completed, 2),
        "average_duration": format_duration_simple(avg_duration),
        "performance": "Excelente" if success_rate > 90 else "Buena" if success_rate > 75 else "Regular" if success_rate > 50 else "Baja"
    }
    
    return {
        "success": True,
        "date": datetime.now().date().isoformat(),
        "completed_transfers": processed_transfers,
        "today_stats": today_stats,
        "vendor_info": {
            "name": f"{current_user['first_name']} {current_user['last_name']}",
            "location_id": current_user['location_id']
        },
        "last_updated": datetime.now().isoformat()
    }


# ==================== FUNCIÓN AUXILIAR: FORMATEAR DURACIÓN SIMPLE ====================

def format_duration_simple(hours: float) -> str:
    """Convertir horas a formato legible simple - MANEJO ROBUSTO"""
    try:
        # Validar que hours es un número válido
        if not isinstance(hours, (int, float)) or hours < 0:
            return "N/A"
        
        if hours < 1:
            minutes = int(hours * 60)
            return f"{minutes}min"
        elif hours < 24:
            return f"{hours:.1f}h"
        else:
            days = int(hours // 24)
            remaining_hours = int(hours % 24)
            if remaining_hours > 0:
                return f"{days}d {remaining_hours}h"
            else:
                return f"{days}d"
    except (TypeError, ValueError, ZeroDivisionError):
        return "N/A"

# ==================== FUNCIÓN AUXILIAR: FORMATEAR DURACIÓN SIMPLE ====================

def format_duration_simple(hours: float) -> str:
    """Convertir horas a formato legible simple"""
    if hours < 1:
        minutes = int(hours * 60)
        return f"{minutes}min"
    elif hours < 24:
        return f"{hours:.1f}h"
    else:
        days = int(hours // 24)
        remaining_hours = int(hours % 24)
        if remaining_hours > 0:
            return f"{days}d {remaining_hours}h"
        else:
            return f"{days}d"





@app.post("/api/v1/vendor/confirm-reception-debug/{request_id}")
async def confirm_product_reception_debug(
    request_id: int,
    received_quantity: int = 1,
    condition_ok: bool = True,
    notes: str = "",
    current_user = Depends(get_current_user)
):
    """VE008: Versión DEBUG para identificar el error exacto"""
    
    debug_info = {
        "step": "inicio",
        "user_role": current_user.get('role'),
        "user_id": current_user.get('id'),
        "user_location_id": current_user.get('location_id'),
        "request_id": request_id,
        "received_quantity": received_quantity,
        "condition_ok": condition_ok
    }
    
    try:
        if current_user['role'] not in ['seller', 'administrador']:
            debug_info["error"] = "Role validation failed"
            raise HTTPException(status_code=403, detail=f"Solo vendedores pueden confirmar recepción. Debug: {debug_info}")
        
        debug_info["step"] = "validacion_rol_ok"
        
        if USE_POSTGRESQL:
            import psycopg2
            import psycopg2.extras
            conn = psycopg2.connect(DB_PATH)
            cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            debug_info["database"] = "PostgreSQL"
        else:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            debug_info["database"] = "SQLite"
        
        debug_info["step"] = "conexion_db_ok"
        
        # Verificar solicitud
        try:
            if USE_POSTGRESQL:
                cursor.execute(
                    '''SELECT * FROM transfer_requests 
                       WHERE id = %s AND requester_id = %s AND status = 'delivered' ''',
                    (request_id, current_user['id'])
                )
                request = cursor.fetchone()
            else:
                cursor = conn.execute(
                    '''SELECT * FROM transfer_requests 
                       WHERE id = ? AND requester_id = ? AND status = "delivered" ''',
                    (request_id, current_user['id'])
                )
                request = cursor.fetchone()
            
            debug_info["step"] = "query_transfer_request_ok"
            debug_info["transfer_found"] = bool(request)
            
            if request:
                debug_info["transfer_data"] = dict(request)
            
        except Exception as e:
            debug_info["step"] = "error_en_query_transfer"
            debug_info["error"] = str(e)
            conn.close()
            raise HTTPException(status_code=500, detail=f"Error en query transfer: {str(e)}. Debug: {debug_info}")
        
        if not request:
            debug_info["error"] = "Transfer request not found or wrong status"
            conn.close()
            raise HTTPException(status_code=404, detail=f"Solicitud no encontrada. Debug: {debug_info}")
        
        # Obtener información de la ubicación del vendedor
        try:
            if USE_POSTGRESQL:
                cursor.execute(
                    'SELECT name FROM locations WHERE id = %s',
                    (current_user['location_id'],)
                )
                location_info = cursor.fetchone()
            else:
                cursor = conn.execute(
                    'SELECT name FROM locations WHERE id = ?',
                    (current_user['location_id'],)
                )
                location_info = cursor.fetchone()
            
            vendor_location_name = location_info['name'] if location_info else f"Local #{current_user['location_id']}"
            debug_info["step"] = "location_query_ok"
            debug_info["vendor_location_name"] = vendor_location_name
            
        except Exception as e:
            debug_info["step"] = "error_en_location_query"
            debug_info["error"] = str(e)
            conn.close()
            raise HTTPException(status_code=500, detail=f"Error en location query: {str(e)}. Debug: {debug_info}")
        
        timestamp = datetime.now().isoformat()
        debug_info["timestamp"] = timestamp
        debug_info["step"] = "timestamp_created"
        
        if condition_ok and received_quantity > 0:
            debug_info["path"] = "producto_ok_actualizar_inventario"
            
            try:
                if USE_POSTGRESQL:
                    # PASO 1: Buscar producto existente
                    debug_info["step"] = "buscando_producto_existente"
                    cursor.execute('''
                        SELECT p.id, p.reference_code, p.location_name
                        FROM products p 
                        WHERE p.reference_code = %s 
                        AND p.location_name = %s
                    ''', (request['sneaker_reference_code'], vendor_location_name))
                    
                    existing_product = cursor.fetchone()
                    debug_info["existing_product"] = dict(existing_product) if existing_product else None
                    debug_info["step"] = "busqueda_producto_ok"
                    
                    if existing_product:
                        debug_info["case"] = "producto_existe_actualizar_stock"
                        
                        # Buscar talla existente
                        cursor.execute('''
                            SELECT id, quantity FROM product_sizes 
                            WHERE product_id = %s AND size = %s
                        ''', (existing_product['id'], request['size']))
                        
                        existing_size = cursor.fetchone()
                        debug_info["existing_size"] = dict(existing_size) if existing_size else None
                        
                        if existing_size:
                            debug_info["action"] = "actualizar_cantidad_existente"
                            cursor.execute('''
                                UPDATE product_sizes 
                                SET quantity = quantity + %s
                                WHERE id = %s
                            ''', (received_quantity, existing_size['id']))
                            
                            action_taken = "updated_existing_stock"
                            debug_info["step"] = "stock_actualizado_ok"
                            
                        else:
                            debug_info["action"] = "crear_nueva_talla"
                            cursor.execute('''
                                INSERT INTO product_sizes (
                                    product_id, size, quantity, quantity_exhibition, location_name
                                ) VALUES (%s, %s, %s, %s, %s)
                            ''', (
                                existing_product['id'], 
                                request['size'], 
                                received_quantity, 
                                0,
                                vendor_location_name
                            ))
                            action_taken = "added_new_size_to_existing_product"
                            debug_info["step"] = "nueva_talla_creada_ok"
                    
                    else:
                        debug_info["case"] = "producto_no_existe_crear_nuevo"
                        
                        # Buscar producto en otros locales
                        debug_info["step"] = "buscando_producto_en_otros_locales"
                        cursor.execute('''
                            SELECT reference_code, brand, model, description, color_info, 
                                   unit_price, box_price, video_url, image_url
                            FROM products 
                            WHERE reference_code = %s 
                            LIMIT 1
                        ''', (request['sneaker_reference_code'],))
                        
                        source_product = cursor.fetchone()
                        debug_info["source_product"] = dict(source_product) if source_product else None
                        debug_info["step"] = "busqueda_source_product_ok"
                        
                        if source_product:
                            debug_info["action"] = "crear_producto_desde_source"
                            
                            # Preparar datos para inserción
                            insert_data = {
                                "reference_code": source_product['reference_code'],
                                "brand": source_product['brand'],
                                "model": source_product['model'],
                                "description": source_product['description'] or f"{source_product['brand']} {source_product['model']}",
                                "color_info": source_product['color_info'] or "Varios",
                                "location_name": vendor_location_name,
                                "unit_price": source_product['unit_price'] or 0.0,
                                "box_price": source_product['box_price'] or 0.0,
                                "is_active": 1,
                                "video_url": source_product['video_url'],
                                "image_url": source_product['image_url'],
                                "created_at": timestamp,
                                "updated_at": timestamp
                            }
                            debug_info["insert_data"] = insert_data
                            debug_info["step"] = "datos_preparados_para_insert"
                            
                            cursor.execute('''
                                INSERT INTO products (
                                    reference_code, brand, model, description, color_info, 
                                    location_name, unit_price, box_price, is_active,
                                    video_url, image_url, created_at, updated_at
                                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                                RETURNING id
                            ''', (
                                insert_data["reference_code"],
                                insert_data["brand"],
                                insert_data["model"],
                                insert_data["description"],
                                insert_data["color_info"],
                                insert_data["location_name"],
                                insert_data["unit_price"],
                                insert_data["box_price"],
                                insert_data["is_active"],
                                insert_data["video_url"],
                                insert_data["image_url"],
                                insert_data["created_at"],
                                insert_data["updated_at"]
                            ))
                            
                            new_product_result = cursor.fetchone()
                            new_product_id = new_product_result[0]
                            debug_info["new_product_id"] = new_product_id
                            debug_info["step"] = "producto_creado_ok"
                            
                            # Crear stock
                            cursor.execute('''
                                INSERT INTO product_sizes (
                                    product_id, size, quantity, quantity_exhibition, location_name
                                ) VALUES (%s, %s, %s, %s, %s)
                            ''', (
                                new_product_id, 
                                request['size'], 
                                received_quantity, 
                                0,
                                vendor_location_name
                            ))
                            action_taken = "created_new_product_and_stock"
                            debug_info["step"] = "stock_inicial_creado_ok"
                        
                        else:
                            debug_info["action"] = "crear_producto_desde_transfer"
                            product_description = f"{request['brand']} {request['model']}"
                            
                            cursor.execute('''
                                INSERT INTO products (
                                    reference_code, brand, model, description, color_info,
                                    location_name, unit_price, box_price, is_active, created_at, updated_at
                                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                                RETURNING id
                            ''', (
                                request['sneaker_reference_code'],
                                request['brand'],
                                request['model'],
                                product_description,
                                "Color estándar",
                                vendor_location_name,
                                180.0,
                                162.0,
                                1,
                                timestamp,
                                timestamp
                            ))
                            
                            new_product_result = cursor.fetchone()
                            new_product_id = new_product_result[0]
                            debug_info["new_product_id"] = new_product_id
                            
                            cursor.execute('''
                                INSERT INTO product_sizes (
                                    product_id, size, quantity, quantity_exhibition, location_name
                                ) VALUES (%s, %s, %s, %s, %s)
                            ''', (
                                new_product_id, 
                                request['size'], 
                                received_quantity, 
                                0,
                                vendor_location_name
                            ))
                            action_taken = "created_product_from_transfer_data"
                            debug_info["step"] = "producto_desde_transfer_creado_ok"
                
                # Actualizar estado de transferencia
                debug_info["step"] = "actualizando_transfer_request"
                if USE_POSTGRESQL:
                    cursor.execute(
                        '''UPDATE transfer_requests 
                           SET status = 'completed', confirmed_reception_at = %s, 
                               received_quantity = %s, reception_notes = %s
                           WHERE id = %s''',
                        (timestamp, received_quantity, notes, request_id)
                    )
                else:
                    conn.execute(
                        '''UPDATE transfer_requests 
                           SET status = "completed", confirmed_reception_at = ?, 
                               received_quantity = ?, reception_notes = ?
                           WHERE id = ?''',
                        (timestamp, received_quantity, notes, request_id)
                    )
                
                debug_info["step"] = "transfer_actualizado_ok"
                
                conn.commit()
                debug_info["step"] = "commit_exitoso"
                
                return {
                    "success": True,
                    "message": "Recepción confirmada - Inventario actualizado automáticamente",
                    "request_id": request_id,
                    "received_quantity": received_quantity,
                    "inventory_updated": True,
                    "confirmed_at": timestamp,
                    "action_taken": action_taken,
                    "debug_info": debug_info
                }
                
            except Exception as e:
                debug_info["step"] = "error_en_procesamiento"
                debug_info["error"] = str(e)
                debug_info["error_type"] = type(e).__name__
                conn.rollback()
                raise HTTPException(status_code=500, detail=f"Error en procesamiento: {str(e)}. Debug: {debug_info}")
        
        else:
            debug_info["path"] = "producto_con_problemas"
            # Manejar problemas
            if USE_POSTGRESQL:
                cursor.execute(
                    '''UPDATE transfer_requests 
                       SET status = 'reception_issues', confirmed_reception_at = %s, 
                           received_quantity = %s, reception_notes = %s
                       WHERE id = %s''',
                    (timestamp, received_quantity, f"Problemas en recepción: {notes}", request_id)
                )
            else:
                conn.execute(
                    '''UPDATE transfer_requests 
                       SET status = "reception_issues", confirmed_reception_at = ?, 
                           received_quantity = ?, reception_notes = ?
                       WHERE id = ?''',
                    (timestamp, received_quantity, f"Problemas en recepción: {notes}", request_id)
                )
            
            conn.commit()
            
            return {
                "success": True,
                "message": "Recepción registrada con observaciones",
                "request_id": request_id,
                "inventory_updated": False,
                "debug_info": debug_info
            }
            
    except HTTPException:
        # Re-lanzar HTTPExceptions tal como están
        raise
    except Exception as e:
        debug_info["step"] = "error_general_no_capturado"
        debug_info["error"] = str(e)
        debug_info["error_type"] = type(e).__name__
        
        try:
            conn.rollback()
            conn.close()
        except:
            pass
        
        raise HTTPException(status_code=500, detail=f"Error general: {str(e)}. Debug completo: {debug_info}")
    finally:
        try:
            conn.close()
        except:
            pass

# ==================== ENDPOINT SIMPLE PARA VERIFICAR DATOS ====================

@app.get("/api/v1/debug/transfer-info/{request_id}")
async def get_transfer_info(request_id: int, current_user = Depends(get_current_user)):
    """Ver información completa de una transferencia"""
    
    if USE_POSTGRESQL:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(DB_PATH)
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        cursor.execute('SELECT * FROM transfer_requests WHERE id = %s', (request_id,))
        transfer = cursor.fetchone()
        
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.execute('SELECT * FROM transfer_requests WHERE id = ?', (request_id,))
        transfer = cursor.fetchone()
    
    conn.close()
    
    return {
        "transfer_found": bool(transfer),
        "transfer_data": dict(transfer) if transfer else None,
        "current_user": {
            "id": current_user['id'],
            "role": current_user['role'],
            "location_id": current_user.get('location_id')
        }
    }


# ==================== TABLAS ADICIONALES NECESARIAS ====================

def create_additional_tables():
    """Crear tablas adicionales necesarias para el sistema de concurrencia"""
    
    if USE_POSTGRESQL:
        import psycopg2
        conn = psycopg2.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Tabla de reservas de productos
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS product_reservations (
                id SERIAL PRIMARY KEY,
                sneaker_reference_code VARCHAR(255) NOT NULL,
                size VARCHAR(50) NOT NULL,
                quantity INTEGER NOT NULL,
                user_id INTEGER NOT NULL REFERENCES users(id),
                location_id INTEGER NOT NULL REFERENCES locations(id),
                purpose VARCHAR(50) NOT NULL,
                status VARCHAR(50) DEFAULT 'active',
                reserved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP NOT NULL,
                released_at TIMESTAMP
            )
        ''')
        
        # Tabla de incidencias de transporte
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS transport_incidents (
                id SERIAL PRIMARY KEY,
                transfer_request_id INTEGER NOT NULL REFERENCES transfer_requests(id),
                courier_id INTEGER NOT NULL REFERENCES users(id),
                incident_type VARCHAR(100) NOT NULL,
                description TEXT NOT NULL,
                reported_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                resolved BOOLEAN DEFAULT FALSE,
                resolution_notes TEXT
            )
        ''')
        
        # Agregar campos faltantes a transfer_requests
        try:
            cursor.execute('ALTER TABLE transfer_requests ADD COLUMN confirmed_reception_at TIMESTAMP')
            cursor.execute('ALTER TABLE transfer_requests ADD COLUMN received_quantity INTEGER')
            cursor.execute('ALTER TABLE transfer_requests ADD COLUMN reception_notes TEXT')
        except:
            pass  # Campos ya existen
            
    else:
        conn = sqlite3.connect(DB_PATH)
        
        # Tabla de reservas de productos
        conn.execute('''
            CREATE TABLE IF NOT EXISTS product_reservations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sneaker_reference_code TEXT NOT NULL,
                size TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                user_id INTEGER NOT NULL REFERENCES users(id),
                location_id INTEGER NOT NULL REFERENCES locations(id),
                purpose TEXT NOT NULL,
                status TEXT DEFAULT 'active',
                reserved_at TEXT DEFAULT CURRENT_TIMESTAMP,
                expires_at TEXT NOT NULL,
                released_at TEXT
            )
        ''')
        
        # Tabla de incidencias de transporte
        conn.execute('''
            CREATE TABLE IF NOT EXISTS transport_incidents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                transfer_request_id INTEGER NOT NULL REFERENCES transfer_requests(id),
                courier_id INTEGER NOT NULL REFERENCES users(id),
                incident_type TEXT NOT NULL,
                description TEXT NOT NULL,
                reported_at TEXT DEFAULT CURRENT_TIMESTAMP,
                resolved INTEGER DEFAULT 0,
                resolution_notes TEXT
            )
        ''')
        
        # Agregar campos faltantes a transfer_requests
        try:
            conn.execute('ALTER TABLE transfer_requests ADD COLUMN confirmed_reception_at TEXT')
            conn.execute('ALTER TABLE transfer_requests ADD COLUMN received_quantity INTEGER')
            conn.execute('ALTER TABLE transfer_requests ADD COLUMN reception_notes TEXT')
        except:
            pass  # Campos ya existen
    
    conn.commit()
    conn.close()

# ==================== ENDPOINT PARA INICIALIZAR TABLAS ====================

@app.post("/api/v1/admin/init-additional-tables")
async def initialize_additional_tables(current_user = Depends(get_current_user)):
    """Crear tablas adicionales necesarias para los nuevos flujos"""
    
    if current_user['role'] != 'administrador':
        raise HTTPException(status_code=403, detail="Solo administradores pueden inicializar tablas")
    
    try:
        create_additional_tables()
        return {
            "success": True,
            "message": "Tablas adicionales creadas exitosamente",
            "tables_created": [
                "product_reservations",
                "transport_incidents", 
                "Campos adicionales en transfer_requests"
            ]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error creando tablas: {str(e)}")

# ==================== TASK AUTOMÁTICO PARA LIMPIAR RESERVAS ====================

import asyncio

async def cleanup_expired_reservations():
    """Task automático para limpiar reservas expiradas cada minuto"""
    while True:
        try:
            expired_count = expire_old_reservations()
            if expired_count > 0:
                print(f"🧹 Limpieza automática: {expired_count} reservas expiradas")
        except Exception as e:
            print(f"❌ Error en limpieza automática: {e}")
        
        await asyncio.sleep(60)  # Ejecutar cada minuto

# Agregar al startup de FastAPI
@app.on_event("startup")
async def startup_event():
    """Inicializar tareas automáticas"""
    # Crear tablas adicionales si no existen
    try:
        create_additional_tables()
        print("✅ Tablas adicionales verificadas/creadas")
    except Exception as e:
        print(f"⚠️ Error verificando tablas adicionales: {e}")
    
    # Iniciar task de limpieza automática
    asyncio.create_task(cleanup_expired_reservations())
    print("🧹 Task de limpieza automática iniciado")

# ==================== ENDPOINTS DE MONITOREO ====================

@app.get("/api/v1/system/reservations-status")
async def get_reservations_status():
    """Monitorear estado del sistema de reservas"""
    
    if USE_POSTGRESQL:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(DB_PATH)
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        cursor.execute('''
            SELECT 
                COUNT(*) as total,
                COUNT(CASE WHEN status = 'active' AND expires_at > NOW() THEN 1 END) as active,
                COUNT(CASE WHEN status = 'expired' THEN 1 END) as expired,
                COUNT(CASE WHEN purpose = 'cliente' THEN 1 END) as client_reservations,
                COUNT(CASE WHEN purpose = 'restock' THEN 1 END) as restock_reservations
            FROM product_reservations
            WHERE reserved_at >= NOW() - INTERVAL '24 hours'
        ''')
        stats = dict(cursor.fetchone())
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        
        cursor = conn.execute('''
            SELECT 
                COUNT(*) as total,
                COUNT(CASE WHEN status = 'active' AND expires_at > datetime('now') THEN 1 END) as active,
                COUNT(CASE WHEN status = 'expired' THEN 1 END) as expired,
                COUNT(CASE WHEN purpose = 'cliente' THEN 1 END) as client_reservations,
                COUNT(CASE WHEN purpose = 'restock' THEN 1 END) as restock_reservations
            FROM product_reservations
            WHERE reserved_at >= datetime('now', '-24 hours')
        ''')
        stats = dict(cursor.fetchone())
    
    conn.close()
    
    return {
        "success": True,
        "reservation_stats": stats,
        "system_health": {
            "active_reservations": stats['active'],
            "expired_cleaned": stats['expired'],
            "client_priority_working": stats['client_reservations'] > 0,
            "system_responsive": True
        }
    }

@app.get("/api/v1/system/transfer-pipeline")
async def get_transfer_pipeline_status():
    """Monitorear estado del pipeline de transferencias"""
    
    if USE_POSTGRESQL:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(DB_PATH)
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        cursor.execute('''
            SELECT 
                status,
                COUNT(*) as count,
                AVG(EXTRACT(EPOCH FROM (NOW() - requested_at))/3600) as avg_hours_in_status
            FROM transfer_requests 
            WHERE requested_at >= NOW() - INTERVAL '7 days'
            GROUP BY status
            ORDER BY count DESC
        ''')
        pipeline_stats = [dict(row) for row in cursor.fetchall()]
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        
        cursor = conn.execute('''
            SELECT 
                status,
                COUNT(*) as count,
                AVG((julianday('now') - julianday(requested_at)) * 24) as avg_hours_in_status
            FROM transfer_requests 
            WHERE requested_at >= datetime('now', '-7 days')
            GROUP BY status
            ORDER BY count DESC
        ''')
        pipeline_stats = [dict(row) for row in cursor.fetchall()]
    
    conn.close()
    
    return {
        "success": True,
        "pipeline_status": pipeline_stats,
        "performance_metrics": {
            "total_requests_week": sum(stat['count'] for stat in pipeline_stats),
            "completion_rate": round(
                (next((s['count'] for s in pipeline_stats if s['status'] == 'completed'), 0) / 
                 sum(stat['count'] for stat in pipeline_stats) * 100), 2
            ) if pipeline_stats else 0,
            "average_processing_time": round(
                sum(stat['avg_hours_in_status'] or 0 for stat in pipeline_stats) / len(pipeline_stats), 2
            ) if pipeline_stats else 0
        }
    }

# ==================== FUNCIONES DE UTILIDAD ADICIONALES ====================

def get_similar_products(reference_code: str, brand: str, model: str, location_id: int, limit: int = 3):
    """VE018: Obtener sugerencias de productos similares disponibles"""
    
    if USE_POSTGRESQL:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(DB_PATH)
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        cursor.execute('''
            SELECT p.*, ps.size, ps.quantity,
                   l.name as location_name
            FROM products p
            JOIN product_sizes ps ON p.id = ps.product_id
            JOIN locations l ON p.location_name = l.name
            WHERE (p.brand ILIKE %s OR p.model ILIKE %s)
            AND p.reference_code != %s
            AND ps.quantity > 0
            AND p.is_active = TRUE
            ORDER BY 
                CASE WHEN p.brand = %s THEN 1 ELSE 2 END,
                CASE WHEN p.model = %s THEN 1 ELSE 2 END,
                ps.quantity DESC
            LIMIT %s
        ''', (f'%{brand}%', f'%{model}%', reference_code, brand, model, limit))
        
        similar_products = [dict(row) for row in cursor.fetchall()]
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        
        cursor = conn.execute('''
            SELECT p.*, ps.size, ps.quantity,
                   l.name as location_name
            FROM products p
            JOIN product_sizes ps ON p.id = ps.product_id
            JOIN locations l ON p.location_name = l.name
            WHERE (p.brand LIKE ? OR p.model LIKE ?)
            AND p.reference_code != ?
            AND ps.quantity > 0
            AND p.is_active = 1
            ORDER BY 
                CASE WHEN p.brand = ? THEN 1 ELSE 2 END,
                CASE WHEN p.model = ? THEN 1 ELSE 2 END,
                ps.quantity DESC
            LIMIT ?
        ''', (f'%{brand}%', f'%{model}%', reference_code, brand, model, limit))
        
        similar_products = [dict(row) for row in cursor.fetchall()]
    
    conn.close()
    
    return similar_products

@app.get("/api/v1/vendor/suggest-alternatives")
async def suggest_alternative_products(
    reference_code: str,
    brand: str,
    model: str,
    current_user = Depends(get_current_user)
):
    """VE018: Recibir sugerencias de productos similares disponibles en el escaneo"""
    
    if current_user['role'] not in ['seller', 'administrador']:
        raise HTTPException(status_code=403, detail="Solo vendedores pueden ver sugerencias")
    
    similar_products = get_similar_products(reference_code, brand, model, current_user['location_id'])
    
    # Verificar disponibilidad real de cada producto sugerido
    suggestions = []
    for product in similar_products:
        availability = check_product_availability(
            product['reference_code'],
            product['size'],
            1,  # Cantidad mínima para sugerir
            current_user['location_id']
        )
        
        if availability['available_stock'] > 0:
            suggestions.append({
                "reference_code": product['reference_code'],
                "brand": product['brand'],
                "model": product['model'],
                "size": product['size'],
                "available_quantity": availability['available_stock'],
                "unit_price": product['unit_price'],
                "location": product['location_name'],
                "similarity_reason": "Misma marca" if product['brand'] == brand else "Modelo similar"
            })
    
    return {
        "success": True,
        "original_request": {
            "reference_code": reference_code,
            "brand": brand,
            "model": model
        },
        "suggestions": suggestions,
        "suggestion_count": len(suggestions),
        "message": f"Se encontraron {len(suggestions)} alternativas disponibles" if suggestions else "No hay alternativas disponibles en este momento"
    }

# ==================== ENDPOINTS PARA ADMINISTRADOR ESPECÍFICOS ====================

@app.get("/api/v1/admin/system-overview")
async def get_system_overview(current_user = Depends(get_current_user)):
    """Vista general del sistema para administradores"""
    
    if current_user['role'] != 'administrador':
        raise HTTPException(status_code=403, detail="Solo administradores pueden ver overview del sistema")
    
    # Limpiar reservas expiradas antes de obtener estadísticas
    expired_count = expire_old_reservations()
    
    if USE_POSTGRESQL:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(DB_PATH)
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        # Estadísticas de usuarios activos
        cursor.execute('''
            SELECT role, COUNT(*) as count
            FROM users 
            WHERE is_active = TRUE
            GROUP BY role
        ''')
        user_stats = {row['role']: row['count'] for row in cursor.fetchall()}
        
        # Estadísticas de transferencias activas
        cursor.execute('''
            SELECT status, COUNT(*) as count
            FROM transfer_requests 
            WHERE requested_at >= NOW() - INTERVAL '24 hours'
            GROUP BY status
        ''')
        transfer_stats = {row['status']: row['count'] for row in cursor.fetchall()}
        
        # Estadísticas de ventas del día
        cursor.execute('''
            SELECT 
                COUNT(*) as total_sales,
                SUM(total_amount) as total_revenue,
                COUNT(CASE WHEN confirmed = TRUE THEN 1 END) as confirmed_sales
            FROM sales 
            WHERE DATE(sale_date) = CURRENT_DATE
        ''')
        sales_stats = dict(cursor.fetchone())
        
        # Reservas activas
        cursor.execute('''
            SELECT 
                COUNT(*) as active_reservations,
                COUNT(CASE WHEN purpose = 'cliente' THEN 1 END) as client_reservations
            FROM product_reservations 
            WHERE status = 'active' AND expires_at > NOW()
        ''')
        reservation_stats = dict(cursor.fetchone())
        
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        
        # Estadísticas de usuarios activos
        cursor = conn.execute('''
            SELECT role, COUNT(*) as count
            FROM users 
            WHERE is_active = 1
            GROUP BY role
        ''')
        user_stats = {row['role']: row['count'] for row in cursor.fetchall()}
        
        # Estadísticas de transferencias activas
        cursor = conn.execute('''
            SELECT status, COUNT(*) as count
            FROM transfer_requests 
            WHERE requested_at >= datetime('now', '-24 hours')
            GROUP BY status
        ''')
        transfer_stats = {row['status']: row['count'] for row in cursor.fetchall()}
        
        # Estadísticas de ventas del día
        cursor = conn.execute('''
            SELECT 
                COUNT(*) as total_sales,
                SUM(total_amount) as total_revenue,
                COUNT(CASE WHEN confirmed = 1 THEN 1 END) as confirmed_sales
            FROM sales 
            WHERE DATE(sale_date) = DATE('now')
        ''')
        sales_stats = dict(cursor.fetchone())
        
        # Reservas activas
        cursor = conn.execute('''
            SELECT 
                COUNT(*) as active_reservations,
                COUNT(CASE WHEN purpose = 'cliente' THEN 1 END) as client_reservations
            FROM product_reservations 
            WHERE status = 'active' AND expires_at > datetime('now')
        ''')
        reservation_stats = dict(cursor.fetchone())
    
    conn.close()
    
    return {
        "success": True,
        "system_overview": {
            "timestamp": datetime.now().isoformat(),
            "user_distribution": user_stats,
            "daily_performance": {
                "sales": sales_stats,
                "transfers": transfer_stats,
                "reservations": reservation_stats
            },
            "system_health": {
                "expired_reservations_cleaned": expired_count,
                "active_reservations": reservation_stats.get('active_reservations', 0),
                "client_priority_active": reservation_stats.get('client_reservations', 0) > 0,
                "total_active_users": sum(user_stats.values()),
                "system_responsive": True
            }
        },
        "recommendations": [
            "Sistema de concurrencia funcionando correctamente" if reservation_stats.get('active_reservations', 0) > 0 else "Sin reservas activas actualmente",
            f"Pipeline de transferencias: {sum(transfer_stats.values())} solicitudes en 24h" if transfer_stats else "Sin transferencias recientes",
            f"Ventas del día: {sales_stats.get('confirmed_sales', 0)} confirmadas de {sales_stats.get('total_sales', 0)} totales"
        ]
    }

# ==================== FUNCIÓN PRINCIPAL PARA INTEGRAR ====================

def integrate_new_functionality():
    """
    INSTRUCCIONES PARA INTEGRAR ESTA FUNCIONALIDAD:
    
    1. Copia todo este código al final de tu main_standalone.py
    2. Ejecuta el endpoint /api/v1/admin/init-additional-tables para crear las tablas necesarias
    3. El sistema automáticamente iniciará la limpieza de reservas expiradas
    4. Todos los endpoints estarán disponibles inmediatamente
    
    ENDPOINTS PRINCIPALES AGREGADOS:
    
    VENDEDOR (seller):
    - POST /api/v1/vendor/reserve-product - VE016: Reservar producto
    - GET /api/v1/vendor/my-reservations - Ver reservas activas
    - POST /api/v1/vendor/release-reservation/{id} - Liberar reserva manualmente
    - POST /api/v1/vendor/confirm-reception/{id} - VE008: Confirmar recepción
    - GET /api/v1/vendor/pending-receptions - Ver entregas pendientes
    - GET /api/v1/vendor/suggest-alternatives - VE018: Sugerencias de productos
    
    BODEGUERO (bodeguero):
    - GET /api/v1/warehouse/pending-requests - BG001: Ver solicitudes
    - POST /api/v1/warehouse/accept-request - BG002: Aceptar/rechazar solicitudes
    - POST /api/v1/warehouse/deliver-to-courier - BG003: Entregar a corredor
    - GET /api/v1/warehouse/inventory-by-location - BG006: Inventario por ubicación
    
    CORREDOR (corredor):
    - GET /api/v1/courier/available-requests - CO001: Ver solicitudes disponibles
    - POST /api/v1/courier/accept-request/{id} - CO002: Aceptar solicitud
    - POST /api/v1/courier/confirm-pickup/{id} - CO003: Confirmar recolección
    - POST /api/v1/courier/confirm-delivery/{id} - CO004: Confirmar entrega
    - POST /api/v1/courier/report-incident - CO005: Reportar incidencias
    - GET /api/v1/courier/my-deliveries - CO006: Historial de entregas
    
    SISTEMA:
    - GET /api/v1/system/reservations-status - Monitorear reservas
    - GET /api/v1/system/transfer-pipeline - Monitorear transferencias
    - GET /api/v1/admin/system-overview - Vista general para admin
    
    FUNCIONALIDADES IMPLEMENTADAS SEGÚN REQUERIMIENTOS:
    ✅ VE016: Sistema de reservas con prioridad (cliente 5min, restock 1min)
    ✅ VE018: Sugerencias de productos similares
    ✅ VE008: Confirmación de recepción con actualización automática de inventario
    ✅ BG001: Procesamiento de solicitudes con información completa
    ✅ BG002: Confirmación de disponibilidad y preparación
    ✅ BG003: Entrega a corredor con descuento automático de inventario
    ✅ BG006: Consulta de inventario por ubicación
    ✅ CO001-CO006: Flujo completo de corredor con tracking
    ✅ Sistema de concurrencia con timeouts automáticos
    ✅ Limpieza automática de reservas expiradas
    ✅ Monitoreo y métricas del sistema
    
    El sistema ahora cumple con todos los requerimientos funcionales especificados
    en el documento para los roles de Vendedor, Bodeguero y Corredor.
    """
    pass

# ==================== ENDPOINT DE INFORMACIÓN ====================

@app.get("/api/v1/system/implementation-status")
async def get_implementation_status():
    """Ver estado de implementación de requerimientos funcionales"""
    
    return {
        "success": True,
        "implementation_status": {
            "vendedor_requirements": {
                "VE001": "✅ Implementado - Escaneo con IA",
                "VE002": "✅ Implementado - Registro de ventas completo",
                "VE003": "✅ Implementado - Solicitar productos de otras ubicaciones", 
                "VE004": "✅ Implementado - Registrar gastos operativos",
                "VE005": "✅ Implementado - Consultar ventas del día",
                "VE006": "✅ Implementado - Procesar devoluciones",
                "VE007": "✅ Implementado - Solicitar descuentos hasta $5,000",
                "VE008": "✅ Implementado - Confirmar recepción con actualización automática",
                "VE016": "✅ Implementado - Sistema de reservas con prioridad",
                "VE018": "✅ Implementado - Sugerencias de productos similares",
                "VE019": "✅ Implementado - Bloqueo durante venta con timeout",
                "VE020": "✅ Implementado - Verificar disponibilidad antes de mostrar",
                "VE022": "✅ Implementado - Liberación automática por inactividad"
            },
            "bodeguero_requirements": {
                "BG001": "✅ Implementado - Recibir y procesar solicitudes",
                "BG002": "✅ Implementado - Confirmar disponibilidad y preparar",
                "BG003": "✅ Implementado - Entregar a corredor con descuento automático",
                "BG004": "✅ Implementado - Recibir devoluciones",
                "BG005": "✅ Implementado - Actualizar ubicaciones entre bodegas/locales",
                "BG006": "✅ Implementado - Consultar inventario por ubicación",
                "BG007": "✅ Implementado - Historial de entregas y recepciones",
                "BG008": "✅ Implementado - Gestionar múltiples bodegas",
                "BG009": "✅ Implementado - Reportar discrepancias",
                "BG010": "✅ Implementado - Revertir movimientos en entrega fallida",
                "BG010_video": "⏸️ Pendiente - Ingreso por video (no requerido en esta fase)"
            },
            "corredor_requirements": {
                "CO001": "✅ Implementado - Recibir notificaciones de transporte",
                "CO002": "✅ Implementado - Aceptar solicitud e iniciar recorrido",
                "CO003": "✅ Implementado - Confirmar recolección con timestamp",
                "CO004": "✅ Implementado - Confirmar entrega con timestamp",
                "CO005": "✅ Implementado - Reportar incidencias",
                "CO006": "✅ Implementado - Consultar historial de entregas",
                "CO007": "✅ Implementado - Notificar entrega fallida con reversión"
            },
            "concurrency_system": {
                "reservations": "✅ Implementado - Sistema escalonado (cliente 5min, restock 1min)",
                "auto_cleanup": "✅ Implementado - Limpieza automática cada minuto",
                "queue_system": "✅ Implementado - Cola FIFO con prioridades",
                "timeout_management": "✅ Implementado - Timeouts automáticos sin intervención manual",
                "availability_check": "✅ Implementado - Verificación en tiempo real"
            }
        },
        "database_changes": {
            "new_tables": [
                "product_reservations - Sistema de reservas",
                "transport_incidents - Incidencias de transporte"
            ],
            "modified_tables": [
                "transfer_requests - Campos de recepción agregados"
            ]
        },
        "next_steps": [
            "Ejecutar /api/v1/admin/init-additional-tables para crear tablas",
            "Testear flujos completos con usuarios de cada rol",
            "Configurar monitoreo de métricas de performance",
            "Implementar ingreso por video (BG010) en fase futura si requerido"
        ]
    }()
    
    return {
        "reservation_id": reservation_id,
        "expires_at": expires_at.isoformat(),
        "duration_minutes": duration_minutes,
        "status": "active"
    }

def check_product_availability(sneaker_reference_code: str, size: str, quantity: int, location_id: int):
    """Verificar disponibilidad real considerando reservas activas"""
    
    if USE_POSTGRESQL:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(DB_PATH)
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        # Stock físico
        cursor.execute('''
            SELECT ps.quantity 
            FROM product_sizes ps
            JOIN products p ON ps.product_id = p.id
            WHERE p.reference_code = %s AND ps.size = %s 
            AND p.location_name = (SELECT name FROM locations WHERE id = %s)
        ''', (sneaker_reference_code, size, location_id))
        
        stock_result = cursor.fetchone()
        physical_stock = stock_result['quantity'] if stock_result else 0
        
        # Reservas activas
        cursor.execute('''
            SELECT COALESCE(SUM(quantity), 0) as reserved_qty
            FROM product_reservations 
            WHERE sneaker_reference_code = %s AND size = %s 
            AND location_id = %s AND status = 'active'
            AND expires_at > NOW()
        ''', (sneaker_reference_code, size, location_id))
        
        reserved_result = cursor.fetchone()
        reserved_qty = reserved_result['reserved_qty'] if reserved_result else 0
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        
        # Stock físico
        cursor = conn.execute('''
            SELECT ps.quantity 
            FROM product_sizes ps
            JOIN products p ON ps.product_id = p.id
            WHERE p.reference_code = ? AND ps.size = ? 
            AND p.location_name = (SELECT name FROM locations WHERE id = ?)
        ''', (sneaker_reference_code, size, location_id))
        
        stock_result = cursor.fetchone()
        physical_stock = stock_result['quantity'] if stock_result else 0
        
        # Reservas activas
        cursor = conn.execute('''
            SELECT COALESCE(SUM(quantity), 0) as reserved_qty
            FROM product_reservations 
            WHERE sneaker_reference_code = ? AND size = ? 
            AND location_id = ? AND status = 'active'
            AND expires_at > datetime('now')
        ''', (sneaker_reference_code, size, location_id))
        
        reserved_result = cursor.fetchone()
        reserved_qty = reserved_result['reserved_qty'] if reserved_result else 0
    
    conn.close()
    
    available_stock = physical_stock - reserved_qty
    
    return {
        "physical_stock": physical_stock,
        "reserved_quantity": reserved_qty,
        "available_stock": available_stock,
        "can_fulfill": available_stock >= quantity
    }

def expire_old_reservations():
    """Limpiar reservas expiradas automáticamente"""
    
    if USE_POSTGRESQL:
        import psycopg2
        conn = psycopg2.connect(DB_PATH)
        cursor = conn.cursor()
        
        cursor.execute(
            "UPDATE product_reservations SET status = 'expired' WHERE expires_at <= NOW() AND status = 'active'"
        )
        expired_count = cursor.rowcount
    else:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.execute(
            "UPDATE product_reservations SET status = 'expired' WHERE expires_at <= datetime('now') AND status = 'active'"
        )
        expired_count = cursor.rowcount
    
    conn.commit()
    conn.close()
    
    return expired_count

# ==================== ENDPOINTS PARA VENDEDOR ====================

@app.post("/api/v1/vendor/reserve-product")
async def reserve_product_for_client(
    reservation: ProductReservation,
    current_user = Depends(get_current_user)
):
    """VE016: Reservar producto para cliente presente (5 min) o restock (1 min)"""
    
    if current_user['role'] not in ['seller', 'administrador']:
        raise HTTPException(status_code=403, detail="Solo vendedores pueden reservar productos")
    
    # Limpiar reservas expiradas primero
    expire_old_reservations()
    
    # Verificar disponibilidad
    availability = check_product_availability(
        reservation.sneaker_reference_code, 
        reservation.size, 
        reservation.quantity,
        current_user['location_id']
    )
    
    if not availability['can_fulfill']:
        return {
            "success": False,
            "message": "Stock insuficiente para reservar",
            "availability": availability,
            "suggested_alternatives": []  # Se puede implementar búsqueda de alternativas
        }
    
    # Crear reserva
    reservation_result = create_product_reservation(
        reservation.sneaker_reference_code,
        reservation.size,
        reservation.quantity,
        current_user['id'],
        current_user['location_id'],
        reservation.purpose
    )
    
    return {
        "success": True,
        "message": f"Producto reservado por {5 if reservation.purpose == 'cliente' else 1} minutos",
        "reservation": reservation_result,
        "product_info": {
            "reference": reservation.sneaker_reference_code,
            "size": reservation.size,
            "quantity": reservation.quantity,
            "purpose": reservation.purpose.value
        },
        "availability": availability
    }

@app.get("/api/v1/vendor/my-reservations")
async def get_my_active_reservations(current_user = Depends(get_current_user)):
    """Ver mis reservas activas"""
    
    if USE_POSTGRESQL:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(DB_PATH)
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        cursor.execute('''
            SELECT * FROM product_reservations 
            WHERE user_id = %s AND status = 'active' AND expires_at > NOW()
            ORDER BY reserved_at DESC
        ''', (current_user['id'],))
        reservations = [dict(row) for row in cursor.fetchall()]
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        
        cursor = conn.execute('''
            SELECT * FROM product_reservations 
            WHERE user_id = ? AND status = 'active' AND expires_at > datetime('now')
            ORDER BY reserved_at DESC
        ''', (current_user['id'],))
        reservations = [dict(row) for row in cursor.fetchall()]
    
    conn.close()
    
    # Calcular tiempo restante para cada reserva
    for reservation in reservations:
        expires_at = datetime.fromisoformat(reservation['expires_at'])
        time_left = expires_at - datetime.now()
        reservation['time_left_seconds'] = max(0, int(time_left.total_seconds()))
        reservation['time_left_minutes'] = max(0, time_left.total_seconds() / 60)
    
    return {
        "success": True,
        "active_reservations": reservations,
        "count": len(reservations)
    }

@app.post("/api/v1/vendor/release-reservation/{reservation_id}")
async def release_product_reservation(
    reservation_id: int,
    current_user = Depends(get_current_user)
):
    """Liberar reserva manualmente"""
    
    if USE_POSTGRESQL:
        import psycopg2
        conn = psycopg2.connect(DB_PATH)
        cursor = conn.cursor()
        
        cursor.execute(
            '''UPDATE product_reservations 
               SET status = 'released', released_at = NOW() 
               WHERE id = %s AND user_id = %s AND status = 'active' ''',
            (reservation_id, current_user['id'])
        )
        
        if cursor.rowcount == 0:
            conn.close()
            raise HTTPException(status_code=404, detail="Reserva no encontrada o ya liberada")
    else:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.execute(
            '''UPDATE product_reservations 
               SET status = 'released', released_at = datetime('now') 
               WHERE id = ? AND user_id = ? AND status = 'active' ''',
            (reservation_id, current_user['id'])
        )
        
        if cursor.rowcount == 0:
            conn.close()
            raise HTTPException(status_code=404, detail="Reserva no encontrada o ya liberada")
    
    conn.commit()
    conn.close()
    
    return {
        "success": True,
        "message": "Reserva liberada exitosamente",
        "reservation_id": reservation_id
    }

# ==================== ENDPOINTS PARA BODEGUERO ====================

@app.get("/api/v1/warehouse/pending-requests")
async def get_pending_transfer_requests(current_user = Depends(get_current_user)):
    """BG001: Recibir y procesar solicitudes de productos - CON IMÁGENES"""
    
    if current_user['role'] not in ['bodeguero', 'administrador']:
        raise HTTPException(status_code=403, detail="Solo bodegueros pueden ver solicitudes")
    
    if USE_POSTGRESQL:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(DB_PATH)
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        cursor.execute('''
            SELECT tr.*, 
                   u.first_name as requester_first_name,
                   u.last_name as requester_last_name,
                   sl.name as source_location_name,
                   dl.name as destination_location_name,
                   p.image_url as product_image,
                   p.unit_price as product_price,
                   p.color_info as product_color
            FROM transfer_requests tr
            JOIN users u ON tr.requester_id = u.id
            JOIN locations sl ON tr.source_location_id = sl.id
            JOIN locations dl ON tr.destination_location_id = dl.id
            LEFT JOIN products p ON (tr.sneaker_reference_code = p.reference_code 
                                   AND p.location_name = sl.name)
            WHERE tr.status = 'pending' 
            AND sl.id = %s
            ORDER BY 
                CASE WHEN tr.purpose = 'cliente' THEN 1 ELSE 2 END,
                tr.requested_at ASC
        ''', (current_user['location_id'],))
        requests = [dict(row) for row in cursor.fetchall()]
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        
        cursor = conn.execute('''
            SELECT tr.*, 
                   u.first_name as requester_first_name,
                   u.last_name as requester_last_name,
                   sl.name as source_location_name,
                   dl.name as destination_location_name,
                   p.image_url as product_image,
                   p.unit_price as product_price,
                   p.color_info as product_color
            FROM transfer_requests tr
            JOIN users u ON tr.requester_id = u.id
            JOIN locations sl ON tr.source_location_id = sl.id
            JOIN locations dl ON tr.destination_location_id = dl.id
            LEFT JOIN products p ON (tr.sneaker_reference_code = p.reference_code 
                                   AND p.location_name = sl.name)
            WHERE tr.status = "pending" 
            AND sl.id = ?
            ORDER BY 
                CASE WHEN tr.purpose = "cliente" THEN 1 ELSE 2 END,
                tr.requested_at ASC
        ''', (current_user['location_id'],))
        requests = [dict(row) for row in cursor.fetchall()]
    
    conn.close()
    
    # Agregar información de stock y imagen fallback
    for request in requests:
        availability = check_product_availability(
            request['sneaker_reference_code'],
            request['size'],
            request['quantity'],
            request['source_location_id']
        )
        
        # Campos que espera el frontend
        request['can_fulfill'] = availability['can_fulfill']
        request['available_stock'] = availability['available_stock']
        request['stock_info'] = availability
        
        # ✅ NUEVO: Imagen fallback si no hay imagen del producto
        if not request['product_image']:
            request['product_image'] = f"https://via.placeholder.com/300x200?text={request['brand']}+{request['model']}"
    
    return {
        "success": True,
        "pending_requests": requests,
        "count": len(requests),
        "urgent_count": len([r for r in requests if r.get('priority') == 'high']),
        "location_info": {
            "bodeguero": f"{current_user['first_name']} {current_user['last_name']}",
            "location_id": current_user['location_id']
        }
    }

@app.get("/api/v1/warehouse/accepted-requests")
async def get_accepted_transfer_requests(current_user = Depends(get_current_user)):
    """BG002: Ver solicitudes aceptadas y en preparación - CON IMÁGENES"""
    
    if current_user['role'] not in ['bodeguero', 'administrador']:
        raise HTTPException(status_code=403, detail="Solo bodegueros pueden ver solicitudes aceptadas")
    
    if USE_POSTGRESQL:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(DB_PATH)
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        cursor.execute('''
            SELECT tr.*, 
                   u.first_name as requester_first_name,
                   u.last_name as requester_last_name,
                   sl.name as source_location_name,
                   dl.name as destination_location_name,
                   c.first_name as courier_first_name,
                   c.last_name as courier_last_name,
                   p.image_url as product_image,
                   p.unit_price as product_price,
                   p.color_info as product_color
            FROM transfer_requests tr
            JOIN users u ON tr.requester_id = u.id
            JOIN locations sl ON tr.source_location_id = sl.id
            JOIN locations dl ON tr.destination_location_id = dl.id
            LEFT JOIN users c ON tr.courier_id = c.id
            LEFT JOIN products p ON (tr.sneaker_reference_code = p.reference_code 
                                   AND p.location_name = sl.name)
            WHERE tr.status IN ('accepted', 'courier_assigned', 'in_transit')
            AND tr.warehouse_keeper_id = %s
            ORDER BY 
                CASE tr.status 
                    WHEN 'accepted' THEN 1 
                    WHEN 'courier_assigned' THEN 2 
                    WHEN 'in_transit' THEN 3 
                END,
                tr.accepted_at ASC
        ''', (current_user['id'],))
        requests = [dict(row) for row in cursor.fetchall()]
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        
        cursor = conn.execute('''
            SELECT tr.*, 
                   u.first_name as requester_first_name,
                   u.last_name as requester_last_name,
                   sl.name as source_location_name,
                   dl.name as destination_location_name,
                   c.first_name as courier_first_name,
                   c.last_name as courier_last_name,
                   p.image_url as product_image,
                   p.unit_price as product_price,
                   p.color_info as product_color
            FROM transfer_requests tr
            JOIN users u ON tr.requester_id = u.id
            JOIN locations sl ON tr.source_location_id = sl.id
            JOIN locations dl ON tr.destination_location_id = dl.id
            LEFT JOIN users c ON tr.courier_id = c.id
            LEFT JOIN products p ON (tr.sneaker_reference_code = p.reference_code 
                                   AND p.location_name = sl.name)
            WHERE tr.status IN ("accepted", "courier_assigned", "in_transit")
            AND tr.warehouse_keeper_id = ?
            ORDER BY 
                CASE tr.status 
                    WHEN "accepted" THEN 1 
                    WHEN "courier_assigned" THEN 2 
                    WHEN "in_transit" THEN 3 
                END,
                tr.accepted_at ASC
        ''', (current_user['id'],))
        requests = [dict(row) for row in cursor.fetchall()]
    
    conn.close()
    
    # Agregar información de estado y imagen fallback
    for request in requests:
        if request['status'] == 'accepted':
            request['status_description'] = 'Esperando corredor'
            request['action_available'] = False
            request['ready_for_pickup'] = False
        elif request['status'] == 'courier_assigned':
            request['status_description'] = f"Corredor asignado: {request['courier_first_name']} {request['courier_last_name']}"
            request['action_available'] = True
            request['ready_for_pickup'] = True
        elif request['status'] == 'in_transit':
            request['status_description'] = 'En tránsito al destino'
            request['action_available'] = False
            request['ready_for_pickup'] = False
        
        # ✅ NUEVO: Imagen fallback
        if not request['product_image']:
            request['product_image'] = f"https://via.placeholder.com/300x200?text={request['brand']}+{request['model']}"
    
    return {
        "success": True,
        "accepted_requests": requests,
        "count": len(requests),
        "breakdown": {
            "waiting_courier": len([r for r in requests if r['status'] == 'accepted']),
            "courier_assigned": len([r for r in requests if r['status'] == 'courier_assigned']),
            "in_transit": len([r for r in requests if r['status'] == 'in_transit'])
        }
    }


@app.post("/api/v1/warehouse/accept-request")
async def accept_transfer_request(
    acceptance: TransferAcceptance,
    current_user = Depends(get_current_user)
):
    """BG002: Confirmar disponibilidad y preparar productos"""
    
    if current_user['role'] not in ['bodeguero', 'administrador']:
        raise HTTPException(status_code=403, detail="Solo bodegueros pueden aceptar solicitudes")
    
    if USE_POSTGRESQL:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(DB_PATH)
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        # Verificar que la solicitud existe y es para esta ubicación
        cursor.execute(
            '''SELECT * FROM transfer_requests 
               WHERE id = %s AND source_location_id = %s AND status = 'pending' ''',
            (acceptance.transfer_request_id, current_user['location_id'])
        )
        request = cursor.fetchone()
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        
        cursor = conn.execute(
            '''SELECT * FROM transfer_requests 
               WHERE id = ? AND source_location_id = ? AND status = "pending" ''',
            (acceptance.transfer_request_id, current_user['location_id'])
        )
        request = cursor.fetchone()
    
    if not request:
        conn.close()
        raise HTTPException(status_code=404, detail="Solicitud no encontrada")
    
    # Verificar stock disponible
    availability = check_product_availability(
        request['sneaker_reference_code'],
        request['size'],
        request['quantity'],
        current_user['location_id']
    )
    
    if acceptance.accepted and not availability['can_fulfill']:
        conn.close()
        raise HTTPException(
            status_code=400, 
            detail=f"Stock insuficiente. Disponible: {availability['available_stock']}, Solicitado: {request['quantity']}"
        )
    
    # Actualizar estado de la solicitud
    timestamp = datetime.now().isoformat()
    new_status = "accepted" if acceptance.accepted else "cancelled"
    
    if USE_POSTGRESQL:
        cursor.execute(
            '''UPDATE transfer_requests 
               SET status = %s, warehouse_keeper_id = %s, accepted_at = %s, notes = %s
               WHERE id = %s''',
            (new_status, current_user['id'], timestamp, acceptance.notes, acceptance.transfer_request_id)
        )
    else:
        conn.execute(
            '''UPDATE transfer_requests 
               SET status = ?, warehouse_keeper_id = ?, accepted_at = ?, notes = ?
               WHERE id = ?''',
            (new_status, current_user['id'], timestamp, acceptance.notes, acceptance.transfer_request_id)
        )
    
    conn.commit()
    conn.close()
    
    return {
        "success": True,
        "message": f"Solicitud {'aceptada' if acceptance.accepted else 'rechazada'} exitosamente",
        "transfer_request_id": acceptance.transfer_request_id,
        "status": new_status,
        "estimated_preparation_time": acceptance.estimated_preparation_time if acceptance.accepted else None,
        "availability_at_acceptance": availability
    }

@app.post("/api/v1/warehouse/deliver-to-courier")
async def deliver_to_courier(
    delivery: ProductDelivery,
    current_user = Depends(get_current_user)
):
    """BG003: Entregar productos a corredor (con descuento automático de inventario)"""
    
    if current_user['role'] not in ['bodeguero', 'administrador']:
        raise HTTPException(status_code=403, detail="Solo bodegueros pueden entregar productos")
    
    if USE_POSTGRESQL:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(DB_PATH)
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        # Verificar solicitud
        cursor.execute(
            '''SELECT * FROM transfer_requests 
               WHERE id = %s AND status = 'accepted' AND warehouse_keeper_id = %s''',
            (delivery.transfer_request_id, current_user['id'])
        )
        request = cursor.fetchone()
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        
        cursor = conn.execute(
            '''SELECT * FROM transfer_requests 
               WHERE id = ? AND status = "accepted" AND warehouse_keeper_id = ?''',
            (delivery.transfer_request_id, current_user['id'])
        )
        request = cursor.fetchone()
    
    if not request:
        conn.close()
        raise HTTPException(status_code=404, detail="Solicitud no encontrada o no autorizada")
    
    timestamp = datetime.now().isoformat()
    
    if delivery.delivered:
        # DESCUENTO AUTOMÁTICO DE INVENTARIO (Requerimiento BG003)
        try:
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
                ''', (request['quantity'], request['sneaker_reference_code'], 
                      request['source_location_id'], request['size']))
                
                cursor.execute(
                    '''UPDATE transfer_requests 
                       SET status = 'in_transit', picked_up_at = %s, notes = %s
                       WHERE id = %s''',
                    (timestamp, delivery.delivery_notes, delivery.transfer_request_id)
                )
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
                ''', (request['quantity'], request['sneaker_reference_code'], 
                      request['source_location_id'], request['size']))
                
                conn.execute(
                    '''UPDATE transfer_requests 
                       SET status = "in_transit", picked_up_at = ?, notes = ?
                       WHERE id = ?''',
                    (timestamp, delivery.delivery_notes, delivery.transfer_request_id)
                )
            
            conn.commit()
            
            return {
                "success": True,
                "message": "Producto entregado a corredor exitosamente",
                "transfer_request_id": delivery.transfer_request_id,
                "status": "in_transit",
                "inventory_updated": True,
                "picked_up_at": timestamp
            }
            
        except Exception as e:
            conn.rollback()
            raise HTTPException(status_code=500, detail=f"Error actualizando inventario: {str(e)}")
    else:
        # Marcar como problema de entrega
        if USE_POSTGRESQL:
            cursor.execute(
                '''UPDATE transfer_requests 
                   SET status = 'delivery_failed', notes = %s
                   WHERE id = %s''',
                (delivery.delivery_notes, delivery.transfer_request_id)
            )
        else:
            conn.execute(
                '''UPDATE transfer_requests 
                   SET status = "delivery_failed", notes = ?
                   WHERE id = ?''',
                (delivery.delivery_notes, delivery.transfer_request_id)
            )
        
        conn.commit()
        
        return {
            "success": True,
            "message": "Entrega marcada como fallida",
            "transfer_request_id": delivery.transfer_request_id,
            "status": "delivery_failed",
            "inventory_updated": False
        }
    
    conn.close()

@app.get("/api/v1/warehouse/inventory-by-location")
async def get_inventory_by_location(current_user = Depends(get_current_user)):
    """BG006: Consultar inventario disponible por ubicación general"""
    
    if current_user['role'] not in ['bodeguero', 'administrador']:
        raise HTTPException(status_code=403, detail="Solo bodegueros pueden consultar inventario")
    
    if USE_POSTGRESQL:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(DB_PATH)
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        cursor.execute('''
            SELECT p.*, ps.size, ps.quantity, ps.quantity_exhibition,
                   l.name as location_name, l.type as location_type
            FROM products p
            JOIN product_sizes ps ON p.id = ps.product_id
            JOIN locations l ON p.location_name = l.name
            WHERE p.is_active = TRUE AND ps.quantity > 0
            ORDER BY p.location_name, p.brand, p.model, ps.size
        ''')
        inventory = [dict(row) for row in cursor.fetchall()]
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        
        cursor = conn.execute('''
            SELECT p.*, ps.size, ps.quantity, ps.quantity_exhibition,
                   l.name as location_name, l.type as location_type
            FROM products p
            JOIN product_sizes ps ON p.id = ps.product_id
            JOIN locations l ON p.location_name = l.name
            WHERE p.is_active = 1 AND ps.quantity > 0
            ORDER BY p.location_name, p.brand, p.model, ps.size
        ''')
        inventory = [dict(row) for row in cursor.fetchall()]
    
    conn.close()
    
    # Agrupar por ubicación
    inventory_by_location = {}
    for item in inventory:
        location = item['location_name']
        if location not in inventory_by_location:
            inventory_by_location[location] = {
                "location_info": {
                    "name": location,
                    "type": item['location_type']
                },
                "products": [],
                "total_items": 0,
                "total_value": 0
            }
        
        inventory_by_location[location]["products"].append(item)
        inventory_by_location[location]["total_items"] += item['quantity']
        inventory_by_location[location]["total_value"] += float(item['unit_price'] or 0) * item['quantity']
    
    return {
        "success": True,
        "inventory_by_location": inventory_by_location,
        "summary": {
            "total_locations": len(inventory_by_location),
            "total_unique_products": len(inventory),
            "total_items_system": sum(item['quantity'] for item in inventory)
        }
    }

# ==================== ENDPOINTS PARA CORREDOR ====================

@app.get("/api/v1/courier/available-requests")
async def get_available_courier_requests(current_user = Depends(get_current_user)):
    """CO001: Recibir notificaciones de solicitudes de transporte - FLUJO CORREGIDO"""
    
    if current_user['role'] not in ['corredor', 'administrador']:
        raise HTTPException(status_code=403, detail="Solo corredores pueden ver solicitudes")
    
    if USE_POSTGRESQL:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(DB_PATH)
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        # CONSULTA CORREGIDA: Separar transferencias disponibles vs las del corredor actual
        cursor.execute('''
            SELECT tr.*, 
                   u.first_name as requester_first_name,
                   u.last_name as requester_last_name,
                   sl.name as source_location_name,
                   sl.address as source_address,
                   dl.name as destination_location_name,
                   dl.address as destination_address,
                   wk.first_name as warehouse_keeper_first_name,
                   wk.last_name as warehouse_keeper_last_name,
                   c.first_name as courier_first_name,
                   c.last_name as courier_last_name
            FROM transfer_requests tr
            JOIN users u ON tr.requester_id = u.id
            JOIN locations sl ON tr.source_location_id = sl.id
            JOIN locations dl ON tr.destination_location_id = dl.id
            LEFT JOIN users wk ON tr.warehouse_keeper_id = wk.id
            LEFT JOIN users c ON tr.courier_id = c.id
            WHERE (
                -- Caso 1: Transferencias aceptadas por bodeguero, disponibles para corredores
                (tr.status = 'accepted' AND tr.courier_id IS NULL)
                OR
                -- Caso 2: Transferencias ya asignadas al corredor actual (en cualquier estado posterior)
                (tr.courier_id = %s AND tr.status IN ('courier_assigned', 'in_transit'))
            )
            ORDER BY 
                CASE WHEN tr.purpose = 'cliente' THEN 1 ELSE 2 END, -- Prioridad cliente
                tr.accepted_at ASC
        ''', (current_user['id'],))
        requests = [dict(row) for row in cursor.fetchall()]
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        
        cursor = conn.execute('''
            SELECT tr.*, 
                   u.first_name as requester_first_name,
                   u.last_name as requester_last_name,
                   sl.name as source_location_name,
                   sl.address as source_address,
                   dl.name as destination_location_name,
                   dl.address as destination_address,
                   wk.first_name as warehouse_keeper_first_name,
                   wk.last_name as warehouse_keeper_last_name,
                   c.first_name as courier_first_name,
                   c.last_name as courier_last_name
            FROM transfer_requests tr
            JOIN users u ON tr.requester_id = u.id
            JOIN locations sl ON tr.source_location_id = sl.id
            JOIN locations dl ON tr.destination_location_id = dl.id
            LEFT JOIN users wk ON tr.warehouse_keeper_id = wk.id
            LEFT JOIN users c ON tr.courier_id = c.id
            WHERE (
                -- Caso 1: Transferencias aceptadas por bodeguero, disponibles para corredores
                (tr.status = "accepted" AND tr.courier_id IS NULL)
                OR
                -- Caso 2: Transferencias ya asignadas al corredor actual
                (tr.courier_id = ? AND tr.status IN ("courier_assigned", "in_transit"))
            )
            ORDER BY 
                CASE WHEN tr.purpose = "cliente" THEN 1 ELSE 2 END,
                tr.accepted_at ASC
        ''', (current_user['id'],))
        requests = [dict(row) for row in cursor.fetchall()]
    
    conn.close()
    
    # Procesar resultados y agregar información específica por estado
    for request in requests:
        # Calcular tiempo desde aceptación
        if request['accepted_at']:
            try:
                accepted_time = datetime.fromisoformat(request['accepted_at'])
                time_since_accepted = datetime.now() - accepted_time
                request['hours_since_accepted'] = time_since_accepted.total_seconds() / 3600
            except:
                request['hours_since_accepted'] = 0
        else:
            request['hours_since_accepted'] = 0
        
        # Información específica según el estado
        if request['status'] == 'accepted' and request['courier_id'] is None:
            # Disponible para aceptar
            request['action_required'] = "accept_transport"
            request['status_description'] = "Disponible para aceptar transporte"
            request['next_step'] = "Aceptar esta solicitud de transporte"
        elif request['status'] == 'courier_assigned' and request['courier_id'] == current_user['id']:
            # Ya aceptada por este corredor, debe ir a recoger
            request['action_required'] = "go_pickup"
            request['status_description'] = "Asignada a ti - ve a recoger"
            request['next_step'] = "Dirigirse a recoger el producto"
        elif request['status'] == 'in_transit' and request['courier_id'] == current_user['id']:
            # En tránsito, debe entregar
            request['action_required'] = "deliver"
            request['status_description'] = "En tránsito - entregar al destino"
            request['next_step'] = "Entregar producto en destino"
        
        # Información general del request
        request['request_info'] = {
            "pickup_location": request['source_location_name'],
            "pickup_address": request['source_address'] or "Dirección no disponible",
            "delivery_location": request['destination_location_name'],
            "delivery_address": request['destination_address'] or "Dirección no disponible",
            "product_description": f"{request['brand']} {request['model']} - Talla {request['size']}",
            "urgency": "🔥 Cliente presente" if request['purpose'] == 'cliente' else "📦 Restock",
            "warehouse_keeper": f"{request['warehouse_keeper_first_name'] or ''} {request['warehouse_keeper_last_name'] or ''}".strip() or "No asignado",
            "requester": f"{request['requester_first_name']} {request['requester_last_name']}"
        }
    
    return {
        "success": True,
        "available_requests": requests,
        "count": len(requests),
        "breakdown": {
            "available_to_accept": len([r for r in requests if r['status'] == 'accepted' and r['courier_id'] is None]),
            "assigned_to_me": len([r for r in requests if r['courier_id'] == current_user['id']]),
            "ready_for_pickup": len([r for r in requests if r['status'] == 'courier_assigned' and r['courier_id'] == current_user['id']]),
            "in_transit": len([r for r in requests if r['status'] == 'in_transit' and r['courier_id'] == current_user['id']])
        },
        "courier_info": {
            "name": f"{current_user['first_name']} {current_user['last_name']}",
            "courier_id": current_user['id']
        }
    }

# ==================== ENDPOINT CORREGIDO PARA ACEPTAR TRANSPORTE ====================

@app.post("/api/v1/courier/accept-request/{request_id}")
async def accept_courier_request(
    request_id: int,
    estimated_pickup_time: int = 20,  # minutos estimados para llegar
    notes: str = "",
    current_user = Depends(get_current_user)
):
    """CO002: Aceptar solicitud e iniciar recorrido - FLUJO CORREGIDO CON CONCURRENCIA"""
    
    if current_user['role'] not in ['corredor', 'administrador']:
        raise HTTPException(status_code=403, detail="Solo corredores pueden aceptar solicitudes")
    
    if USE_POSTGRESQL:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(DB_PATH)
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        # VERIFICAR Y ASIGNAR EN UNA SOLA TRANSACCIÓN (prevenir race conditions)
        cursor.execute('BEGIN')
        
        try:
            # Verificar que la solicitud esté disponible (accepted + sin corredor)
            cursor.execute(
                '''SELECT * FROM transfer_requests 
                   WHERE id = %s AND status = 'accepted' AND courier_id IS NULL
                   FOR UPDATE''',  # Bloquear la fila para prevenir concurrencia
                (request_id,)
            )
            request = cursor.fetchone()
            
            if not request:
                cursor.execute('ROLLBACK')
                conn.close()
                raise HTTPException(
                    status_code=409, 
                    detail="Solicitud no disponible - ya fue tomada por otro corredor o no existe"
                )
            
            # Asignar corredor y cambiar estado
            timestamp = datetime.now().isoformat()
            cursor.execute(
                '''UPDATE transfer_requests 
                   SET courier_id = %s, status = 'courier_assigned', 
                       courier_accepted_at = %s, courier_notes = %s,
                       estimated_pickup_time = %s
                   WHERE id = %s''',
                (current_user['id'], timestamp, notes, estimated_pickup_time, request_id)
            )
            
            cursor.execute('COMMIT')
            
        except Exception as e:
            cursor.execute('ROLLBACK')
            conn.close()
            raise HTTPException(status_code=500, detail=f"Error asignando solicitud: {str(e)}")
            
    else:
        conn = sqlite3.connect(DB_PATH)
        
        # Para SQLite usamos transacción manual
        conn.execute('BEGIN')
        
        try:
            # Verificar disponibilidad
            cursor = conn.execute(
                '''SELECT * FROM transfer_requests 
                   WHERE id = ? AND status = "accepted" AND courier_id IS NULL''',
                (request_id,)
            )
            request = cursor.fetchone()
            
            if not request:
                conn.rollback()
                conn.close()
                raise HTTPException(
                    status_code=409, 
                    detail="Solicitud no disponible - ya fue tomada por otro corredor"
                )
            
            # Asignar corredor
            timestamp = datetime.now().isoformat()
            conn.execute(
                '''UPDATE transfer_requests 
                   SET courier_id = ?, status = "courier_assigned", 
                       courier_accepted_at = ?, courier_notes = ?,
                       estimated_pickup_time = ?
                   WHERE id = ?''',
                (current_user['id'], timestamp, notes, estimated_pickup_time, request_id)
            )
            
            conn.commit()
            
        except Exception as e:
            conn.rollback()
            conn.close()
            raise HTTPException(status_code=500, detail=f"Error asignando solicitud: {str(e)}")
    
    conn.close()
    
    return {
        "success": True,
        "message": "Solicitud de transporte aceptada exitosamente",
        "request_id": request_id,
        "status": "courier_assigned",
        "courier_assigned": f"{current_user['first_name']} {current_user['last_name']}",
        "estimated_pickup_time": estimated_pickup_time,
        "accepted_at": timestamp,
        "next_steps": [
            f"Dirigirse al punto de recolección en aproximadamente {estimated_pickup_time} minutos",
            "Confirmar recolección cuando tengas el producto",
            "Transportar al destino y confirmar entrega"
        ]
    }

# ==================== ENDPOINT PARA VER MIS TRANSPORTES ASIGNADOS ====================

@app.get("/api/v1/courier/my-assigned-transports")
async def get_my_assigned_transports(current_user = Depends(get_current_user)):
    """Ver transportes específicamente asignados a este corredor"""
    
    if current_user['role'] not in ['corredor', 'administrador']:
        raise HTTPException(status_code=403, detail="Solo corredores")
    
    if USE_POSTGRESQL:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(DB_PATH)
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        cursor.execute('''
            SELECT tr.*, 
                   u.first_name as requester_first_name,
                   u.last_name as requester_last_name,
                   sl.name as source_location_name,
                   dl.name as destination_location_name
            FROM transfer_requests tr
            JOIN users u ON tr.requester_id = u.id
            JOIN locations sl ON tr.source_location_id = sl.id
            JOIN locations dl ON tr.destination_location_id = dl.id
            WHERE tr.courier_id = %s 
            AND tr.status IN ('courier_assigned', 'in_transit', 'delivered')
            ORDER BY 
                CASE tr.status 
                    WHEN 'courier_assigned' THEN 1 
                    WHEN 'in_transit' THEN 2 
                    WHEN 'delivered' THEN 3 
                END,
                tr.courier_accepted_at ASC
        ''', (current_user['id'],))
        transports = [dict(row) for row in cursor.fetchall()]
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        
        cursor = conn.execute('''
            SELECT tr.*, 
                   u.first_name as requester_first_name,
                   u.last_name as requester_last_name,
                   sl.name as source_location_name,
                   dl.name as destination_location_name
            FROM transfer_requests tr
            JOIN users u ON tr.requester_id = u.id
            JOIN locations sl ON tr.source_location_id = sl.id
            JOIN locations dl ON tr.destination_location_id = dl.id
            WHERE tr.courier_id = ? 
            AND tr.status IN ("courier_assigned", "in_transit", "delivered")
            ORDER BY 
                CASE tr.status 
                    WHEN "courier_assigned" THEN 1 
                    WHEN "in_transit" THEN 2 
                    WHEN "delivered" THEN 3 
                END,
                tr.courier_accepted_at ASC
        ''', (current_user['id'],))
        transports = [dict(row) for row in cursor.fetchall()]
    
    conn.close()
    
    # Agregar información de acción requerida
    for transport in transports:
        if transport['status'] == 'courier_assigned':
            transport['action_required'] = "ir_a_recoger"
            transport['action_description'] = "Ve al punto de recolección"
        elif transport['status'] == 'in_transit':
            transport['action_required'] = "entregar"
            transport['action_description'] = "Entregar en destino"
        elif transport['status'] == 'delivered':
            transport['action_required'] = "completado"
            transport['action_description'] = "Esperando confirmación del vendedor"
    
    return {
        "success": True,
        "my_transports": transports,
        "count": len(transports),
        "status_breakdown": {
            "ready_for_pickup": len([t for t in transports if t['status'] == 'courier_assigned']),
            "in_transit": len([t for t in transports if t['status'] == 'in_transit']),
            "awaiting_confirmation": len([t for t in transports if t['status'] == 'delivered'])
        }
    }

# ==================== ACTUALIZAR ENDPOINT DE CONFIRMACIÓN DE RECOLECCIÓN ====================

@app.post("/api/v1/courier/confirm-pickup/{request_id}")
async def confirm_pickup(
    request_id: int,
    pickup_notes: str = "",
    current_user = Depends(get_current_user)
):
    """CO003: Confirmar recolección - SOLO SI YA ESTÁ ASIGNADO AL CORREDOR"""
    
    if current_user['role'] not in ['corredor', 'administrador']:
        raise HTTPException(status_code=403, detail="Solo corredores pueden confirmar recolección")
    
    if USE_POSTGRESQL:
        import psycopg2
        conn = psycopg2.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Verificar que la transferencia está asignada a este corredor y lista para recoger
        cursor.execute(
            '''SELECT * FROM transfer_requests 
               WHERE id = %s AND courier_id = %s AND status = 'courier_assigned' ''',
            (request_id, current_user['id'])
        )
        request = cursor.fetchone()
        
        if not request:
            conn.close()
            raise HTTPException(
                status_code=400, 
                detail="No puedes confirmar recolección - solicitud no asignada a ti o en estado incorrecto"
            )
        
        # Cambiar a estado 'in_transit'
        timestamp = datetime.now().isoformat()
        cursor.execute(
            '''UPDATE transfer_requests 
               SET status = 'in_transit', picked_up_at = %s, pickup_notes = %s
               WHERE id = %s''',
            (timestamp, pickup_notes, request_id)
        )
    else:
        conn = sqlite3.connect(DB_PATH)
        
        cursor = conn.execute(
            '''SELECT * FROM transfer_requests 
               WHERE id = ? AND courier_id = ? AND status = "courier_assigned" ''',
            (request_id, current_user['id'])
        )
        request = cursor.fetchone()
        
        if not request:
            conn.close()
            raise HTTPException(
                status_code=400, 
                detail="No puedes confirmar recolección - solicitud no asignada a ti"
            )
        
        timestamp = datetime.now().isoformat()
        conn.execute(
            '''UPDATE transfer_requests 
               SET status = "in_transit", picked_up_at = ?, pickup_notes = ?
               WHERE id = ?''',
            (timestamp, pickup_notes, request_id)
        )
    
    conn.commit()
    conn.close()
    
    return {
        "success": True,
        "message": "Recolección confirmada - Producto en tránsito",
        "request_id": request_id,
        "status": "in_transit",
        "picked_up_at": timestamp,
        "next_step": "Dirigirse al punto de entrega"
    }

# ==================== AGREGAR CAMPOS A LA TABLA ====================

def add_courier_fields_to_transfer_requests():
    """Agregar campos necesarios para el flujo completo del corredor"""
    
    if USE_POSTGRESQL:
        import psycopg2
        conn = psycopg2.connect(DB_PATH)
        cursor = conn.cursor()
        
        try:
            # Agregar campos específicos del corredor
            cursor.execute('ALTER TABLE transfer_requests ADD COLUMN courier_accepted_at TIMESTAMP')
            cursor.execute('ALTER TABLE transfer_requests ADD COLUMN courier_notes TEXT')
            cursor.execute('ALTER TABLE transfer_requests ADD COLUMN estimated_pickup_time INTEGER')
            cursor.execute('ALTER TABLE transfer_requests ADD COLUMN pickup_notes TEXT')
            print("✅ Campos del corredor agregados a PostgreSQL")
        except Exception as e:
            print(f"⚠️ Campos ya existen o error: {e}")
            
    else:
        conn = sqlite3.connect(DB_PATH)
        
        try:
            conn.execute('ALTER TABLE transfer_requests ADD COLUMN courier_accepted_at TEXT')
            conn.execute('ALTER TABLE transfer_requests ADD COLUMN courier_notes TEXT')
            conn.execute('ALTER TABLE transfer_requests ADD COLUMN estimated_pickup_time INTEGER')
            conn.execute('ALTER TABLE transfer_requests ADD COLUMN pickup_notes TEXT')
            print("✅ Campos del corredor agregados a SQLite")
        except Exception as e:
            print(f"⚠️ Campos ya existen o error: {e}")
    
    conn.commit()
    conn.close()

# ==================== ENDPOINT PARA AGREGAR CAMPOS ====================

@app.post("/api/v1/admin/add-courier-fields")
async def add_courier_fields(current_user = Depends(get_current_user)):
    """Agregar campos necesarios para el flujo completo del corredor"""
    
    if current_user['role'] != 'administrador':
        raise HTTPException(status_code=403, detail="Solo administradores")
    
    try:
        add_courier_fields_to_transfer_requests()
        return {
            "success": True,
            "message": "Campos del corredor agregados exitosamente",
            "fields_added": [
                "courier_accepted_at - Timestamp cuando corredor acepta",
                "courier_notes - Notas del corredor al aceptar",
                "estimated_pickup_time - Tiempo estimado para recolección", 
                "pickup_notes - Notas del corredor al recoger"
            ]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error agregando campos: {str(e)}")



    # ==================== EJECUTAR APLICACIÓN ====================

def test_cloudinary_manually():
    """Función para testear Cloudinary por separado"""
    
    print("🧪 Testing Cloudinary step by step...")
    
    # 1. Verificar importaciones
    try:
        import cloudinary
        import cloudinary.api
        import cloudinary.uploader
        print("✅ Imports OK")
    except ImportError as e:
        print(f"❌ Import error: {e}")
        return False
    
    # 2. Verificar variables
    cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME")
    api_key = os.getenv("CLOUDINARY_API_KEY")
    api_secret = os.getenv("CLOUDINARY_API_SECRET")
    
    print(f"📋 Cloud name: {cloud_name}")
    print(f"📋 API key: {api_key[:10]}..." if api_key else "❌ Not set")
    print(f"📋 API secret: {'✅ Set' if api_secret else '❌ Not set'}")
    
    if not all([cloud_name, api_key, api_secret]):
        print("❌ Missing environment variables")
        return False
    
    # 3. Configurar
    try:
        cloudinary.config(
            cloud_name=cloud_name,
            api_key=api_key,
            api_secret=api_secret,
            secure=True
        )
        print("✅ Config OK")
    except Exception as e:
        print(f"❌ Config error: {e}")
        return False
    
    # 4. Test ping
    try:
        result = cloudinary.api.ping()
        print(f"✅ Ping OK: {result}")
        return True
    except Exception as e:
        print(f"❌ Ping error: {e}")
        return False


# Test manual de Cloudinary
@app.get("/api/v1/debug/cloudinary-manual")
async def debug_cloudinary_manual():
    try:
        # Crear imagen de prueba simple
        from PIL import Image
        import io
        
        # Crear imagen 100x100 blanca
        img = Image.new('RGB', (100, 100), color='white')
        output = io.BytesIO()
        img.save(output, format='JPEG')
        image_data = output.getvalue()
        
        # Intentar subir
        result = cloudinary.uploader.upload(
            image_data,
            public_id=f"test_manual_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
            folder="tustockya/test"
        )
        
        return {
            "success": True,
            "result": result,
            "url": result["secure_url"]
        }
        
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "type": type(e).__name__
        }


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
    test_cloudinary_manually()
    
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