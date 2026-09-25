"""RGMC custom API v2.0 — Warehouse Activity Header/Line Pydantic models (Pag50351 / Pag50352)."""
from typing import Any, Dict, List, Optional
from pydantic import BaseModel


class RgmcWarehouseActivityHeaderCreate(BaseModel):
    activityType: Optional[str] = None
    no: Optional[str] = None
    locationCode: Optional[str] = None
    assignedUserId: Optional[str] = None
    sortingMethod: Optional[str] = None
    postingDate: Optional[str] = None
    destinationType: Optional[str] = None
    destinationNo: Optional[str] = None
    externalDocumentNo: Optional[str] = None
    expectedReceiptDate: Optional[str] = None
    shipmentDate: Optional[str] = None
    lines: Optional[List[Dict[str, Any]]] = None


class RgmcWarehouseActivityHeaderUpdate(BaseModel):
    locationCode: Optional[str] = None
    assignedUserId: Optional[str] = None
    sortingMethod: Optional[str] = None
    postingDate: Optional[str] = None
    destinationType: Optional[str] = None
    destinationNo: Optional[str] = None
    externalDocumentNo: Optional[str] = None
    expectedReceiptDate: Optional[str] = None
    shipmentDate: Optional[str] = None


class RgmcWarehouseActivityLineCreate(BaseModel):
    locationCode: Optional[str] = None
    binCode: Optional[str] = None
    zoneCode: Optional[str] = None
    itemNo: Optional[str] = None
    variantCode: Optional[str] = None
    unitOfMeasureCode: Optional[str] = None
    description: Optional[str] = None
    description2: Optional[str] = None
    quantity: Optional[float] = None
    qtyToHandle: Optional[float] = None
    actionType: Optional[str] = None
    dueDate: Optional[str] = None
    lotNo: Optional[str] = None
    serialNo: Optional[str] = None
    packageNo: Optional[str] = None


class RgmcWarehouseActivityLineUpdate(RgmcWarehouseActivityLineCreate):
    pass
