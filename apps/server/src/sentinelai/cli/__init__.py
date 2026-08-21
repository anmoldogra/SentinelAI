"""Operator/developer command-line tools for the backend.

A third entrypoint alongside ``entrypoints/http`` and ``entrypoints/worker``, and it follows the
same rules: it owns its transaction (ADR-0005), validates configuration before opening a
connection, and writes audit entries through ``record_audit_event`` rather than touching
``platform.audit_log`` directly.
"""
