# app/api/v1/classification_simple.py
import os
import time
import tempfile
import random
from datetime import datetime
from fastapi import APIRouter, File, UploadFile, HTTPException, status
from app.services.classification.clip_mock_simple import clip_service

router = APIRouter()

@router.post("/scan")
async def scan_sneaker(image: UploadFile = File(...)):
    """Escanear tenis"""
    
    start_time = time.time()
    
    if not image.content_type or not image.content_type.startswith('image/'):
        raise HTTPException(status_code=400, detail="Debe ser una imagen")
    
    temp_path = None
    try:
        content = await image.read()
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmp:
            tmp.write(content)
            temp_path = tmp.name
        
        results = await clip_service.classify_sneaker(temp_path, top_k=5)
        
        # Simular inventario
        for result in results:
            result['inventory'] = {
                'total_stock': random.randint(0, 10),
                'available_sizes': ['8.5', '9.0', '9.5', '10.0'],
                'local_stock': [{'size': '9.0', 'stock_quantity': 3, 'unit_price': 120.0}],
                'other_locations': []
            }
            result['availability'] = {
                'in_stock': result['inventory']['total_stock'] > 0,
                'available_sizes': result['inventory']['available_sizes']
            }
        
        processing_time = (time.time() - start_time) * 1000
        
        return {
            "success": True,
            "scan_timestamp": datetime.now().isoformat(),
            "user_location": "Local Principal",
            "best_match": results[0] if results else None,
            "alternative_matches": results[1:3] if len(results) > 1 else [],
            "total_matches_found": len(results),
            "processing_time_ms": processing_time
        }
        
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)

@router.get("/health")
async def health():
    return {
        "service": "classification", 
        "status": "healthy",
        "details": clip_service.health_check()
    }