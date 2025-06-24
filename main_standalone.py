# main_standalone.py - Versión completa con todos los requerimientos del vendedor
import sys
import os
import sqlite3
import tempfile
import random
import asyncio
from datetime import datetime, timedelta

from fastapi import FastAPI, HTTPException, status, File, UploadFile, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from passlib.context import CryptContext
from jose import jwt

# ==================== CONFIGURACIÓN PARA RAILWAY ====================

# Variables de entorno para Railway
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./data/tustockya.db")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
SECRET_KEY = os.getenv("SECRET_KEY", "super-secret-key-cambia-en-produccion")
PORT = int(os.getenv("PORT", "10000"))  # Render usa puerto 10000 por defecto


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
# ==================== SCHEMAS ====================

class UserLogin(BaseModel):
    email: str
    password: str

# Schemas para métodos de pago
class PaymentMethod(BaseModel):
    type: str  # 'efectivo', 'tarjeta', 'transferencia', 'mixto'
    amount: float
    reference: str = None  # Número de tarjeta (últimos 4), referencia transferencia, etc.

# Schemas para módulo vendedor completo
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
    pickup_type: str  # 'vendedor' o 'corredor'
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

# ==================== CONFIGURACIÓN FASTAPI ====================

app = FastAPI(
    title="TuStockYa Backend - Railway Ready",
    version="1.0.0",
    docs_url="/docs",
    description="Sistema completo para gestión de inventario de tenis con módulo vendedor completo - Railway Compatible"
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
            "Dashboard completo del vendedor"
        ]
    }

