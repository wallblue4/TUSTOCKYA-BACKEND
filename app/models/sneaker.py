# app/models/sneaker.py
from sqlalchemy import Column, Integer, String, Text, DateTime, Enum
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
import enum
from app.core.database import Base

class Gender(enum.Enum):
    hombre = "hombre"
    mujer = "mujer"
    unisex = "unisex"

class SneakerReference(Base):
    __tablename__ = "sneaker_references"
    
    id = Column(Integer, primary_key=True, index=True)
    reference_code = Column(String(50), unique=True, nullable=False, index=True)
    brand = Column(String(50), nullable=False)
    model = Column(String(100), nullable=False)
    color = Column(String(50))
    gender = Column(Enum(Gender), default=Gender.unisex)
    image_url = Column(Text)
    description = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
    # Relationships
    inventory = relationship("Inventory", back_populates="sneaker_reference")