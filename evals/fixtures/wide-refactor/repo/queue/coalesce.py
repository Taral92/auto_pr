"""On a new push, supersede queued runs for the same pull request."""

SUPERSEDE_SQL = """
UPDATE runs SET status = 'superseded'
WHERE repo = %s AND number = %s AND status = 'queued'
"""
