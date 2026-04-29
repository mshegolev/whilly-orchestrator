"""Alembic migrations for Whilly v4.0 Postgres schema (PRD FR-2.1, FR-2.4).

The migration directory itself is a regular Python package so it can be
imported by tests / helpers (for instance to discover the latest revision id
without spinning up Alembic). Alembic's CLI does not require the package to
exist, but ``setuptools.find_packages`` does — without this file the migrations
folder would be silently dropped from the wheel.
"""
