# Decisions

1. **Indexes.** A unique employee code index protects customer identity. Department/code and department/join-date indexes support listing and trend lookups; joined-date supports unfiltered monthly headcount. Attendance has a unique employee/date index for one record per shift day and race-safe punch-in. Date/code supports sorted attendance pages and monthly leaderboard scans. I considered indexing status alone and rejected it: status is low-cardinality and does not help the main date range queries.

2. **Punch-in race.** Both requests can pass the employee lookup and attempt insertion. MongoDB's unique `(emp_code, date)` index serializes the conflicting writes: one insert succeeds and returns 201; the other receives `DuplicateKeyError`, translated to 409. No application-level check decides the winner.

3. **Ties.** `$rank` gives tied totals the same rank and skips the next rank. I return all rows with rank at or below the requested limit, ordered by total minutes and then code. A tie at the cutoff can therefore produce extra rows.

4. **Headcount.** The summary begins with employees joined by month end and looks up their logs. Employees with no logs remain in the employee stream with zero-valued stats; later joiners are excluded.

5. **100x data.** I would measure the hot plans, then consider pre-aggregated monthly summaries and a date-partition strategy while preserving correction updates.
