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
