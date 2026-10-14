# REVIEW.md

## Scope

This review identifies potential defects in the original starter code, the tests that expose them, and the corresponding fixes. Each item must be verified against the original starter and final implementation before submission.

## Defects Reviewed

| # | Location | Defect | Test or Symptom | Fix |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Incorrect handling of the strict 10-minute grace period. | For a 09:30 shift, 09:40:00 should yield 0 late minutes; 09:40:01 should yield 10. | Calculate elapsed time in IST and apply the strict 600-second boundary. |
| 2 | `compute_work_hours` | Rounding may not follow the required half-up rule. | Test a duration that produces a rounding tie. | Use `Decimal` with `ROUND_HALF_UP`. |
| 3 | `compute_overtime` | Missing minimum overtime threshold or overnight-shift handling. | A shift ending at 18:30 with punch-out at 18:45 should yield 0 overtime minutes. | Handle overnight shifts and count overtime only when it reaches 30 minutes. |
| 4 | `EmployeeIn` | Inadequate validation of employee fields, shift times, or joining dates. | Submit an invalid time such as `25:99`. | Add strict field validation and return HTTP 422 for invalid input. |
| 5 | `PunchInIn` | Invalid statuses or timestamps may be accepted. | Submit an unsupported status or a seconds-based timestamp. | Validate allowed statuses and integer epoch-millisecond timestamps. |
| 6 | `health` | Database availability may not be checked. | Disconnect MongoDB and call `/health`. | Ping MongoDB and report service unavailability when appropriate. |
| 7 | `create_employee` | Concurrent requests may create duplicate employee codes without a unique index. | Send simultaneous requests with the same employee code. | Add a unique index and translate duplicate-key errors to HTTP 409. |
| 8 | `list_employees` | Pagination offsets, filtered totals, or ordering may be incorrect. | Compare pages and totals after applying a department filter. | Use `(page - 1) * page_size`, count filtered records, and sort deterministically. |
| 9 | Punch-in employee lookup | An unknown employee may cause a server error instead of HTTP 404. | Punch in with an unknown employee code. | Check whether the employee exists before accessing its fields. |
| 10 | Punch-in date calculation | Incorrect timezone or overnight-shift handling may assign the wrong attendance date. | Test timestamps near midnight IST and early-morning overnight shifts. | Convert timestamps to IST and apply the required shift-date rule. |
| 11 | `punch_in` concurrency | Concurrent punch-ins may create duplicate daily records without a database constraint. | Send simultaneous punch-ins for the same employee and date. | Enforce a unique `(emp_code, date)` index and handle duplicate-key errors. |
| 12 | Punch-in response | Database identifiers or raw datetimes may violate the API response contract. | Inspect the response fields and timestamp formats. | Exclude `_id` and serialize timestamps as epoch milliseconds where required. |
| 13 | `list_attendance` | Loading and sorting all records in Python can be inefficient and affect pagination correctness. | Test filtering, ordering, pagination, and a large dataset. | Perform filtering, sorting, counting, and pagination in MongoDB. |
| 14 | Startup index creation | Missing indexes can weaken uniqueness guarantees and slow common queries. | Inspect indexes and query execution plans. | Create required indexes idempotently at startup. |
| 15 | `MongoClient` configuration | Missing timeouts can make requests wait too long when MongoDB is unavailable. | Make MongoDB unreachable and observe failure behavior. | Configure connection and server-selection timeouts and handle connection failures. |

## Reviewed and Not Considered Defects

- Excluding `_id` using `{"_id": 0}` is appropriate when database identifiers must not be exposed.
- Calling `load_dotenv()` without overriding existing environment variables preserves environment-variable precedence.
- Derived attendance fields should be calculated by the application rather than trusted from client input.