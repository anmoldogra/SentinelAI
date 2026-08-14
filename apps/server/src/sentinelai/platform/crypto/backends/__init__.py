"""SentinelAI — platform.crypto.backends package.

Concrete ``CryptoProvider`` implementations. Dev (software) and Vault Transit are implemented;
AWS KMS / Azure Key Vault / GCP Cloud KMS / PKCS#11 are registered slots added behind the same
port. Nothing here is imported outside ``platform.crypto``.
"""
