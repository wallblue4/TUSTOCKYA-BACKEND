# app/services/classification/clip_service.py
import asyncio
import pickle
import hashlib
from concurrent.futures import ThreadPoolExecutor
from typing import List, Dict, Optional
import numpy as np
import torch
import clip
from PIL import Image
import cv2
import os

from app.core.config import settings
from app.core.database import get_redis
from app.services.classification.pinecone_service import pinecone_service

class CLIPClassificationService:
    _instance = None
    _initialized = False
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        if not self._initialized:
            self.device = "cuda" if torch.cuda.is_available() else "cpu"
            self.model = None
            self.preprocess = None
            self.executor = ThreadPoolExecutor(max_workers=2)
            self.redis_client = get_redis()
            self._load_model()
            CLIPClassificationService._initialized = True
    
    def _load_model(self):
        """Cargar CLIP ViT-L/14"""
        print(f"🔥 Cargando {settings.CLIP_MODEL_NAME} en {self.device}")
        self.model, self.preprocess = clip.load(settings.CLIP_MODEL_NAME, device=self.device)
        print("✅ Modelo CLIP cargado")
        
        if self.device == "cuda":
            print(f"🚀 GPU: {torch.cuda.get_device_name()}")
    
    def _get_image_hash(self, image_path: str) -> str:
        """Hash único para imagen"""
        with open(image_path, 'rb') as f:
            return hashlib.md5(f.read()).hexdigest()
    
    def _optimize_image(self, image_path: str) -> str:
        """Optimizar imagen"""
        image = cv2.imread(image_path)
        height, width = image.shape[:2]
        
        if max(height, width) > 512:
            scale = 512 / max(height, width)
            new_width = int(width * scale)
            new_height = int(height * scale)
            image = cv2.resize(image, (new_width, new_height))
        
        optimized_path = image_path.replace('.jpg', '_opt.jpg')
        cv2.imwrite(optimized_path, image, [cv2.IMWRITE_JPEG_QUALITY, 85])
        return optimized_path
    
    def _generate_embedding_sync(self, image_path: str) -> np.ndarray:
        """Generar embedding 768D"""
        try:
            opt_image_path = self._optimize_image(image_path)
            
            image = Image.open(opt_image_path).convert("RGB")
            image_input = self.preprocess(image).unsqueeze(0).to(self.device)
            
            with torch.no_grad():
                image_features = self.model.encode_image(image_input).float()
                embedding = image_features.cpu().numpy()[0]
            
            if opt_image_path != image_path and os.path.exists(opt_image_path):
                os.unlink(opt_image_path)
            
            assert embedding.shape == (768,), f"Shape incorrecto: {embedding.shape}"
            return embedding
        except Exception as e:
            print(f"❌ Error generando embedding: {e}")
            return None
    
    async def generate_embedding(self, image_path: str, use_cache: bool = True) -> Optional[np.ndarray]:
        """Generar embedding async"""
        if use_cache and self.redis_client:
            image_hash = self._get_image_hash(image_path)
            cache_key = f"embedding:{image_hash}"
            
            try:
                cached = self.redis_client.get(cache_key)
                if cached:
                    return pickle.loads(cached)
            except:
                pass
        
        loop = asyncio.get_event_loop()
        embedding = await loop.run_in_executor(
            self.executor,
            self._generate_embedding_sync,
            image_path
        )
        
        if use_cache and embedding is not None and self.redis_client:
            try:
                cache_data = pickle.dumps(embedding)
                self.redis_client.setex(cache_key, 3600, cache_data)
            except:
                pass
        
        return embedding
    
    async def classify_sneaker(self, image_path: str, top_k: int = 5) -> List[Dict]:
        """Clasificar con Pinecone"""
        try:
            print("🔍 Generando embedding...")
            embedding = await self.generate_embedding(image_path)
            
            if embedding is None:
                return []
            
            print(f"✅ Embedding generado: {embedding.shape}")
            
            print("🔎 Buscando en Pinecone...")
            search_results = await pinecone_service.search_similar(
                query_embedding=embedding,
                top_k=top_k
            )
            
            if not search_results:
                return []
            
            # Formatear resultados
            formatted_results = []
            for result in search_results:
                metadata = result.get("metadata", {})
                
                formatted_result = {
                    "rank": result["rank"],
                    "similarity_score": result["similarity_score"],
                    "confidence_percentage": result["confidence_percentage"],
                    "model_name": metadata.get("reference_code", "unknown"),
                    "brand": metadata.get("brand", "Unknown"),
                    "color": metadata.get("color", "N/A"),
                    "description": metadata.get("description", ""),
                    "image_url": metadata.get("image_url", None)
                }
                formatted_results.append(formatted_result)
            
            print(f"✅ {len(formatted_results)} resultados")
            return formatted_results
        except Exception as e:
            print(f"❌ Error clasificación: {e}")
            return []
    
    def health_check(self) -> Dict:
        """Estado del servicio"""
        pinecone_health = pinecone_service.health_check()
        
        return {
            "clip_model_loaded": self.model is not None,
            "clip_model_name": settings.CLIP_MODEL_NAME,
            "device": self.device,
            "embedding_dimensions": settings.EMBEDDING_DIMENSIONS,
            "cache_available": self.redis_client.ping() if self.redis_client else False,
            "pinecone_status": pinecone_health
        }

# Singleton
clip_service = CLIPClassificationService()