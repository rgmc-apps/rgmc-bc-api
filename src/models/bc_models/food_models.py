"""SBIC Consignment Webapp — Food & Beverages: request/response models for the
dedicated /food/* router. This router is intentionally separate from the
garments-app RGMC v2 models — every endpoint here reads live from Business
Central and returns bounded, paginated pages (see bc_functions.rgmc_v2_list_table_live)."""
from typing import List, Optional
from pydantic import BaseModel


class FoodSalesOrderLineCreate(BaseModel):
    itemNumber: str
    description: Optional[str] = None
    quantity: float
    unitOfMeasureCode: Optional[str] = None


class FoodSalesOrderCreate(BaseModel):
    customerNumber: str
    postingDate: str
    orderNumber: str
    lines: List[FoodSalesOrderLineCreate]


class FoodSalesOrderResult(BaseModel):
    documentNumber: str
    externalDocumentNo: str


class FoodOrderHistoryLine(BaseModel):
    itemNumber: str
    description: Optional[str] = None
    quantity: float
    unitOfMeasureCode: Optional[str] = None


class FoodOrderHistoryRecord(BaseModel):
    """One order-submission attempt, written to Firestore whether it succeeded
    or failed — see services/food_order_history_service.py. Distinct from and
    unrelated to the garments app's session_history_{env} collection."""
    id: str
    username: str
    userDisplayName: Optional[str] = None
    companyCode: Optional[str] = None
    customerNumber: Optional[str] = None
    customerDisplayName: Optional[str] = None
    orderNumber: Optional[str] = None
    postingDate: Optional[str] = None
    status: str  # "success" | "failed"
    salesOrderNumber: Optional[str] = None
    errorMessage: Optional[str] = None
    lines: List[FoodOrderHistoryLine] = []
    createdAt: str
