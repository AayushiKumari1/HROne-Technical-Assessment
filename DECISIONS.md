# DECISIONS.md

## 1. Index choices

I use a unique index on `employees.emp_code` to enforce employee-code uniqueness and support employee lookups. A unique compound index on `logs.emp_code` and `logs.date` prevents duplicate daily attendance records. Additional indexes support attendance sorting, punch-out lookups, late-arrival leaderboard queries, and employee headcount filtering. I would avoid an index on `status` alone unless query analysis demonstrates a benefit, because additional indexes consume storage and increase write costs.

## 2. Simultaneous punch-ins

Two requests can attempt to insert attendance for the same employee and date concurrently. The unique compound index allows only one insert to succeed. The other request encounters `DuplicateKeyError`, which the API converts into HTTP 409 Conflict. This database-level constraint is safer than relying on a check-then-insert sequence.

## 3. Leaderboard cutoff ties

The leaderboard uses MongoDB ranking so employees with equal late-minute totals receive the same rank. Filtering is applied after ranking, allowing all employees tied at the cutoff rank to appear. Therefore, the result may contain more rows than the requested limit.

## 4. Department headcount

Headcount includes employees who joined on or before the end of the selected month, including employees without attendance logs. Starting from eligible employees and preserving unmatched lookup results prevents these employees from being excluded.

## 5. Scaling to 100 times the data

I would inspect query execution plans and benchmark the most expensive analytics first. Monthly pre-aggregated summaries could reduce repeated aggregation work, while source attendance records remain available for verification. I would also evaluate keyset pagination for large attendance listings and validate improvements using realistic load tests before introducing additional infrastructure.
