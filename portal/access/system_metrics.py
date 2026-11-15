"""On-demand host snapshots from node_exporter over the private network."""
import math
import time
from datetime import datetime, timezone

import requests
from prometheus_client.parser import text_string_to_metric_families


def fetch_samples(url):
    with requests.get(url, timeout=(3, 10), allow_redirects=False, stream=True) as response:
        if response.status_code != 200:
            raise ValueError('Metrics endpoint did not return HTTP 200.')
        body = bytearray()
        for chunk in response.iter_content(chunk_size=65536):
            body.extend(chunk)
            if len(body) > 2 * 1024 * 1024:
                raise ValueError('Metrics response exceeds 2 MiB.')
    wanted = {
        'node_cpu_seconds_total', 'node_memory_MemTotal_bytes', 'node_memory_MemAvailable_bytes',
        'node_filesystem_size_bytes', 'node_filesystem_free_bytes', 'node_filesystem_avail_bytes', 'node_uname_info',
    }
    return [sample for family in text_string_to_metric_families(body.decode('utf-8'))
            for sample in family.samples if sample.name in wanted or sample.name.startswith('fleet_oracle_')]


def cpu_counters(samples):
    counters = {}
    for sample in samples:
        if sample.name == 'node_cpu_seconds_total' and sample.labels['mode'] not in ('guest', 'guest_nice'):
            if not math.isfinite(sample.value) or sample.value < 0:
                raise ValueError('Invalid CPU counter.')
            counters[(sample.labels['cpu'], sample.labels['mode'])] = sample.value
    if not counters:
        raise ValueError('Host CPU metrics missing.')
    return counters


def pressure(percent):
    return 'critical' if percent >= 90 else 'warning' if percent >= 80 else 'normal'


def database_snapshot(samples):
    def one(name):
        values = [s.value for s in samples if s.name == name]
        if len(values) != 1 or not math.isfinite(values[0]) or values[0] < 0:
            raise ValueError('Database metric missing or invalid.')
        return values[0]
    try:
        if one('fleet_oracle_db_report_available') != 1:
            raise ValueError('Database report unavailable.')
        stamp = one('fleet_oracle_db_last_collected_timestamp_seconds')
        age = datetime.now(timezone.utc).timestamp() - stamp
        if age < -300:
            raise ValueError('Database report timestamp is in the future.')
        tablespaces = []
        for sample in samples:
            if sample.name == 'fleet_oracle_tablespace_used_percent':
                if not math.isfinite(sample.value) or not 0 <= sample.value <= 100:
                    raise ValueError('Invalid tablespace usage.')
                tablespaces.append({'name': sample.labels['tablespace'], 'percent': sample.value,
                                    'pressure': pressure(sample.value)})
        if not tablespaces:
            raise ValueError('Tablespace metrics missing.')
        return {
            'collected_at': datetime.fromtimestamp(stamp, timezone.utc).isoformat(),
            'stale': age > 7200, 'allocated': int(one('fleet_oracle_db_allocated_bytes')),
            'tablespaces': sorted(tablespaces, key=lambda t: t['percent'], reverse=True),
        }
    except (ValueError, KeyError, OverflowError, OSError):
        return {'error': 'Existing database report unavailable or invalid; check the host report adapter.'}


def snapshot(server):
    before = cpu_counters(fetch_samples(server.metrics_url))
    started = time.monotonic()
    time.sleep(1)
    samples = fetch_samples(server.metrics_url)
    after = cpu_counters(samples)
    window = time.monotonic() - started
    if before.keys() != after.keys() or any(after[key] < before[key] for key in before):
        raise ValueError('CPU counters changed/reset during the sample.')
    deltas = {key: after[key] - before[key] for key in before}
    total = sum(deltas.values())
    if total <= 0:
        raise ValueError('CPU sample has no elapsed counters.')
    idle = sum(value for (_, mode), value in deltas.items() if mode in ('idle', 'iowait'))
    steal = sum(value for (_, mode), value in deltas.items() if mode == 'steal')
    cpu = round(100 * max(0, total - idle - steal) / total, 1)

    def number(name, labels=None):
        matches = [s.value for s in samples if s.name == name and
                   all(s.labels.get(key) == value for key, value in (labels or {}).items())]
        if len(matches) != 1 or not math.isfinite(matches[0]) or matches[0] < 0:
            raise ValueError(f'Missing or ambiguous host metric: {name}')
        return matches[0]

    memory_total = number('node_memory_MemTotal_bytes')
    memory_available = number('node_memory_MemAvailable_bytes')
    if memory_total <= 0 or memory_available > memory_total:
        raise ValueError('Invalid host memory values.')
    memory_used = memory_total - memory_available
    memory_percent = round(100 * memory_used / memory_total, 1)
    disks = []
    for mountpoint in server.mountpoint_list:
        labels = {'mountpoint': mountpoint}
        size = number('node_filesystem_size_bytes', labels)
        free = number('node_filesystem_free_bytes', labels)
        available = number('node_filesystem_avail_bytes', labels)
        if size <= 0 or not 0 <= available <= free <= size:
            raise ValueError('Invalid filesystem values.')
        used = size - free
        percent = round(100 * used / (used + available), 1) if used + available else 100.0
        disks.append({'mountpoint': mountpoint, 'total': int(size), 'used': int(used),
                      'available': int(available), 'percent': percent, 'pressure': pressure(percent)})
    hostnames = [s.labels.get('nodename', '') for s in samples if s.name == 'node_uname_info']
    result = {
        'hostname': hostnames[0] if hostnames else server.name,
        'cpu_percent': cpu, 'cpu_count': len({core for core, _ in after}),
        'cpu_pressure': pressure(cpu), 'steal_percent': round(100 * steal / total, 1),
        'sample_seconds': round(window, 1),
        'memory_total': int(memory_total), 'memory_used': int(memory_used),
        'memory_available': int(memory_available), 'memory_percent': memory_percent,
        'memory_pressure': pressure(memory_percent), 'disks': disks,
    }
    if server.database_metrics:
        result['database'] = database_snapshot(samples)
    return result
