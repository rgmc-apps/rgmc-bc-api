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
    # Lot picked in the Add Items modal (FEFO default, user-overridable) —
    # written to Business Central as an Item Tracking Line (Reservation Entry)
    # once the sales line itself is created. lotNo absent = no tracking write.
    lotNo: Optional[str] = None
    expirationDate: Optional[str] = None
    # The lot's own physical location (from /food/items/{item}/lots) — a
    # lot-tracked item's Sales Line requires a Location Code at posting even
    # when the line doesn't reserve against separate supply; the location
    # must match where that specific lot's stock actually sits.
    locationCode: Optional[str] = None
    # Item's Qty. per Unit of Measure for unitOfMeasureCode — converts
    # `quantity` to the item's base UOM, which is what the tracking line's
    # quantityBase must be expressed in. Defaults to 1 if not supplied.
    qtyPerUnitOfMeasure: Optional[float] = None


class FoodSalesOrderCreate(BaseModel):
    customerNumber: str
    postingDate: str
    orderNumber: str
    submittedBy: Optional[str] = None
    lines: List[FoodSalesOrderLineCreate]


class FoodSalesOrderResult(BaseModel):
    documentNumber: str
    externalDocumentNo: str
    # Non-fatal: present only when one or more lines' lot could not be
    # written as a BC Item Tracking Line (e.g. the AL page isn't published
    # to this environment yet). The order itself still succeeded.
    trackingWarnings: Optional[List[str]] = None


class FoodOrderHistoryLine(BaseModel):
    itemNumber: str
    description: Optional[str] = None
    quantity: float
    unitOfMeasureCode: Optional[str] = None
    lotNo: Optional[str] = None
    expirationDate: Optional[str] = None
    locationCode: Optional[str] = None


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
