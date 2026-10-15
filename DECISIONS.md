# DECISIONS.md

1. **Indexes.** `uq_employee_code` and `uq_attendance_employee_date` (emp_code, date) are unique, so there is one employee per code and one record per employee per day. `attendance_date_emp` serves the attendance list sort. `attendance_late_date` (date, late_minutes) serves the leaderboard. `employee_department_joined` and `employee_joined_on` serve headcount. `attendance_employee_punch_in` finds the record to punch out. I rejected an index on `status` alone: it is rarely the only filter and every index slows writes.

2. **Punch-in race.** Both requests call `insert_one`. The unique (emp_code, date) index lets one succeed. The other raises `DuplicateKeyError`, and I return 409. There is no check-then-insert.

3. **Ties.** I use `$rank`, so tied employees share a rank (1, 2, 2, 4), and I filter `rank <= limit` afterwards. Everyone tied at the cutoff is returned, so the list can be longer than `limit`.

4. **Headcount.** I start from `employees` (joined on or before month end) and `$lookup` their logs with `preserveNullAndEmptyArrays`, so employees with zero logs still count.

5. **Change at 100x.** I would copy `department` into each attendance record, so the trend and leaderboard don't need a `$lookup` to `employees` for every log.
