# api/app/main.py
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import List, Optional, Literal
from dotenv import load_dotenv, find_dotenv
import os

from elasticsearch import Elasticsearch
from elasticsearch import NotFoundError

# api/app/main.py  (add imports at the top)
from fastapi.responses import RedirectResponse
from urllib.parse import urlparse

load_dotenv(find_dotenv(".env", usecwd=True))

ES_URL = os.getenv("ELASTICSEARCH_URL")
ES_KEY = os.getenv("ES_API_KEY")
ES_INDEX = os.getenv("ELASTICSEARCH_INDEX", "products")


if not ES_URL or not ES_KEY:
    raise RuntimeError("Missing ELASTICSEARCH_URL or ES_API_KEY in environment")

es = Elasticsearch(
    ES_URL,
    api_key=ES_KEY,
    request_timeout=10,
    retry_on_timeout=True,
    max_retries=3,
)

app = FastAPI()

# Add CORS middleware for development (can be tightened in prod)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # TODO: tighten to your Vercel domain in prod
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/health")
def health():
    return {"ok": True}

# ---- Models ----
SortOpt = Literal["best", "price_asc", "newest"]

class SearchBody(BaseModel):
    q: Optional[str] = None
    brand: List[str] = Field(default_factory=list)
    size: List[str] = Field(default_factory=list)          # placeholder for later
    condition: List[str] = Field(default_factory=list)
    marketplace: List[str] = Field(default_factory=list)
    price_min: Optional[int] = None                        # cents
    price_max: Optional[int] = None                        # cents
    sort: SortOpt = "best"
    page: int = 1
    per_page: int = 24

class SearchItem(BaseModel):
    id: str
    title: Optional[str]
    brand: Optional[str]
    condition: Optional[str]
    price_cents: Optional[int]
    currency: Optional[str]
    image: Optional[str]
    seller_urls: List[str] = []
    marketplace: Optional[str]

class SearchResponse(BaseModel):
    items: List[SearchItem]
    total: int
    page: int
    per_page: int

# ---- Helpers ----
def build_es_query(body: SearchBody):
    must = []
    filters = []

    # ---- full-text q ----
    if body.q:
        q = body.q.strip()
        should = [
            {"multi_match": {
                "query": q,
                "fields": ["title^2", "brand^1"]
            }},
            {"match_phrase_prefix": {"title": {"query": q}}},
            {"query_string": {
                "query": f"{q}*",
                "default_field": "title",
                "analyze_wildcard": True
            }},
        ]
        must.append({"bool": {"should": should, "minimum_should_match": 1}})
    else:
        must.append({"match_all": {}})

    # ---- filters ----
    if body.brand:
        # brand is keyword+lc normalizer; lowercase the incoming terms
        filters.append({"terms": {"brand": [b.lower() for b in body.brand]}})

    if body.condition:
        filters.append({"terms": {"condition": body.condition}})

    if body.marketplace:
        filters.append({"terms": {"marketplace": body.marketplace}})

    if body.price_min is not None or body.price_max is not None:
        rng = {}
        if body.price_min is not None:
            rng["gte"] = body.price_min
        if body.price_max is not None:
            rng["lte"] = body.price_max
        filters.append({"range": {"price_cents": rng}})

    q = {"bool": {"must": must}}
    if filters:
        q["bool"]["filter"] = filters
    return q

def build_es_sort(body: SearchBody):
    if body.sort == "price_asc":
        return [{"price_cents": "asc"}, {"_score": "desc"}]
    if body.sort == "newest":
        return [{"updated_at": "desc"}]
    return ["_score"]  # best/relevance

# ---- Route ----
@app.post("/search", response_model=SearchResponse)
def post_search(body: SearchBody):
    if body.page < 1 or body.per_page < 1 or body.per_page > 100:
        raise HTTPException(status_code=400, detail="invalid pagination")

    frm = (body.page - 1) * body.per_page
    try:
        res = es.search(
            index=ES_INDEX,
            from_=frm,
            size=body.per_page,
            query=build_es_query(body),
            sort=build_es_sort(body),
            _source=["id","title","brand","condition","price_cents","currency","image","seller_urls","marketplace","updated_at"],
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"es_error: {repr(e)}")

    hits = res.get("hits", {}).get("hits", [])
    total = res.get("hits", {}).get("total", {}).get("value", 0)

    items = []
    for h in hits:
        s = h.get("_source", {})
        # Ensure each item has a stable id the web can use
        items.append(SearchItem(
            id=s.get("id") or h.get("_id"),
            title=s.get("title"),
            brand=s.get("brand"),
            condition=s.get("condition"),
            price_cents=s.get("price_cents"),
            currency=s.get("currency"),
            image=s.get("image"),
            seller_urls=s.get("seller_urls") or [],
            marketplace=s.get("marketplace"),
        ))
    return SearchResponse(items=items, total=total, page=body.page, per_page=body.per_page)

# api/app/main.py  (add this route near the bottom, after /search)
@app.get("/click")
def click(id: str):
    if not id:
        raise HTTPException(status_code=400, detail="missing id")

    try:
        doc = es.get(index=ES_INDEX, id=id, _source_includes=["seller_urls"])
    except NotFoundError:
        raise HTTPException(status_code=404, detail=f"doc_not_found: {id}")

    src = (doc or {}).get("_source") or {}
    urls = src.get("seller_urls") or []

    if not urls:
        raise HTTPException(status_code=404, detail="no_seller_url")

    target = urls[0]
    parsed = urlparse(target)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="invalid_url_scheme")

    # 302 redirect to seller
    return RedirectResponse(url=target, status_code=302)