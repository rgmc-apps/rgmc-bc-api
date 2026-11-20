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
    # The Sales Header's Location Code — the frontend sends the selected
    # customer's own Location Code here (Customer table "Location Code"),
    # not a user-entered value.
    locationCode: Optional[str] = None
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


class FoodCameraErrorReport(BaseModel):
    """A client-side camera-unavailable failure, reported by the frontend's
    useCameraTroubleshoot composable so the developer gets an email instead
    of the failure only ever being visible on the user's own device. Fields
    are whatever the browser could gather — all optional, since availability
    varies by browser/OS (e.g. the Permissions API isn't in Safari)."""
    feature: str  # "barcode-scanner" | "ocr-order-number"
    errorName: Optional[str] = None  # DOMException.name, e.g. "NotAllowedError"
    errorMessage: Optional[str] = None  # DOMException.message / raw error text
    userFacingMessage: Optional[str] = None  # the troubleshooting text shown to the user
    permissionState: Optional[str] = None  # navigator.permissions camera query result, if supported
    mediaDevicesSupported: Optional[bool] = None
    cameraDeviceCount: Optional[int] = None  # videoinput entries from enumerateDevices(), if queryable
    isSecureContext: Optional[bool] = None
    url: Optional[str] = None
    userAgent: Optional[str] = None
    platform: Optional[str] = None
    language: Optional[str] = None
    screenResolution: Optional[str] = None
    viewportSize: Optional[str] = None
    online: Optional[bool] = None
    appVersion: Optional[str] = None
    username: Optional[str] = None
    userDisplayName: Optional[str] = None
    companyCode: Optional[str] = None
    clientTimestamp: Optional[str] = None  # ISO timestamp from the device's own clock


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
