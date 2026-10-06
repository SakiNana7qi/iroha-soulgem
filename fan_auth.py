"""Password verification for fan-control writes, separate from BMC credentials."""

import hashlib
import hmac
import json
import logging
from pathlib import Path
import secrets


PASSWORD_PATH = Path(__file__).parent / "fanctl-password.json"
ITERATIONS = 600_000


def password_record(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS)
    return {
        "algorithm": "pbkdf2_sha256",
        "iterations": ITERATIONS,
        "salt": salt.hex(),
        "digest": digest.hex(),
    }


class FanControlPassword:
    def __init__(self, record):
        if record["algorithm"] != "pbkdf2_sha256" or record["iterations"] != ITERATIONS:
            raise ValueError("Unsupported fan-control password format")
        self.salt = bytes.fromhex(record["salt"])
        self.digest = bytes.fromhex(record["digest"])
        if len(self.salt) != 16 or len(self.digest) != 32:
            raise ValueError("Invalid fan-control password hash")

    @classmethod
    def load(cls, path=PASSWORD_PATH):
        try:
            return cls(json.loads(path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError):
            logging.getLogger("fanctl.auth").warning(
                "Fan-control password file is invalid or unreadable; writes are locked"
            )
            return None

    def verify(self, password):
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), self.salt, ITERATIONS
        )
        return hmac.compare_digest(digest, self.digest)
