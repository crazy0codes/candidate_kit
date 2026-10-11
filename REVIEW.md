# Starter review

| # | Where | What is wrong | How you'd notice | How it was fixed |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Compares naive and timezone-aware values inconsistently and only works for same-day shifts. | Punch-in around midnight or timezone-aware input. | Replaced with IST shift-boundary calculation using the attendance day. |
| 2 | `compute_late_minutes` | Truncates the result instead of truncating the instant first; boundary seconds are wrong. | 09:40:00.900 and 09:40:01 test cases. | Instants are truncated to whole seconds at the boundary; grace comparison is strict. |
| 3 | `compute_overtime` | End boundary is always on the punch-in day, so overnight shifts fail. | 22:00–06:00 shift punched out at 06:40 next day. | Resolve the shift end on the next calendar day for overnight shifts. |
| 4 | `compute_overtime` | Counts any positive minute as overtime. | Punch out 20 minutes after shift end. | Overtime is recorded only at 30 or more full minutes. |
| 5 | `compute_work_hours` | Python `round` uses ties-to-even and binary floats. | A duration landing exactly at a half-cent rounding boundary. | Use `Decimal` with `ROUND_HALF_UP`. |
| 6 | `create_employee` | Duplicate check then insert races; MongoDB uniqueness was not enforced. | Parallel creates with the same code. | Unique `emp_code` index and translate duplicate-key errors to 409. |
| 7 | `create_employee` | Returned `created_at` is a BSON/Python datetime instead of epoch milliseconds. | Inspect response type. | Convert instants to integer milliseconds at the API boundary. |
| 8 | `list_employees` | Skip uses `page * page_size`, dropping page one. | Request page=1 and check first employee. | Use `(page - 1) * page_size`. |
| 9 | `list_employees` | Total count ignores the department filter and documents are unsorted. | Filter department or compare stable page order. | Count filtered query; sort by `emp_code`. |
| 10 | `punch_in` | Does not return 404 for an unknown employee and later crashes on missing shift data. | Punch in with unknown code. | Check employee first and return 404. |
| 11 | `punch_in` | Uses local machine time and seconds instead of validated epoch milliseconds. | Set a different server timezone; send seconds or float. | Strict epoch-ms validation, UTC conversion, whole-second truncation. |
| 12 | `punch_in` | Attendance date ignores IST and overnight-shift assignment. | Punch in after midnight on an overnight shift. | Compute attendance date using IST and shift boundaries. |
| 13 | `punch_in` | Duplicate check-then-insert is racy. | Parallel punch-ins for one employee/date. | Unique compound index on `(emp_code, date)` decides the winner atomically. |
| 14 | `list_attendance` | Loads the entire matching collection and sorts/pages in Python. | Large dataset or memory profiling. | MongoDB count, sort, skip and limit use indexed queries. |
| 15 | `list_attendance` | Pagination skip was wrong, no validation/order for date range, and `_id` leaked. | Page 1, inverted date range, or inspect response. | Correct offset, validate inclusive date bounds, serialize only contract fields. |
| 16 | `health` | Always says ready without checking MongoDB. | Stop MongoDB and call `/health`. | Ping MongoDB and return 503 on failure. |
| 17 | API models | Starter accepts malformed codes, timestamps, shift times, and unexpected derived inputs. | Invalid request bodies and derived fields. | Added constrained Pydantic request models; derived client fields are ignored. |
| 18 | Missing endpoints | Punch-out, regularization, analytics and explain operations were absent. | Call any contract route marked for implementation. | Implemented the contract routes and their error paths. |

**Reviewed and retained:** `compute_work_hours`' duration basis (elapsed seconds divided by 3600) matches R4; the employee identity is correctly based on `emp_code`, not MongoDB `_id`; and `load_dotenv()` remains appropriate because actual environment variables take precedence over `.env` values.
