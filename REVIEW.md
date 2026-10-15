# REVIEW.md

Defects found in the starter `app/main.py`. Helper-function results come from calling the starter functions directly; endpoint results come from calling the starter API.

| # | Where | What is wrong | How I noticed it | How I fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | It floors to whole minutes and then checks `> 10`, so 09:40:01 gives 0 instead of 10. It also uses the punch's own date and timezone (not IST) and ignores overnight shifts. | Called it with 09:40:00, 09:40:01 and 10:15:59 for a 09:30 shift: got 0, 0, 45 (expected 0, 10, 45). | Work in IST, compare elapsed seconds, apply the grace only when more than 600 seconds, and use the attendance date (R1) as the shift's start day. |
| 2 | `compute_work_hours` | Python `round()` on a float is not half-up. | A 450-second shift (0.125 h) returned 0.12; R4 needs 0.13. | `Decimal` with `ROUND_HALF_UP`. |
| 3 | `compute_overtime` | No 30-minute minimum, so 15 minutes counts. The shift end is placed on the same date, which is wrong for overnight shifts. Subtracting a naive and an aware datetime raises `TypeError`. | Punch-out 18:45 for an 18:30 end returned 15 (expected 0). A naive punch-out raised `TypeError`. | Compute the end from the shift start date (next day for overnight), use aware UTC datetimes, return minutes only when at least 30. |
| 4 | `EmployeeIn` | Every field is a plain `str`: bad `emp_code`, email, shift times, equal shift times or an invalid date are all accepted. | POST /employees with `emp_code` "X1" and a shift of "25:99" was accepted. | Pydantic patterns and lengths, a date check, and a check that `shift_start != shift_end`. Invalid input returns 422. |
| 5 | `PunchInIn` | `status` is any string and `punched_at` accepts seconds, floats, strings and 0 (`if body.punched_at` treats 0 as missing). | Sent status "FOO" and a seconds-based timestamp: both accepted. | `Literal` status limited to PRESENT, WFH, ON_DUTY; strict integer epoch milliseconds in a sane range; `is not None` check. |
| 6 | `health` | It always returns ok without touching MongoDB. | Stopped MongoDB and /health still said ok. | Ping MongoDB; return 503 on failure. |
| 7 | `create_employee` | Check-then-insert with no unique index, so two simultaneous requests can create duplicates. It also stores a naive local `datetime.now()` and returns `created_at` as a datetime instead of epoch milliseconds. | Read the code; sent two requests for the same code and inspected the response. | Unique index on `emp_code`, catch `DuplicateKeyError` and return 409; store UTC; return epoch milliseconds. |
| 8 | `list_employees` | `skip = page * page_size` skips the first page. `total` counts the whole collection, not the filtered set. There is no sort and `page` / `page_size` are not validated. | Page 1 started at employee 21; `total` ignored the department filter. | `(page - 1) * page_size`, count with the same filter, sort by `emp_code`, `Query(ge=1)` and `page_size <= 100`. |
| 9 | `punch_in` employee lookup | An unknown `emp_code` makes `emp` `None`, so `emp["shift_start"]` raises `TypeError` and the API returns 500. | POST punch-in with an unknown code returned 500. | Return 404 when the employee is not found. |
| 10 | `punch_in` date and time | `datetime.fromtimestamp()` and `.date()` use the server's local time, not IST, and ignore the overnight rule (R1). | Read the code; a punch near midnight IST gets the wrong date on a non-IST server. | Store UTC, derive the attendance date from IST and the shift (`attendance_date_for_punch`). |
| 11 | `punch_in` race | Check-then-insert: two simultaneous punch-ins can both pass the check. | Read the code. | Unique (emp_code, date) index; catch `DuplicateKeyError` and return 409. |
| 12 | `punch_in` response | It returns an `id` made from `_id` (forbidden by the contract) and raw datetimes instead of epoch milliseconds. | Looked at the response of POST punch-in. | One `api_log` function removes `_id` and converts instants to milliseconds. |
| 13 | `list_attendance` | It loads every record into Python, sorts by `date` only and slices in Python. Sorting has no `emp_code` tiebreaker. | Read the code; it cannot scale to 100k records. | Filter, sort `(date desc, emp_code asc)`, count, skip and limit in MongoDB, with a matching index. |
| 14 | `list_attendance` contract | It returns `id`, raw datetimes, and does not default `half_day`, `history`, `late_minutes`, `overtime_minutes` on legacy records. It does not validate `page`, `page_size`, `status` or `date_from > date_to`. | Compared the output with DATA_MODEL.md (legacy record, sample 9). | `api_log` fills the defaults; validation returns 422. |
| 15 | Startup | No indexes are created, so uniqueness is not guaranteed and queries scan the collection. | Listed indexes: only `_id`. | Create the indexes at startup (idempotent). |

## Looked at and not a defect

- `load_dotenv()` does not override variables already set in the environment, so the real environment wins, as required.
- Synchronous `pymongo` inside plain `def` endpoints is fine: FastAPI runs them in a thread pool.
- `{"_id": 0}` in the `list_employees` projection correctly hides the internal id.
- Status code 201 on the create endpoints is correct.
