from fastapi import FastAPI, Query, HTTPException, Header
import os
from datetime import datetime
from dotenv import load_dotenv
import hashlib
import hmac
import secrets
import base64

import re
import json

from pydantic import BaseModel
import psycopg2
from elasticsearch import Elasticsearch
from fastapi.middleware.cors import CORSMiddleware

from services.ad_service import generate_group_ad_script

brand_name_mapping = {'louis vuitton': 'Louis Vuitton', 'adidas': 'Adidas', 'gucci': 'Gucci', 'nike': 'Nike'}

# Load environment variables
load_dotenv()

newapi = FastAPI(
    title="WebCloset API",
    version="1.0.0",
    description="Fashion marketplace aggregation API"
)

# Enhanced CORS configuration
ALLOWED_ORIGINS = [
    "http://localhost:3000",  # Next.js dev
    "http://127.0.0.1:3000",  # Alternative localhost
    "https://localhost:3000",  # HTTPS dev
]

# Add production domain when deployed
PROD_DOMAIN = os.getenv('PROD_WEB_DOMAIN')
if PROD_DOMAIN:
    ALLOWED_ORIGINS.append(f"https://{PROD_DOMAIN}")
    ALLOWED_ORIGINS.append(f"https://www.{PROD_DOMAIN}")

newapi.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS", "PATCH"],
    allow_headers=["*"],
    expose_headers=["*"]
)

class GenerateAdRequest(BaseModel):
    canonical_product_id: str


class UpdateAdRequest(BaseModel):
    script_body: str
    edited_by: str


class AdminLoginRequest(BaseModel):
    username: str
    password: str


class AdminSourceToggleRequest(BaseModel):
    enabled: bool


class AdminTokenData(BaseModel):
    username: str
    issued_at: int
    expires_at: int


def get_es_connection():
    es_url = os.getenv("ELASTICSEARCH_URL", "https://elasticsearch-production-3ce1.up.railway.app")
    es_username = os.getenv("ELASTICSEARCH_USERNAME", None)
    es_password = os.getenv("ELASTICSEARCH_PASSWORD", None)

    if es_password and es_username:
        return Elasticsearch(es_url, basic_auth=(es_username, es_password))

    # Initialize Elasticsearch
    return Elasticsearch(
        es_url
    )

DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_URL = "postgresql://neondb_owner:npg_5LdJSKuC8bFY@ep-damp-field-aey694y3-pooler.c-2.us-east-2.aws.neon.tech/neondb?sslmode=require&channel_binding=require"

def get_db_connection():
    return psycopg2.connect(DATABASE_URL, connect_timeout=10)


def _get_admin_auth_config():
    return {
        "username": os.getenv("ADMIN_USERNAME", "admin"),
        "password": os.getenv("ADMIN_PASSWORD", "admin123"),
        "secret": os.getenv("ADMIN_AUTH_SECRET", "webcloset-admin-secret")
    }


