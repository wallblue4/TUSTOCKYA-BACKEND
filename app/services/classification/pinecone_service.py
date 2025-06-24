# app/services/classification/pinecone_service.py
import asyncio
import numpy as np
from typing import List, Dict, Optional, Any
from pinecone import Pinecone, ServerlessSpec
import logging

from app.core.config import settings

logger = logging.getLogger(__name__)

class PineconeService:
    _instance = None
    _initialized = False
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        if not self._initialized:
            self.pc = None
            self.index = None
            self._initialize_pinecone()
            PineconeService._initialized = True
    
    def _initialize_pinecone(self):
        """Inicializar conexión a Pinecone"""
        try:
            self.pc = Pinecone(api_key=settings.PINECONE_API_KEY)
            self._ensure_index_exists()
            self.index = self.pc.Index(settings.PINECONE_INDEX_NAME)
            logger.info(f"✅ Pinecone inicializado: {settings.PINECONE_INDEX_NAME}")
        except Exception as e:
            logger.error(f"❌ Error inicializando Pinecone: {e}")
            raise
    
    def _ensure_index_exists(self):
        """Crear índice si no existe"""
        try:
            existing_indexes = [index.name for index in self.pc.list_indexes()]
            
            if settings.PINECONE_INDEX_NAME not in existing_indexes:
                logger.info(f"📝 Creando índice: {settings.PINECONE_INDEX_NAME}")
                
                self.pc.create_index(
                    name=settings.PINECONE_INDEX_NAME,
                    dimension=settings.EMBEDDING_DIMENSIONS,
                    metric="cosine",
                    spec=ServerlessSpec(
                        cloud="aws",
                        region=settings.PINECONE_ENVIRONMENT
                    )
                )
                logger.info("✅ Índice creado exitosamente")
            else:
                logger.info(f"✅ Índice {settings.PINECONE_INDEX_NAME} ya existe")
        except Exception as e:
            logger.error(f"❌ Error con índice: {e}")
            raise
    
    async def search_similar(
        self, 
        query_embedding: np.ndarray, 
        top_k: int = 5
    ) -> List[Dict]:
        """Buscar embeddings similares"""
        try:
            if isinstance(query_embedding, np.ndarray):
                query_embedding = query_embedding.tolist()
            
            if len(query_embedding) != settings.EMBEDDING_DIMENSIONS:
                raise ValueError(f"Dimensiones incorrectas: {len(query_embedding)}")
            
            search_results = self.index.query(
                vector=query_embedding,
                top_k=top_k,
                include_metadata=True,
                include_values=False
            )
            
            formatted_results = []
            for i, match in enumerate(search_results.matches):
                result = {
                    "rank": i + 1,
                    "id": match.id,
                    "similarity_score": float(match.score),
                    "confidence_percentage": float(match.score * 100),
                    "metadata": match.metadata
                }
                formatted_results.append(result)
            
            logger.info(f"🔍 Búsqueda completada: {len(formatted_results)} resultados")
            return formatted_results
        except Exception as e:
            logger.error(f"❌ Error en búsqueda: {e}")
            return []
    
    async def upsert_embedding(
        self, 
        vector_id: str, 
        embedding: np.ndarray, 
        metadata: Dict[str, Any]
    ) -> bool:
        """Insertar embedding"""
        try:
            if isinstance(embedding, np.ndarray):
                embedding = embedding.tolist()
            
            if len(embedding) != settings.EMBEDDING_DIMENSIONS:
                raise ValueError(f"Dimensiones incorrectas: {len(embedding)}")
            
            self.index.upsert(
                vectors=[{
                    "id": vector_id,
                    "values": embedding,
                    "metadata": metadata
                }]
            )
            
            logger.info(f"✅ Embedding insertado: {vector_id}")
            return True
        except Exception as e:
            logger.error(f"❌ Error insertando {vector_id}: {e}")
            return False
    
    def health_check(self) -> Dict:
        """Verificar estado"""
        try:
            stats = self.index.describe_index_stats()
            return {
                "pinecone_connected": True,
                "index_name": settings.PINECONE_INDEX_NAME,
                "total_vectors": stats.total_vector_count,
                "dimension": stats.dimension
            }
        except Exception as e:
            return {
                "pinecone_connected": False,
                "error": str(e)
            }

# Singleton
pinecone_service = PineconeService()