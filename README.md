# Employee Attendance & Analytics API

FastAPI + MongoDB service for punch-in/out, manual corrections with an audit trail, and attendance analytics.
All code is in `app/main.py`.

## Run locally

Requires Python 3.11+ and MongoDB 6.0 or newer (local Docker or a free Atlas cluster).

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
docker run -d -p 27017:27017 --name mongo mongo:7     # or use an Atlas connection string
```

Create a `.env` file in the project root (it is not committed):

```
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

Start the API (indexes are created at startup):

```bash
uvicorn app.main:app --port 8000
```

Interactive docs: http://localhost:8000/docs

## What is implemented

- Starter fixes (see `REVIEW.md`): late/overtime/work-hours rules in IST with half-up rounding, validation, pagination, indexes, unique constraints for punch-in and employee codes, `/health` returning 503 when MongoDB is down.
- `POST /attendance/punch-out`: closes the latest record at or before `punched_at` with one atomic update, works across midnight.
- `PATCH /attendance/{emp_code}/{date}`: manual correction that recomputes derived fields and appends one history entry; concurrent edits are guarded by a compare-and-set on the history length.
- Analytics through MongoDB aggregation pipelines: employee monthly summary, department summary, late leaderboard (standard competition ranking with `$rank`), and department trend (`$densify` for gap filling, `$setWindowFields` for the 7-day moving average).
- `GET /admin/explain/{endpoint}`: runs `explain("executionStats")` on the same query each endpoint uses.

## Notes and assumptions

- `attendance_pct` is rounded to 4 decimals half-up. I read R8 ("rates: 4 decimals") as covering percentages.
- Records are not filtered by `joined_on`; it is used only for working days (R7) and headcount (R9).
- The trend computes rates with integer arithmetic so rounding is exactly half-up (MongoDB's `$round` rounds ties to even).

## Testing

- Tested against the sample data only. I did not test with a large dataset or run a concurrency load test.
