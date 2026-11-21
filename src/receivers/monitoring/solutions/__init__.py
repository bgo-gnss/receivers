"""Daily GNSS POS solution monitoring (slice 1: facts only, no database).

See ``docs/architecture/daily-solutions-implementation-plan.md``. This package
reports *facts* about station-days (valid / invalid / absent / unverifiable).
It never derives the six display states; that is the job of the SQL function
``solution_day_grid`` (slice 2), and there must be no Python copy of it.
"""
