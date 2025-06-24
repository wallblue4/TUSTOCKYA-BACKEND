# app/services/inventory/inventory_service.py
from typing import Dict, List
from sqlalchemy.orm import Session
from sqlalchemy import and_

from app.models.inventory import Inventory
from app.models.sneaker import SneakerReference
from app.models.user import Location

class InventoryService:
    
    async def get_inventory_by_reference(
        self, 
        reference_code: str, 
        user_location_id: int,
        db: Session
    ) -> Dict:
        """Obtener inventario completo para una referencia"""
        
        # Buscar referencia del tenis
        sneaker_ref = db.query(SneakerReference).filter(
            SneakerReference.reference_code == reference_code
        ).first()
        
        if not sneaker_ref:
            return self._empty_inventory_response()
        
        # Inventario local
        local_inventory = db.query(Inventory).filter(
            and_(
                Inventory.sneaker_reference_id == sneaker_ref.id,
                Inventory.location_id == user_location_id
            )
        ).all()
        
        # Inventario en otras ubicaciones
        other_inventory = db.query(Inventory, Location).join(
            Location, Inventory.location_id == Location.id
        ).filter(
            and_(
                Inventory.sneaker_reference_id == sneaker_ref.id,
                Inventory.location_id != user_location_id,
                Inventory.quantity_stock > 0
            )
        ).all()
        
        return {
            "reference": {
                "code": sneaker_ref.reference_code,
                "brand": sneaker_ref.brand,
                "model": sneaker_ref.model,
                "color": sneaker_ref.color,
                "image_url": sneaker_ref.image_url,
                "description": sneaker_ref.description
            },
            "local_stock": self._format_local_inventory(local_inventory),
            "total_stock": sum(inv.quantity_stock for inv in local_inventory),
            "available_sizes": [inv.size for inv in local_inventory if inv.quantity_stock > 0],
            "other_locations": self._format_other_locations(other_inventory),
            "prices": {
                "unit_price": float(local_inventory[0].unit_price) if local_inventory and local_inventory[0].unit_price else 0,
                "box_price": float(local_inventory[0].box_price) if local_inventory and local_inventory[0].box_price else 0
            }
        }
    
    def _format_local_inventory(self, inventory_list: List[Inventory]) -> List[Dict]:
        """Formatear inventario local"""
        return [
            {
                "size": inv.size,
                "stock_quantity": inv.quantity_stock,
                "exhibition_quantity": inv.quantity_exhibition,
                "unit_price": float(inv.unit_price) if inv.unit_price else 0,
                "box_price": float(inv.box_price) if inv.box_price else 0,
                "location": f"Local #{inv.location_id}"
            }
            for inv in inventory_list
        ]
    
    def _format_other_locations(self, other_inventory) -> List[Dict]:
        """Formatear otras ubicaciones"""
        locations_data = {}
        
        for inv, location in other_inventory:
            location_key = location.name
            if location_key not in locations_data:
                locations_data[location_key] = {
                    "location_id": location.id,
                    "location_name": location.name,
                    "location_type": location.type.value,
                    "available_sizes": []
                }
            
            locations_data[location_key]["available_sizes"].append({
                "size": inv.size,
                "quantity": inv.quantity_stock
            })
        
        return list(locations_data.values())
    
    def _empty_inventory_response(self) -> Dict:
        """Respuesta vacía"""
        return {
            "reference": None,
            "local_stock": [],
            "total_stock": 0,
            "available_sizes": [],
            "other_locations": [],
            "prices": {"unit_price": 0, "box_price": 0}
        }