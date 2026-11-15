"""Expose only storage totals/freshness from an existing Oracle report; no DB calls."""
import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import tempfile


def convert(text):
    generated = re.search(r'_Generated: ([0-9-]+ [0-9:]+) UTC', text)
    allocated = re.search(r'\*\*Total allocated: ([0-9.]+) GB\*\*', text)
    if not generated or not allocated:
        raise ValueError('Report header/storage not available.')
    stamp = datetime.strptime(generated[1], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc).timestamp()
    size = float(allocated[1]) * 1024 ** 3
    if not math.isfinite(size) or size < 0:
        raise ValueError('Invalid allocated size.')
    lines = ['fleet_oracle_db_report_available 1',
             f'fleet_oracle_db_last_collected_timestamp_seconds {stamp}',
             f'fleet_oracle_db_allocated_bytes {size}']
    storage = text.split('## Storage', 1)[-1].split('## Schemas', 1)[0]
    count = 0
    for line in storage.splitlines():
        cells = [cell.strip() for cell in line.strip().strip('|').split('|')]
        if len(cells) != 5:
            continue
        match = re.match(r'^([0-9.]+)%', cells[1])
        if not match:
            continue
        value = float(match[1])
        if not math.isfinite(value) or not 0 <= value <= 100:
            raise ValueError('Invalid tablespace percentage.')
        label = json.dumps(cells[0], ensure_ascii=False)
        lines.append(f'fleet_oracle_tablespace_used_percent{{tablespace={label}}} {value}')
        count += 1
    if not count:
        raise ValueError('No tablespace observations.')
    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    failed = False
    try:
        with args.input.open(encoding='utf-8') as source:
            text = source.read(2 * 1024 * 1024 + 1)
        if len(text) > 2 * 1024 * 1024:
            raise ValueError('Report too large.')
        metrics = convert(text)
    except (OSError, ValueError):
        failed = True
        metrics = 'fleet_oracle_db_report_available 0\n'
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=args.output.parent, suffix='.tmp', delete=False) as output:
        output.write(metrics)
    os.chmod(output.name, 0o644)
    os.replace(output.name, args.output)
    if failed:
        raise SystemExit('Database report unavailable; exported availability=0.')


if __name__ == '__main__':
    main()
