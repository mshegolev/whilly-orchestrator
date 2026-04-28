"""I/O-side of the Whilly v4.0 hexagonal architecture (PRD TC-8 / SC-6).

Everything that touches the network, the filesystem, a database, or a
subprocess lives under :mod:`whilly.adapters`. The pure domain layer
(:mod:`whilly.core`) is forbidden — by ``.importlinter`` — from importing
anything in here.
"""
