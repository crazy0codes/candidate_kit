# Employee Attendance & Analytics API

FastAPI and MongoDB service for employee records, attendance punch-in/out, manual corrections, and monthly and daily analytics. API behavior and business rules are described in `openapi.yaml`.

## Run locally

Install dependencies with `pip install -r requirements.txt`, then set `MONGO_URI` and `MONGO_DB` in the environment. Start MongoDB and run:

```bash
uvicorn app.main:app --port 8000
```

Indexes are created automatically at application startup. `GET /health` checks the MongoDB connection.

## Postman checks

Import `postman_collection.json` into Postman and run the collection in order. Its default `baseUrl` is `http://127.0.0.1:8000`. The run creates two employees and one attendance record, so point the app at a dedicated test database first; the API has no delete endpoint.
