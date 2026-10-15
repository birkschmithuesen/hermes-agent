#!/usr/bin/env python3
"""Reproduces the measurements from docs/ipv6-443-office-artesmobiles-befund.md.

Card t_fedc3249. Runs ON the vServer as uid birk, without root.

What this script can do:
  - local v4/v6 reachability of 443 (with TLS handshake and HTTP response)
  - counter-check on a port without a listener (8443) -> RST locally
  - the server's v6 egress as a positive control (expected: RED, all timeout)
  - address family choice of the expiry monitor + fallback mutation test

What it CANNOT do and does not pretend to:
  - the EXTERNAL view. The server has no v6 egress; the external figures in
    the report come from ipscantxt.cgi (v6) and check-host.net (v4). Anyone
    who wants to re-measure them has to do so from an external vantage point.

Run:  python3 verify_ipv6_443_befund.py
Exit 0 = all checkable expectations met, otherwise 1 (fail-loud).
"""
import socket
import ssl
import sys
import time

HOST_NAME = "office.artesmobiles.art"
V6 = "2a01:4f8:c2c:f426::1"
V4 = "78.47.156.115"
MONITOR = "/home/birk/.hermes/profiles/birk/scripts/cert_expiry_watch.py"

# RFC 6666 discard-only prefix: guaranteed no peer, ideal for "v6 is dead".
DEAD_V6 = "100::1"

results = []


def check(label, ok, detail):
    results.append((label, ok, detail))
    print(f"[{'OK ' if ok else 'FAIL'}] {label}: {detail}")


def tls_probe(ip, fam, port=443, timeout=8):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    s = socket.socket(fam, socket.SOCK_STREAM)
    s.settimeout(timeout)
    t0 = time.time()
    try:
        s.connect((ip, port))
        with ctx.wrap_socket(s, server_hostname=HOST_NAME) as tls:
            tls.send(
                b"HEAD / HTTP/1.1\r\nHost: %s\r\nConnection: close\r\n\r\n"
                % HOST_NAME.encode()
            )
            first = tls.recv(200).split(b"\r\n")[0].decode(errors="replace")
        return True, f"{first} ({time.time() - t0:.2f}s)"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc} ({time.time() - t0:.2f}s)"


def main():
    print("=== 1. Local: 443 on both families (card hypothesis counter-proof) ===")
    ok6, d6 = tls_probe(V6, socket.AF_INET6)
    check("v6 [%s]:443 delivers HTTP" % V6, ok6 and "302" in d6, d6)
    ok4, d4 = tls_probe(V4, socket.AF_INET)
    check("v4 %s:443 delivers HTTP" % V4, ok4 and "302" in d4, d4)

    print("\n=== 2. Counter-check: port without listener -> RST, not timeout ===")
    for ip, fam, lbl in ((V6, socket.AF_INET6, "v6"), (V4, socket.AF_INET, "v4")):
        s = socket.socket(fam, socket.SOCK_STREAM)
        s.settimeout(5)
        t0 = time.time()
        try:
            s.connect((ip, 8443))
            s.close()
            check(f"{lbl}:8443 without listener", False, "unexpectedly OPEN")
        except ConnectionRefusedError as exc:
            check(f"{lbl}:8443 without listener", True,
                  f"{exc.__class__.__name__} after {time.time() - t0:.2f}s (= RST locally)")
        except Exception as exc:
            check(f"{lbl}:8443 without listener", False,
                  f"{type(exc).__name__}: {exc} -- expected ConnectionRefusedError")

    print("\n=== 3. Positive control of the server's v6 egress (expected: RED) ===")
    dead = 0
    targets = ["ipv6.google.com", "www.cloudflare.com", "one.one.one.one"]
    for host in targets:
        try:
            info = socket.getaddrinfo(host, 443, socket.AF_INET6, socket.SOCK_STREAM)[0]
            s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            s.settimeout(6)
            s.connect(info[4])
            s.close()
            print(f"    {host}: reachable")
        except Exception as exc:
            dead += 1
            print(f"    {host}: {type(exc).__name__}")
    check("server's v6 egress is dead", dead == len(targets),
          f"{dead}/{len(targets)} targets unreachable -- measurements from the "
          f"server would be WORTHLESS without an external vantage point")

    print("\n=== 4. Expiry monitor: which family does it choose? ===")
    order = [
        ("IPv6" if fam == socket.AF_INET6 else "IPv4", sa[0])
        for fam, _, _, _, sa in socket.getaddrinfo(HOST_NAME, 443, 0, socket.SOCK_STREAM)
    ]
    print(f"    getaddrinfo order: {order}")
    s = socket.create_connection((HOST_NAME, 443), timeout=8)
    used = "IPv6" if s.family == socket.AF_INET6 else "IPv4"
    peer = s.getpeername()[0]
    s.close()
    check("monitor measures over IPv6 (not IPv4, as the card assumed)",
          used == "IPv6", f"create_connection uses {used} peer={peer}")

    print("\n=== 5. Mutation test: does the monitor survive dead IPv6? ===")
    orig_gai = socket.getaddrinfo

    def fake_gai(host, port, family=0, type=0, proto=0, flags=0):
        if host == HOST_NAME:
            return [
                (socket.AF_INET6, socket.SOCK_STREAM, 6, "", (DEAD_V6, port, 0, 0)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", (V4, port)),
            ]
        return orig_gai(host, port, family, type, proto, flags)

    import importlib.util

    spec = importlib.util.spec_from_file_location("cew", MONITOR)
    cew = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cew)

    socket.getaddrinfo = fake_gai
    used_families = []
    orig_cc = socket.create_connection

    def spy_cc(address, *a, **kw):
        sock = orig_cc(address, *a, **kw)
        used_families.append("IPv6" if sock.family == socket.AF_INET6 else "IPv4")
        return sock

    socket.create_connection = spy_cc
    t0 = time.time()
    try:
        not_after = cew.fetch_live_notafter(HOST_NAME, 443, cew.CONNECT_TIMEOUT)
        elapsed = time.time() - t0
        # 🔴 It is NOT enough for a result to come out: the fallback must
        # actually have been taken. Otherwise this point would stay green
        # if DEAD_V6 accidentally points to a LIVE address -- the test
        # would then be testing nothing. (Exactly this mutation was tried.)
        really_fell_back = used_families == ["IPv4"]
        check("monitor falls back to v4 when v6 is dead", really_fell_back,
              f"notAfter={not_after} after {elapsed:.1f}s over {used_families} "
              f"(cost: the v6 timeout of {cew.CONNECT_TIMEOUT}s)"
              if really_fell_back else
              f"connection ran over {used_families} instead of ['IPv4'] -- the "
              f"fallback was NEVER exercised, this test proves nothing. "
              f"Does DEAD_V6 ({DEAD_V6}) point to a reachable address?")
    except Exception as exc:
        check("monitor falls back to v4 when v6 is dead", False,
              f"{type(exc).__name__}: {exc} -- NO fallback, monitor would "
              f"falsely raise an alarm")
    finally:
        socket.getaddrinfo = orig_gai
        socket.create_connection = orig_cc

    failed = [label for label, ok, _ in results if not ok]
    print(f"\n{'=' * 70}")
    print(f"{len(results) - len(failed)}/{len(results)} expectations met")
    if failed:
        print("NOT met:")
        for label in failed:
            print(f"  - {label}")
        print("\nThe situation has changed since the report -- re-check the report.")
        return 1
    print("All checkable expectations of the report confirmed.")
    print("NOTE: the EXTERNAL v6 measurement is NOT included here "
          "(no v6 egress from the server).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
