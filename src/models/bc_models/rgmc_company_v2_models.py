"""Pydantic models for RGMC custom API v2.0 Company Settings endpoints (Pag50492)."""
from typing import Optional
from pydantic import BaseModel, ConfigDict


class RgmcCompanySettingResponse(BaseModel):
    """Response shape for companySettings (EntitySet: companySettings, Pag50492).
    extra='allow' preserves any additional BC fields not listed here."""
    model_config = ConfigDict(extra="allow")

    id: Optional[str] = None
    companyName: Optional[str] = None
    consignmentAppVisible: Optional[bool] = None


class RgmcCompanySettingUpdate(BaseModel):
    """Only consignmentAppVisible is ModifyAllowed on Pag50492."""
    consignmentAppVisible: Optional[bool] = None
