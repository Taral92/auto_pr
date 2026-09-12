SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    key   text PRIMARY KEY,
    payload bytea NOT NULL,
    etag  text
);
"""
