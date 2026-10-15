# Employee Attendance & Analytics API

A FastAPI and MongoDB service that records employee punch-ins and punch-outs, supports manual corrections with an audit trail, and provides attendance analytics. All application code is in `app/main.py`.

## Running locally

**Requirements:** Python 3.11 or newer, and MongoDB 6.0 or newer (a local Docker container or a free Atlas cluster both work).

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
docker run -d -p 27017:27017 --name mongo mongo:7    # or use an Atlas connection string
```

Create a `.env` file in the project root. It is excluded from version control.

```
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

Start the API. Indexes are created automatically at startup.

```bash
uvicorn app.main:app --port 8000
```

Interactive API documentation is available at http://localhost:8000/docs.

## What is included

- **Starter fixes** (details in `REVIEW.md`): late, overtime and work-hour rules calculated in IST with half-up rounding; input validation; corrected pagination; indexes; unique constraints for employee codes and daily punch-ins; and a `/health` endpoint that returns 503 when MongoDB is unavailable.
- **Punch-out** (`POST /attendance/punch-out`): closes the employee's latest record at or before `punched_at` using a single atomic update, including across midnight.
- **Manual correction** (`PATCH /attendance/{emp_code}/{date}`): recomputes derived fields and appends one audit entry per correction. Concurrent edits are protected by a compare-and-set check on the history length.
- **Analytics**, all built on MongoDB aggregation pipelines:
  - employee monthly summary
  - department summary
  - late leaderboard, using `$rank` for standard competition ranking
  - department trend, using `$densify` to fill missing days and `$setWindowFields` for the 7-day moving average
- **Query plans** (`GET /admin/explain/{endpoint}`): returns `explain("executionStats")` for the same query each endpoint runs.

## Design notes and assumptions

- `attendance_pct` is rounded to four decimals, half-up. I interpreted the rounding rule for rates (R8) as covering percentages.
- `joined_on` is used only to count working days (R7) and headcount (R9). Attendance records are not filtered by it.
- The trend calculates rates with integer arithmetic so that rounding is exactly half-up, because MongoDB's `$round` rounds ties to the nearest even number.

## Limitations and testing

- Tested against the provided sample data only. I have not run it against a large dataset or performed a concurrency load test.
- _(Update this section to reflect what you have and have not tested.)_
