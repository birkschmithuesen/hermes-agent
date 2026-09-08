#!/usr/bin/env python3
"""Reproduziert die Messungen aus docs/ipv6-443-office-artesmobiles-befund.md.

Karte t_fedc3249. Laeuft AUF dem vServer als uid birk, ohne root.

Was dieses Skript kann:
  - lokale v4/v6-Erreichbarkeit von 443 (mit TLS-Handshake und HTTP-Antwort)
  - Gegenprobe auf einem Port ohne Listener (8443) -> RST lokal
  - v6-Egress des Servers als Positivkontrolle (erwartet: ROT, alles Timeout)
  - Adressfamilien-Wahl des Ablaufmonitors + Fallback-Mutationsprobe

Was es NICHT kann und auch nicht vortaeuscht:
  - die EXTERNE Sicht. Der Server hat kein v6-Egress; die externen Zahlen im
    Bericht stammen aus ipscantxt.cgi (v6) und check-host.net (v4). Wer sie
    nachmessen will, muss das ueber einen externen Messpunkt tun.

Aufruf:  python3 verify_ipv6_443_befund.py
Exit 0 = alle pruefbaren Erwartungen erfuellt, sonst 1 (fail-loud).
"""
import socket
import ssl
import sys
import time

HOST_NAME = "office.artesmobiles.art"
V6 = "2a01:4f8:c2c:f426::1"
V4 = "78.47.156.115"
MONITOR = "/home/birk/.hermes/profiles/birk/scripts/cert_expiry_watch.py"

# RFC 6666 Discard-Only-Praefix: garantiert kein Peer, ideal fuer "v6 ist tot".
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
    print("=== 1. Lokal: 443 auf beiden Familien (Kartenhypothese-Gegenbeweis) ===")
    ok6, d6 = tls_probe(V6, socket.AF_INET6)
    check("v6 [%s]:443 liefert HTTP" % V6, ok6 and "302" in d6, d6)
    ok4, d4 = tls_probe(V4, socket.AF_INET)
    check("v4 %s:443 liefert HTTP" % V4, ok4 and "302" in d4, d4)

    print("\n=== 2. Gegenprobe: Port ohne Listener -> RST, nicht Timeout ===")
    for ip, fam, lbl in ((V6, socket.AF_INET6, "v6"), (V4, socket.AF_INET, "v4")):
        s = socket.socket(fam, socket.SOCK_STREAM)
        s.settimeout(5)
        t0 = time.time()
        try:
            s.connect((ip, 8443))
            s.close()
            check(f"{lbl}:8443 ohne Listener", False, "unerwartet OFFEN")
        except ConnectionRefusedError as exc:
            check(f"{lbl}:8443 ohne Listener", True,
                  f"{exc.__class__.__name__} nach {time.time() - t0:.2f}s (= RST lokal)")
        except Exception as exc:
            check(f"{lbl}:8443 ohne Listener", False,
                  f"{type(exc).__name__}: {exc} -- erwartet war ConnectionRefusedError")

    print("\n=== 3. Positivkontrolle v6-Egress des Servers (erwartet: ROT) ===")
    dead = 0
    targets = ["ipv6.google.com", "www.cloudflare.com", "one.one.one.one"]
    for host in targets:
        try:
            info = socket.getaddrinfo(host, 443, socket.AF_INET6, socket.SOCK_STREAM)[0]
            s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            s.settimeout(6)
            s.connect(info[4])
            s.close()
            print(f"    {host}: erreichbar")
        except Exception as exc:
            dead += 1
            print(f"    {host}: {type(exc).__name__}")
    check("v6-Egress des Servers ist tot", dead == len(targets),
          f"{dead}/{len(targets)} Ziele unerreichbar -- Messungen vom Server aus "
          f"waeren ohne externen Messpunkt WERTLOS")

    print("\n=== 4. Ablaufmonitor: welche Familie waehlt er? ===")
    order = [
        ("IPv6" if fam == socket.AF_INET6 else "IPv4", sa[0])
        for fam, _, _, _, sa in socket.getaddrinfo(HOST_NAME, 443, 0, socket.SOCK_STREAM)
    ]
    print(f"    getaddrinfo-Reihenfolge: {order}")
    s = socket.create_connection((HOST_NAME, 443), timeout=8)
    used = "IPv6" if s.family == socket.AF_INET6 else "IPv4"
    peer = s.getpeername()[0]
    s.close()
    check("Monitor misst ueber IPv6 (nicht IPv4, wie die Karte annahm)",
          used == "IPv6", f"create_connection nutzt {used} peer={peer}")

    print("\n=== 5. Mutationsprobe: haelt der Monitor totes IPv6 aus? ===")
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
        # 🔴 Es genuegt NICHT, dass ein Ergebnis herauskommt: der Fallback muss
        # tatsaechlich beschritten worden sein. Sonst bliebe dieser Punkt gruen,
        # wenn DEAD_V6 versehentlich auf eine LEBENDE Adresse zeigt -- der Test
        # testete dann gar nichts. (Genau diese Mutation wurde geprobt.)
        really_fell_back = used_families == ["IPv4"]
        check("Monitor faellt bei totem v6 auf v4 zurueck", really_fell_back,
              f"notAfter={not_after} nach {elapsed:.1f}s ueber {used_families} "
              f"(Kosten: das v6-Timeout von {cew.CONNECT_TIMEOUT}s)"
              if really_fell_back else
              f"Verbindung lief ueber {used_families} statt ['IPv4'] -- der "
              f"Fallback wurde NIE ausgeuebt, dieser Test beweist nichts. "
              f"Zeigt DEAD_V6 ({DEAD_V6}) auf eine erreichbare Adresse?")
    except Exception as exc:
        check("Monitor faellt bei totem v6 auf v4 zurueck", False,
              f"{type(exc).__name__}: {exc} -- KEIN Fallback, Monitor wuerde "
              f"faelschlich Alarm schlagen")
    finally:
        socket.getaddrinfo = orig_gai
        socket.create_connection = orig_cc

    failed = [label for label, ok, _ in results if not ok]
    print(f"\n{'=' * 70}")
    print(f"{len(results) - len(failed)}/{len(results)} Erwartungen erfuellt")
    if failed:
        print("NICHT erfuellt:")
        for label in failed:
            print(f"  - {label}")
        print("\nDie Lage hat sich seit dem Bericht geaendert -- Bericht neu pruefen.")
        return 1
    print("Alle pruefbaren Erwartungen des Berichts bestaetigt.")
    print("HINWEIS: die EXTERNE v6-Messung ist hierin NICHT enthalten "
          "(kein v6-Egress vom Server).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
