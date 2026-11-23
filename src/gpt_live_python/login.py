import hashlib
import secrets

ITERATIONS = 600_000


def password_hash(password: str) -> str:
    if not 8 <= len(password) <= 128:
        raise ValueError("Password must contain 8 to 128 characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt.hex()}${digest.hex()}"


def validate_password_hash(value: str) -> None:
    try:
        algorithm, iterations, salt, digest = value.split("$")
        valid = (
            algorithm == "pbkdf2_sha256"
            and iterations == str(ITERATIONS)
            and len(bytes.fromhex(salt)) == 16
            and len(bytes.fromhex(digest)) == 32
        )
    except (ValueError, TypeError):
        raise ValueError("Invalid configured password hash") from None
    if not valid:
        raise ValueError("Invalid configured password hash")


def verify_password(password: str, configured: str) -> bool:
    _, _, salt, expected = configured.split("$")
    actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), ITERATIONS)
    return secrets.compare_digest(actual.hex(), expected)
