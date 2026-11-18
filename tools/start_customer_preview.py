#!/usr/bin/env python3
"""Run an isolated, password-protected temporary HTTPS preview.

Use the project interpreter. Keep this supervisor and the computer online.
The state directory must be outside the source checkout; it contains credentials.
"""
from __future__ import annotations

import argparse
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
from urllib.request import build_opener, ProxyHandler

ROOT = Path(__file__).resolve().parents[1]


def save(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    temporary.chmod(0o600)
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--app-port', type=int, default=7393)
    parser.add_argument('--gateway-port', type=int, default=7394)
    parser.add_argument('--render-port', type=int, default=7395)
    args = parser.parse_args()
    state = args.state_dir.expanduser().resolve()
    if state == ROOT or ROOT in state.parents:
        parser.error('state-dir must be outside the source checkout')
    if not os.environ.get('DEEPSEEK_API_KEY'):
        parser.error('DEEPSEEK_API_KEY is required in the process environment')
    ports = [args.app_port, args.gateway_port, args.render_port]
    if len(set(ports)) != 3 or any(p in {7377, 7391, 7392} or not 1024 <= p <= 65535 for p in ports):
        parser.error('use three distinct, unreserved ports')
    for port in ports:
        with socket.socket() as probe:
            # Match HTTPServer's reuse policy: TIME_WAIT is not a live listener.
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(('127.0.0.1', port))
    cloudflared = shutil.which('cloudflared')
    if not cloudflared:
        parser.error('cloudflared is not installed')
    os.umask(0o077)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.chmod(0o700)
    access_path = state / 'access.json'
    access = json.loads(access_path.read_text()) if access_path.exists() else {
        'preview_password': secrets.token_urlsafe(24)}
    save(access_path, access)
    # Do not pass unrelated provider credentials to the child services.
    env = {k: v for k, v in os.environ.items() if k in {
        'PATH', 'HOME', 'TMPDIR', 'LANG', 'LC_ALL', 'SSL_CERT_FILE', 'SSL_CERT_DIR',
        'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
        'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy',
        'DEEPSEEK_API_KEY', 'JINA_API_KEY', 'PEXELS_API_KEY', 'UNSPLASH_ACCESS_KEY',
        'GOOGLE_API_KEY', 'GOOGLE_MAPS_API_KEY', 'GEMINI_API_KEY'}}
    env.update(TRAVEL_RENDER_BRIDGE=str(state / 'render-bridge'),
               TRAVEL_RENDER_OUTPUT=str(state / 'render-captures'),
               TRAVEL_PDF_PYTHON=sys.executable, PYTHONUNBUFFERED='1')
    children, logs = {}, []

    def spawn(name, command, child_env):
        log = open(state / f'{name}.log', 'ab', buffering=0)
        logs.append(log)
        child = subprocess.Popen(command, cwd=ROOT, env=child_env,
                                 stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        children[name] = child
        return child

    def check_children():
        failed = [name for name, process in children.items() if process.poll() is not None]
        if failed:
            raise RuntimeError('Child process stopped: ' + ', '.join(failed))

    def stop(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        spawn('render', [sys.executable, 'tools/render_service.py', str(args.render_port)], env)
        spawn('app', [sys.executable, 'tools/serve_single_session.py', '--directory',
              str(state / 'sessions'), '--port', str(args.app_port), '--static-root', 'apps/v1'], env)
        opener = build_opener(ProxyHandler({}))
        deadline = time.monotonic() + 40
        while True:
            check_children()
            try:
                with opener.open(f'http://127.0.0.1:{args.app_port}/api/agent/capabilities', timeout=2) as response:
                    if not json.load(response).get('planner_available'):
                        raise RuntimeError('Planner unavailable')
                with opener.open(f'http://127.0.0.1:{args.render_port}/health', timeout=2) as response:
                    if json.load(response)['bridge'] != env['TRAVEL_RENDER_BRIDGE']:
                        raise RuntimeError('Renderer does not use the isolated bridge')
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError('Local services did not become ready') from None
                time.sleep(.5)
        tunnel_env = {k: v for k, v in env.items() if not k.endswith(('KEY', 'TOKEN'))}
        tunnel_log = state / 'tunnel.log'
        tunnel_offset = tunnel_log.stat().st_size if tunnel_log.exists() else 0
        spawn('tunnel', [cloudflared, 'tunnel', '--no-autoupdate', '--protocol', 'http2',
              '--url', f'http://127.0.0.1:{args.gateway_port}'], tunnel_env)
        deadline = time.monotonic() + 75
        host = None
        while time.monotonic() < deadline:
            check_children()
            with open(tunnel_log, 'rb') as stream:
                stream.seek(tunnel_offset)
                text = stream.read().decode(errors='replace')
            matches = re.findall(r'https://([a-z0-9-]+\.trycloudflare\.com)', text)
            if matches:
                host = matches[-1]
                break
            time.sleep(.5)
        if not host:
            raise RuntimeError('Temporary HTTPS hostname was not returned; inspect tunnel.log')
        gate_env = {k: v for k, v in tunnel_env.items() if k not in {'GOOGLE_API_KEY'}}
        gate_env['TRIP_PREVIEW_PASSWORD'] = access['preview_password']
        spawn('gateway', [sys.executable, 'tools/serve_customer_preview.py', '--public-host', host,
              '--backend-port', str(args.app_port), '--listen-port', str(args.gateway_port)], gate_env)
        deadline = time.monotonic() + 15
        while True:
            check_children()
            connection = HTTPConnection('127.0.0.1', args.gateway_port, timeout=2)
            try:
                connection.request('GET', '/__preview/login', headers={'Host': host})
                response = connection.getresponse()
                response.read()
                if response.status != 200:
                    raise RuntimeError('Gateway rejected the current tunnel hostname')
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError('Gateway did not become ready') from None
                time.sleep(.25)
            finally:
                connection.close()
        access['url'] = f'https://{host}/studio.html'
        save(access_path, access)
        save(state / 'runtime.json', {'source': str(ROOT), 'python': sys.executable,
             'ports': ports, 'url': access['url'], 'supervisor_pid': os.getpid(),
             'pids': {name: child.pid for name, child in children.items()},
             'started_at': time.time(), 'render_bridge': env['TRAVEL_RENDER_BRIDGE']})
        print(f"Preview: {access['url']}\nAccess password is in {access_path}", flush=True)
        while True:
            check_children()
            time.sleep(2)
    finally:
        for child in reversed(list(children.values())):
            if child.poll() is None:
                child.terminate()
        for child in children.values():
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
        for log in logs:
            log.close()


if __name__ == '__main__':
    main()
