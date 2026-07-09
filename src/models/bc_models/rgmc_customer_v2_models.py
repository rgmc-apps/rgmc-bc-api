"""Pydantic models for RGMC custom API v2.0 Customer endpoints."""
from typing import Optional
from pydantic import BaseModel, ConfigDict


class RgmcCustomerV2Response(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: Optional[str] = None
    number: Optional[str] = None
    displayName: Optional[str] = None
    type: Optional[str] = None
    addressLine1: Optional[str] = None
    addressLine2: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    country: Optional[str] = None
    postalCode: Optional[str] = None
    phoneNumber: Optional[str] = None
    email: Optional[str] = None
    website: Optional[str] = None
    salespersonCode: Optional[str] = None
    creditLimit: Optional[float] = None
    taxLiable: Optional[bool] = None
    taxAreaId: Optional[str] = None
    taxRegistrationNumber: Optional[str] = None
    currencyId: Optional[str] = None
    currencyCode: Optional[str] = None
    paymentTermsId: Optional[str] = None
    shipmentMethodId: Optional[str] = None
    paymentMethodId: Optional[str] = None
    blocked: Optional[str] = None


class RgmcCustomerV2Create(BaseModel):
    number: Optional[str] = None
    displayName: Optional[str] = None
    type: Optional[str] = None
    addressLine1: Optional[str] = None
    addressLine2: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    country: Optional[str] = None
    postalCode: Optional[str] = None
    phoneNumber: Optional[str] = None
    email: Optional[str] = None
    website: Optional[str] = None
    salespersonCode: Optional[str] = None
    creditLimit: Optional[float] = None
    taxLiable: Optional[bool] = None
    taxAreaId: Optional[str] = None
    taxRegistrationNumber: Optional[str] = None
    currencyId: Optional[str] = None
    currencyCode: Optional[str] = None
    paymentTermsId: Optional[str] = None
    shipmentMethodId: Optional[str] = None
    paymentMethodId: Optional[str] = None
    blocked: Optional[str] = None


class RgmcCustomerV2Update(BaseModel):
    displayName: Optional[str] = None
    type: Optional[str] = None
    addressLine1: Optional[str] = None
    addressLine2: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    country: Optional[str] = None
    postalCode: Optional[str] = None
    phoneNumber: Optional[str] = None
    email: Optional[str] = None
    website: Optional[str] = None
    salespersonCode: Optional[str] = None
    creditLimit: Optional[float] = None
    taxLiable: Optional[bool] = None
    taxAreaId: Optional[str] = None
    taxRegistrationNumber: Optional[str] = None
    currencyId: Optional[str] = None
    currencyCode: Optional[str] = None
    paymentTermsId: Optional[str] = None
    shipmentMethodId: Optional[str] = None
    paymentMethodId: Optional[str] = None
    blocked: Optional[str] = None
