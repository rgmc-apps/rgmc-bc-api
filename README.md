<div align="center">

# <span style="color:#A07320">RGMC BC API</span>

<span style="color:#666">Microsoft Business Central integration layer for RGMC — exposing standard and custom AL page endpoints via a clean REST API.</span>

[![Python](https://img.shields.io/badge/Python-3.12.6-3776AB?logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.128.0-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Pydantic](https://img.shields.io/badge/Pydantic-2.12.5-E92063?logo=pydantic&logoColor=white)](https://docs.pydantic.dev)
[![Uvicorn](https://img.shields.io/badge/Uvicorn-0.40.0-499848?logo=gunicorn&logoColor=white)](https://www.uvicorn.org)
[![Docker](https://img.shields.io/badge/Docker-ready-2496ED?logo=docker&logoColor=white)](https://www.docker.com)

</div>

---

## 📋 Table of Contents

- [Overview](#-overview)
- [Tech Stack](#-tech-stack)
- [Features](#-features)
- [API Routes](#-api-routes)
- [Project Structure](#-project-structure)
- [Setup & Installation](#-setup--installation)
- [Environment Variables](#-environment-variables)
- [Running the App](#-running-the-app)
- [Building for Production](#-building-for-production)
- [API Endpoints](#-api-endpoints)
- [Data & Caching Strategy](#-data--caching-strategy)
- [Authentication Flow](#-authentication-flow)
- [Core Data Flow](#-core-data-flow)
- [Error Notification System](#-error-notification-system)
- [License](#-license)

---

## 🔍 Overview

**RGMC BC API** is a FastAPI-based REST gateway that wraps Microsoft Dynamics 365 Business Central's OData v4 API — both the standard `api/v2.0` surface and RGMC's custom AL page extensions (`api/rgmc/rgmccustom/v1.0`).

**Who uses it:** Internal RGMC applications (mobile sales apps, web portals) that need to read and write BC data without embedding OAuth2 token management or raw OData logic in the client.

**Key design decisions:**

- **Server-side token cache** — a single OAuth2 `client_credentials` token is fetched once per process and refreshed 60 seconds before expiry. Clients never touch Azure AD directly.
- **OData pagination handled centrally** — `@odata.nextLink` chaining is transparent to callers; every list endpoint returns the full dataset.
- **Dual API surface** — standard BC v2.0 endpoints (items, customers, sales orders, credit memos, item categories) plus RGMC custom AL pages (retail customers, contacts, return orders, item families, item prices, RGMC sales orders).
- **In-process item price cache** — price lookups are cached per (company, product\_no, date) key to reduce redundant BC calls; a dedicated PATCH endpoint lets callers invalidate / update cached entries.
- **Error email alerting** — HTTP 500/502 responses trigger a daemon-thread email to the configured `DEVELOPER_EMAIL` so incidents surface without a dedicated APM tool.

---

## 🛠 Tech Stack

| Layer | Technology | Version |
|---|---|---|
| Web framework | FastAPI | 0.128.0 |
| ASGI server (dev) | Uvicorn | 0.40.0 |
| ASGI server (prod) | Gunicorn + UvicornWorker | — |
| Data validation | Pydantic v2 | 2.12.5 |
| HTTP client | Requests | 2.32.5 |
| Auth library | OAuthlib | 3.3.1 |
| Form parsing | python-multipart | 0.0.20 |
| Email validation | email-validator | 2.3.0 |
| Starlette | Starlette | 0.50.0 |
| Runtime | Python | 3.12.6 |
| Container | Docker (slim) | — |
| Orchestration | Docker Compose | — |
| Upstream ERP | Microsoft Dynamics 365 BC | api/v2.0 + rgmccustom/v1.0 |

---

## ✨ Features

### <span style="color:#2a9d8f">🔐 Authentication & Token Management</span>

- OAuth2 `client_credentials` flow against Azure AD / Microsoft Identity Platform
- Thread-safe in-memory token cache with automatic 60-second pre-expiry refresh
- All downstream BC calls carry a `Bearer` header — callers never handle tokens
- `/bc/token` endpoint to expose the current token for debugging

### <span style="color:#2a9d8f">📦 Standard BC Endpoints (api/v2.0)</span>

- Full CRUD for **Items** (with OData `$filter`, `$expand`, `$select`, `category_code` shortcut)
- Full CRUD for **Customers** (with OData query passthrough)
- Full CRUD for **Sales Orders** and **Sales Order Lines** (via standard BC v2.0)
- Full CRUD for **Sales Credit Memos** and lines
- Full CRUD for **Item Categories**
- Read-only **Dimensions** and **Dimension Values** (brands, departments)
- **Companies** list and single-company lookup

### <span style="color:#2a9d8f">🏷 RGMC Custom API Endpoints (api/rgmc/rgmccustom/v1.0)</span>

- Full CRUD for **Retail Customers** (Pag50200)
- Full CRUD for **Sales Return Orders** + Lines (Pag50201/50202), with rollback on line failure
- Full CRUD for **Contacts** (Pag50203) with picture upload/download and brand tag management
- Full CRUD for **RGMC Sales Orders** + Lines (Pag50216/50217), supports inline line creation
- Read-only **RGMC Items** (Pag50205) with `category_code` and `family_code` filters
- Read-only **RGMC Item Families** (Pag50206)
- **Item Prices** (Pag50210) with date-window filtering, active-price shortcut, and in-process cache

### <span style="color:#2a9d8f">📸 Contact Picture Management</span>

- `GET /{contact_id}/picture` — fetches base64 picture from BC, decodes, auto-detects MIME type (JPEG/PNG/GIF/BMP/WebP), returns raw binary
- `PATCH /{contact_id}/picture` — accepts a multipart file upload, encodes to base64, PATCHes BC
- `GET /{contact_id}/picture/debug` — diagnostic endpoint showing raw b64 length, hex header, detected MIME type

### <span style="color:#2a9d8f">🏷 Contact Brand Tags (Pag50209)</span>

- List, add, and delete brand tags on a contact as a nested sub-resource
- Tags are a many-to-many relationship managed via `contacts({id})/contactBrandTags`

### <span style="color:#2a9d8f">💰 Item Price Cache</span>

- Prices are fetched with OData date-window filter: `startingDate le <date>` AND `(endingDate ge <date> OR endingDate eq 0001-01-01)`
- Ordered `startingDate desc`; `$top=1` returns the single active price
- Cached in-process keyed by `(company, product_no, product_nos, on_date, filter, top)`
- `PATCH /bc/custom/item-prices/cache` lets callers push optimistic price updates into the cache without re-fetching BC

### <span style="color:#2a9d8f">📬 Error Notification</span>

- HTTP middleware intercepts 500 and 502 responses
- Fires a daemon thread calling `notify_error()` — never blocks the HTTP response
- Sends an HTML-formatted email to `DEVELOPER_EMAIL` with method, URL, status, body, and client IP

### <span style="color:#2a9d8f">📊 Structured Logging</span>

- JSON-structured log lines via `OnelineFormatter` — single-line, machine-parseable
- Log level controlled by `LOG_LEVEL` env var
- Process time injected as `X-Process-Time` response header

---

## 🗺 API Routes

```
# Health
GET  /healthcheck                              — Liveness probe, returns version + timestamp

# Index
GET  /index                                    — Simple status check {"status": "BC API is running"}

# Business Central — General
GET  /bc/token                                 — Return current OAuth2 access token
GET  /bc/companies                             — List all BC companies
GET  /bc/companies/{company_id}                — Get a single BC company by GUID
GET  /bc/dimensions                            — All BC dimension definitions
GET  /bc/brands                                — Dimension values for BRAND
GET  /bc/departments                           — Dimension values for DEPARTMENT
GET  /bc/contacts                              — BC standard contacts (api/v2.0)

# Items (api/v2.0)
GET    /bc/items                               — List items (supports filter, expand, select, category_code)
GET    /bc/items/{item_id}                     — Get item by GUID
POST   /bc/items                               — Create item
PATCH  /bc/items/{item_id}                     — Update item
DELETE /bc/items/{item_id}                     — Delete item

# Customers (api/v2.0)
GET    /bc/customers                           — List customers
GET    /bc/customers/{customer_id}             — Get customer by GUID
POST   /bc/customers                           — Create customer
PATCH  /bc/customers/{customer_id}             — Update customer
DELETE /bc/customers/{customer_id}             — Delete customer

# Sales Orders (api/v2.0)
GET    /bc/sales-orders                        — List sales orders
GET    /bc/sales-orders/{order_id}             — Get sales order by GUID
POST   /bc/sales-orders                        — Create sales order (with optional inline lines + rollback)
PATCH  /bc/sales-orders/{order_id}             — Update sales order
DELETE /bc/sales-orders/{order_id}             — Delete sales order
GET    /bc/sales-orders/{order_id}/lines       — List lines for a sales order
POST   /bc/sales-orders/{order_id}/lines       — Create a sales order line
PATCH  /bc/sales-orders/{order_id}/lines/{line_id}   — Update a sales order line
DELETE /bc/sales-orders/{order_id}/lines/{line_id}   — Delete a sales order line

# Sales Credit Memos (api/v2.0)
GET    /bc/sales-credit-memos                  — List sales credit memos
GET    /bc/sales-credit-memos/{memo_id}        — Get sales credit memo by GUID
POST   /bc/sales-credit-memos                  — Create sales credit memo
PATCH  /bc/sales-credit-memos/{memo_id}        — Update sales credit memo
DELETE /bc/sales-credit-memos/{memo_id}        — Delete sales credit memo

# Item Categories (api/v2.0)
GET    /bc/item-categories                     — List item categories
GET    /bc/item-categories/{category_id}       — Get item category by GUID
POST   /bc/item-categories                     — Create item category
PATCH  /bc/item-categories/{category_id}       — Update item category
DELETE /bc/item-categories/{category_id}       — Delete item category

# RGMC Retail Customers (Pag50200)
GET    /bc/custom/retail-customers             — List retail customers
GET    /bc/custom/retail-customers/{customer_id}   — Get retail customer by GUID
POST   /bc/custom/retail-customers             — Create retail customer
PATCH  /bc/custom/retail-customers/{customer_id}   — Update retail customer
DELETE /bc/custom/retail-customers/{customer_id}   — Delete retail customer

# RGMC Sales Return Orders (Pag50201/50202)
GET    /bc/custom/sales-return-orders                         — List return orders
GET    /bc/custom/sales-return-orders/{order_id}              — Get return order by GUID
POST   /bc/custom/sales-return-orders                         — Create return order (with lines + rollback)
PATCH  /bc/custom/sales-return-orders/{order_id}              — Update return order
DELETE /bc/custom/sales-return-orders/{order_id}              — Delete return order
GET    /bc/custom/sales-return-orders/{order_id}/lines        — List return order lines
GET    /bc/custom/sales-return-orders/{order_id}/lines/{line_id}  — Get single line
POST   /bc/custom/sales-return-orders/{order_id}/lines        — Create return order line
PATCH  /bc/custom/sales-return-orders/{order_id}/lines/{line_id}  — Update line
DELETE /bc/custom/sales-return-orders/{order_id}/lines/{line_id}  — Delete line

# RGMC Contacts (Pag50203)
GET    /bc/custom/contacts                     — List contacts
GET    /bc/custom/contacts/{contact_id}        — Get contact by GUID
POST   /bc/custom/contacts                     — Create contact
PATCH  /bc/custom/contacts/{contact_id}        — Update contact
DELETE /bc/custom/contacts/{contact_id}        — Delete contact
GET    /bc/custom/contacts/{contact_id}/picture         — Get contact picture (binary image)
PATCH  /bc/custom/contacts/{contact_id}/picture         — Upload contact picture (multipart)
GET    /bc/custom/contacts/{contact_id}/picture/debug   — Debug raw BC picture response
GET    /bc/custom/contacts/{contact_id}/brand-tags      — List brand tags for contact
POST   /bc/custom/contacts/{contact_id}/brand-tags      — Add brand tag to contact
DELETE /bc/custom/contacts/{contact_id}/brand-tags/{tag_id}  — Remove brand tag

# RGMC Items (Pag50205 — read-only)
GET    /bc/custom/items                        — List RGMC items (filter, category_code, family_code)
GET    /bc/custom/items/{item_id}              — Get RGMC item by GUID

# RGMC Item Families (Pag50206 — read-only)
GET    /bc/custom/item-families                — List item families
GET    /bc/custom/item-families/{family_id}    — Get item family by GUID

# RGMC Item Prices (Pag50210)
GET    /bc/custom/item-prices                  — List item prices (product_no, product_nos, on_date, filter)
GET    /bc/custom/item-prices/active           — Get single active price for item on date
PATCH  /bc/custom/item-prices/cache            — Update cached price entry without BC round-trip

# RGMC Sales Orders (Pag50216/50217)
GET    /bc/custom/sales-orders                 — List RGMC sales orders
GET    /bc/custom/sales-orders/{order_id}      — Get RGMC sales order by GUID
POST   /bc/custom/sales-orders                 — Create RGMC sales order (with inline lines)
PATCH  /bc/custom/sales-orders/{order_id}      — Update RGMC sales order
DELETE /bc/custom/sales-orders/{order_id}      — Delete RGMC sales order
GET    /bc/custom/sales-orders/{order_id}/lines                    — List order lines
GET    /bc/custom/sales-orders/{order_id}/lines/{line_id}          — Get single line
POST   /bc/custom/sales-orders/{order_id}/lines                    — Create order line
PATCH  /bc/custom/sales-orders/{order_id}/lines/{line_id}          — Update order line
DELETE /bc/custom/sales-orders/{order_id}/lines/{line_id}          — Delete order line
```

---

## 📁 Project Structure

```
rgmc-bc-api/
├── .env.example                   # All required environment variables with descriptions
├── Dockerfile                     # Python 3.12.6-slim, non-root appuser, port 8080
├── compose.yaml                   # Docker Compose — single service, port 8080, loads .env
├── requirements.txt               # Python dependencies (pinned versions)
├── __init__.py                    # Package marker
└── src/
    ├── __init__.py
    ├── main.py                    # FastAPI app init, router registration, HTTP middlewares
    ├── config.py                  # All env vars loaded here — single source of config truth
    ├── gunicorn_config.py         # Gunicorn: 3 workers, 2 threads, UvicornWorker, port 8080
    ├── logger.py                  # Structured JSON logger (OnelineFormatter)
    ├── mappings.py                # Module name → human label map (used in email subjects)
    ├── types_py.py                # Shared Pydantic types: Token, TokenData, HealthcheckResponse
    ├── models/
    │   └── bc_models/
    │       ├── __init__.py                       # Re-exports all model classes
    │       ├── customer_models.py                # CustomerCreate / CustomerUpdate
    │       ├── item_models.py                    # ItemCreate / ItemUpdate
    │       ├── item_category_models.py           # ItemCategoryCreate / ItemCategoryUpdate
    │       ├── item_price_models.py              # ItemPriceUpdate
    │       ├── retail_customer_models.py         # RetailCustomerCreate / RetailCustomerUpdate
    │       ├── rgmc_contact_models.py            # RgmcContactCreate / RgmcContactUpdate
    │       ├── rgmc_contact_brand_tag_models.py  # ContactBrandTagCreate
    │       ├── rgmc_sales_order_models.py        # RgmcSalesOrderCreate/Update + Line models
    │       ├── sales_credit_memo_models.py       # SalesCreditMemoCreate / SalesCreditMemoUpdate
    │       ├── sales_order_models.py             # SalesOrderCreate/Update + Line models
    │       └── sales_return_order_models.py      # SalesReturnOrderCreate/Update + Line models
    ├── routers/
    │   ├── __init__.py                           # Imports all routers
    │   ├── health.py                             # GET /healthcheck
    │   └── bc_routes/
    │       ├── __init__.py                       # Re-exports all BC routers
    │       ├── bc_routes.py                      # /bc/token, /bc/companies, /bc/dimensions, /bc/brands, /bc/departments, /bc/contacts
    │       ├── item_routes.py                    # /bc/items CRUD
    │       ├── customer_routes.py                # /bc/customers CRUD
    │       ├── sales_order_routes.py             # /bc/sales-orders CRUD + lines
    │       ├── sales_credit_memo_routes.py       # /bc/sales-credit-memos CRUD
    │       ├── item_category_routes.py           # /bc/item-categories CRUD
    │       ├── retail_customer_routes.py         # /bc/custom/retail-customers CRUD
    │       ├── sales_return_order_routes.py      # /bc/custom/sales-return-orders CRUD + lines
    │       ├── rgmc_contact_routes.py            # /bc/custom/contacts CRUD + picture + brand-tags
    │       ├── rgmc_item_routes.py               # /bc/custom/items (read-only)
    │       ├── rgmc_item_family_routes.py        # /bc/custom/item-families (read-only)
    │       ├── rgmc_item_price_routes.py         # /bc/custom/item-prices + cache PATCH
    │       └── rgmc_sales_order_routes.py        # /bc/custom/sales-orders CRUD + lines
    └── services/
        ├── bc_functions.py        # All BC API calls: token, company lookup, CRUD helpers, price cache
        └── send_mail.py           # SMTP email: general notify + error alert (notify_error)
```

---

## ⚙ Setup & Installation

### Prerequisites

- Python 3.12+
- pip
- A Microsoft Dynamics 365 Business Central tenant with an Azure AD App Registration (client credentials)
- (Optional) Docker + Docker Compose for containerized runs

### Clone & Install

```bash
git clone <repo-url>
cd rgmc-bc-api
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate
pip install -r requirements.txt
```

### Configure Environment

```bash
cp .env.example .env
# Fill in BC_CLIENT_ID, BC_CLIENT_SECRET, BC_TENANT_ID, and other required vars
```

---

## 🔑 Environment Variables

> 📌 All variables are loaded in `src/config.py`. Copy `.env.example` to `.env` and fill in the blanks before running.

| Variable | File | Required | Default | Description |
|---|---|---|---|---|
| `BC_CLIENT_ID` | `.env` | ✅ | — | Azure AD App Registration client ID |
| `BC_CLIENT_SECRET` | `.env` | ✅ | — | Azure AD App Registration client secret |
| `BC_TENANT_ID` | `.env` | ✅ | — | Azure AD tenant GUID |
| `BC_SCOPE` | `.env` | — | `https://api.businesscentral.dynamics.com/.default` | OAuth2 scope for BC API |
| `BC_AUTH_URL` | `.env` | — | `https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token` | Azure AD token endpoint |
| `BC_ENVIRONMENT` | `.env` | — | `UAT` | BC environment name (e.g. `Production`, `UAT`) |
| `BC_COMPANY` | `.env` | — | `CGI` | Default BC company name for all requests |
| `API_TAG_VERSION` | `.env` | — | `0.1.0` | API version shown in Swagger title and `/healthcheck` |
| `PROJECT_ID` | `.env` | — | `RGMC0001` | Internal project identifier |
| `DEVELOPER_EMAIL` | `.env` | — | _(blank)_ | Address to receive 500/502 error alerts; leave blank to disable |
| `SMTP_HOST` | `.env` | — | `smtp.gmail.com` | SMTP server for developer error alerts |
| `SMTP_PORT` | `.env` | — | `587` | SMTP port (STARTTLS) |
| `SMTP_USER` | `.env` | — | — | SMTP login username |
| `SMTP_PASSWORD` | `.env` | — | — | SMTP login password |
| `MAIL_RECIPIENT` | `.env` | — | — | Comma-separated recipients for general `send_mail` notifications |
| `MAIL_SENDER` | `.env` | — | — | Sender address for general `send_mail` notifications |
| `MAIL_PASSWORD` | `.env` | — | — | Sender password for general notifications |
| `MAIL_PORT` | `.env` | — | `587` | SMTP port for general notifications |
| `MAIL_SERVER` | `.env` | — | `smtp.gmail.com` | SMTP server for general notifications |
| `LOG_LEVEL` | `.env` | — | `INFO` | Python logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `PORT` | runtime | — | `8080` | Bind port (used by `gunicorn_config.py`) |
| `K_REVISION` | runtime | — | `00001` | Cloud Run revision label shown in the Swagger title |

---

## 🚀 Running the App

### Development (Uvicorn with hot-reload)

```bash
uvicorn src.main:api --host 0.0.0.0 --port 8080 --reload
```

### Development (plain Uvicorn, no reload)

```bash
uvicorn src.main:api --host 0.0.0.0 --port 8080
```

### Production (Gunicorn + UvicornWorker)

```bash
gunicorn src.main:api -c src/gunicorn_config.py
```

Gunicorn is configured with **3 workers**, **2 threads**, **30 s timeout**, binding to `0.0.0.0:8080`.

### Docker Compose

```bash
docker compose up --build
```

The service binds to `http://localhost:8080`. Swagger UI is at `http://localhost:8080/swagger`.

---

## 🏗 Building for Production

### Docker Image

```bash
docker build -t rgmc-bc-api:latest .
docker run -p 8080:8080 --env-file .env rgmc-bc-api:latest
```

The image is based on `python:3.12.6-slim` and runs as a non-root `appuser` (UID 10001).

### Google Cloud Run (typical deployment target)

```bash
# Build and push
docker build -t gcr.io/<project>/<image>:latest .
docker push gcr.io/<project>/<image>:latest

# Deploy
gcloud run deploy rgmc-bc-api \
  --image gcr.io/<project>/<image>:latest \
  --platform managed \
  --region <region> \
  --set-env-vars "BC_CLIENT_ID=...,BC_TENANT_ID=...,..."
```

> 💡 The `K_REVISION` env var is injected automatically by Cloud Run and appears in the Swagger title as `RGMC BC API (Release - <revision>)`.

---

## 📡 API Endpoints

### <span style="color:#555">🏢 Business Central — General</span>

| Method | Path | Description |
|---|---|---|
| GET | `/healthcheck` | Liveness check — returns version and timestamp |
| GET | `/index` | Simple running status |
| GET | `/bc/token` | Return current OAuth2 bearer token |
| GET | `/bc/companies` | List all BC companies |
| GET | `/bc/companies/{id}` | Get a BC company by GUID |
| GET | `/bc/dimensions` | All BC dimensions |
| GET | `/bc/brands` | Dimension values for BRAND |
| GET | `/bc/departments` | Dimension values for DEPARTMENT |
| GET | `/bc/contacts` | BC standard contacts (api/v2.0) |

### <span style="color:#555">📦 Items (api/v2.0)</span>

| Method | Path | Description |
|---|---|---|
| GET | `/bc/items` | List items; supports `?filter=`, `?expand=`, `?select=`, `?category_code=` |
| GET | `/bc/items/{item_id}` | Get item by GUID |
| POST | `/bc/items` | Create item |
| PATCH | `/bc/items/{item_id}` | Update item |
| DELETE | `/bc/items/{item_id}` | Delete item |

**POST /bc/items — payload**

```json
{
  "number": "ITEM-001",
  "displayName": "Widget A",
  "type": "Inventory",
  "itemCategoryCode": "WIDGETS",
  "unitPrice": 29.99,
  "unitCost": 12.50,
  "baseUnitOfMeasureCode": "PCS",
  "blocked": false
}
```

### <span style="color:#555">👤 Customers (api/v2.0)</span>

| Method | Path | Description |
|---|---|---|
| GET | `/bc/customers` | List customers; supports `?filter=`, `?expand=`, `?select=` |
| GET | `/bc/customers/{customer_id}` | Get customer by GUID |
| POST | `/bc/customers` | Create customer |
| PATCH | `/bc/customers/{customer_id}` | Update customer |
| DELETE | `/bc/customers/{customer_id}` | Delete customer |

**POST /bc/customers — payload**

```json
{
  "number": "CUST-001",
  "displayName": "Acme Corp",
  "addressLine1": "123 Main St",
  "city": "Manila",
  "country": "PH",
  "phoneNumber": "+63-2-1234567",
  "email": "billing@acme.com",
  "currencyCode": "PHP",
  "paymentTermsId": "...",
  "creditLimit": 500000.00
}
```

### <span style="color:#555">🧾 Sales Orders (api/v2.0)</span>

| Method | Path | Description |
|---|---|---|
| GET | `/bc/sales-orders` | List sales orders |
| GET | `/bc/sales-orders/{order_id}` | Get sales order by GUID |
| POST | `/bc/sales-orders` | Create sales order with optional inline lines |
| PATCH | `/bc/sales-orders/{order_id}` | Update sales order |
| DELETE | `/bc/sales-orders/{order_id}` | Delete sales order |
| GET | `/bc/sales-orders/{order_id}/lines` | List lines |
| POST | `/bc/sales-orders/{order_id}/lines` | Create line |
| PATCH | `/bc/sales-orders/{order_id}/lines/{line_id}` | Update line |
| DELETE | `/bc/sales-orders/{order_id}/lines/{line_id}` | Delete line |

**POST /bc/sales-orders — payload**

```json
{
  "customerNumber": "CUST-001",
  "externalDocumentNumber": "EXT-2024-001",
  "orderDate": "2024-06-17",
  "salesperson": "SALES01",
  "shortcutDimension1Code": "BRAND-A",
  "lines": [
    {
      "itemNumber": "ITEM-001",
      "description": "Widget A",
      "quantity": 10,
      "unitPrice": 29.99,
      "discountPercent": 5
    }
  ]
}
```

> ⚠️ If any line creation fails after the order header is created, the entire order is deleted (rolled back) and a 502 error is returned.

### <span style="color:#555">📄 Sales Credit Memos (api/v2.0)</span>

| Method | Path | Description |
|---|---|---|
| GET | `/bc/sales-credit-memos` | List sales credit memos |
| GET | `/bc/sales-credit-memos/{memo_id}` | Get memo by GUID |
| POST | `/bc/sales-credit-memos` | Create memo |
| PATCH | `/bc/sales-credit-memos/{memo_id}` | Update memo |
| DELETE | `/bc/sales-credit-memos/{memo_id}` | Delete memo |

### <span style="color:#555">🗂 Item Categories (api/v2.0)</span>

| Method | Path | Description |
|---|---|---|
| GET | `/bc/item-categories` | List item categories |
| GET | `/bc/item-categories/{category_id}` | Get category by GUID |
| POST | `/bc/item-categories` | Create category |
| PATCH | `/bc/item-categories/{category_id}` | Update category |
| DELETE | `/bc/item-categories/{category_id}` | Delete category |

### <span style="color:#555">🏪 RGMC Retail Customers (Pag50200)</span>

| Method | Path | Description |
|---|---|---|
| GET | `/bc/custom/retail-customers` | List retail customers |
| GET | `/bc/custom/retail-customers/{customer_id}` | Get by GUID |
| POST | `/bc/custom/retail-customers` | Create retail customer |
| PATCH | `/bc/custom/retail-customers/{customer_id}` | Update |
| DELETE | `/bc/custom/retail-customers/{customer_id}` | Delete |

**POST /bc/custom/retail-customers — payload**

```json
{
  "number": "RC-001",
  "name": "Juan dela Cruz",
  "phoneNo": "+63-917-1234567",
  "email": "juan@example.com",
  "address": "456 Rizal Ave",
  "city": "Quezon City",
  "countryRegionCode": "PH",
  "currencyCode": "PHP",
  "salespersonCode": "SALES01",
  "paymentTermsCode": "COD"
}
```

### <span style="color:#555">↩ RGMC Sales Return Orders (Pag50201/50202)</span>

| Method | Path | Description |
|---|---|---|
| GET | `/bc/custom/sales-return-orders` | List return orders |
| GET | `/bc/custom/sales-return-orders/{order_id}` | Get by GUID |
| POST | `/bc/custom/sales-return-orders` | Create return order (with lines) |
| PATCH | `/bc/custom/sales-return-orders/{order_id}` | Update |
| DELETE | `/bc/custom/sales-return-orders/{order_id}` | Delete |
| GET | `/bc/custom/sales-return-orders/{order_id}/lines` | List lines |
| GET | `/bc/custom/sales-return-orders/{order_id}/lines/{line_id}` | Get single line |
| POST | `/bc/custom/sales-return-orders/{order_id}/lines` | Create line |
| PATCH | `/bc/custom/sales-return-orders/{order_id}/lines/{line_id}` | Update line |
| DELETE | `/bc/custom/sales-return-orders/{order_id}/lines/{line_id}` | Delete line |

**POST /bc/custom/sales-return-orders — payload**

```json
{
  "customerNumber": "CUST-001",
  "externalDocumentNo": "RTN-2024-001",
  "yourReference": "Customer REF",
  "submittedBy": "sales.rep@rgmc.com",
  "locationCode": "MAIN",
  "lines": [
    {
      "itemNumber": "ITEM-001",
      "description": "Damaged Widget",
      "quantity": 2,
      "unitPrice": 29.99,
      "returnReasonCode": "DEFECTIVE"
    }
  ]
}
```

### <span style="color:#555">👥 RGMC Contacts (Pag50203)</span>

| Method | Path | Description |
|---|---|---|
| GET | `/bc/custom/contacts` | List contacts |
| GET | `/bc/custom/contacts/{contact_id}` | Get by GUID |
| POST | `/bc/custom/contacts` | Create contact |
| PATCH | `/bc/custom/contacts/{contact_id}` | Update contact |
| DELETE | `/bc/custom/contacts/{contact_id}` | Delete contact |
| GET | `/bc/custom/contacts/{contact_id}/picture` | Get contact picture (binary image response) |
| PATCH | `/bc/custom/contacts/{contact_id}/picture` | Upload contact picture (multipart/form-data) |
| GET | `/bc/custom/contacts/{contact_id}/picture/debug` | Debug BC picture raw response |
| GET | `/bc/custom/contacts/{contact_id}/brand-tags` | List brand tags |
| POST | `/bc/custom/contacts/{contact_id}/brand-tags` | Add brand tag |
| DELETE | `/bc/custom/contacts/{contact_id}/brand-tags/{tag_id}` | Remove brand tag |

**POST /bc/custom/contacts — payload**

```json
{
  "name": "Maria Santos",
  "firstName": "Maria",
  "lastName": "Santos",
  "jobTitle": "Store Manager",
  "phoneNo": "+63-2-5551234",
  "mobilePhoneNo": "+63-917-5551234",
  "email": "maria.santos@store.com",
  "salespersonCode": "SALES01",
  "username": "maria.santos",
  "passwordHash": "<bcrypt-hash>"
}
```

**POST /bc/custom/contacts/{id}/brand-tags — payload**

```json
{
  "brandCode": "BRAND-A"
}
```

### <span style="color:#555">📋 RGMC Items (Pag50205 — read-only)</span>

| Method | Path | Description |
|---|---|---|
| GET | `/bc/custom/items` | List RGMC items; supports `?filter=`, `?category_code=`, `?family_code=` |
| GET | `/bc/custom/items/{item_id}` | Get RGMC item by GUID |

### <span style="color:#555">🗃 RGMC Item Families (Pag50206 — read-only)</span>

| Method | Path | Description |
|---|---|---|
| GET | `/bc/custom/item-families` | List item families |
| GET | `/bc/custom/item-families/{family_id}` | Get item family by GUID |

### <span style="color:#555">💰 RGMC Item Prices (Pag50210)</span>

| Method | Path | Description |
|---|---|---|
| GET | `/bc/custom/item-prices` | List prices; supports `?product_no=`, `?product_nos=` (CSV), `?on_date=YYYY-MM-DD`, `?filter=` |
| GET | `/bc/custom/item-prices/active` | Get single active price — requires `?product_no=` and `?on_date=` |
| PATCH | `/bc/custom/item-prices/cache` | Push optimistic price update into in-process cache |

**PATCH /bc/custom/item-prices/cache — payload**

```json
{
  "unitPrice": 35.00,
  "startingDate": "2024-06-01",
  "endingDate": "2024-12-31"
}
```

Query params: `?product_no=ITEM-001&on_date=2024-06-17&company=CGI`

### <span style="color:#555">🛒 RGMC Sales Orders (Pag50216/50217)</span>

| Method | Path | Description |
|---|---|---|
| GET | `/bc/custom/sales-orders` | List RGMC sales orders |
| GET | `/bc/custom/sales-orders/{order_id}` | Get by GUID |
| POST | `/bc/custom/sales-orders` | Create RGMC sales order (with inline lines) |
| PATCH | `/bc/custom/sales-orders/{order_id}` | Update |
| DELETE | `/bc/custom/sales-orders/{order_id}` | Delete |
| GET | `/bc/custom/sales-orders/{order_id}/lines` | List lines |
| GET | `/bc/custom/sales-orders/{order_id}/lines/{line_id}` | Get single line |
| POST | `/bc/custom/sales-orders/{order_id}/lines` | Create line |
| PATCH | `/bc/custom/sales-orders/{order_id}/lines/{line_id}` | Update line |
| DELETE | `/bc/custom/sales-orders/{order_id}/lines/{line_id}` | Delete line |

**POST /bc/custom/sales-orders — payload**

```json
{
  "sellToCustomerNo": "CUST-001",
  "sellToContactNo": "CONT-001",
  "externalDocumentNo": "EXT-2024-001",
  "orderDate": "2024-06-17",
  "locationCode": "MAIN",
  "salespersonCode": "SALES01",
  "shortcutDimension1Code": "BRAND-A",
  "submittedBy": "sales.rep@rgmc.com",
  "lines": [
    {
      "lineType": "Item",
      "number": "ITEM-001",
      "description": "Widget A",
      "quantity": 5,
      "unitPrice": 29.99,
      "lineDiscountPercent": 10,
      "unitOfMeasureCode": "PCS"
    }
  ]
}
```

---

## 💾 Data & Caching Strategy

All caching is **in-process** (Python module-level dicts). There is no Redis or external cache.

| Cache | Location | Key | What is stored | When refreshed |
|---|---|---|---|---|
| OAuth2 access token | `bc_functions._token_cache` | singleton dict | `{"token": str, "expires_at": float}` | On every request if within 60 s of expiry |
| BC company ID | `bc_functions._company_id_cache` | company name (uppercase) | Company GUID string | Never evicted; populated on first request per company name |
| Item prices | `bc_functions._item_price_cache` | `(company, product_no, product_nos, on_date, filter, top)` tuple | Full BC response `{"value": [...]}` | Updated on every fresh BC price fetch; mutated in-place by `PATCH /cache` |

> ⚠️ **Company ID cache is never cleared.** If you rename a company in BC, restart the process.

> 💡 **Item price cache serves as a read-through.** If BC is unreachable and a prior result is cached, the cache result is returned automatically (fallback on exception in `rgmc_list_item_prices`).

> ⚠️ **No distributed cache.** With multiple Gunicorn workers or replicas, each process maintains its own cache. A cache update via `PATCH /cache` only affects the worker that handled the request.

---

## 🔐 Authentication Flow

This API uses the OAuth2 **Client Credentials** grant — no user login is required.

```
1. Client app calls any /bc/* endpoint
         |
         v
2. FastAPI route handler calls bc_functions helper
         |
         v
3. bc_functions.get_access_token() is invoked
         |
         +-- If cached token is still valid (expires_at - now > 60s):
         |       Return cached token
         |
         +-- If expired or within 60s of expiry:
                 POST https://login.microsoftonline.com/{BC_TENANT_ID}/oauth2/v2.0/token
                 Body: grant_type=client_credentials
                       client_id={BC_CLIENT_ID}
                       client_secret={BC_CLIENT_SECRET}
                       scope=https://api.businesscentral.dynamics.com/.default
                 |
                 v
                 Store token + (now + expires_in) in _token_cache
                 Return new token
         |
         v
4. Token is injected as Authorization: Bearer <token> on every BC request
         |
         v
5. Business Central validates the token against Azure AD
   and returns the requested data
```

> 🔐 The token is acquired once per process lifecycle and reused across all concurrent requests thanks to `threading.Lock()` on `_token_lock`.

---

## 🔄 Core Data Flow

### Sales Order Creation (with lines)

```
POST /bc/sales-orders
Body: { customerNumber, externalDocumentNumber, lines: [...] }
        |
        v
1. Route handler extracts lines from payload, pops them out
        |
        v
2. POST salesOrders to BC (header only, no lines yet)
   BC returns { id: "<order-guid>", ... }
        |
        v
3. For each line in lines[]:
   a. Map frontend fields -> BC field names (_map_line_payload)
      itemNumber -> number
      discountPercent -> lineDiscountPercent
      lineDiscountAmount -> lineDiscountPercent (computed from qty * unitPrice)
      Assign lineNo = index * 10000
   b. POST salesOrders(<order-guid>)/salesOrderLines to BC
   c. If BC returns non-2xx:
        DELETE salesOrders(<order-guid>)   <- ROLLBACK
        Raise HTTPException 502
        |
        v
4. Return created order JSON to client
```

### Item Price Lookup (active price for a date)

```
GET /bc/custom/item-prices/active?product_no=ITEM-001&on_date=2024-06-17
        |
        v
1. Build OData filter:
   productNo eq 'ITEM-001'
   AND startingDate le 2024-06-17
   AND (endingDate ge 2024-06-17 OR endingDate eq 0001-01-01)
   ORDER BY startingDate desc
   $top=1
        |
        v
2. Check cache key (company, product_no, None, on_date, None, 1)
   Hit -> return cached value
   Miss -> GET from BC
        |
        v
3. BC returns value[0] = most recent effective price
        |
        v
4. Store in _item_price_cache
   Return records[0] to client
```

### Contact Picture Upload

```
PATCH /bc/custom/contacts/{contact_id}/picture
Body: multipart/form-data  file=<image file>
        |
        v
1. Read file bytes from UploadFile
        |
        v
2. base64.b64encode(image_bytes) -> picture_b64 string
        |
        v
3. PATCH contactPictures({contact_id})
   Body: { "picture": "<base64 string>" }
   Header: If-Match: *
        |
        v
4. Return { "ok": true }
```

---

## 📬 Error Notification System

When any route returns HTTP 500 or 502, the `error_email_middleware` in `main.py` fires automatically:

```
Response status == 500 or 502
        |
        v
1. Buffer the full response body (async iterator drain)
        |
        v
2. Extract client IP from X-Forwarded-For or request.client.host
        |
        v
3. threading.Thread(target=notify_error, daemon=True).start()
   (Non-blocking — response is returned to client immediately)
        |
        v
4. notify_error() sends HTML email to DEVELOPER_EMAIL via SMTP STARTTLS
   Subject: [RGMC BC API <status>] <METHOD> <URL>
   Body: timestamp, method, URL, status code, client IP, response body
        |
        v
5. Silently skips if DEVELOPER_EMAIL / SMTP_USER / SMTP_PASSWORD are blank
```

> 💡 Email alerts fire silently in a daemon thread — a failed email send is logged but never surfaces to the API client.

---

## 📝 Structured Logging

Every log line is a single-line JSON object emitted by `OnelineFormatter`:

```json
{
  "message": "...",
  "levelname": "INFO",
  "name": "src.logger",
  "host": "hostname",
  "asctime": "2024-06-17 10:00:00",
  "version": "0.1.0",
  "time": "2024-06-17T10:00:00.123456"
}
```

The `X-Process-Time` header is added to every HTTP response showing elapsed seconds for the request.

---

## 📜 License

Private — RGMC Group. All rights reserved.

This software is proprietary to RGMC Group. Unauthorized reproduction, distribution, or use is prohibited.
