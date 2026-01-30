from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, ValidationError
from typing import List, Optional, Literal, Dict, Any
import psycopg2
from psycopg2.extras import RealDictCursor
import os
import traceback
import time
from datetime import datetime
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

app = FastAPI(
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["*"]
)

# Standardized error response model
class ErrorResponse(BaseModel):
    error: str
    message: str
    timestamp: str
    path: Optional[str] = None
    details: Optional[Dict[str, Any]] = None

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
    page: int = Field(default=1, ge=1, le=1000)
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
    search_time_ms: Optional[int] = None

# Custom exception handlers
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content=ErrorResponse(
            error="Validation Error",
            message="Invalid request parameters",
            timestamp=datetime.utcnow().isoformat(),
            path=str(request.url.path),
            details={"validation_errors": exc.errors()}
        ).dict()
    )

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content=ErrorResponse(
            error=f"HTTP {exc.status_code}",
            message=exc.detail,
            timestamp=datetime.utcnow().isoformat(),
            path=str(request.url.path)
        ).dict()
    )

@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    # Log the full error for debugging
    print(f"Unexpected error: {str(exc)}")
    print(traceback.format_exc())
    
    return JSONResponse(
        status_code=500,
        content=ErrorResponse(
            error="Internal Server Error",
            message="An unexpected error occurred",
            timestamp=datetime.utcnow().isoformat(),
            path=str(request.url.path),
            details={"type": type(exc).__name__} if os.getenv('DEBUG') == 'true' else None
        ).dict()
    )

# Database connection with proper error handling
def get_db_connection():
    db_url = os.getenv('DATABASE_URL')
    if not db_url:
        raise HTTPException(status_code=500, detail="Database configuration missing")
    
    try:
        conn = psycopg2.connect(db_url)
        return conn
    except psycopg2.OperationalError as e:
        raise HTTPException(status_code=503, detail="Database connection failed")
    except Exception as e:
        raise HTTPException(status_code=500, detail="Database error")

# Health check endpoint
@app.get("/health")
async def health_check():
    """Health check endpoint with database connectivity test"""
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        conn.close()
        
        return {
            "status": "healthy",
            "timestamp": datetime.utcnow().isoformat(),
            "database": "connected"
        }
    except Exception as e:
        return JSONResponse(
            status_code=503,
            content={
                "status": "unhealthy",
                "timestamp": datetime.utcnow().isoformat(),
                "database": "disconnected",
                "error": str(e)
            }
        )

@app.post("/search", response_model=SearchResponse)
async def search_items(request: SearchRequest):
    """
    Enhanced search endpoint with comprehensive filters, sorting, and error handling.
    """
    start_time = time.time()
    
    # Input validation
    if request.price_min is not None and request.price_min < 0:
        raise HTTPException(status_code=400, detail="Price minimum must be non-negative")
    
    if request.price_max is not None and request.price_max < 0:
        raise HTTPException(status_code=400, detail="Price maximum must be non-negative")
    
    if request.price_min is not None and request.price_max is not None and request.price_min > request.price_max:
        raise HTTPException(status_code=400, detail="Price minimum cannot be greater than maximum")
    
    conn = get_db_connection()
    
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Build WHERE conditions
            where_conditions = []
            params = []

            # Text search
            if request.q and request.q.strip():
                where_conditions.append("""(
                    ic.title ILIKE %s OR 
                    ic.brand ILIKE %s OR 
                    ic.category ILIKE %s
                )""")
                like_term = f"%{request.q.strip()}%"
                params.extend([like_term, like_term, like_term])

            # Brand filter
            if request.brands:
                placeholders = ",".join(["%s"] * len(request.brands))
                where_conditions.append(f"ic.brand IN ({placeholders})")
                params.extend(request.brands)

            # Size filter
            if request.sizes:
                placeholders = ",".join(["%s"] * len(request.sizes))
                where_conditions.append(f"s.size IN ({placeholders})")
                params.extend(request.sizes)

            # Condition filter
            if request.conditions:
                placeholders = ",".join(["%s"] * len(request.conditions))
                where_conditions.append(f"s.condition IN ({placeholders})")
                params.extend(request.conditions)

            # Marketplace filter
            if request.marketplaces:
                placeholders = ",".join(["%s"] * len(request.marketplaces))
                where_conditions.append(f"s.marketplace_code IN ({placeholders})")
                params.extend(request.marketplaces)

            # Price range filters
            if request.price_min is not None:
                where_conditions.append("s.price_cents >= %s")
                params.append(int(request.price_min * 100))

            if request.price_max is not None:
                where_conditions.append("s.price_cents <= %s")
                params.append(int(request.price_max * 100))

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

            # Search query with error handling
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
            
            # Execute search with timeout protection
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

            # Transform results with error handling
            items = []
            for row in rows:
                try:
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
                except Exception as item_error:
                    # Log item parsing error but continue with other items
                    print(f"Error parsing item {row.get('id', 'unknown')}: {item_error}")
                    continue

            # Calculate pagination info
            total_pages = (total + request.per_page - 1) // request.per_page
            has_more = request.page * request.per_page < total
            
            # Calculate search time
            search_time_ms = int((time.time() - start_time) * 1000)

            return SearchResponse(
                items=items,
                total=total,
                page=request.page,
                per_page=request.per_page,
                total_pages=total_pages,
                has_more=has_more,
                search_time_ms=search_time_ms
            )

    except psycopg2.Error as db_error:
        raise HTTPException(status_code=503, detail="Database query failed")
    except ValidationError as val_error:
        raise HTTPException(status_code=422, detail=f"Data validation error: {val_error}")
    except Exception as e:
        print(f"Search error: {str(e)}")
        raise HTTPException(status_code=500, detail="Search operation failed")
    
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
    
    try:
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
    
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=f"Invalid parameters: {e}")

@app.get("/click")
async def redirect_to_seller(id: str):
    """Redirect to seller URL for click tracking"""
    
    if not id or not id.strip():
        raise HTTPException(status_code=400, detail="Item ID is required")
    
    conn = get_db_connection()
    
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Get first seller URL for the canonical item
            cur.execute("""
                SELECT s.seller_url, s.marketplace_code
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
            
            seller_url = result['seller_url']
            if not seller_url or not seller_url.startswith(('http://', 'https://')):
                raise HTTPException(status_code=422, detail="Invalid seller URL")
            
            from fastapi.responses import RedirectResponse
            return RedirectResponse(url=seller_url, status_code=302)
    
    except psycopg2.Error:
        raise HTTPException(status_code=503, detail="Database error during redirect")
    except Exception as e:
        print(f"Redirect error: {str(e)}")
        raise HTTPException(status_code=500, detail="Redirect failed")
    
    finally:
        conn.close()

# Add API info endpoint
@app.get("/info")
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

#if __name__ == "__main__":
#    import uvicorn
#    uvicorn.run(app, host="0.0.0.0", port=8000)
