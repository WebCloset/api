from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import List, Optional, Literal
import psycopg2
from psycopg2.extras import RealDictCursor
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

app = FastAPI(title="WebCloset API", version="1.0.0")

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure this properly for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Pydantic models
class SearchRequest(BaseModel):
    q: Optional[str] = ""
    brands: List[str] = Field(default_factory=list)
    sizes: List[str] = Field(default_factory=list)
    conditions: List[str] = Field(default_factory=list)
    marketplaces: List[str] = Field(default_factory=list)
    price_min: Optional[float] = None
    price_max: Optional[float] = None
    sort: Literal["best", "price_asc", "price_desc", "newest"] = "best"
    page: int = Field(default=1, ge=1)
    per_page: int = Field(default=24, ge=1, le=100)

class SearchItem(BaseModel):
    id: str
    brand: Optional[str]
    title: Optional[str]
    category: Optional[str]
    image_url: Optional[str]
    price_cents: Optional[int]
    listings_count: int
    condition: Optional[str]
    marketplace_code: Optional[str]
    size: Optional[str]
    seller_urls: List[str]

class SearchResponse(BaseModel):
    items: List[SearchItem]
    total: int
    page: int
    per_page: int
    total_pages: int
    has_more: bool

# Database connection
def get_db_connection():
    db_url = os.getenv('DATABASE_URL')
    if not db_url:
        raise HTTPException(status_code=500, detail="DATABASE_URL not configured")
    
    try:
        conn = psycopg2.connect(db_url)
        return conn
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database connection failed: {str(e)}")

@app.get("/health")
async def health_check():
    return {"ok": True}

@app.post("/search", response_model=SearchResponse)
async def search_items(request: SearchRequest):
    """
    Enhanced search endpoint with filters and sorting.
    
    Filters:
    - q: Text search across title, brand, category
    - brands: Filter by specific brands
    - sizes: Filter by sizes
    - conditions: Filter by item condition
    - marketplaces: Filter by marketplace
    - price_min/price_max: Price range in dollars
    
    Sorting:
    - best: Optimized ranking (low price + high listing count)
    - price_asc: Price low to high
    - price_desc: Price high to low
    - newest: Most recently seen items first
    """
    
    conn = get_db_connection()
    
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Build WHERE conditions
            where_conditions = []
            params = []
            param_idx = 0

            # Text search
            if request.q and request.q.strip():
                param_idx += 1
                where_conditions.append(f"""(
                    ic.title ILIKE %s OR 
                    ic.brand ILIKE %s OR 
                    ic.category ILIKE %s
                )""")
                like_term = f"%{request.q.strip()}%"
                params.extend([like_term, like_term, like_term])

            # Brand filter
            if request.brands:
                param_idx += 1
                placeholders = ",".join(["%s"] * len(request.brands))
                where_conditions.append(f"ic.brand IN ({placeholders})")
                params.extend(request.brands)

            # Size filter
            if request.sizes:
                param_idx += 1
                placeholders = ",".join(["%s"] * len(request.sizes))
                where_conditions.append(f"s.size IN ({placeholders})")
                params.extend(request.sizes)

            # Condition filter
            if request.conditions:
                param_idx += 1
                placeholders = ",".join(["%s"] * len(request.conditions))
                where_conditions.append(f"s.condition IN ({placeholders})")
                params.extend(request.conditions)

            # Marketplace filter
            if request.marketplaces:
                param_idx += 1
                placeholders = ",".join(["%s"] * len(request.marketplaces))
                where_conditions.append(f"s.marketplace_code IN ({placeholders})")
                params.extend(request.marketplaces)

            # Price range filters
            if request.price_min is not None:
                where_conditions.append("s.price_cents >= %s")
                params.append(int(request.price_min * 100))  # Convert dollars to cents

            if request.price_max is not None:
                where_conditions.append("s.price_cents <= %s")
                params.append(int(request.price_max * 100))  # Convert dollars to cents

            # Build WHERE clause
            where_clause = ""
            if where_conditions:
                where_clause = f"WHERE {' AND '.join(where_conditions)}"

            # Build ORDER BY clause
            if request.sort == "price_asc":
                order_by = "MIN(s.price_cents) ASC NULLS LAST, ic.id DESC"
            elif request.sort == "price_desc":
                order_by = "MIN(s.price_cents) DESC NULLS LAST, ic.id DESC"
            elif request.sort == "newest":
                order_by = "ic.last_seen DESC, ic.id DESC"
            else:  # best
                order_by = "(MIN(s.price_cents) * 0.7 + (1.0 / COUNT(s.*)) * 1000) ASC, ic.id DESC"

            # Calculate offset
            offset = (request.page - 1) * request.per_page

            # Search query
            search_query = f"""
                SELECT
                    ic.id,
                    ic.brand,
                    ic.title,
                    ic.category,
                    ic.image_url,
                    MIN(s.price_cents) AS price_cents,
                    COUNT(s.*) AS listings_count,
                    STRING_AGG(DISTINCT s.condition, ', ' ORDER BY s.condition) AS condition,
                    STRING_AGG(DISTINCT s.marketplace_code, ', ') AS marketplace_code,
                    STRING_AGG(DISTINCT s.size, ', ' ORDER BY s.size) AS size,
                    ARRAY_AGG(DISTINCT s.seller_url) AS seller_urls
                FROM item_canonical ic
                JOIN item_links l ON l.canonical_id = ic.id AND l.active = true
                JOIN item_source s ON s.id = l.source_id
                {where_clause}
                GROUP BY ic.id
                ORDER BY {order_by}
                LIMIT %s OFFSET %s
            """

            # Add pagination params
            search_params = params + [request.per_page, offset]
            
            # Execute search
            cur.execute(search_query, search_params)
            rows = cur.fetchall()

            # Get total count
            count_query = f"""
                SELECT COUNT(DISTINCT ic.id) as total
                FROM item_canonical ic
                JOIN item_links l ON l.canonical_id = ic.id AND l.active = true
                JOIN item_source s ON s.id = l.source_id
                {where_clause}
            """
            
            cur.execute(count_query, params)
            total_result = cur.fetchone()
            total = total_result['total'] if total_result else 0

            # Transform results
            items = []
            for row in rows:
                item = SearchItem(
                    id=str(row['id']),
                    brand=row['brand'],
                    title=row['title'],
                    category=row['category'],
                    image_url=row['image_url'],
                    price_cents=row['price_cents'],
                    listings_count=int(row['listings_count']),
                    condition=row['condition'],
                    marketplace_code=row['marketplace_code'],
                    size=row['size'],
                    seller_urls=list(row['seller_urls']) if row['seller_urls'] else []
                )
                items.append(item)

            # Calculate pagination info
            total_pages = (total + request.per_page - 1) // request.per_page
            has_more = request.page * request.per_page < total

            return SearchResponse(
                items=items,
                total=total,
                page=request.page,
                per_page=request.per_page,
                total_pages=total_pages,
                has_more=has_more
            )

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Search failed: {str(e)}")
    
    finally:
        conn.close()

