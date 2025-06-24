# app/api/v1/classification.py
import os
import time
import tempfile
import aiofiles
from datetime import datetime
from fastapi import APIRouter, File, UploadFile, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.config import settings
from app.models.user import User
from app.schemas.classification import ScanResponse, ClassificationResult
from app.services.classification.clip_service import clip_service
from app.services.inventory.inventory_service import InventoryService
from app.api.deps import get_current_user, get_vendedor

router = APIRouter()

def _get_confidence_level(similarity_score: float) -> str:
    """Determinar nivel de confianza"""
    if similarity_score >= 0.90:
        return "muy_alta"
    elif similarity_score >= 0.80:
        return "alta"
    elif similarity_score >= 0.65:
        return "media"
    else:
        return "baja"

@router.post("/scan", response_model=ScanResponse)
async def scan_sneaker(
    image: UploadFile = File(...),
    current_user: User = Depends(get_vendedor),
    db: Session = Depends(get_db)
):
    """
    🔍 Escanear tenis usando CLIP + Pinecone
    """
    start_time = time.time()
    
    # Validaciones
    if not image.content_type or not image.content_type.startswith('image/'):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El archivo debe ser una imagen (JPG, PNG, WEBP)"
        )
    
    # Validar tamaño
    content = await image.read()
    if len(content) > settings.MAX_FILE_SIZE:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Archivo muy grande. Máximo {settings.MAX_FILE_SIZE // 1024 // 1024}MB"
        )
    
    # Validar extensión
    file_extension = os.path.splitext(image.filename or "image.jpg")[1].lower()
    if file_extension not in settings.ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Extensión no permitida. Usar: {', '.join(settings.ALLOWED_EXTENSIONS)}"
        )
    
    temp_image_path = None
    
    try:
        # Guardar temporalmente
        with tempfile.NamedTemporaryFile(delete=False, suffix=file_extension) as tmp_file:
            tmp_file.write(content)
            temp_image_path = tmp_file.name
        
        print(f"🔍 Procesando imagen para: {current_user.email}")
        
        # Clasificar con CLIP + Pinecone
        classification_results = await clip_service.classify_sneaker(
            temp_image_path, 
            top_k=5
        )
        
        if not classification_results:
            processing_time = (time.time() - start_time) * 1000
            return ScanResponse(
                success=False,
                scan_timestamp=datetime.now(),
                user_location=current_user.location.name if current_user.location else "Sin ubicación",
                best_match=None,
                alternative_matches=[],
                total_matches_found=0,
                processing_time_ms=processing_time,
                error="No se pudo identificar el tenis en la imagen",
                suggestions=[
                    "Asegúrate de que el tenis esté bien iluminado",
                    "Toma la foto desde un ángulo lateral",
                    "Evita sombras o reflejos",
                    "Acerca más la cámara al tenis"
                ]
            )
        
        # Enriquecer con inventario
        inventory_service = InventoryService()
        enriched_results = []
        
        for result in classification_results:
            try:
                inventory_data = await inventory_service.get_inventory_by_reference(
                    reference_code=result['model_name'],
                    user_location_id=current_user.location_id or 1,
                    db=db
                )
                
                confidence_level = _get_confidence_level(result['similarity_score'])
                
                enriched_result = ClassificationResult(
                    rank=result['rank'],
                    similarity_score=result['similarity_score'],
                    confidence_percentage=result['confidence_percentage'],
                    confidence_level=confidence_level,
                    reference={
                        "code": result['model_name'],
                        "brand": result.get('brand', 'Desconocido'),
                        "model": result.get('model_name', 'N/A'),
                        "color": result.get('color', 'N/A'),
                        "image_url": result.get('image_url'),
                        "description": result.get('description', '')
                    },
                    inventory=inventory_data,
                    availability={
                        "in_stock": inventory_data['total_stock'] > 0,
                        "available_sizes": inventory_data['available_sizes'],
                        "can_request_from_other_locations": len(inventory_data['other_locations']) > 0
                    }
                )
                enriched_results.append(enriched_result)
                
            except Exception as e:
                print(f"⚠️ Error enriqueciendo {result['model_name']}: {e}")
                continue
        
        # Respuesta final
        processing_time = (time.time() - start_time) * 1000
        
        response = ScanResponse(
            success=True,
            scan_timestamp=datetime.now(),
            user_location=current_user.location.name if current_user.location else "Sin ubicación",
            best_match=enriched_results[0] if enriched_results else None,
            alternative_matches=enriched_results[1:4] if len(enriched_results) > 1 else [],
            total_matches_found=len(enriched_results),
            processing_time_ms=processing_time
        )
        
        print(f"✅ Clasificación completada en {processing_time:.0f}ms")
        return response
        
    except Exception as e:
        print(f"❌ Error en clasificación: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error procesando imagen: {str(e)}"
        )
    
    finally:
        # Limpiar archivo temporal
        if temp_image_path and os.path.exists(temp_image_path):
            try:
                os.unlink(temp_image_path)
            except:
                pass

@router.get("/health")
async def classification_health():
    """Estado del servicio de clasificación"""
    health_status = clip_service.health_check()
    
    return {
        "service": "classification",
        "status": "healthy" if health_status["clip_model_loaded"] and health_status["pinecone_status"]["pinecone_connected"] else "unhealthy",
        "details": health_status
    }

@router.post("/add-sneaker")
async def add_sneaker_to_database(
    image: UploadFile = File(...),
    reference_code: str,
    brand: str,
    model: str,
    color: str = "",
    description: str = "",
    current_user: User = Depends(get_admin),  # Solo admins pueden agregar
    db: Session = Depends(get_db)
):
    """Agregar nuevo tenis a la BD vectorial (Para admins)"""
    
    if not image.content_type.startswith('image/'):
        raise HTTPException(status_code=400, detail="Debe ser una imagen")
    
    # Verificar que no exista la referencia
    existing = db.query(SneakerReference).filter(
        SneakerReference.reference_code == reference_code
    ).first()
    
    if existing:
        raise HTTPException(status_code=400, detail="Referencia ya existe")
    
    temp_path = None
    try:
        # Guardar imagen temporalmente
        content = await image.read()
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmp:
            tmp.write(content)
            temp_path = tmp.name
        
        # Preparar metadata
        metadata = {
            "brand": brand,
            "model": model,
            "color": color,
            "description": description
        }
        
        # Agregar a Pinecone
        success = await clip_service.add_sneaker_to_database(
            image_path=temp_path,
            reference_code=reference_code,
            metadata=metadata
        )
        
        if not success:
            raise HTTPException(status_code=500, detail="Error agregando a Pinecone")
        
        # Agregar a BD relacional
        sneaker_ref = SneakerReference(
            reference_code=reference_code,
            brand=brand,
            model=model,
            color=color,
            description=description
        )
        
        db.add(sneaker_ref)
        db.commit()
        
        return {
            "message": "Tenis agregado exitosamente",
            "reference_code": reference_code,
            "added_by": current_user.email
        }
        
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)