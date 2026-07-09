"""RGMC custom API v2.0 — Contact endpoints (Pag50308 / Pag50309)."""
import base64
import logging
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, File, HTTPException, Query, UploadFile, status
from fastapi.responses import Response
from src.services.bc_functions import (
    call_rgmc_v2_table,
    rgmc_v2_get_record,
    rgmc_v2_create_record,
    rgmc_v2_update_record,
    rgmc_v2_delete_record,
    rgmc_v2_get_contact_picture,
    rgmc_v2_update_contact_picture,
)
from src.models.bc_models.rgmc_contact_models import RgmcContactCreate, RgmcContactUpdate
from src import config

logger = logging.getLogger("bc_routes.rgmc_contacts_v2")

rgmc_contact_v2_router = APIRouter(prefix="/bc/custom/v2/contacts", tags=["BC RGMC Contacts v2"])

_TABLE = "contacts"

_IMAGE_SIGNATURES = [
    (b'\xff\xd8\xff', "image/jpeg"),
    (b'\x89PNG\r\n\x1a\n', "image/png"),
    (b'GIF87a', "image/gif"),
    (b'GIF89a', "image/gif"),
    (b'BM', "image/bmp"),
]
_MIN_IMAGE_BYTES = 64


def _unwrap_list(bc_result: tuple) -> List[Dict[str, Any]]:
    http_status, data = bc_result
    if http_status != 200:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Business Central returned {http_status}: {data}",
        )
    return data.get("value", data)


def _unwrap_single(http_status: int, data: Any, label: str = "Contact") -> Dict[str, Any]:
    if http_status == 404:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{label} not found")
    if http_status not in (200, 201):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Business Central returned {http_status}: {data}",
        )
    return data


def _detect_media_type(image_bytes: bytes) -> Optional[str]:
    for sig, mime in _IMAGE_SIGNATURES:
        if image_bytes[:len(sig)] == sig:
            return mime
    if image_bytes[:4] == b'RIFF' and image_bytes[8:12] == b'WEBP':
        return "image/webp"
    return None


@rgmc_contact_v2_router.get("", summary="List RGMC Contacts v2")
def list_rgmc_contacts_v2(
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
    filter: Optional[str] = Query(None, description="OData $filter expression"),
    select: Optional[str] = Query(None, description="OData $select"),
):
    try:
        result = call_rgmc_v2_table(_TABLE, company_name=company or config.BC_COMPANY, odata_filter=filter, select=select)
        return {"data": _unwrap_list(result)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing RGMC contacts v2: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@rgmc_contact_v2_router.get("/{contact_id}", summary="Get RGMC Contact v2 by ID")
def get_rgmc_contact_v2(
    contact_id: str,
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    try:
        http_status, data = rgmc_v2_get_record(_TABLE, contact_id, company_name=company or config.BC_COMPANY)
        return _unwrap_single(http_status, data)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching RGMC contact v2 {contact_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@rgmc_contact_v2_router.post("", summary="Create RGMC Contact v2", status_code=status.HTTP_201_CREATED)
def create_rgmc_contact_v2(
    body: RgmcContactCreate,
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    try:
        payload = body.model_dump(exclude_none=True)
        http_status, data = rgmc_v2_create_record(_TABLE, payload, company_name=company or config.BC_COMPANY)
        return _unwrap_single(http_status, data)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating RGMC contact v2: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@rgmc_contact_v2_router.patch("/{contact_id}", summary="Update RGMC Contact v2")
def update_rgmc_contact_v2(
    contact_id: str,
    body: RgmcContactUpdate,
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    try:
        payload = body.model_dump(exclude_none=True)
        if not payload:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No fields provided for update")
        http_status, data = rgmc_v2_update_record(_TABLE, contact_id, payload, company_name=company or config.BC_COMPANY)
        return _unwrap_single(http_status, data)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating RGMC contact v2 {contact_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@rgmc_contact_v2_router.delete("/{contact_id}", summary="Delete RGMC Contact v2", status_code=status.HTTP_204_NO_CONTENT)
def delete_rgmc_contact_v2(
    contact_id: str,
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    try:
        http_status = rgmc_v2_delete_record(_TABLE, contact_id, company_name=company or config.BC_COMPANY)
        if http_status == 404:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Contact not found")
        if http_status not in (204, 200):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Business Central returned {http_status}",
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting RGMC contact v2 {contact_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@rgmc_contact_v2_router.get("/{contact_id}/picture/debug", summary="Debug: Raw BC Picture Response v2")
def debug_contact_picture_v2(
    contact_id: str,
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    try:
        http_status, data = rgmc_v2_get_contact_picture(contact_id, company_name=company or config.BC_COMPANY)
        picture_b64 = (data.get("picture") or "") if isinstance(data, dict) else ""
        try:
            decoded = base64.b64decode(picture_b64) if picture_b64 else b""
            hex_head = decoded[:16].hex() if decoded else ""
            media_type = _detect_media_type(decoded)
        except Exception:
            decoded = b""
            hex_head = ""
            media_type = None
        return {
            "bc_http_status": http_status,
            "picture_b64_length": len(picture_b64),
            "picture_b64_prefix": picture_b64[:40],
            "decoded_bytes": len(decoded),
            "hex_header": hex_head,
            "detected_media_type": media_type,
            "bc_fields": list(data.keys()) if isinstance(data, dict) else None,
        }
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@rgmc_contact_v2_router.get("/{contact_id}/picture", summary="Get RGMC Contact Picture v2")
def get_contact_picture_v2(
    contact_id: str,
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    try:
        http_status, data = rgmc_v2_get_contact_picture(contact_id, company_name=company or config.BC_COMPANY)
        if http_status == 404:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Contact picture not found")
        if http_status != 200:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Business Central returned {http_status}: {data}")
        if not isinstance(data, dict):
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Unexpected response shape from Business Central: {str(data)[:200]}")
        picture_b64 = data.get("picture") or ""
        if not picture_b64:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No picture data on this contact record")
        image_bytes = base64.b64decode(picture_b64)
        if len(image_bytes) < _MIN_IMAGE_BYTES:
            logger.error(
                f"Picture for contact v2 {contact_id} decoded to only {len(image_bytes)} bytes — field may be truncated."
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"BC returned only {len(image_bytes)} decoded bytes — picture field is truncated in AL page 50309",
            )
        media_type = _detect_media_type(image_bytes) or "image/jpeg"
        return Response(content=image_bytes, media_type=media_type)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching picture for contact v2 {contact_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@rgmc_contact_v2_router.patch("/{contact_id}/picture", summary="Update RGMC Contact Picture v2")
async def update_contact_picture_v2(
    contact_id: str,
    file: UploadFile = File(...),
    company: Optional[str] = Query(None, description="BC company name (defaults to BC_COMPANY env var)"),
):
    try:
        image_bytes = await file.read()
        picture_b64 = base64.b64encode(image_bytes).decode("utf-8")
        upd_status, upd_data = rgmc_v2_update_contact_picture(contact_id, picture_b64, company_name=company or config.BC_COMPANY)
        if upd_status not in (200, 204):
            logger.warning(f"BC picture sync for contact v2 {contact_id} returned {upd_status}: {upd_data}.")
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating picture for contact v2 {contact_id}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
