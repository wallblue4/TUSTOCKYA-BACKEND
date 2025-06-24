# app/models/inventory.py
from sqlalchemy import Column, Integer, String, DECIMAL, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.core.database import Base

class Inventory(Base):
    __tablename__ = "inventory"
    
    id = Column(Integer, primary_key=True, index=True)
    sneaker_reference_id = Column(Integer, ForeignKey("sneaker_references.id"), nullable=False)
    location_id = Column(Integer, ForeignKey("locations.id"), nullable=False)
    size = Column(String(10), nullable=False)
    quantity_stock = Column(Integer, default=0)
    quantity_exhibition = Column(Integer, default=0)
    unit_price = Column(DECIMAL(10, 2))
    box_price = Column(DECIMAL(10, 2))
    minimum_stock = Column(Integer, default=5)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    
    # Relationships
    sneaker_reference = relationship("SneakerReference", back_populates="inventory")
    location = relationship("Location", back_populates="inventory")
    
    # Constraint
    __table_args__ = (
        UniqueConstraint('sneaker_reference_id', 'location_id', 'size', name='_inventory_unique'),
    )