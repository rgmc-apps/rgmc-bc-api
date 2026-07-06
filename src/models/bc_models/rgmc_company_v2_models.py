"""Pydantic models for RGMC custom API v2.0 Company endpoints."""
from typing import Optional
from pydantic import BaseModel


class RgmcCompanyV2Update(BaseModel):
    consignmentAppVisible: Optional[bool] = None