@app.get("/search")
async def search_items_get(
    q: str = Query("", description="Search query"),
    brands: str = Query("", description="Comma-separated brands"),
    sizes: str = Query("", description="Comma-separated sizes"),
    conditions: str = Query("", description="Comma-separated conditions"),
    marketplaces: str = Query("", description="Comma-separated marketplaces"),
    price_min: Optional[float] = Query(None, description="Minimum price in dollars"),
    price_max: Optional[float] = Query(None, description="Maximum price in dollars"),
    sort: str = Query("best", description="Sort order"),
    page: int = Query(1, ge=1, description="Page number"),
    per_page: int = Query(24, ge=1, le=100, description="Items per page")
):
    """GET version of search for backward compatibility"""
    
    # Convert GET parameters to POST format
    request = SearchRequest(
        q=q,
        brands=[b.strip() for b in brands.split(',') if b.strip()] if brands else [],
        sizes=[s.strip() for s in sizes.split(',') if s.strip()] if sizes else [],
        conditions=[c.strip() for c in conditions.split(',') if c.strip()] if conditions else [],
        marketplaces=[m.strip() for m in marketplaces.split(',') if m.strip()] if marketplaces else [],
        price_min=price_min,
        price_max=price_max,
        sort=sort if sort in ["best", "price_asc", "price_desc", "newest"] else "best",
        page=page,
        per_page=per_page
    )
    
    return await search_items(request)

@app.get("/click")
async def redirect_to_seller(id: str):
    """Redirect to seller URL for tracking"""
    conn = get_db_connection()
    
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Get first seller URL for the canonical item
            cur.execute("""
                SELECT s.seller_url
                FROM item_canonical ic
                JOIN item_links l ON l.canonical_id = ic.id AND l.active = true
                JOIN item_source s ON s.id = l.source_id
                WHERE ic.id = %s
                ORDER BY s.price_cents ASC NULLS LAST
                LIMIT 1
            """, (id,))
            
            result = cur.fetchone()
            
            if not result:
                raise HTTPException(status_code=404, detail="Item not found")
            
            from fastapi.responses import RedirectResponse
            return RedirectResponse(url=result['seller_url'], status_code=302)
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Redirect failed: {str(e)}")
    
    finally:
        conn.close()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
