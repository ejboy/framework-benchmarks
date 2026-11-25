#!/usr/bin/env python3
"""Run a benchmark suite or regenerate its offline evidence report."""
import argparse
from datetime import datetime, timezone
from html import escape
import hashlib
import importlib
import json
import math
from pathlib import Path
import platform
import re
import signal
import subprocess
from urllib.parse import quote

import benchmark_config

runner = importlib.import_module('colocated-run')
memory = importlib.import_module('colocated-summary')

STACKS = {
    'jex-coilbase': 'Avaje Jex + CoilBase + HikariCP',
    'quarkus-coilbase': 'Quarkus REST + CoilBase + Agroal',
    'spring-boot': 'Spring Boot (Tomcat) + Spring JDBC + HikariCP',
}
FOUR_CANDIDATE_STACKS = {**STACKS, 'micronaut-data-jdbc': 'Micronaut HTTP + Data JDBC + HikariCP'}
PUBLIC_FIXTURE_SHA256 = 'c0aa93e19e0dead195427852c7743a5b31949e2bacc0ea1da41f9298fb94aae1'
STACK_JARS = re.compile(r'^(?:.*\.)?(?:avaje-jex|avaje-http-api|coilbase-core|coilbase-jex|'
                        r'HikariCP|postgresql|quarkus-rest|agroal-pool|spring-boot|'
                        r'spring-jdbc|tomcat-embed-core|micronaut-http-server-netty|micronaut-data-jdbc|micronaut-jdbc-hikari)-[0-9].*\.jar$')
POSTGRES = dict(max_connections='10', superuser_reserved_connections='1', jit='off',
                max_parallel_workers_per_gather='0', max_parallel_workers='0',
                shared_buffers='2048 8kB', work_mem='1024 kB',
                maintenance_work_mem='16384 kB', io_method='worker', fsync='on',
                autovacuum='on', wal_level='replica')
PHASES = ('startup', 'idle', 'light', 'warmup', 'sustained')
ROSTER_ERROR = 'Expected exactly one bundle or manifest entry for each of: ' + ', '.join(STACKS)
MAX_SAMPLE_GAP_SECONDS = 3


def protocol(preparation=False, version='3.4-mvp', *, archived=False, config=None):
    values = benchmark_config.STANDARD if config is None else config
    settings = dict(id='3.4-mvp-preparation' if preparation else '3.4-mvp',
                jvm=list(values['jvm']),
                guest='QEMU/KVM, 1 vCPU, 512 MiB allocated, zero swap',
                postgres=POSTGRES, pool_max=4, quote_poll_seconds=60,
                replay_step_seconds=3600, stabilize_seconds=1 if preparation else 10,
                idle_seconds=runner.PREPARATION_IDLE_SECONDS if preparation else 30,
                light_seconds=3 if preparation else 30,
                warmup_seconds=3 if preparation else 15,
                sustained_seconds=3 if preparation else 30,
                light_rate=5, sustained_rate=40, workers=8, users=1000, seed=42,
                sample_window_markers=True,
                operations=dict(portfolio=40, edit=25, valuation=25, latest=8, history=2))
    if version == 'v1':
        protocol_id = 'v1' if values == benchmark_config.STANDARD else 'v1-custom'
        settings.update(id=protocol_id + ('-preparation' if preparation else ''),
                        rounds=2, warmup_seconds=3 if preparation else 125,
                        sustained_seconds=3 if preparation else 185,
                        jdk_build=values['jdk_build'], timeout_seconds=5)
        if not archived:
            settings.update(fixture_sha256=PUBLIC_FIXTURE_SHA256, postgres_version='18.7')
        if config is not None:
            settings['java_executable'] = values['java_executable']
    return settings


LABELS = {
    'jex-coilbase': 'Jex + Jdbi',
    'quarkus-coilbase': 'Quarkus + Jdbi',
    'spring-boot': 'Spring Boot + JDBC',
    'micronaut-data-jdbc': 'Micronaut Data JDBC',
}
IMPLEMENTATIONS = {
    'jex-coilbase': 'Jex + CoilBase + Jdbi + HikariCP',
    'quarkus-coilbase': 'Quarkus REST + CoilBase + Jdbi + Agroal',
    'spring-boot': 'Spring Boot + Spring JDBC + HikariCP',
    'micronaut-data-jdbc': 'Micronaut HTTP + Micronaut Data JDBC + HikariCP',
}


def candidate_label(candidate):
    label = LABELS[candidate["id"]]
    if "round" in candidate:
        return f"{label} · Round {candidate['round']}"
    return label


def round_path(number):
    return f'round-{number:02d}'


def read_json(path):
    return json.loads(path.read_text())


def persist(path, data):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data, indent=2)+'\n')
    temp.replace(path)


def check(condition, reason):
    if not condition:
        raise ValueError(reason)


