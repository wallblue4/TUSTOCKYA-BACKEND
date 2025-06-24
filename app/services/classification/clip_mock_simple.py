# app/services/classification/clip_mock_simple.py
import asyncio
import random
from typing import List, Dict

class SimpleCLIPService:
    def __init__(self):
        self.device = "cpu"
        print("🔄 Usando servicio CLIP simulado")
    
    async def classify_sneaker(self, image_path: str, top_k: int = 5) -> List[Dict]:
        """Clasificación simulada"""
        await asyncio.sleep(0.3)  # Simular procesamiento
        
        mock_sneakers = [
            {
                "rank": 1,
                "similarity_score": 0.94,
                "confidence_percentage": 94.0,
                "model_name": "nike_air_max_90_white",
                "brand": "Nike",
                "color": "Blanco/Negro",
                "description": "Nike Air Max 90 Blanco con detalles negros"
            },
            {
                "rank": 2,
                "similarity_score": 0.87,
                "confidence_percentage": 87.0,
                "model_name": "adidas_ultraboost_22_black",
                "brand": "Adidas",
                "color": "Negro",
                "description": "Adidas Ultraboost 22 Negro"
            },
            {
                "rank": 3,
                "similarity_score": 0.79,
                "confidence_percentage": 79.0,
                "model_name": "jordan_1_retro_high_chicago",
                "brand": "Nike",
                "color": "Rojo/Blanco/Negro",
                "description": "Air Jordan 1 Retro High Chicago"
            }
        ]
        
        return mock_sneakers[:top_k]
    
    def health_check(self) -> Dict:
        return {
            "clip_model_loaded": True,
            "clip_model_name": "Mock ViT-L/14",
            "device": "cpu",
            "mode": "simulation"
        }

clip_service = SimpleCLIPService()