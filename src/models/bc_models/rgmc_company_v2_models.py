"""Pydantic models for RGMC custom API v2.0 Company endpoints."""
from typing import Optional
from pydantic import BaseModel, ConfigDict


class RgmcCompanyV2Response(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: Optional[str] = None
    name: Optional[str] = None
    displayName: Optional[str] = None
    consignmentAppVisible: Optional[bool] = None


class RgmcCompanyV2Update(BaseModel):
    consignmentAppVisible: Optional[bool] = None
