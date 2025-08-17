# WebCloset API (FastAPI)

## Quick start
```bash
# create & activate venv (optional)
python3 -m venv .venv && source .venv/bin/activate

# install deps
pip install -r requirements.txt

# run dev server
uvicorn app.main:app --reload --port 8000