@app.get("/health")
async def health():
    try:
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
        "database": f"SQLite ({db_status})" if DATABASE_URL.startswith("sqlite") else f"PostgreSQL ({db_status})",
        "users": user_count,
        "tables": len(tables),
        "table_list": tables,
        "port": PORT,
        "upload_dir": upload_dir,
        "redis_available": bool(os.getenv("REDIS_URL")),
        "modules": [
            "Autenticación",
            "Clasificación con CLIP",
            "Módulo Vendedor Completo",
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

@app.post("/api/v1/classify/scan")
async def scan_sneaker(
    image: UploadFile = File(...),
    current_user = Depends(get_current_user)
):
    """Escanear tenis - Obtener información completa según requerimientos"""
    
    start_time = datetime.now()
    
    if not image.content_type or not image.content_type.startswith('image/'):
        raise HTTPException(status_code=400, detail="El archivo debe ser una imagen")
    
    content = await image.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Archivo muy grande (máximo 10MB)")
    
    await asyncio.sleep(0.5)  # Simular procesamiento CLIP
    
    # Mock results con información completa según requerimientos
    mock_results = [
        {
            "rank": 1,
            "similarity_score": 0.94,
            "confidence_percentage": 94.0,
            "confidence_level": "muy_alta",
            "reference": {
                "code": "NK-AM90-WHT-001",
                "brand": "Nike",
                "model": "Air Max 90",
                "color": "Blanco/Negro",
                "description": "Nike Air Max 90 clásico en colorway blanco con detalles negros",
                "photo": "https://storage.tustockya.com/sneakers/nike-am90-001.jpg"
            },
            "inventory": {
                # Información según requerimientos: ubicación(local #), precio unidad, precio por caja, talla, cantidad, exhibición
                "local_info": {
                    "location_number": current_user['location_id'],
                    "location_name": "Local Principal"
                },
                "pricing": {
                    "unit_price": 120.0,
                    "box_price": 110.0
                },
                "stock_by_size": [
                    {
                        "size": "8.5",
                        "quantity_stock": 3,
                        "quantity_exhibition": 1,
                        "location": f"Local #{current_user['location_id']}"
                    },
                    {
                        "size": "9.0", 
                        "quantity_stock": 5,
                        "quantity_exhibition": 2,
                        "location": f"Local #{current_user['location_id']}"
                    },
                    {
                        "size": "9.5",
                        "quantity_stock": 2,
                        "quantity_exhibition": 0,
                        "location": f"Local #{current_user['location_id']}"
                    }
                ],
                "total_stock": 10,
                "total_exhibition": 3,
                "available_sizes": ["8.5", "9.0", "9.5"],
                "other_locations": [
                    {
                        "location_id": 2,
                        "location_name": "Local Norte",
                        "location_number": 2,
                        "available_stock": [
                            {"size": "10.0", "quantity": 4, "exhibition": 1},
                            {"size": "10.5", "quantity": 2, "exhibition": 0}
                        ]
                    },
                    {
                        "location_id": 3,
                        "location_name": "Bodega Central",
                        "location_number": 3,
                        "available_stock": [
                            {"size": "8.0", "quantity": 8, "exhibition": 0},
                            {"size": "11.0", "quantity": 3, "exhibition": 0}
                        ]
                    }
                ]
            },
            "availability": {
                "in_stock": True,
                "can_sell": True,
                "can_request_from_other_locations": True,
                "recommended_action": "Venta disponible en stock local"
            }
        },
        {
            "rank": 2,
            "similarity_score": 0.87,
            "confidence_percentage": 87.0,
            "confidence_level": "alta",
            "reference": {
                "code": "AD-UB22-BLK-001",
                "brand": "Adidas",
                "model": "Ultraboost 22",
                "color": "Negro",
                "description": "Adidas Ultraboost 22 en colorway triple negro",
                "photo": "https://storage.tustockya.com/sneakers/adidas-ub22-001.jpg"
            },
            "inventory": {
                "local_info": {
                    "location_number": current_user['location_id'],
                    "location_name": "Local Principal"
                },
                "pricing": {
                    "unit_price": 180.0,
                    "box_price": 170.0
                },
                "stock_by_size": [
                    {
                        "size": "9.0",
                        "quantity_stock": 0,
                        "quantity_exhibition": 1,
                        "location": f"Local #{current_user['location_id']}"
                    }
                ],
                "total_stock": 0,
                "total_exhibition": 1,
                "available_sizes": [],
                "other_locations": [
                    {
                        "location_id": 3,
                        "location_name": "Bodega Central",
                        "location_number": 3,
                        "available_stock": [
                            {"size": "8.5", "quantity": 5, "exhibition": 0},
                            {"size": "9.0", "quantity": 3, "exhibition": 0},
                            {"size": "9.5", "quantity": 3, "exhibition": 0}
                        ]
                    }
                ]
            },
            "availability": {
                "in_stock": False,
                "can_sell": False,
                "can_request_from_other_locations": True,
                "recommended_action": "Solicitar transferencia de Bodega Central"
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
        "alternative_matches": mock_results[1:],
        "total_matches_found": len(mock_results),
        "processing_time_ms": round(processing_time, 2),
        "image_info": {
            "filename": image.filename,
            "size_bytes": len(content),
            "content_type": image.content_type
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

# ==================== MÓDULO VENDEDOR COMPLETO ====================

# DASHBOARD COMPLETO DEL VENDEDOR
@app.get("/api/v1/vendor/dashboard")
async def get_vendor_dashboard_complete(current_user = Depends(get_current_user)):
    """Dashboard completo del vendedor con todas las funcionalidades según requerimientos"""
    
    if current_user['role'] not in ['vendedor', 'administrador']:
        raise HTTPException(status_code=403, detail="Acceso denegado")
    
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    
    try:
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
        unread_returns = cursor.fetchone()[0]
        
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
    finally:
        conn.close()

# UBICACIONES
@app.get("/api/v1/locations")
async def get_locations(current_user = Depends(get_current_user)):
    """Obtener todas las ubicaciones disponibles para transferencias"""
    
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    
    cursor = conn.execute(
        '''SELECT *, 
           CASE 
             WHEN id = ? THEN 1 
             ELSE 0 
           END as is_current_location
           FROM locations 
           WHERE is_active = TRUE
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
    sale_data: SaleCreateComplete,
    current_user = Depends(get_current_user)
):
    """Registrar una nueva venta completa con métodos de pago según requerimientos"""
    
    if current_user['role'] not in ['vendedor', 'administrador']:
        raise HTTPException(status_code=403, detail="Solo vendedores pueden registrar ventas")
    
    # Validar que los métodos de pago sumen el total
    total_payments = sum(payment.amount for payment in sale_data.payment_methods)
    if abs(total_payments - sale_data.total_amount) > 0.01:  # Tolerancia de 1 centavo
        raise HTTPException(
            status_code=400, 
            detail=f"Los métodos de pago (${total_payments:.2f}) no coinciden con el total (${sale_data.total_amount:.2f})"
        )
    
    conn = sqlite3.connect(DB_PATH)
    
    try:
        # Crear la venta con hora de venta y confirmación
        sale_timestamp = datetime.now().isoformat()
        cursor = conn.execute(
            '''INSERT INTO sales (seller_id, location_id, total_amount, receipt_image, notes, 
                                requires_confirmation, confirmed, confirmed_at, sale_date)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (current_user['id'], current_user['location_id'], sale_data.total_amount, 
             sale_data.receipt_image, sale_data.notes, sale_data.requires_confirmation,
             not sale_data.requires_confirmation,  # Si no requiere confirmación, ya está confirmada
             None if sale_data.requires_confirmation else sale_timestamp,
             sale_timestamp)
        )
        sale_id = cursor.lastrowid
        
        # Crear los métodos de pago
        for payment in sale_data.payment_methods:
            conn.execute(
                '''INSERT INTO sale_payments (sale_id, payment_type, amount, reference)
                   VALUES (?, ?, ?, ?)''',
                (sale_id, payment.type, payment.amount, payment.reference)
            )
        
        # Crear los items de la venta
        for item in sale_data.items:
            subtotal = item['quantity'] * item['unit_price']
            conn.execute(
                '''INSERT INTO sale_items (sale_id, sneaker_reference_code, brand, model, color, 
                                         size, quantity, unit_price, subtotal)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (sale_id, item['sneaker_reference_code'], item['brand'], item['model'], 
                 item.get('color'), item['size'], item['quantity'], item['unit_price'], subtotal)
            )
        
        conn.commit()
        
        return {
            "success": True,
            "sale_id": sale_id,
            "message": "Venta registrada exitosamente",
            "sale_timestamp": sale_timestamp,  # Hora de la venta
            "total_amount": sale_data.total_amount,
            "items_count": len(sale_data.items),
            "payment_methods_count": len(sale_data.payment_methods),
            "payment_breakdown": [
                {"type": p.type, "amount": p.amount, "reference": p.reference} 
                for p in sale_data.payment_methods
            ],
            "status": "pending_confirmation" if sale_data.requires_confirmation else "confirmed",
            "requires_confirmation": sale_data.requires_confirmation,
            "has_receipt": bool(sale_data.receipt_image)
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
    
    if current_user['role'] not in ['vendedor', 'administrador']:
        raise HTTPException(status_code=403, detail="Solo vendedores pueden confirmar ventas")
    
    conn = sqlite3.connect(DB_PATH)
    
    # Verificar que la venta existe y pertenece al vendedor
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
    
    if current_user['role'] not in ['vendedor', 'administrador']:
        raise HTTPException(status_code=403, detail="Acceso denegado")
    
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    
    try:
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
            
            # Agregar información de estado
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
    finally:
        conn.close()

@app.get("/api/v1/sales/pending-confirmation")
async def get_pending_confirmation_sales(current_user = Depends(get_current_user)):
    """Obtener ventas pendientes de confirmación"""
    if current_user['role'] not in ['vendedor', 'administrador']:
        raise HTTPException(status_code=403, detail="Acceso denegado")
    
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    
    try:
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
        
        return {
            "success": True,
            "pending_sales": sales,
            "count": len(sales),
            "total_pending_amount": sum(sale['total_amount'] for sale in sales)
        }
    finally:
        conn.close()

# GASTOS
@app.post("/api/v1/expenses/create")
async def create_expense(
expense_data: ExpenseCreate,
current_user = Depends(get_current_user)
):
    """Registrar gasto (concepto del gasto, valor, comprobante) según requerimientos"""
    
    if current_user['role'] not in ['vendedor', 'administrador']:
        raise HTTPException(status_code=403, detail="Solo vendedores pueden registrar gastos")
    
    conn = sqlite3.connect(DB_PATH)
    
    expense_timestamp = datetime.now().isoformat()
    cursor = conn.execute(
        '''INSERT INTO expenses (user_id, location_id, concept, amount, receipt_image, notes, expense_date)
            VALUES (?, ?, ?, ?, ?, ?, ?)''',
        (current_user['id'], current_user['location_id'], expense_data.concept, 
            expense_data.amount, expense_data.receipt_image, expense_data.notes, expense_timestamp)
    )
    expense_id = cursor.lastrowid
    conn.commit()
    conn.close()
    print(f"✅ Gasto registrado: {expense_data.concept} ({expense_data.amount})")
    
    return {
        "success": True,
        "expense_id": expense_id,
        "message": "Gasto registrado exitosamente",
        "expense_timestamp": expense_timestamp,
        "expense_details": {
            "concept": expense_data.concept,
            "amount": expense_data.amount,
            "has_receipt": bool(expense_data.receipt_image),
            "notes": expense_data.notes
        },
        "registered_by": f"{current_user['first_name']} {current_user['last_name']}"
    }

@app.get("/api/v1/expenses/today")
async def get_today_expenses(current_user = Depends(get_current_user)):
    """Obtener gastos del día actual"""
    
    if current_user['role'] not in ['vendedor', 'administrador']:
        raise HTTPException(status_code=403, detail="Acceso denegado")
    
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
    
    if current_user['role'] not in ['vendedor', 'administrador']:
        raise HTTPException(status_code=403, detail="Solo vendedores pueden solicitar transferencias")
    
    conn = sqlite3.connect(DB_PATH)
    
    request_timestamp = datetime.now().isoformat()
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
                "description": "El mismo vendedor recogerá" if transfer_data.pickup_type == "vendedor" else "Un corredor recogerá"
            },
            "destination_storage": "Exhibición" if transfer_data.destination_type == "exhibicion" else "Bodega"
        },
        "status": "pending",
        "next_steps": [
            "Esperando aceptación del bodeguero",
            f"{'Vendedor' if transfer_data.pickup_type == 'vendedor' else 'Corredor'} será notificado para recolección",
            "Transferencia será registrada al completarse"
        ]
    }

@app.get("/api/v1/transfers/my-requests")
async def get_my_transfer_requests(current_user = Depends(get_current_user)):
    """Obtener mis solicitudes de transferencia"""
    
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
            "pickup_person": "El mismo vendedor" if request['pickup_type'] == "vendedor" else "Corredor",
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
    
    if current_user['role'] not in ['vendedor', 'administrador']:
        raise HTTPException(status_code=403, detail="Solo vendedores pueden solicitar descuentos")
    
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
    
    conn = sqlite3.connect(DB_PATH)
    
    request_timestamp = datetime.now().isoformat()
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
    
    if current_user['role'] not in ['vendedor', 'administrador']:
        raise HTTPException(status_code=403, detail="Solo vendedores pueden solicitar devoluciones")
    
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

    # ==================== FUNCIONES AUXILIARES PARA TESTING ====================

@app.post("/api/v1/admin/create-test-data")
async def create_test_data(current_user = Depends(get_current_user)):
    """Crear datos de prueba para testing (solo para desarrollo)"""
        
    if current_user['role'] != 'administrador':
        raise HTTPException(status_code=403, detail="Solo administradores pueden crear datos de prueba")
    
    conn = sqlite3.connect(DB_PATH)
    
    try:
        # Crear una venta de prueba
        sale_timestamp = datetime.now().isoformat()
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
            conn.execute(
                '''INSERT INTO sale_payments (sale_id, payment_type, amount, reference)
                    VALUES (?, ?, ?, ?)''',
                (sale_id, *payment)
            )
        
        # Gasto de prueba
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
            role VARCHAR(50) NOT NULL DEFAULT 'vendedor',
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
                    "email": "vendedor@tustockya.com",
                    "password": "vendedor123",
                    "first_name": "Juan",
                    "last_name": "Vendedor",
                    "role": "vendedor"
                },
                {
                    "email": "vendedor2@tustockya.com",
                    "password": "vendedor123",
                    "first_name": "María",
                    "last_name": "González",
                    "role": "vendedor"
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