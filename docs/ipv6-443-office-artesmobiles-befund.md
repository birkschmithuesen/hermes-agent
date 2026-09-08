# IPv6 auf 443 unerreichbar — office.artesmobiles.art

Karte t_fedc3249 · gemessen 2026-09-08 · Rolle ops

## Kurzfassung (Urteil zuerst)

**Der Befund ist bestätigt, die Ursachenhypothese der Karte ist WIDERLEGT.**

TCPv6:443 ist von außen tatsächlich unerreichbar (extern nachgemessen). Aber die
in der Karte übernommene Erklärung — „`docker0` hat kein globales v6-Subnetz und
`net.ipv6.conf.all.forwarding = 0`, deshalb kann `docker-proxy` nicht
weiterleiten" — hält der Messung nicht stand:

> **Über IPv6 auf die globale Serveradresse funktioniert Port 443 lokal
> vollständig** — TCP-Verbindung, TLS-Handshake und `HTTP/1.1 302 Moved
> Temporarily` vom OnlyOffice-Container. Wäre `docker-proxy` durch das fehlende
> v6-Subnetz oder das abgeschaltete Forwarding blockiert, könnte genau das nicht
> gelingen.

Die echte Ursache liegt **vor der Maschine bzw. im Paketfilter**, nicht in der
Docker-Netzkonfiguration: ein *portselektiver* Filter, der 443 und 80 auf **beiden**
IP-Familien verwirft und 22 auf beiden durchlässt. Der v6-Anteil dieses Filters
ist derselbe Sachverhalt wie das bereits bekannte Port-80-Problem aus
t_b8b9a6a7 / t_d17a974c — nur, dass v4:443 zusätzlich freigeschaltet ist.

**Konsequenz für die Karte:** Es gibt nichts an Docker zu reparieren. Ein
Docker-Daemon-Neustart oder `"ipv6": true` in `daemon.json` würde das Problem
**nicht** lösen und die Karte hätte den Dienst ohne Nutzen unterbrochen.

**Ohne root ist hier nichts zu beheben** — die entscheidende Regelwerkszeile ist
als uid `birk` weder lesbar noch schreibbar. Karte wird per
`kanban_block(kind="capability")` übergeben.

---

## 1. Der Befund, unabhängig reproduziert (Abnahmepunkt 1)

### 1.1 Warum vom Server aus nicht messbar

Der vServer hat **selbst kein funktionierendes IPv6-Egress**. Alle externen
v6-Ziele laufen in Timeout:

```
ipv6.google.com      FAIL [2a00:1450:4001:c17::71]  timeout 6.02s
www.cloudflare.com   FAIL [2606:4700::6810:7b60]    timeout 6.01s
one.one.one.one      FAIL [2606:4700:4700::1001]    timeout 6.02s
ipv6.test-ipv6.com   FAIL [2a01:7e03::2000:a4ff:fe2d:805a] timeout 6.00s
www.heise.de         FAIL [2a02:2e0:3fe:1001:7777:772e:2:85] timeout 6.01s
www.kernel.org       FAIL [2a04:4e42:8e::311]       timeout 6.01s
```

🔴 **Das ist die Positivkontrolle, die die Karte einfordert — und sie ist ROT.**
Damit wäre jede vom Server aus gemessene v6-Negativaussage wertlos gewesen: „Ziel
antwortet nicht" hätte exakt so ausgesehen wie „ich darf gar nicht auf v6 raus".
Genau der Fehler, den t_b8b9a6a7 schon einmal machen musste.

Nebenbefund zur Einordnung: auch das v4-Egress ist eine uid-gescopte
default-deny-Allowlist (`pypi.org` offen, `1.1.1.1` / `google` / `check-host.net`
dicht). Das ist gewollt (Skill `egress-allowlist-provider-onboarding`) und
**kein** Fehler — aber es heißt, dass jede externe Messung über den
Broker-Kontext laufen muss.

### 1.2 Externe Messung mit funktionierender Positivkontrolle

