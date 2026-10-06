"""Set or replace the local fan-control password without storing plaintext."""

import argparse
import getpass
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fan_auth import PASSWORD_PATH, password_record


def main():
    parser = argparse.ArgumentParser(description="Set the password for fan-control changes.")
    parser.parse_args()
    try:
        password = getpass.getpass("New fan-control password (12-1024 characters): ")
        if not 12 <= len(password) <= 1024:
            parser.error("Use a password of 12 to 1024 characters.")
        if password != getpass.getpass("Confirm password: "):
            parser.error("Passwords do not match; nothing was changed.")
        temporary_path = PASSWORD_PATH.with_suffix(".json.tmp")
        temporary_path.write_text(
            json.dumps(password_record(password), indent=2) + "\n", encoding="utf-8"
        )
        temporary_path.replace(PASSWORD_PATH)
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled; nothing was changed.", file=sys.stderr)
        return 1
    print("Password saved. Restart SoulGemMonitor to apply it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