def _create_admin_token(username: str):
    config = _get_admin_auth_config()
    issued_at = int(datetime.utcnow().timestamp())
    expires_at = issued_at + (60 * 60 * 8)  # 8 hours
    payload = {"username": username, "issued_at": issued_at, "expires_at": expires_at}
    payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    payload_encoded = base64.urlsafe_b64encode(payload_json.encode("utf-8")).decode("utf-8").rstrip("=")
    signature = hmac.new(
        config["secret"].encode("utf-8"),
        payload_encoded.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    return f"{payload_encoded}.{signature}"


def _verify_admin_token(authorization: str | None) -> AdminTokenData:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized")

    token = authorization.replace("Bearer ", "", 1).strip()
    try:
        payload_encoded, signature = token.split(".", 1)
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid token")

    config = _get_admin_auth_config()
    expected_signature = hmac.new(
        config["secret"].encode("utf-8"),
        payload_encoded.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(signature, expected_signature):
        raise HTTPException(status_code=401, detail="Invalid token signature")

    padded_payload = payload_encoded + "=" * ((4 - len(payload_encoded) % 4) % 4)
    try:
        payload_json = base64.urlsafe_b64decode(padded_payload).decode("utf-8")
        payload = json.loads(payload_json)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token payload")

    now_timestamp = int(datetime.utcnow().timestamp())
    if payload.get("expires_at", 0) < now_timestamp:
        raise HTTPException(status_code=401, detail="Token expired")

    return AdminTokenData(
        username=payload.get("username", ""),
        issued_at=int(payload.get("issued_at", 0)),
        expires_at=int(payload.get("expires_at", 0))
    )


def _ensure_admin_source_settings_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS admin_source_settings (
            source_code TEXT PRIMARY KEY,
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            updated_at TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """)


def _normalize_source_code(source: str) -> str:
    lowered = source.strip().lower()
    allowed = {"amazon", "ebay", "reverb"}
    if lowered not in allowed:
        raise HTTPException(status_code=400, detail="Unsupported source code")
    return lowered


def _get_source_statuses(cursor):
    _ensure_admin_source_settings_table(cursor)
    known_sources = ["amazon", "ebay", "reverb"]

    status_map = {}
    cursor.execute("SELECT source_code, enabled, updated_at FROM admin_source_settings")
    for row in cursor.fetchall():
        status_map[row[0]] = {
            "enabled": bool(row[1]),
            "updated_at": row[2].isoformat() if row[2] else None
        }

    source_statuses = []
    for source_code in known_sources:
        cursor.execute(
            """
            SELECT COUNT(*)::int
            FROM item_source
            WHERE LOWER(marketplace_code) = %s
            """,
            (source_code,)
        )
        listing_count = cursor.fetchone()[0]
        status = status_map.get(source_code, {"enabled": True, "updated_at": None})
        source_statuses.append({
            "source_code": source_code,
            "enabled": status["enabled"],
            "connected": listing_count > 0,
            "listing_count": listing_count,
            "updated_at": status["updated_at"]
        })

    return source_statuses


def build_es_query_new(user_query):
    q_lower = user_query.lower()
    cheap = any(word in q_lower for word in ["cheap", "affordable", "budget", "under"])
    luxury = any(word in q_lower for word in ["luxury", "costliest"])
    range = any(word in q_lower for word in ["range", "between"])
    color = next((c for c in ["red", "blue", "black", "white"] if c in q_lower), None)
    brand = next((b for b in ["louis vuitton", "gucci", "nike", "adidas"] if b in q_lower), None)

    # Step 2: Create semantic embedding
    # Step 3: Build ES query dynamically
    nested_filters = []
    filters = []
    if color:
        nested_filters.append({"term": {"items.color.keyword": color}})
    if brand:
        nested_filters.append({"term": {"items.brand.keyword": brand_name_mapping.get(brand, brand)}})
    # Cheap / affordable filter
    if cheap:
        numbers = re.findall(r'-?\d+\.?\d*', q_lower)
        if numbers:
            price = int(numbers[0])
        else:
            price = 40000
        filters.append({"nested": {
            "path": "items",
            "query": {"range": {"items.price_cents": {"lte": price}}},
            "inner_hits": {
                "size": 100
            }
        }
        })
        nested_filters.append({"range": {"items.price_cents": {"lte": price}}})
    if luxury:
        price_filter = "gte"
        numbers = re.findall(r'-?\d+\.?\d*', q_lower)
        if numbers:
            price = int(numbers[0])
        else:
            price = 75000
        nested_filters.append({"range": {"items.price_cents": {"gte": price}}})
    if range:
        price_filter = "gte"
        numbers = re.findall(r'-?\d+\.?\d*', q_lower)
        if numbers:
            low_price = int(numbers[0])
            high_price = int(numbers[1])
        else:
            low_price = 0
            high_price = 100000
        nested_filters.append({"range": {"items.price_cents": {"lte": high_price, "gte": low_price}}})

    must_terms = []
    if "bag" in q_lower:
        nested_filters.append({"match_phrase": {"items.category": "Clothing"}})
    nested_filter = {
        "nested": {
            "path": "items",
            "query": {
                "bool": {
                    "must": nested_filters
                }
            },
            "inner_hits": {
                "size": 100
            }
        }
    }
    nested_filter_new = {
        "nested": {
            "path": "items",
            "query": {
                "bool": {
                    "should": nested_filters,
                    "minimum_should_match": 1
                }
            },
            "inner_hits": {
                "size": 100
            }
        }
    }
    print("filters : ", nested_filter)
    base_query = {
        "bool": {
            "should": [
                {"multi_match": {
                    "query": user_query,
                    "fields": ["title^3", "brand", "category"],
                    "fuzziness": "AUTO"
                }}
            ],
            "filter": nested_filter
        }
    }
    query = {
        "query": base_query
    }
    query_new = {
        "query": {
            "nested": {
                "path": "items",
                "query": {
                    "exists": {"field": "items.price_cents"}
                }
            }
        }
    }

    return query


def search_index_new(query_text):
    query = build_es_query_new(query_text)
    es = get_es_connection()
    res = es.search(index="ebay_canonical", body=query)
    # print(res)
    items = []
    for hit in res['hits']['hits']:
        items.append(hit['inner_hits']['items']['hits']['hits'])
    return items


@newapi.get("/nlp/search/")
async def nlp_search_items(query: str = Query("", description="NLP Search items")):
    return search_index_new(query)


@newapi.post("/admin/login")
async def admin_login(request: AdminLoginRequest):
    config = _get_admin_auth_config()
    if not (
        secrets.compare_digest(request.username, config["username"])
        and secrets.compare_digest(request.password, config["password"])
    ):
        raise HTTPException(status_code=401, detail="Invalid username or password")

    token = _create_admin_token(request.username)
    token_data = _verify_admin_token(f"Bearer {token}")
    return {
        "token": token,
        "username": request.username,
        "issued_at": token_data.issued_at,
        "expires_at": token_data.expires_at
    }


@newapi.get("/admin/overview")
async def admin_overview(authorization: str | None = Header(default=None)):
    _verify_admin_token(authorization)
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        source_statuses = _get_source_statuses(cursor)

        cursor.execute(
            """
            SELECT
                COUNT(*)::int AS total_rows,
                COUNT(*) FILTER (WHERE title IS NULL OR TRIM(title) = '')::int AS missing_title,
                COUNT(*) FILTER (WHERE price_cents IS NULL OR price_cents <= 0)::int AS invalid_price,
                COUNT(*) FILTER (WHERE image_url IS NULL OR TRIM(image_url) = '')::int AS missing_image
            FROM item_source
            """
        )
        issue_counts = cursor.fetchone()

        return {
            "sources": source_statuses,
            "summary": {
                "total_listings": issue_counts[0],
                "missing_title": issue_counts[1],
                "invalid_price": issue_counts[2],
                "missing_image": issue_counts[3]
            }
        }
    finally:
        cursor.close()
        conn.close()


@newapi.patch("/admin/sources/{source_code}")
async def update_source_status(
    source_code: str,
    request: AdminSourceToggleRequest,
    authorization: str | None = Header(default=None)
):
    _verify_admin_token(authorization)
    normalized_source = _normalize_source_code(source_code)

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        _ensure_admin_source_settings_table(cursor)
        cursor.execute(
            """
            INSERT INTO admin_source_settings (source_code, enabled, updated_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (source_code)
            DO UPDATE SET enabled = EXCLUDED.enabled, updated_at = NOW()
            """,
            (normalized_source, request.enabled)
        )
        conn.commit()

        source_statuses = _get_source_statuses(cursor)
        updated_source = next((s for s in source_statuses if s["source_code"] == normalized_source), None)
        return {"source": updated_source}
    finally:
        cursor.close()
        conn.close()


@newapi.get("/admin/listings")
async def admin_listings(
    source: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    authorization: str | None = Header(default=None)
):
    _verify_admin_token(authorization)
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        where_clause = ""
        params = []
        if source and source.strip():
            normalized_source = _normalize_source_code(source)
            where_clause = "WHERE LOWER(s.marketplace_code) = %s"
            params.append(normalized_source)

        params.append(limit)
        cursor.execute(
            f"""
            SELECT
                s.id,
                COALESCE(s.title, '') AS title,
                COALESCE(s.brand, '') AS brand,
                COALESCE(s.marketplace_code, '') AS source_code,
                s.price_cents,
                COALESCE(s.currency, '') AS currency,
                COALESCE(s.condition, '') AS item_condition,
                COALESCE(s.seller_url, '') AS seller_url,
                COALESCE(s.image_url, '') AS image_url
            FROM item_source s
            {where_clause}
            ORDER BY s.id DESC
            LIMIT %s
            """,
            tuple(params)
        )
        rows = cursor.fetchall()
        listings = []
        for row in rows:
            listings.append({
                "id": str(row[0]),
                "title": row[1],
                "brand": row[2],
                "source_code": row[3].lower(),
                "price_cents": row[4],
                "currency": row[5],
                "condition": row[6],
                "seller_url": row[7],
                "image_url": row[8]
            })
        return {"listings": listings}
    finally:
        cursor.close()
        conn.close()


@newapi.get("/admin/issues")
async def admin_issues(
    limit: int = Query(default=50, ge=1, le=200),
    authorization: str | None = Header(default=None)
):
    _verify_admin_token(authorization)
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT
                id,
                COALESCE(title, '') AS title,
                COALESCE(marketplace_code, '') AS source_code,
                CASE
                    WHEN title IS NULL OR TRIM(title) = '' THEN 'missing_title'
                    WHEN price_cents IS NULL OR price_cents <= 0 THEN 'invalid_price'
                    WHEN image_url IS NULL OR TRIM(image_url) = '' THEN 'missing_image'
                    ELSE 'unknown'
                END AS issue_type,
                CASE
                    WHEN title IS NULL OR TRIM(title) = '' THEN 'Listing is missing a title'
                    WHEN price_cents IS NULL OR price_cents <= 0 THEN 'Listing has invalid price'
                    WHEN image_url IS NULL OR TRIM(image_url) = '' THEN 'Listing has missing image'
                    ELSE 'Unknown issue'
                END AS issue_message
            FROM item_source
            WHERE
                title IS NULL OR TRIM(title) = ''
                OR price_cents IS NULL OR price_cents <= 0
                OR image_url IS NULL OR TRIM(image_url) = ''
            ORDER BY id DESC
            LIMIT %s
            """,
            (limit,)
        )
        rows = cursor.fetchall()
        issues = []
        for row in rows:
            issues.append({
                "listing_id": str(row[0]),
                "title": row[1],
                "source_code": row[2].lower(),
                "issue_type": row[3],
                "issue_message": row[4]
            })
        return {"issues": issues}
    finally:
        cursor.close()
        conn.close()

# ----------------------------
# Generate & Store Script
# ----------------------------
@newapi.post("/ads/generate")
def generate_ad(request: GenerateAdRequest):

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        # 1️⃣ Fetch canonical product
        cursor.execute("""
            SELECT t1.brand, t1.title, t1.size, t1.category, t1.price_cents, t1.currency, t1.color FROM item_source AS t1
            INNER JOIN item_links AS t2 ON t1.id = t2.source_id WHERE t2.canonical_id = %s
        """, (request.canonical_product_id,))
        product = cursor.fetchall()

        if not product:
            raise HTTPException(status_code=404, detail="Canonical product not found")

        # 3️⃣ Generate script
        script = generate_group_ad_script(product)

        # 4️⃣ Store in ad_generators
        cursor.execute("""
            INSERT INTO ad_generators
            (canonical_product_id, generated_script)
            VALUES (%s, %s)
            RETURNING id
        """, (request.canonical_product_id, script))

        ad_id = cursor.fetchone()[0]

        # 5️⃣ Store version 1
        cursor.execute("""
            INSERT INTO ad_generator_versions
            (ad_generator_id, version_number, script_body)
            VALUES (%s, 1, %s)
        """, (ad_id, script))

        conn.commit()

        return {
            "ad_id": ad_id,
            "script": script
        }

    finally:
        cursor.close()
        conn.close()

@newapi.patch("/ads/product/{canonical_product_id}")
def update_latest_ad(canonical_product_id: str, request: UpdateAdRequest):

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        # 1️⃣ Get latest ad for product
        cursor.execute("""
            SELECT id
            FROM ad_generators
            WHERE canonical_product_id = %s
            ORDER BY created_at DESC
            LIMIT 1
        """, (canonical_product_id,))

        ad = cursor.fetchone()

        if not ad:
            raise HTTPException(status_code=404, detail="No ad found for product")

        ad_id = ad[0]

        # 2️⃣ Update main ad
        cursor.execute("""
            UPDATE ad_generators
            SET generated_script = %s,
                is_override = TRUE,
                status = 'edited',
                updated_at = NOW()
            WHERE id = %s
        """, (request.script_body, ad_id))

        # 3️⃣ Insert new version
        cursor.execute("""
            INSERT INTO ad_generator_versions
            (ad_generator_id, version_number, script_body, edited_by)
            VALUES (
                %s,
                (SELECT COALESCE(MAX(version_number),0)+1
                 FROM ad_generator_versions
                 WHERE ad_generator_id = %s),
                %s,
                %s
            )
        """, (ad_id, ad_id, request.script_body, request.edited_by))

        conn.commit()

        return {"message": "Latest ad updated successfully"}

    finally:
        cursor.close()
        conn.close()


@newapi.get("/ads/product/{canonical_product_id}")
def list_ads_for_product(canonical_product_id: str):

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute("""
            SELECT id, generated_script, status, created_at
            FROM ad_generators
            WHERE canonical_product_id = %s
            ORDER BY created_at DESC
        """, (canonical_product_id,))

        ads = cursor.fetchall()

        return ads

    finally:
        cursor.close()
        conn.close()

# Add API info endpoint
@newapi.get("/info")
async def api_info():
    """API information and statistics"""
    return {
        "name": "WebCloset API",
        "version": "1.0.0",
        "description": "Fashion marketplace aggregation API",
        "endpoints": {
            "health": "GET /health - Health check",
            "search": "POST /search - Search items with filters",
            "search_get": "GET /search - Search items (query params)",
            "click": "GET /click?id={id} - Redirect to seller",
            "info": "GET /info - This endpoint"
        },
        "cors_origins": ALLOWED_ORIGINS,
        "timestamp": datetime.utcnow().isoformat()
    }