def positive(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def validate_sample_coverage(timestamps, duration, bounds=None):
    check(len(timestamps) >= max(2, math.ceil(duration/2)) and all(map(positive, timestamps)),
          'Insufficient memory sample coverage')
    check(all(0 < end-start <= MAX_SAMPLE_GAP_SECONDS
              for start, end in zip(timestamps, timestamps[1:])),
          'Memory sample timestamps are out of order or more than 3 seconds apart')
    if bounds is None:
        # Earlier MVP evidence did not record both idle and warm-up edges.
        check(timestamps[-1]-timestamps[0] >= duration-MAX_SAMPLE_GAP_SECONDS,
              'Memory samples do not span the phase')
    else:
        start, end = bounds
        check(0 <= timestamps[0]-start <= MAX_SAMPLE_GAP_SECONDS
              and 0 <= end-timestamps[-1] <= MAX_SAMPLE_GAP_SECONDS,
              'Memory samples do not cover the phase boundaries')


def inspect_candidate(root, candidate, settings):
    run = root/candidate['evidence']
    guest = run/'guest'
    errors = list(candidate.get('execution_errors', []))
    issues = (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError)

    def read_phase(operation):
        try:
            return operation()
        except FileNotFoundError as exc:
            return dict(status='missing' if candidate['execution'] != 'pending' else 'incomplete',
                        reason=str(Path(exc.filename).relative_to(run))+' unavailable')
        except issues as exc:
            return dict(status='incomplete', reason=str(exc))

    def summary():
        result = read_json(guest/'summary.json')
        check(result['candidate'] == candidate['id'], 'Guest candidate identity mismatch')
        return result

    def sample_metrics(phase, prefix, duration):
        rows = samples[phase]
        check(summary()['sample_counts'][phase] == len(rows), 'Sample count mismatch')
        start_path = guest/f'{prefix}-start.json'
        end_path = guest/f'{prefix}-end.json'
        bounds = None
        legacy = (prefix in ('idle', 'warmup') and not settings.get('sample_window_markers')
                  and not start_path.exists() and not end_path.exists())
        if not legacy:
            check(start_path.is_file() and end_path.is_file(), 'Guest phase window markers are missing')
            start = read_json(start_path)['monotonic']
            end = read_json(end_path)['monotonic']
            check(positive(start) and positive(end) and end-start >= duration, 'Guest phase window is incomplete')
            bounds = (start, end)
            rows = memory.samples_within(rows, start, end)
        validate_sample_coverage([row['monotonic'] for row in rows], duration, bounds)
        item = memory.summarize_phase(rows)
        return dict(app_rss_mib=item['app']['process_rss_max_kib']/1024,
                    app_pss_mib=item['app']['process_pss_max_kib']/1024,
                    postgres_pss_mib=item['postgres']['sampled_pss_sum_max_kib']/1024,
                    available_mib=item['mem_available_min_kib']/1024,
                    samples=item['samples'])

    def startup():
        info = summary()
        if not positive(info.get('ready_seconds')):
            return dict(status='failed' if info['status'] == 'failed' else 'incomplete',
                        reason='Application readiness was not recorded')
        check(read_json(guest/'ready.json')['status'] == 'UP', 'Readiness did not return UP')
        check(positive(info['postgres_start_seconds']), 'Invalid PostgreSQL startup time')
        return dict(status='completed', ready_seconds=info['ready_seconds'],
                    postgres_seconds=info['postgres_start_seconds'])

    def idle():
        check((guest/'connections-after-idle.txt').stat().st_size > 0, 'Idle check is empty')
        check(summary()['idle_seconds'] == settings['idle_seconds'], 'Idle duration mismatch')
        return dict(status='completed', **sample_metrics('idle', 'idle', settings['idle_seconds']))

    def load(name, phase, rate, window):
        status = read_json(run/f'{name}-status.json')
        if status['status'] not in ('completed', 'workload-failed'):
            return dict(status='incomplete', reason=status.get('reason', 'Driver did not finish'))
        config = read_json(run/f'{name}-config.json')
        expected = dict(rate=rate, duration_seconds=settings[window], seed=settings['seed'],
                        users=settings['users'], operations=settings['operations'],
                        concurrency=settings['workers'], pending_capacity=settings['workers'])
        if 'timeout_seconds' in settings:
            expected['timeout_seconds'] = settings['timeout_seconds']
        check(all(config[key] == value for key, value in expected.items()), 'Suite workload mismatch')
        data = read_json(run/f'{name}.json')
        runner.validate_load(data, config, status['wall_seconds'])
        result = dict(offered=data['offered'], completed=data['completed'], successful=data['successful'],
                      errors=data['errors'], timeouts=data['timed_out'], unsent=data['unsent'],
                      achieved_rps=data['achieved_requests_per_second_including_drain'])
        if data['latency_ms']:
            result.update(p95_ms=data['latency_ms']['p95'], p99_ms=data['latency_ms']['p99'])
        for source, metric in [('dispatch_lag_ms', 'dispatch_p99_ms'),
                               ('queue_wait_ms', 'queue_p99_ms'), ('http_ms', 'http_p99_ms')]:
            if data[source]:
                result[metric] = data[source]['p99']
        if status['status'] == 'workload-failed' or data['errors'] or data['unsent'] or status['exit_code']:
            return dict(result, status='failed', reason=status.get('reason', 'Workload failed'))
        try:
            if phase == 'warmup':
                (guest/'measurement-start.json').stat()
            prefix = memory.PHASE_MARKERS[phase]
            result.update(sample_metrics(phase, prefix, settings[window]))
        except issues as exc:
            return dict(result, status='incomplete', reason=f'Guest phase evidence: {exc}')
        return dict(result, status='completed')

    try:
        samples = memory.read_samples(guest)
    except issues as exc:
        samples = {}
        if not isinstance(exc, FileNotFoundError):
            errors.append(f'Invalid guest samples: {exc}')
    phases = {
        'startup': read_phase(startup),
        'idle': read_phase(idle),
        'light': read_phase(lambda: load('light', 'light-load', settings['light_rate'], 'light_seconds')),
        'warmup': read_phase(lambda: load('warmup', 'warmup', settings['sustained_rate'], 'warmup_seconds')),
        'sustained': read_phase(lambda: load('load', 'measurement', settings['sustained_rate'], 'sustained_seconds')),
    }

    runtime = {}
    compatible = False
    try:
        host_file = run/'environment/host.txt'
        host = host_file.read_text().splitlines()
        runtime['Host'] = host[1] + ' / ' + next(line.split(':', 1)[1].strip() for line in host
                                                 if line.startswith('Model name:'))
        guest_env = (guest/'environment.txt').read_text().splitlines()
        runtime['Guest'] = guest_env[0] + ' / ' + next(line for line in guest_env
                                                      if line.startswith('PRETTY_NAME='))
        vm = read_json(run/'environment/vm.json')
        runtime['VM'] = f"{vm['vmType']}, {vm['cpus']} vCPU, {vm['memory']/1024**2:g} MiB allocated"
        runtime['Guest memory'] = next(line.strip() for line in guest_env if line.startswith('Mem:'))
        runtime['Guest swap'] = next(line.strip() for line in guest_env if line.startswith('Swap:'))
        runtime['JDK'] = (guest/'java.txt').read_text().strip()
        settings_rows = (line.split('|') for line in (guest/'postgres-settings.txt').read_text().splitlines())
        pg = {row[0].strip(): ' '.join(part.strip() for part in row[1:3] if part.strip())
              for row in settings_rows if len(row) == 4}
        runtime['PostgreSQL'] = pg['server_version']
        info = summary()
        runtime['JVM flags'] = ' '.join(info['flags'])
        launch = read_json(guest/'launch.json')
        runtime['Java launch'] = ' '.join(launch['command'])
        artifacts = (run/'environment/artifacts.txt').read_text().splitlines()
        runtime['Stack JARs'] = ', '.join(sorted(jar for line in artifacts if '  ./' in line
                                               for jar in [Path(line.split('  ', 1)[1]).name]
                                               if STACK_JARS.fullmatch(jar)))
        check(vm['vmType'] == 'qemu' and vm['cpus'] == 1 and vm['memory'] == 512*1024**2
              and any('KVM' in line for line in guest_env if line.startswith('Hypervisor vendor:'))
              and runtime['Guest swap'].split()[1:] == ['0', '0', '0'], 'Unexpected guest configuration')
        custom = settings['id'].startswith('v1-custom')
        if not custom:
            check(runtime['JDK'].startswith('openjdk version "27"'), 'Unexpected JDK')
        if 'jdk_build' in settings:
            build = settings['jdk_build']
            matches = (re.search(r'\bbuild ' + re.escape(build) + r'(?=[,)])', runtime['JDK'])
                       if custom else f'build {build})' in runtime['JDK'])
            check(matches, 'Unexpected JDK build')
        if 'java_executable' in settings:
            check(launch['command'][:1+len(settings['jvm'])] ==
                  [settings['java_executable'], *settings['jvm']], 'Java launch differs from the suite configuration')
        check(pg['server_version'].startswith('18.'), 'Unexpected PostgreSQL major version')
        if 'fixture_sha256' in settings:
            check(pg['server_version'].split()[0] == settings['postgres_version'], 'Unexpected PostgreSQL version')
            inputs = read_json(run/'inputs.json')
            check(inputs['quotes-2026-09-30.csv'] == settings['fixture_sha256'], 'Unexpected replay fixture')
            check(set(inputs) == {'server.py', 'quotes-2026-09-30.csv', 'run.py'}
                  and all(re.fullmatch(r'[0-9a-f]{64}', digest) for digest in inputs.values()),
                  'Invalid fixture or workload hash inventory')
        check(all(pg[key] == value for key, value in settings['postgres'].items()),
              'PostgreSQL settings differ from the suite protocol')
        check(info['flags'] == settings['jvm'], 'JVM flags differ from the suite protocol')
        check(info['stabilize_seconds'] == settings['stabilize_seconds'], 'Stabilization mismatch')
        check(launch['environment']['PORTFOLIO_SEED_USERS'] == str(settings['users'])
              and launch['environment']['MARKET_POLL_INTERVAL_MS'] ==
              str(settings['quote_poll_seconds']*1000), 'Fixture configuration mismatch')
        check(runtime['Stack JARs'], 'Stack runtime artifacts are missing')
        (run/'environment/complete.txt').stat()
        compatible = True
        check(info['status'] == 'completed' and info['postgres_stopped'] is True
              and info['forced_shutdown'] is False and info['oom_kill_delta'] == 0,
              f"Guest outcome: {info['status']}; OOM delta: {info.get('oom_kill_delta')}; "
              f"errors: {info.get('errors')}")
        (guest/'portfolio-after-load.json').stat()
    except issues + (StopIteration,) as exc:
        errors.append(f'Environment or guest verification: {exc}')
    try:
        status = read_json(run/'run.json')
        errors.extend(status['errors'])
        check(status['status'] == 'completed', f"Runner status: {status['status']}")
    except issues as exc:
        errors.append(f'Runner evidence: {exc}')
    completed = not errors and all(phase['status'] == 'completed' for phase in phases.values())
    return dict(candidate, phases=phases, runtime=runtime, failures=errors, compatible=compatible,
                status='completed' if completed else 'failed' if candidate['execution'] == 'finished' else 'incomplete')


COLUMNS = (
    ('ready_seconds', 'App readiness (s)'), ('postgres_seconds', 'PostgreSQL start (s)'),
    ('app_rss_mib', 'Peak app RSS (MiB)'), ('postgres_pss_mib', 'Peak PostgreSQL PSS (MiB)'),
    ('available_mib', 'Min guest MemAvailable (MiB)'), ('samples', 'Guest samples'),
    ('p95_ms', 'Aggregate p95 (ms)'), ('p99_ms', 'Aggregate p99 (ms)'),
    ('dispatch_p99_ms', 'Dispatch p99 (ms)'), ('achieved_rps', 'Completed/s incl. drain'),
    ('offered', 'Scheduled'), ('completed', 'Completed'), ('successful', 'Successful'),
    ('errors', 'Errors incl. timeouts'), ('timeouts', 'Timeouts'), ('unsent', 'Unsent'),
)


def display(value):
    if value is None:
        return 'Unavailable'
    return escape(f'{value:.2f}' if isinstance(value, float) else str(value))


def print_summary(suite, report_path):
    rows = [('Stack', 'Round', 'Status', 'Ready (s)', 'Peak RSS (MiB)',
             'Sustained p99 (ms)', 'Min guest MemAvailable (MiB)')]
    for candidate in suite['candidates']:
        phases = candidate['phases']
        values = [phases[phase].get(metric) if candidate['compatible']
                  and phases[phase]['status'] == 'completed' else None
                  for phase, metric in (('startup', 'ready_seconds'), ('sustained', 'app_rss_mib'),
                                        ('sustained', 'p99_ms'), ('sustained', 'available_mib'))]
        rows.append((LABELS[candidate['id']], str(candidate.get('round', '-')), candidate['status'],
                     *(f'{value:.2f}' if value is not None else '-' for value in values)))
    widths = [max(map(len, column)) for column in zip(*rows)]
    print(f"\nSuite status: {suite['status']}")
    for index, row in enumerate(rows):
        cells = [cell.ljust(width) if column < 3 else cell.rjust(width)
                 for column, (cell, width) in enumerate(zip(row, widths))]
        print(' | '.join(cells))
        if index == 0:
            print('-+-'.join('-' * width for width in widths))
    print(f'Report: {report_path}')


def chart(candidates, phase, key, title):
    rows = [c['phases'][phase] for c in candidates]
    values = [row.get(key) if row['status'] == 'completed' and candidate['compatible'] else None
              for row, candidate in zip(rows, candidates)]
    scale = max((v for v in values if v is not None), default=0) or 1
    label_width = 245 if any('round' in c for c in candidates) else 190
    height = 34*len(values) + 8
    svg = [f'<svg role="img" aria-label="{escape(title)}" viewBox="0 0 640 {height}">']
    for i, (candidate, value) in enumerate(zip(candidates, values)):
        y = 34*i + 25
        svg.append(f'<text x="4" y="{y}">{escape(candidate_label(candidate))}</text>')
        width = 325 * value / scale if value is not None else 0
        if value is not None:
            svg.append(f'<rect x="{label_width}" y="{y-14}" width="{width:.2f}" height="18"/>')
        svg.append(f'<text x="{label_width+8+width:.2f}" y="{y}">{display(value)}</text>')
    return f'<figure><figcaption>{escape(title)}</figcaption>'+''.join(svg)+'</svg></figure>'


def render(suite, root):
    candidates = suite['candidates']
    esc = escape
    settings = suite['protocol']
    versioned = 'rounds' in settings
    introduction = ('Protocol v1. Two sequential rounds per candidate.' if settings['id'] == 'v1' else
                    'Protocol v1 preparation. Two rounds with shortened smoke windows.'
                    if versioned else 'Phase 3.4 provisional workflow. One short run per candidate.')
    if 'fixture_sha256' in settings:
        description = ('Four-candidate suite using v1 methodology and synthetic market prices. '
                       if settings['id'].startswith('v1-four-candidate') else
                       'Protocol v1 with synthetic market prices. ')
        introduction = description + ('Two rounds with shortened smoke windows.' if settings['id'].endswith('-preparation')
                           else 'Two sequential rounds per candidate.')
    if settings['id'].startswith('v1-custom'):
        introduction = ('Custom experiment using v1 methodology and synthetic market prices. '
                        + ('Two rounds with shortened smoke windows.' if settings['id'].endswith('-preparation')
                           else 'Two sequential rounds per candidate.'))
    limitation = ('' if versioned else
                  'Long-window stability, repeatability, and final 512 MiB feasibility remain open.\n')
    parts = [f"""<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(suite['id'])} · Framework Benchmarks</title>
<style>
body {{font: 16px system-ui,sans-serif; color:#162739; background:#f7f9fc; max-width:75rem; margin:2rem auto; padding:0 1rem}}
table {{border-collapse:collapse; width:100%; background:white}} th,td {{padding:.5rem; text-align:left; border:1px solid #c9d5df; vertical-align:top}}
pre {{white-space:pre-wrap; overflow-wrap:anywhere}} a {{color:#135b94}} figure {{margin:1rem 0; max-width:43rem}}
figcaption {{font-weight:600}} svg {{width:100%; fill:#162739; background:white}} rect {{fill:#26728e}}
</style>
<body><h1>Framework Benchmarks · {esc(suite['id'])}</h1>
<p>{esc(suite['created_utc'])} · {esc(suite['status'])} · {esc(suite['host'])} / {esc(suite['vm'])}</p>
<p>{introduction} Latency runs from scheduled
client slot to response completion, including dispatch, queueing and SSH transport.
Guest memory reports sampled peaks and minima at roughly 1 Hz. PostgreSQL PSS
sums proportional memory across sampled database processes. MemAvailable estimates
guest headroom. The guest sampler shares the application vCPU. OS caches can remain warm.</p>
<p>{limitation}Successful-request latency from failed or incomplete phases is listed in the table
when independently verified. Charts include completed phases with verified environments.</p>
<p><a href="suite.json">Suite manifest</a></p>
<h2>Comparisons</h2>"""]
    parts.append('<p>These results compare complete application stacks. CoilBase provides '
                 'application structure and persistence integration for Jex and Quarkus. '
                 'The benchmark maintainer is also the CoilBase author.</p>')
    if not settings.get('sample_window_markers'):
        parts.append('<p>Earlier idle and warm-up evidence uses guest phase labels for memory. '
                     'Idle samples may include the connection check where idle markers are absent.</p>')
    for phase, key, title in [
        ('startup', 'ready_seconds', 'App readiness (s)'),
        ('idle', 'app_rss_mib', 'Idle peak app RSS (MiB)'),
        ('light', 'app_rss_mib', 'Light-load peak app RSS (MiB)'),
        ('sustained', 'p99_ms', 'Sustained aggregate p99 (ms)'),
    ]:
        parts.append(chart(candidates, phase, key, f'{title} · lower is better'))
    if versioned:
        parts.append('<h2>Sustained latency across rounds</h2><table><thead><tr>'
                     '<th scope="col">Candidate</th><th scope="col">p95 spread (ms)</th>'
                     '<th scope="col">p99 spread (ms)</th></tr></thead><tbody>')
        for key in dict.fromkeys(candidate['id'] for candidate in candidates):
            runs = [c for c in candidates if c['id'] == key and c['status'] == 'completed']
            spreads = []
            for metric in ('p95_ms', 'p99_ms'):
                values = [c['phases']['sustained'][metric] for c in runs]
                spreads.append(display(max(values)-min(values) if len(values) == settings['rounds'] else None))
            parts.append(f'<tr><th scope="row">{esc(LABELS[key])}</th><td>{spreads[0]}</td><td>{spreads[1]}</td></tr>')
        parts.append('</tbody></table>')
    labels = dict(startup='Startup', idle='Idle', light='Light load', warmup='Sustained warm-up',
                  sustained='Sustained measurement')
    for phase in PHASES:
        applicable = (COLUMNS[:2] if phase == 'startup' else COLUMNS[2:6]
                      if phase == 'idle' else COLUMNS[2:])
        if versioned and phase != 'startup':
            applicable += (('app_pss_mib', 'Peak app PSS (MiB)'),)
            if phase != 'idle':
                applicable += (('queue_p99_ms', 'Queue p99 (ms)'), ('http_p99_ms', 'HTTP p99 (ms)'))
        parts.append(f'<h2>{labels[phase]}</h2><table><thead><tr><th scope="col">Metric</th>')
        parts.extend(f'<th scope="col">{esc(candidate_label(c))}</th>' for c in candidates)
        parts.append('</tr></thead><tbody>')
        rows = [c['phases'][phase] for c in candidates]
        parts.append('<tr><th scope="row">Status</th>'+''.join(
            f'<td>{esc(row["status"])}<br>{esc(row.get("reason", ""))}</td>' for row in rows)+'</tr>')
        for key, label in applicable:
            parts.append(f'<tr><th scope="row">{esc(label)}</th>'+''.join(
                f'<td>{display(row.get(key))}</td>' for row in rows)+'</tr>')
        parts.append('</tbody></table>')
    parts.append('<h2>Protocol</h2><p>PostgreSQL 18 Theory A. Each candidate starts with a '
                 'fresh database, fixture and application. Light load runs after idle, '
                 'followed immediately by sustained warm-up and measurement.</p>')
    parts.append('<pre>'+esc(json.dumps(settings, indent=2))+'</pre><h2>Candidates and raw evidence</h2>')
    for candidate in candidates:
        evidence = root/candidate['evidence']
        directory = (f'<a href="{quote(candidate["evidence"])}/">Raw evidence directory</a>'
                     if evidence.is_dir() else 'Raw evidence directory unavailable')
        heading = IMPLEMENTATIONS[candidate['id']]
        if 'round' in candidate:
            heading += f" · Round {candidate['round']}"
        parts.append(f'<h3>{esc(heading)}</h3><p>{esc(candidate["status"])} · {directory}</p>')
        for name in ('Host', 'Guest', 'VM', 'Guest memory', 'Guest swap', 'JDK', 'PostgreSQL',
                     'JVM flags', 'Java launch', 'Stack JARs'):
            parts.append(f'<p><strong>{name}</strong><br>{display(candidate["runtime"].get(name))}</p>')
        links = []
        sources = [('environment/artifacts.txt', 'Bundle hashes'),
                   ('guest/postgres-settings.txt', 'Effective PostgreSQL settings'),
                   ('guest/effective-jvm-flags.txt', 'Effective JVM flags'),
                   ('guest/samples.jsonl', 'Guest samples'),
                   ('load.json', 'Sustained slot observations')]
        if 'fixture_sha256' in settings:
            sources.append(('inputs.json', 'Fixture and workload hashes'))
        for filename, label in sources:
            target = evidence/filename
            links.append(f'<a href="{quote(target.relative_to(root).as_posix())}">{label}</a>'
                         if target.is_file() else f'{label} unavailable')
        parts.append('<p>'+' · '.join(links)+'</p>')
        if candidate['failures']:
            parts.append('<ul>'+''.join(f'<li>{esc(error)}</li>' for error in candidate['failures'])+'</ul>')
    parts.append('</body></html>')
    return '\n'.join(parts)+'\n'


def artifact_hashes(path):
    hashes = {}
    for line in path.read_text().splitlines():
        entry = re.fullmatch(r'([0-9a-f]{64}) [ *](.+)', line)
        if entry:
            digest, name = entry.groups()
            check(name not in hashes, f'Duplicate artifact hash for {name}')
            hashes[name] = digest
    check(hashes, 'No captured artifact hashes')
    return hashes


def verify_round_artifacts(root, candidates):
    def fail(run, reason):
        run['failures'].append('Cross-round artifact verification: ' + reason)
        run['compatible'] = False
        run['status'] = 'failed'

    for key in dict.fromkeys(run['id'] for run in candidates):
        verified = []
        for run in candidates:
            if run['id'] != key or not run['compatible']:
                continue
            try:
                hashes = artifact_hashes(root/run['evidence']/'environment/artifacts.txt')
            except (OSError, ValueError) as exc:
                fail(run, str(exc))
            else:
                verified.append((run, hashes))
        if len(verified) != 2:
            continue
        (first, before), (second, after) = verified
        changed = sorted(name for name in before.keys() | after.keys()
                         if before.get(name) != after.get(name))
        if changed:
            reason = 'Bundle hashes differ between rounds for ' + ', '.join(changed)
            fail(first, reason)
            fail(second, reason)


def featured_svg(suite):
    """Render a completed measurement suite using the same validated rows as HTML."""
    settings = suite['protocol']
    if suite['status'] != 'completed' or settings['id'].endswith('-preparation'):
        return None
    runs = suite['candidates']
    keys = [key for key in LABELS if any(run['id'] == key for run in runs)]
    metrics = (
        ('App readiness (s)', 'startup', 'ready_seconds', 'lower'),
        ('Sampled peak app RSS (MiB)', 'sustained', 'app_rss_mib', 'lower'),
        ('Sustained p99 latency (ms)', 'sustained', 'p99_ms', 'lower'),
        ('Min guest MemAvailable (MiB)', 'sustained', 'available_mib', 'higher'),
    )
    debian = all('Debian GNU/Linux 13' in run['runtime'].get('Guest', '') for run in runs)
    ubuntu = all('Ubuntu' in run['runtime'].get('Guest', '') for run in runs)
    os_name = 'Debian 13' if debian else 'Ubuntu' if ubuntu else 'Linux'
    postgres = settings.get('postgres_version') or '18'
    rounds = settings.get('rounds', 1)
    runtime = (f"Custom v1 · {suite['configuration_sha256'][:12]}"
               if settings['id'] == 'v1-custom' else 'HotSpot 27')
    subtitle = f'{os_name} · 1 vCPU · 512 MiB · PostgreSQL {postgres} · {runtime}'
    subtitle += f" · {settings['sustained_rate']:g} RPS sustained"
    colors = ('#176b76', '#6844a5')
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 760" '
             'role="img" aria-labelledby="title desc">',
             '<title id="title">Framework Benchmarks</title>',
             '<desc id="desc">Readiness, application RSS, latency and guest memory headroom '
             'for each recorded run. Lower is better for readiness time, application RSS and latency. '
             'Higher is better for guest memory headroom.</desc>',
             '<rect width="1200" height="760" fill="#f0f5fa"/>',
             '<g font-family="Arial, Helvetica, sans-serif" fill="#12283e">',
             '<text x="36" y="58" font-size="39" font-weight="700">Framework Benchmarks</text>',
             f'<text x="36" y="94" font-size="23">{escape(subtitle)}</text>']
    for index in range(rounds):
        x = 36 + index * 145
        parts.append(f'<rect x="{x}" y="114" width="20" height="20" fill="{colors[index]}"/>'
                     f'<text x="{x+30}" y="131" font-size="20">Round {index+1}</text>')
    for index, (title, phase, metric, direction) in enumerate(metrics):
        x, y = 36 + index % 2 * 574, 152 + index // 2 * 270
        parts.append(f'<rect x="{x}" y="{y}" width="554" height="254" rx="12" fill="white"/>'
                     f'<text x="{x+20}" y="{y+34}" font-size="24" font-weight="700">{title}</text>'
                     f'<text x="{x+534}" y="{y+34}" font-size="18" text-anchor="end">'
                     f'{direction} is better</text>')
        scale = max(run['phases'][phase][metric] for run in runs) or 1
        for row, key in enumerate(keys):
            top = y + 52 + row * 48
            parts.append(f'<text x="{x+20}" y="{top+22}" font-size="20">{LABELS[key]}</text>')
            for run in (run for run in runs if run['id'] == key):
                value = run['phases'][phase][metric]
                order = run.get('round', 1) - 1
                bar_y = top + order * 20
                width = 229 * value / scale
                parts.append(f'<rect x="{x+238}" y="{bar_y}" width="{width:.2f}" height="14" '
                             f'fill="{colors[order]}"/>'
                             f'<text x="{x+245+width:.2f}" y="{bar_y+13}" font-size="18">{value:.2f}</text>')
    parts.extend([
        '<text x="36" y="710" font-size="19">Memory uses sampled peaks and minima. '
        'p99 includes client scheduling and SSH transport.</text>',
        '<text x="36" y="738" font-size="19">Complete application stacks. '
        'Jex and Quarkus use CoilBase for structure and persistence integration.</text>',
        '</g></svg>',
    ])
    return '\n'.join(parts) + '\n'


