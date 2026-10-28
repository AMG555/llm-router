"""Rotate the at-rest encryption key (SECRET_KEY).

Re-encrypts every secret stored in the database from the old key to the new
key without changing the plaintext. Run offline against the database file:

    python -m app.rotate --db ./data/router.db --old "$OLD" --new "$NEW"

Use `--old ""` when the current data is plaintext (encryption was disabled) and
`--new ""` to decrypt back to plaintext. Passwords/secrets never appear on
stdout; only a per-table count is printed.
"""
import argparse
import sqlite3
import sys

from .db import SENSITIVE
from .crypto import SecretBox


def rotate(db_path: str, old_key: str, new_key: str) -> dict:
    old_box = SecretBox(old_key)
    new_box = SecretBox(new_key)
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    counts = {}
    for table, fields in SENSITIVE.items():
        try:
            rows = list(con.execute(f"SELECT * FROM {table}"))
        except sqlite3.OperationalError:
            continue  # table not present in this database
        changed = 0
        for row in rows:
            for f in fields:
                val = row[f]
                if not val:
                    continue
                plain = old_box.decrypt(val)          # validates the old key
                con.execute(f"UPDATE {table} SET {f}=? WHERE id=?",
                            (new_box.encrypt(plain), row["id"]))
                changed += 1
        counts[table] = changed
    con.commit()
    con.close()
    return counts


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Rotate the at-rest encryption key")
    ap.add_argument("--db", required=True, help="path to router.db")
    ap.add_argument("--old", required=True, help="current SECRET_KEY ('' if plaintext)")
    ap.add_argument("--new", required=True, help="new SECRET_KEY ('' to decrypt)")
    args = ap.parse_args(argv)
    try:
        counts = rotate(args.db, args.old, args.new)
    except Exception as e:
        print(f"rotation failed: {e}", file=sys.stderr)
        return 1
    for table, n in counts.items():
        print(f"re-encrypted {n} field(s) in {table}")
    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
