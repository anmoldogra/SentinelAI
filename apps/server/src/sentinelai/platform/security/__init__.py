"""Security primitives (guide Part 8, ADR-0010).

Framework-free cryptographic primitives for authentication: password hashing (``hashing``) and
opaque bearer-token generation (``tokens``). Key material and signing/encryption go through
``platform.crypto`` (ADR-0009); this package holds only the auth-adjacent primitives. The full
session/RBAC/ABAC model that consumes them is a later wave.
"""