def regenerate(path, refresh=False):
    suite = read_json(path)
    entries = suite['candidates']
    settings = suite.get('protocol', {})
    protocol_id = settings.get('id')
    version = (protocol_id or '').removesuffix('-preparation')
    synthetic = 'fixture_sha256' in settings
    versioned = version in ('v1', 'v1-four-candidate', 'v1-custom')
    config = None
    if 'java_executable' in settings or 'configuration_sha256' in suite or version == 'v1-custom':
        config = benchmark_config.validate({key: settings[key] for key in benchmark_config.STANDARD
                                            if key in settings})
    if 'configuration_sha256' in suite or version == 'v1-custom':
        check(suite.get('configuration_sha256') == benchmark_config.sha256(config),
              'JVM configuration hash mismatch')
    if versioned:
        # The pre-release archive used v1 without a fixture hash. Earlier
        # synthetic preparation evidence used a temporary manifest identifier.
        stacks = FOUR_CANDIDATE_STACKS if synthetic else STACKS
        expected_settings = protocol(protocol_id.endswith('-preparation'),
                                     'v1',
                                     archived=version == 'v1' and not synthetic, config=config)
        if version == 'v1-four-candidate' and config is None:
            expected_settings['id'] = protocol_id
        check(settings == expected_settings, 'Manifest settings differ from protocol v1')
        roster = [(number, key) for number in range(1, settings['rounds']+1) for key in stacks]
        candidates = {(c.get('round'), c['id']): c for c in entries}
        check(len(entries) == len(roster) and candidates.keys() == set(roster),
              'Expected one entry per candidate in each protocol v1 round')
        for number, key in roster:
            check(candidates[number, key]['evidence'] == f'{round_path(number)}/{key}',
                  'Unexpected protocol v1 evidence directory')
    else:
        roster = list(STACKS)
        candidates = {c['id']: c for c in entries}
        check(len(entries) == len(roster) and candidates.keys() == STACKS.keys(), ROSTER_ERROR)
    suite['candidates'] = [inspect_candidate(path.parent, candidates[key], settings) for key in roster]
    if versioned:
        verify_round_artifacts(path.parent, suite['candidates'])
    if versioned and synthetic:
        runs = [run for run in suite['candidates'] if run['compatible']]
        inventories = [read_json(path.parent/run['evidence']/'inputs.json') for run in runs]
        if inventories and any(inventory != inventories[0] for inventory in inventories[1:]):
            for run in runs:
                run['failures'].append('Fixture or workload inputs differ across the suite')
                run['compatible'] = False
                run['status'] = 'failed'
    statuses = {c['status'] for c in suite['candidates']}
    suite['status'] = ('completed' if statuses == {'completed'} else
                       'failed' if 'failed' in statuses else 'incomplete')
    if refresh:
        persist(path, suite)
    (path.parent/'report.html').write_text(render(suite, path.parent), encoding='utf-8')
    featured = path.parent/'featured.svg'
    svg = featured_svg(suite)
    if svg is None:
        featured.unlink(missing_ok=True)
    else:
        featured.write_text(svg, encoding='utf-8')
    return suite


