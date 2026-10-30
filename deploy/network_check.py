#!/usr/bin/env python3
"""Credential-free transport checks. Does not call a model or prove billing/API access.

Run on the proposed runner, not on a laptop with different routing. HEAD requests
verify DNS, TCP, TLS/SNI and HTTP without sending prompts, tokens or repo contents.
"""
import concurrent.futures
import datetime
import ipaddress
import json
import socket
import ssl
import time

HOSTS = ('api.telegram.org', 'github.com', 'api.github.com', 'api.commandcode.ai',
         'commandcode.ai', 'registry.npmjs.org')


def check(host, family):
    started = time.monotonic()
    result = {'host': host, 'family': 'ipv6' if family == socket.AF_INET6 else 'ipv4'}
    try:
        addresses = list(dict.fromkeys(info[4][0] for info in socket.getaddrinfo(
            host, 443, family, socket.SOCK_STREAM)))
        # getent ahostsv6 can return ::ffff:<IPv4>, which is not native IPv6.
        if family == socket.AF_INET6:
            addresses = [ip for ip in addresses if not ipaddress.ip_address(ip).ipv4_mapped]
        result['dns_addresses'] = addresses
        result['https_reachable'] = False
        context = ssl.create_default_context()
        for address in addresses[:3]:
            try:
                with socket.socket(family, socket.SOCK_STREAM) as connection:
                    connection.settimeout(5)
                    connection.connect((address, 443))
                    with context.wrap_socket(connection, server_hostname=host) as secure:
                        secure.sendall(f'HEAD / HTTP/1.1\r\nHost: {host}\r\nUser-Agent: ccbot-network-check/1\r\nConnection: close\r\n\r\n'.encode('ascii'))
                        # Only the status line is needed; response bodies are never collected.
                        with secure.makefile('rb') as response:
                            line = response.readline(1024).decode('ascii', errors='replace').strip()
                        if not line.startswith('HTTP/'):
                            raise ValueError('Invalid HTTP response')
                        result.update(https_reachable=True, http_status_line=line)
                        break
            except (OSError, ValueError) as exc:
                result['last_error_type'] = type(exc).__name__
    except OSError as exc:
        result.update(dns_addresses=[], https_reachable=False, last_error_type=type(exc).__name__)
    result['elapsed_ms'] = round((time.monotonic() - started) * 1000)
    return result


def main():
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda pair: check(*pair),
                                [(host, family) for host in HOSTS for family in (socket.AF_INET, socket.AF_INET6)]))
    print(json.dumps({'checked_at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                      'scope': 'transport only; no authentication, inference or billing verification',
                      'checks': results}, indent=2))


if __name__ == '__main__':
    main()
