from fastapi import FastAPI, Query
import os
from datetime import datetime
from dotenv import load_dotenv

import re

from elasticsearch import Elasticsearch

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


def get_es_connection():
    es_url = os.getenv("ELASTICSEARCH_URL", "http://localhost:9200/")
    es_username = os.getenv("ELASTICSEARCH_USERNAME", None)
    es_password = os.getenv("ELASTICSEARCH_PASSWORD", None)

    if es_password and es_username:
        return Elasticsearch(es_url, basic_auth=(es_username, es_password))

    # Initialize Elasticsearch
    return Elasticsearch(
        es_url
    )


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