def verify_guest_storage(vm, guest_root):
    check(Path(guest_root).is_absolute(), 'Guest run root must be an absolute path')
    command = [str(runner.VM), 'exec', vm]
    try:
        subprocess.run([*command, 'mkdir', '-p', '--', guest_root], check=True, timeout=30)
        filesystem = subprocess.check_output(
            [*command, 'findmnt', '-n', '-o', 'FSTYPE', '-T', guest_root],
            text=True, timeout=30).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f'Cannot verify guest evidence storage at {guest_root}: {exc}') from exc
    check(filesystem in {'ext4', 'xfs', 'btrfs'},
          f'Guest evidence needs a disk-backed filesystem at {guest_root}. '
          f'Found {filesystem or "no mount"}. Use /var/tmp/fb-runs on the Debian guest.')


def run_suite(output, bundles, vm, guest_root, preparation=False, version='3.4-mvp', config_path=None):
    if config_path is not None:
        version = 'v1'
    stacks = FOUR_CANDIDATE_STACKS if version == 'v1' else STACKS
    check(bundles.keys() == stacks.keys(), 'Expected exactly one bundle for each of: ' + ', '.join(stacks))
    if version == 'v1':
        fixture = runner.FIXTURE/'data/quotes-2026-09-30.csv'
        check(hashlib.sha256(fixture.read_bytes()).hexdigest() == PUBLIC_FIXTURE_SHA256,
              'The v1 suite requires the public synthetic replay fixture')
    config = benchmark_config.load(config_path) if config_path is not None else (
        benchmark_config.load() if version == 'v1' else None)
    settings = protocol(preparation, version, config=config)
    host = platform.node()
    check(platform.system() == 'Linux', 'Run the suite on a Linux host')
    if version == 'v1':
        verify_guest_storage(vm, guest_root)
    output.mkdir(parents=True, exist_ok=False)
    candidates = [dict(id=key, stack=stacks[key], bundle=bundles[key], evidence=key, execution='pending',
                       status='incomplete', phases={phase: {'status': 'incomplete'} for phase in PHASES},
                       failures=[])
                  for key in stacks]
    if 'rounds' in settings:
        candidates = [dict(candidate, round=number,
                           evidence=f"{round_path(number)}/{candidate['id']}")
                      for number in range(1, settings['rounds']+1) for candidate in candidates]
    manifest = output/'suite.json'
    suite = dict(id=output.name, created_utc=datetime.now(timezone.utc).isoformat(),
                 host=host, vm=vm, protocol=settings, candidates=candidates, status='incomplete')
    if config is not None:
        suite['configuration_sha256'] = benchmark_config.sha256(config)
    persist(manifest, suite)

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        for index, candidate in enumerate(candidates):
            key = candidate['id']
            evidence = Path(candidate['evidence'])
            guest_dir = Path(guest_root)/output.name/evidence.parent
            args = ['--vm', vm, '--guest-run-root', str(guest_dir),
                    '--candidate', key, '--bundle', candidate['bundle'], '--output', str(output/evidence),
                    '--suite-light-seconds', str(settings['light_seconds']),
                    '--warmup-seconds', str(settings['warmup_seconds']),
                    '--duration-seconds', str(settings['sustained_seconds']),
                    '--rate', str(settings['sustained_rate']), '--workers', str(settings['workers'])]
            if version == 'v1':
                args.append('--require-debian-13')
            if preparation:
                args.append('--preparation')
            candidate['execution'] = 'running'
            persist(manifest, suite)
            print(f'Running {candidate_label(candidate)}', flush=True)
            try:
                failed = runner.main(args, jvm_config=config) if config is not None else runner.main(args)
                status_path = output/evidence/'run.json'
                if status_path.exists() and read_json(status_path).get('interrupted'):
                    candidate['execution'] = 'interrupted'
                    break
            except KeyboardInterrupt:
                candidate['execution'] = 'interrupted'
                break
            except Exception as exc:
                candidate['execution_errors'] = [repr(exc)]
            else:
                if failed:
                    candidate['execution_errors'] = ['Candidate runner exited unsuccessfully']
            candidate['execution'] = 'finished'
            # Checkpoint each candidate while preserving pending runs.
            candidates[index] = inspect_candidate(output, candidate, settings)
            persist(manifest, suite)
    finally:
        signal.signal(signal.SIGTERM, previous)
        persist(manifest, suite)
        result = regenerate(manifest, refresh=True)
        print_summary(result, manifest.with_name('report.html'))
    return result['status'] != 'completed'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    run = commands.add_parser('run')
    run.add_argument('--output', type=Path, required=True)
    run.add_argument('--vm', default='fb-512')
    run.add_argument('--guest-run-root', default='/var/tmp/fb-runs')
    run.add_argument('--bundle', action='append', required=True, metavar='CANDIDATE=/GUEST/PATH')
    run.add_argument('--protocol', choices=('v1',), default='v1')
    run.add_argument('--config', type=Path, help='JSON configuration for a custom v1 experiment')
    run.add_argument('--preparation', action='store_true')
    report = commands.add_parser('report')
    report.add_argument('manifest', type=Path)
    args = parser.parse_args()
    if args.command == 'report':
        try:
            regenerate(args.manifest)
        except ValueError as exc:
            report.error(str(exc))
        print(args.manifest.parent/'report.html')
        return 0
    stacks = FOUR_CANDIDATE_STACKS
    bundles = {}
    for item in args.bundle:
        key, sep, bundle = item.partition('=')
        if not sep or key not in stacks or key in bundles or not Path(bundle).is_absolute():
            run.error(f'invalid or duplicate candidate bundle: {item}')
        bundles[key] = bundle
    if bundles.keys() != stacks.keys():
        run.error('Expected exactly one bundle for each of: ' + ', '.join(stacks))
    try:
        return run_suite(args.output, bundles, args.vm, args.guest_run_root,
                         args.preparation, args.protocol, args.config)
    except ValueError as exc:
        run.error(str(exc))


if __name__ == '__main__':
    raise SystemExit(main())
