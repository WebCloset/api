from fastapi import FastAPI, Query, HTTPException
import os
from datetime import datetime
from dotenv import load_dotenv

import re

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