"""Operator tooling, run by hand rather than by the pipeline.

A package rather than a directory of loose files, so the consent trips can
share `scripts/consent.py` instead of carrying three copies of the same
registration call. Nothing in `pipeline/` or `gateway/` imports from here.
"""
