# Employee Attendance & Analytics API

FastAPI and MongoDB service for employee records, attendance punch-in/out, manual corrections, and monthly and daily analytics. API behavior and business rules are described in `openapi.yaml`.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Check service and MongoDB readiness |
| `POST`, `GET` | `/employees` | Create employees and list them with optional department filtering |
| `POST` | `/attendance/punch-in` | Record a punch-in |
| `POST` | `/attendance/punch-out` | Record a punch-out and calculate durations |
| `GET` | `/attendance` | Filter and page through attendance records |
| `PATCH` | `/attendance/{emp_code}/{date}` | Correct a record and append its audit history |
| `GET` | `/analytics/employees/{emp_code}/monthly` | Monthly employee attendance summary |
| `GET` | `/analytics/departments/summary` | Monthly department summary |
| `GET` | `/analytics/leaderboard/late` | Monthly late-comer ranking |
| `GET` | `/analytics/departments/{department}/trend` | Daily department attendance trend |
| `GET` | `/admin/explain/{endpoint}` | Inspect the MongoDB query plan for a supported endpoint |

## Run locally

Install dependencies with `pip install -r requirements.txt`, then set `MONGO_URI` and `MONGO_DB` in the environment. Start MongoDB and run:

```bash
uvicorn app.main:app --port 8000
```

Indexes are created automatically at application startup. `GET /health` checks the MongoDB connection.

## Postman checks

Import `postman_collection.json` into Postman and run the collection in order. Its default `baseUrl` is `http://127.0.0.1:8000`. The run creates two employees and one attendance record, so point the app at a dedicated test database first; the API has no delete endpoint.
