"""Routers Init."""
from .health import healthrouter
from .bc_routes import (
    bc_router,
    sales_order_router,
    item_router,
    customer_router,
    sales_credit_memo_router,
    retail_customer_router,
    sales_return_order_router,
    rgmc_contact_router,
    item_category_router,
    rgmc_item_router,
    rgmc_item_family_router,
    rgmc_item_price_router,
    rgmc_item_price_v2_router,
    rgmc_sales_order_router,
    rgmc_company_v2_router,
)
