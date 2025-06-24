# app/schemas/classification.py
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
from datetime import datetime

class SneakerReference(BaseModel):
    code: str
    brand: str
    model: str
    color: Optional[str] = None
    image_url: Optional[str] = None
    description: Optional[str] = None

class ClassificationResult(BaseModel):
    rank: int
    similarity_score: float
    confidence_percentage: float
    confidence_level: str
    reference: SneakerReference
    inventory: Dict[str, Any]
    availability: Dict[str, Any]

class ScanResponse(BaseModel):
    success: bool
    scan_timestamp: datetime
    user_location: str
    best_match: Optional[ClassificationResult] = None
    alternative_matches: List[ClassificationResult] = []
    total_matches_found: int
    processing_time_ms: float
    error: Optional[str] = None
    suggestions: Optional[List[str]] = None