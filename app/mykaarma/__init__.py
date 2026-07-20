"""myKaarma DMS connector.

    client.py     — HTTP Basic client with scope detection (non-JSON => not granted)
    connector.py  — credential resolution + sync into 3D Dispatch tables

The RO ingestion path is built but gated: myKaarma has not granted our sandbox
account repair-order scope, so that data still comes from the CSV importer. The
connector detects this honestly and falls back — it never fabricates RO data.
"""
