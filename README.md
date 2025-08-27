# api
FastAPI backend for WebCloset

# WebCloset API

FastAPI backend for WebCloset.  
Provides search and click endpoints backed by Elasticsearch, plus a simple health check.

## Quickstart

### 1. Clone & install
```bash
git clone https://github.com/your-org/webcloset-api.git
cd webcloset/api
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Environment variables
Copy `.env.example` to `.env` and fill in your real values:
```
ELASTICSEARCH_URL=https://<your-es-url>
ES_API_KEY=<your-api-key>
ELASTICSEARCH_INDEX=products
```

### 3. Run locally
```bash
uvicorn app.main:app --reload
```

- API will be available at: http://127.0.0.1:8000  
- Interactive docs: http://127.0.0.1:8000/docs

### 4. Endpoints
- `GET /health` → `{ "ok": true }`  
- `POST /search` → search products (query, filters, pagination)  
- `GET /click?id=src-123` → 302 redirect to original seller URL  

### 5. Deployment
- Deploy on Railway.  
- Add env vars from `.env.example` to the Railway project settings.  
- Health endpoint must return `{ok:true}` in production.
