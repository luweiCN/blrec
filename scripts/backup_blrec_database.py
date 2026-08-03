from __future__ import annotations

import argparse
import re
import sqlite3
from datetime import datetime
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--database', default='/cfg/blrec.sqlite3')
    parser.add_argument('--backup-dir', default='/cfg/backups')
    parser.add_argument('--label', required=True)
    args = parser.parse_args()

    label = args.label.strip()
    if not re.fullmatch(r'[A-Za-z0-9._-]+', label):
        raise ValueError('label contains unsupported characters')

    database_path = Path(args.database).resolve()
    backup_dir = Path(args.backup_dir).resolve()
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    backup_path = backup_dir / f'blrec-before-{label}-{timestamp}.sqlite3'
    if backup_path.exists():
        raise FileExistsError(backup_path)

    source = sqlite3.connect(database_path)
    try:
        backup = sqlite3.connect(backup_path)
        try:
            source.backup(backup)
            integrity = backup.execute('PRAGMA quick_check').fetchone()
            if integrity is None or integrity[0] != 'ok':
                raise RuntimeError(f'backup quick_check failed: {integrity!r}')
        finally:
            backup.close()
    finally:
        source.close()

    print(f'backup={backup_path.name} bytes={backup_path.stat().st_size}')


if __name__ == '__main__':
    main()