Der Broker-Browser hat funktionierendes v6-Egress. IPscan
(<https://ipv6.chappell-family.com/ipv6tcptest/>) bestätigt vor dem Scan die
Quelladresse — und sie ist zufällig genau unser Ziel:

```
✔ No HTTP PROXY detected.
✔ Your IP address appears to be valid IPv6 : 2a01:4f8:c2c:f426::1
```

Damit ist der Messpfad selbst als v6-tauglich belegt (= Positivkontrolle GRÜN),
bevor die eigentliche Messung läuft.

**Scan-Ergebnis** (`ipscantxt.cgi`, Ziel `2a01:4f8:c2c:f426::1`,
`Scan beginning at: Tue Sep 8 07:16:28 2026`, `complete at: 07:17:08`):

```
ICMPv6 ECHO REQUEST returned :  ECHO REPLY

Individual TCP port scan results:
Port 443 = STLTH     Port 22 = OPEN     Port 80 = STLTH     Port 8443 = STLTH
```

`STLTH` ist laut Werkzeuglegende: *„No response was received in the allocated
time period."* — also **DROP, nicht REJECT**.

Wichtig für die Deutung: **`STLTH` auf 8443 beweist, dass der Filter vor der
Maschine greift, nicht auf ihr.** Auf 8443 lauscht lokal niemand; der Kernel
würde von sich aus ein RST senden, was extern als `RFSD` erschiene. Gemessen
wird aber `STLTH`. Lokale Gegenprobe:

```
v6 global:8443 -> ConnectionRefusedError: [Errno 111] Connection refused (0.00s)
v4:8443        -> ConnectionRefusedError: [Errno 111] Connection refused (0.00s)
```

Lokal also sofort RST, von außen Schweigen ⇒ das RST kommt nie heraus, bzw. das
SYN kommt nie herein. Ein verwerfender Filter liegt dazwischen.

### 1.3 Die vollständige Matrix Ports × IP-Familien

| Port | IPv6 (extern) | IPv4 (extern) | lokal auf dem Server |
|---|---|---|---|
| 22  | **OPEN** (IPscan) | **Connected** 6/6 Knoten (check-host) | offen |
| 80  | **STLTH** (IPscan) | **Timeout** 6/6 Knoten (check-host) | offen |
| 443 | **STLTH** (IPscan) | **Connected** 6/6 Knoten (check-host) | offen, TLS+HTTP 302 |
| 8443| **STLTH** (IPscan) | n/a (kein Dienst) | kein Dienst |

check-host.net-Belege (je 6 geografisch verteilte Knoten):
- `office.artesmobiles.art:443` → **Connected** aus Larnaca 0.177 s, Frankfurt
  0.017 s, Teheran 0.232 s, Stockholm 0.130 s, London 0.050 s, Chmelnyzkyj 0.094 s
- `office.artesmobiles.art:80` → **Connection timed out** aus Wien, Sofia,
  Jakarta, Chișinău, Moskau, Dallas (6/6)
- `office.artesmobiles.art:22` → **Connected** aus São Paulo, Nürnberg, Hongkong,
  Nyíregyháza, Jakarta, Istanbul (6/6)

⚠️ check-host.net verbindet nur über IPv4 (IP-Spalte zeigt durchgängig
`78.47.156.115`); v6-Literale weist die API mit `{"error":"invalid_url"}` ab.
Deshalb die v6-Spalte über IPscan, die v4-Spalte über check-host — zwei
unabhängige Werkzeuge.

**Das Muster liest sich eindeutig** (Kriterium aus Skill
`tls-certificate-troubleshooting`, Abschnitt „Timeout vs. Refused"):

- ICMPv6 kommt durch → **kein familienweiter v6-Ausfall**, kein Routingproblem.
- Port 22 auf beiden Familien offen → **die Maschine ist auf beiden Familien
  erreichbar**.
- 80 auf beiden Familien Timeout, 443 nur auf v6 Timeout → **portselektiver
  Filter**, nicht Docker.

---

## 2. Ursachenkette (Abnahmepunkt 2)

### 2.1 Die Kartenhypothese ist widerlegt — der entscheidende Gegenbeweis

Lokal auf dem Server, gegen die **globale** v6-Adresse (nicht Loopback), mit
TLS-Handshake und echter HTTP-Antwort:

```
v6 [2a01:4f8:c2c:f426::1]:443 -> OK 0.05s  HTTP=HTTP/1.1 302 Moved Temporarily
v4 78.47.156.115:443          -> OK 0.00s  HTTP=HTTP/1.1 302 Moved Temporarily
v6 [::1]:443                  -> OK 0.01s  HTTP=HTTP/1.1 302 Moved Temporarily
```

Der `302` kommt aus dem OnlyOffice-Container (172.17.0.2). Der Weg
**v6-Socket → docker-proxy → IPv4-Container → Antwort zurück** funktioniert also
tatsächlich, obwohl `docker0` kein globales v6-Subnetz hat und
`net.ipv6.conf.all.forwarding = 0` ist.

**Warum das kein Widerspruch ist:** `docker-proxy` ist ein *Userspace-Prozess*,
kein Kernel-Forwarding. Er akzeptiert die v6-Verbindung, öffnet **selbst** eine
neue v4-Verbindung zum Container und kopiert Bytes. Ein v6-Subnetz auf `docker0`
und `net.ipv6.conf.all.forwarding` betreffen den *Kernel*-Routingpfad — den
braucht dieser Aufbau gar nicht. Das gemessene Ergebnis belegt das direkt:

```
2808274 root /usr/bin/docker-proxy -proto tcp -host-ip 0.0.0.0 -host-port 443 \
             -container-ip 172.17.0.2 -container-port 443
2808281 root /usr/bin/docker-proxy -proto tcp -host-ip ::      -host-port 443 \
             -container-ip 172.17.0.2 -container-port 443
```

Zwei getrennte Proxy-Prozesse, einer je Familie — der v6-Prozess ist erwiesen
funktionsfähig.

### 2.2 Die Beobachtungen der Karte sind korrekt — nur nicht ursächlich

Beide Einzelbeobachtungen aus t_b8b9a6a7 sind reproduziert und stimmen:

```
$ ip -6 addr show docker0
3: docker0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 ...
    inet6 fe80::42:feff:fe5c:2f0d/64 scope link      # nur link-local, kein globales /64

$ cat /proc/sys/net/ipv6/conf/all/forwarding
0
$ cat /proc/sys/net/ipv6/conf/default/forwarding
0

$ cat /etc/docker/daemon.json
{
  "data-root": "/mnt/HC_Volume_106213717/docker"
}                                                    # kein "ipv6", kein "fixed-cidr-v6"
```

Sie sind aber **Nebenbefunde ohne Wirkung auf dieses Symptom**. Bestätigt wird
auch die schon in t_b8b9a6a7 widerlegte `--ip6tables`-Hypothese — Docker 27.4.1:

```
--ip6tables   Enable addition of ip6tables rules (default true)
--iptables    Enable addition of iptables rules  (default true)
```

### 2.3 Der Server hat auf beiden Familien globale Adressen und eine Route

```
$ ip -6 addr show eth0
    inet6 2a01:4f8:c2c:f426::1/64 scope global
    inet6 fe80::9400:ff:fe35:ceaa/64 scope link

$ ip -6 route show default
default via fe80::1 dev eth0 metric 1024 onlink pref medium

$ ip -6 neigh show
fe80::1 dev eth0 lladdr d2:74:7f:6e:37:e3 router REACHABLE
```

DNS ist auf beiden Familien korrekt: `A 78.47.156.115`,
`AAAA 2a01:4f8:c2c:f426::1`. Das Gateway ist erreichbar (`REACHABLE`), ICMPv6
kommt von außen durch — die v6-Anbindung als solche steht.

### 2.4 Cloud oder Dedicated? — eine Cloud-Firewall ist möglich

```
$ cat /sys/class/dmi/id/sys_vendor /sys/class/dmi/id/product_name
Hetzner
vServer
$ ip -4 addr show eth0 | grep inet
    inet 78.47.156.115/32 ...              # /32 -> Cloud
$ ip -4 route show default
default via 172.31.1.1 dev eth0            # Hetzner-Cloud-Signatur
$ ip link show eth0 | grep link/ether
    link/ether 96:00:00:35:ce:aa           # MAC-Praefix 96:00:00 -> Hetzner Cloud
```

Alle drei Indizien zusammen: **Hetzner Cloud**, also kann eine Cloud-Firewall
*vor* der Maschine sitzen. Diese Erklärung ist damit nicht ausgeschlossen.

### 2.5 🔴 BEWEISLÜCKE — offen benannt, nicht geraten

Was den Filter tatsächlich setzt, ist als uid `birk` **nicht feststellbar**:

- `nft`, `iptables`, `ip6tables`, `ufw` sind als Binaries nicht aufrufbar.
- `/etc/ufw/user6.rules`, `before6.rules` → `Permission denied` (`0640 root:root`,
  Leseversuch tatsächlich durchgeführt, nicht aus `ls` geschlossen).
- `sudo` scheitert im Gateway-Kontext (`effective uid is not 0 …`).
- Eine Cloud-Firewall-Regel hinterlässt im Dateisystem **gar keine Spur** und
  wäre auch mit root lokal nicht sichtbar.

Lesbar ist nur, **dass** ufw läuft und v6 einbezieht — nicht, *was* es blockt:

```
/etc/ufw/ufw.conf      : ENABLED=yes, LOGLEVEL=low
/etc/default/ufw       : IPV6=yes
                         DEFAULT_INPUT_POLICY="DROP"
                         DEFAULT_FORWARD_POLICY="DROP"
```

`DEFAULT_INPUT_POLICY="DROP"` erklärt das gemessene **STLTH** (Timeout) statt
`RFSD` (Reject) sauber — ist aber ein Indiz, kein Beweis für die konkrete Regel.

Zeitstempel-Indiz aus t_b8b9a6a7, hier reproduziert:

```
/etc/ufw/user.rules   2026-08-18 19:45:42  640 root:root
/etc/ufw/user6.rules  2026-08-18 19:45:42  640 root:root
/etc/ufw/before6.rules 2018-12-14 18:50:47 640 root:root
```

Die v6-Regeln wurden **zusammen mit den v4-Regeln am 18.08.2026 um 19:45**
zuletzt geändert — derselbe Zeitpunkt, den t_b8b9a6a7 für den Port-80-Wechsel
belegt hat. Das stützt „ein Ereignis, beide Familien", beweist es aber nicht.

### Fertige Lesebefehle für Birk (root)

```bash
# 1. Was blockt lokal? (v6-Regeln UND v4-Regeln, plus ufw-Gesamtsicht)
sudo ufw status verbose
sudo ip6tables -L -n -v --line-numbers
sudo nft list ruleset | grep -B5 -A5 'dport.*\(80\|443\)'

# 2. Falls dort NICHTS zu 443/v6 steht, sitzt der Filter in der Cloud-Firewall:
#    Hetzner Cloud Console -> Firewalls -> Regeln fuer diesen Server pruefen.
#    Achten auf: Regel erlaubt 443 nur fuer IPv4-Quellen (0.0.0.0/0) und
#    NICHT fuer ::/0 -- das ist das haeufigste Muster und passt exakt auf die
#    gemessene Matrix (22 beide Familien offen, 443 nur v4).
```

---

## 3. Docker-Doku: was v6 korrekt herstellen würde (Abnahmepunkt 3)

Quelle: <https://docs.docker.com/engine/daemon/ipv6/> (abgerufen 2026-09-08),
Abschnitt *„Use IPv6 for the default bridge network"* — wörtlich:

> Edit the Docker daemon configuration file, located at `/etc/docker/daemon.json`.
> Configure the following parameters:
> ```json
> { "ipv6": true, "fixed-cidr-v6": "2001:db8:1::/64" }
> ```
> - `ipv6` enables IPv6 networking on the default network.
> - `fixed-cidr-v6` assigns a subnet to the default bridge network, enabling
>   dynamic IPv6 address allocation.
> - `ip6tables` enables additional IPv6 packet filter rules, providing network
>   isolation and port mapping. **It is enabled by-default**, but can be disabled.
>
> Save the configuration file.
> **Restart the Docker daemon for your changes to take effect.**
> `$ sudo systemctl restart docker`

Die Doku weist ausdrücklich darauf hin, `2001:db8::/64` sei
Dokumentations-reserviert und durch ein echtes Netz zu ersetzen, „for example a
Unique Local Address (ULA) subnet from `fd00::/8`".

**Zwei Feststellungen daraus:**

1. **Ja, es wäre ein Daemon-Neustart nötig** — also eine echte
   Dienstunterbrechung für OnlyOffice.
2. **Und es würde das Symptom trotzdem nicht beheben**, denn der v6-Pfad zum
   Container funktioniert bereits (§ 2.1). Ein nativ v6-adressierter Container
   ändert nichts daran, dass das SYN von außen nie an der Maschine ankommt.

Das ist der praktisch wichtigste Satz dieses Berichts: **die naheliegende
Docker-Maßnahme kostet eine Dienstunterbrechung und bringt nichts.**

---

## 4. Auswirkung auf den Ablaufmonitor (Abnahmepunkt 6)

`scripts/cert_expiry_watch.py` misst **nicht** ausdrücklich über IPv4. Er nutzt
`socket.create_connection((HOST, PORT))` (Zeile 84) — das ist `AF_UNSPEC` und
folgt der Reihenfolge von `getaddrinfo`. Gemessen:

```
getaddrinfo-Reihenfolge fuer office.artesmobiles.art:443 (AF_UNSPEC):
  0: IPv6 2a01:4f8:c2c:f426::1
  1: IPv4 78.47.156.115

create_connection tatsaechlich verwendet: IPv6 peer=2a01:4f8:c2c:f426::1
```

**Der Monitor misst heute also über IPv6, nicht über IPv4** — er läuft auf dem
Server selbst, wo v6:443 lokal offen ist. Die Annahme in der Karte („misst heute
über IPv4") trifft nicht zu; sie ändert das Ergebnis aber nicht, weil derselbe
Container antwortet.

### Mutationsprobe: was passiert, wenn der v6-Pfad ausfällt?

`getaddrinfo` wurde so gepatcht, dass die v6-Adresse durch `100::1` ersetzt wird
(RFC 6666 Discard-Only-Präfix, garantiert kein Peer) und v4 unverändert bleibt —
also exakt „v6 zuerst, aber tot":

```
CONNECT_TIMEOUT im Skript: 15.0s
OK nach 15.10s -> notAfter=2026-11-16 16:46:17+00:00
=> Monitor ueberlebt totes IPv6 (v4-Fallback), Kosten: das v6-Timeout.
```

Belegt an der CPython-Quelle (`inspect.getsource(socket.create_connection)`,
Python 3.9.2): die Funktion iteriert über **alle** `getaddrinfo`-Ergebnisse und
gibt beim ersten Erfolg zurück; erst wenn alle scheitern, wird der letzte Fehler
geworfen. Ein Familienausfall ist damit abgedeckt.

**Ergebnis: keine Änderung am Monitor nötig, unabhängig davon, welche Variante
Birk wählt.**

- Variante (a) — v6 wird freigeschaltet: Monitor misst weiter über v6, alles gut.
- Variante (b) — v6-Listener wird abgeschaltet: der Monitor läuft auf dem Server
  selbst, dort antwortet ein Port ohne Listener sofort mit RST (gemessen an
  `[2a01:4f8:c2c:f426::1]:8443` → `ConnectionRefusedError` nach 0.00 s). Der
  Fallback greift also **ohne Timeout**.
- Variante (c) — unverändert: Status quo, funktioniert.

⚠️ **Der eine Fall mit spürbarer Wirkung:** würde jemand später `AAAA` behalten,
aber v6 auf DROP setzen (nicht REFUSE) *und* die Messung von diesem Server
wegverlagern, liefe jeder Lauf 15 s ins Leere, bevor v4 greift. Der Monitor bleibt
korrekt (er meldet dann immer noch das richtige Datum), wird nur langsamer. Kein
Handlungsbedarf, aber der Vollständigkeit halber benannt.

Da nichts am Monitor geändert wurde, ist keine erneute Mutationsprüfung des
Wächters selbst fällig — die Probe oben ist der Nachweis, dass die v6-Frage ihn
nicht berührt.

---

## 5. Entscheidungsvorlage — Birk wählt (Abnahmepunkt 4)

Ausgangslage: **es brennt nichts.** v4:443 funktioniert einwandfrei aus 6/6
Weltregionen. Browser nutzen Happy Eyeballs und fallen still auf v4 zurück.
Betroffen wären nur v6-bevorzugende Clients ohne v4-Fallback — in der Praxis
selten, aber nicht null (CI-Runner, Kommandozeilenwerkzeuge mit `-6`,
IPv6-only-Netze).

### (a) IPv6 sauber freischalten

- **Was:** Die 443-Freigabe im Paketfilter auf `::/0` erweitern. Erst prüfen, ob
  sie lokal (ufw/nft) oder in der Hetzner-Cloud-Firewall sitzt (§ 2.5).
- **Aufwand:** klein — vermutlich eine Regel. `sudo ufw allow 443/tcp` legt bei
  `IPV6=yes` beide Familien an; in der Cloud-Console eine Regelzeile mit `::/0`.
- **Dienstunterbrechung:** **nein.** Eine Filterregel greift ohne Neustart von
  Docker oder OnlyOffice.
- **Risiko:** gering. Der Dienst ist über v4 ohnehin weltweit offen — v6
  freizugeben erweitert die Angriffsfläche nicht inhaltlich, sondern nur um die
  zweite Adressfamilie desselben Dienstes. Zu bedenken: derselbe Handgriff sollte
  **nicht** versehentlich Port 80 mitöffnen, solange das aus t_d17a974c bewusst
  zu ist.
- **Was NICHT nötig ist:** `"ipv6": true` / `fixed-cidr-v6` in `daemon.json` und
  der Docker-Neustart. Beides würde das Symptom nicht beheben (§ 3).

### (b) v6-Listener abschalten, damit kein halb-erreichbarer Zustand bleibt

- **Was:** Entweder den `AAAA`-Record entfernen (Robot-DNS, `ns1.your-server.de`)
  oder den Container nur auf `0.0.0.0:443` publizieren statt auf `::`.
- **Aufwand:** DNS-Weg klein (ein Record). Publish-Weg bedeutet Container neu
  anlegen → **das ist der teure Weg**.
- **Dienstunterbrechung:** DNS-Weg **nein** (nur TTL-Wartezeit). Publish-Weg
  **ja** — Container-Neustart.
- **Risiko:** DNS-Weg mittel — `AAAA` zu entfernen ist ein Rückschritt in der
  Erreichbarkeit und muss später wieder rückgängig gemacht werden, falls (a)
  kommt. Publish-Weg: der Container ist laut Skill
  `tls-certificate-troubleshooting` mit **anonymen Volumes** aufgesetzt; ein
  `docker rm` verliert Daten. **Nicht empfohlen.**

### (c) Bewusst so lassen

- **Aufwand:** null. **Dienstunterbrechung:** nein.
- **Risiko:** der Status quo. Ein v6-only-Client kann `office.artesmobiles.art`
  nicht erreichen, und niemand merkt es, weil Browser es verdecken. Der Zustand
  ist dokumentiert (dieser Bericht), also nicht mehr unsichtbar.

### Einordnung ohne Entscheidung

(a) ist die einzige Variante, die den Mangel behebt, ohne den Dienst
anzufassen — vorausgesetzt, der Filter ist auffindbar (Beweislücke § 2.5).
(b) über DNS wäre der ehrliche Zustand, wenn v6 dauerhaft nicht gewollt ist.
(c) ist vertretbar, solange man weiß, dass man es weiß.

**Diese Karte entscheidet nicht.**

---

## 6. Warum nichts durchgeführt wurde (Abnahmepunkt 5)

Die Behebung ist **nicht** ohne root möglich:

- Der Filter ist als uid `birk` weder lesbar noch änderbar (§ 2.5, Leseversuche
  tatsächlich durchgeführt).
- `nft`/`iptables`/`ip6tables`/`ufw` stehen nicht zur Verfügung,
  `sudo` scheitert im Gateway-Kontext.
- Sollte der Filter in der Hetzner-Cloud-Firewall sitzen, ist er ohnehin nur über
  die Cloud-Console/API erreichbar, nicht über die Maschine.

Deshalb: kein Eingriff, keine Nachmessung im Grünen. Karte geht per
`kanban_block(kind="capability")` an Birk.

---

## Messprotokoll (Werkzeuge und Zeitpunkte)

| Was | Werkzeug | Wann |
|---|---|---|
| v6-Egress des Servers (Positivkontrolle, ROT) | Python `socket`, 6 Ziele | 2026-09-08 |
| v6-Portmatrix von außen | ipscantxt.cgi (chappell-family) | 2026-09-08 07:16:28–07:17:08 |
| v6-Quelladresse des Messpfads (Positivkontrolle, GRÜN) | ipv6.chappell-family.com | 2026-09-08 |
| v4-Portmatrix von außen (443/80/22) | check-host.net, je 6 Knoten | 2026-09-08 |
| v6:443 lokal mit TLS+HTTP | Python `ssl`, globale Adresse | 2026-09-08 |
| Docker-/Netzkonfiguration | `ip -6`, `/proc/sys`, `daemon.json`, `ps` | 2026-09-08 |
| Docker-Doku | docs.docker.com/engine/daemon/ipv6/ | 2026-09-08 |
| Monitor-Familienwahl + Fallback | `getaddrinfo` + Mutationsprobe `100::1` | 2026-09-08 |

Skills: `tls-certificate-troubleshooting` (Matrix-Methodik, Positivkontrolle,
Cloud-Signatur), `egress-allowlist-provider-onboarding` (Erklärung des toten
Egress vom Server).

---

## Reproduzierbarkeit

`scripts/verify_ipv6_443_befund.py` fährt alle auf dem Server prüfbaren
Messungen dieses Berichts nach — 7 Erwartungen, exit 0 bei Erfolg, exit 1 sobald
sich die Lage geändert hat. Es täuscht die externe Messung **nicht** vor und
sagt das im Kopf des Skripts ausdrücklich.

**Mutationsgeprüft** (ein grüner Wächter ohne Gegenprobe beweist nichts):

| Mutation | Erwartung | Ergebnis |
|---|---|---|
| v4-Fallback aus dem gefälschten `getaddrinfo` entfernt | Punkt 5 wird rot | 6/7, exit 1 ✔ |
| `DEAD_V6` auf eine **lebende** Adresse gesetzt | Punkt 5 wird rot | zunächst **fälschlich grün** ✘ |

🔴 **Die zweite Mutation hat einen echten Blindfleck aufgedeckt und wurde
behoben.** Der Test prüfte nur, *dass* ein `notAfter` herauskam — nicht, *über
welche Familie*. Zeigt `DEAD_V6` versehentlich auf einen erreichbaren Host, wäre
die Verbindung direkt über v6 gelaufen und der Test hätte den Fallback bestätigt,
ohne ihn je auszuüben (0.1 s statt 15 s — das einzige sichtbare Indiz). Jetzt
protokolliert ein Spion um `socket.create_connection` die tatsächlich benutzte
Familie und verlangt `["IPv4"]`. Nach dem Fix ist dieselbe Mutation rot
(*„Verbindung lief ueber ['IPv6'] statt ['IPv4'] — der Fallback wurde NIE
ausgeuebt"*), unmutiert wieder 7/7.
