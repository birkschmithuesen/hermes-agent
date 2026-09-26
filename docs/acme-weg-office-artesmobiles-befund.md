# ACME-Weg office.artesmobiles.art — Faktenlage und Entscheidungsvorlage

Karte t_b8b9a6a7 · gemessen 2026-09-07 · uid `birk` auf 78.47.156.115
plus externe Gegenmessungen von einem zweiten Host.

Diese Karte **entscheidet nichts und stellt nichts neu aus.** Sie beschafft die
Fakten für Birks Wahl zwischen (a) Port 80 öffnen und (b) DNS-01.

---

## Kurzfassung

| Frage | Befund |
|---|---|
| DNS-Anbieter | **Hetzner Robot-DNS** (`ns1.your-server.de`), Registrar Key-Systems |
| API für TXT-Records | **Auf dem aktuellen Nameserver: NEIN.** Robot-API kann nur `rdns`/PTR |
| Weg (b) also | **Nicht direkt möglich** — erst nach Zonen-Umzug zu Hetzner Cloud DNS |
| Port-80-Ursache | **Filter VOR der Maschine** (Hetzner Cloud Firewall), nicht lokal |
| Renewal 18.08. | **Beantwortet:** Port 80 war an dem Tag kurzzeitig offen (ufw-Änderung) |
| Ablaufmonitor | **Scharf**, beide Zustände über den echten Zustellweg gemessen |

Restlaufzeit heute: **70,4 Tage** (Ablauf 2026-11-16 17:46 CET).

---

## 1. DNS-Anbieter — belegt aus drei unabhängigen Quellen

Der Elternbefund nannte `dig` als fehlend; **`host` ist installiert** und war
der einfachste Weg. Zusätzlich zwei Quellen, die nicht am lokalen Resolver
hängen.

**Quelle 1 — autoritative Delegation, direkt am `.art`-TLD-Server**
(via zweitem Host, weil dieser hier keine Egress-Erlaubnis für Port 53 hat):

```
dig +norec @a.nic.art artesmobiles.art NS
;; AUTHORITY SECTION:
artesmobiles.art.  3600  IN  NS  ns.second-ns.com.
artesmobiles.art.  3600  IN  NS  ns1.your-server.de.
artesmobiles.art.  3600  IN  NS  ns3.second-ns.de.
```

**Quelle 2 — Registrar-WHOIS:**

```
Registrar: Key-Systems, LLC   (IANA ID 1345, whois.rrpproxy.net)
Name Server: NS.SECOND-NS.COM / NS1.YOUR-SERVER.DE / NS3.SECOND-NS.DE
Domain Status: ok
```

**Quelle 3 — lokaler Resolver**, gleiches Ergebnis, plus SOA:

```
SOA: ns1.your-server.de. postmaster.your-server.de. 2026090402 ...
```

**Der entscheidende Punkt, den man leicht überliest:** `ns1.your-server.de` ist
**nicht** die Hetzner DNS Console. Die Console nutzt `hydrogen/oxygen/helium.ns.hetzner.com`.
`ns1.your-server.de` (213.133.100.102) ist der **Robot-Nameserver** für dedizierte
Server und Domain-Registrierungen. Das ist für Weg (b) der springende Punkt.

Registrar (Key-Systems) und DNS-Betreiber (Hetzner) sind verschieden — die Zone
liegt bei Hetzner, die Domain ist woanders registriert.

---

## 2. API für TXT-Records — Weg (b) ist auf dem aktuellen NS TOT

### 2a. Robot-API kann keine Forward-Records

Die Robot-Webservice-Doku (<https://robot.hetzner.com/doc/webservice/en.html>)
listet an DNS-Endpunkten ausschließlich:

```
GET    /rdns          POST   /rdns/{ip}
GET    /rdns/{ip}     PUT    /rdns/{ip}     DELETE /rdns/{ip}
```

Das ist **Reverse DNS (PTR)**. Es gibt keinen Endpunkt für Zonen, RRSets, TXT
oder irgendeinen Forward-Record. Ein DNS-01-Challenge-Record `_acme-challenge`
ist über diese API **nicht setzbar**.

Gegenprobe an einem Drittprojekt, das genau daran scheiterte
(<https://github.com/br0ziliy/hetzner-robot-dns>): *"Hetzner API does not
provide interface to deal with DNS zones — so here be dragons."*

**⚠️ Beweislücke, offen benannt:** Ich habe die Robot-API nicht mit
Zugangsdaten angesprochen (keine vorhanden, und ein Schreibversuch wäre in
dieser Karte auch nicht zulässig). Der Befund stützt sich auf die vollständige
Endpunktliste der Anbieterdoku. Sollte es einen undokumentierten Zonen-Endpunkt
geben, wäre er per Definition nicht verlässlich automatisierbar.

### 2b. Hetzner Cloud DNS KANN es — aber die Zone müsste dorthin umziehen

Die Cloud-API hat vollwertige Zonen-/RRSet-Endpunkte. Belegt an der echten
OpenAPI-Spec (<https://docs.hetzner.cloud/cloud.spec.json>, 3,4 MB,
maschinell ausgewertet):

```
GET,POST        /zones
GET,POST        /zones/{id_or_name}/rrsets
DELETE,GET,PUT  /zones/{id_or_name}/rrsets/{rr_name}/{rr_type}
POST            /zones/{id_or_name}/rrsets/{rr_name}/{rr_type}/actions/add_records
POST            /zones/{id_or_name}/rrsets/{rr_name}/{rr_type}/actions/remove_records
POST            /zones/{id_or_name}/actions/import_zonefile
```

`"TXT"` kommt 16-mal in der Spec vor. Verhaltensprobe ohne Token:

```
GET https://api.hetzner.cloud/v1/zones                        -> 401 "token is required"
GET https://api.hetzner.cloud/v1/zones/artesmobiles.art/rrsets -> 401 "token is required"
```

Die Endpunkte existieren also real, nicht nur in der Doku.

Nebenbefund: Die alte DNS-Console-API (`dns.hetzner.com/api/v1/*`) leitet
inzwischen auf `console.hetzner.com` um — Hetzner konsolidiert DNS in die Cloud.

### 2c. certbot-Plugin — genau EINS ist snap-tauglich

Der bekannteste Kandidat, `ctrlaltcoop/certbot-dns-hetzner`, **scheidet aus**.
Aus seiner eigenen README:

> *"Not working with snap — We did not nor plan to support snap."*

Certbot läuft hier als snap (`certbot 5.8.0, Rev 5893, latest/stable, classic`),
also ist das disqualifizierend. Der dort verlinkte snap-fähige Fork
(`BigMichi1/certbot-dns-hetzner`) ist **nicht mehr vorhanden** (GitHub-API: 404) —
ein Verweis, der ins Leere zeigt.

Es existiert genau ein passender Snap (Snapcraft-API-Suche nach
`certbot-dns-hetzner`): **`certbot-dns-hetzner-cloud`**

```
version:     v1.0.5 (stable, released 2026-02-03, rev 2)
publisher:   Rüdiger Olschewsky (rolschewsky) — validation: unproven
license:     MIT
base:        core24        confinement: strict
slots:       plugin -> interface: content, content: certbot-1
```

Das `content: certbot-1`-Slot ist genau das Interface, das der certbot-Snap für
Plugins erwartet — technisch also korrekt gebaut. Installation laut Projekt-README:

```
sudo snap install certbot-dns-hetzner-cloud
sudo snap set certbot trust-plugin-with-root=ok
sudo snap connect certbot:plugin certbot-dns-hetzner-cloud
```

**🔴 Und die Einschränkung, die alles entscheidet** — aus derselben README:

> *"This Plugin is not compatible with the old Hetzner DNS Console"*

Es spricht die **Cloud**-API. Die Zone von `artesmobiles.art` liegt auf
Robot-DNS. **Ohne Zonen-Umzug ist das Plugin wirkungslos.**

⚠️ `validation: unproven` heißt: Publisher-Identität von Canonical nicht
verifiziert. Ein Plugin, das einen schreibfähigen DNS-Token liest, mit `root`
läuft und aus einer nicht verifizierten Quelle stammt, ist eine
Vertrauensentscheidung — die gehört zu Birk, nicht in diese Karte.

### 2d. Minimale Token-Berechtigung — es gibt keine feine Abstufung

Belegt an <https://docs.hetzner.com/cloud/api/getting-started/generating-api-token/>:

> *"Choose a permission. You can choose between **Read** … the token will only
> be allowed to perform GET requests … and **Read & Write** … the token will be
> allowed to perform GET, DELETE, PUT and POST requests."*

Die OpenAPI-Spec bestätigt das strukturell: `securitySchemes` enthält genau
einen Eintrag, `APIToken`, **ohne Scopes**. Jeder RRSet-Endpunkt trägt
`security: [{'APIToken': []}]` — leere Scope-Liste.

**Konsequenz, die in die Risikoabwägung gehört:** Für DNS-01 braucht man
schreibenden Zugriff, und den gibt es nur als **projektweites Read & Write**.
Ein solcher Token kann im selben Cloud-Projekt Server löschen, Volumes
abtrennen und Firewalls ändern. Es gibt **keine** Möglichkeit, ihn auf „nur
TXT-Records in einer Zone" einzuschränken.

Die einzige verfügbare Eingrenzung ist ein **eigenes, leeres Cloud-Projekt**,
das ausschließlich die DNS-Zone enthält und keine Server. Dann ist der
Schadensradius des Tokens auf die Zone begrenzt.

---

## 3. Ursache für den dichten Port 80 — Filter VOR der Maschine

### Die Messung, mit Positivkontrolle

**🔴 Die erste Messung war wertlos und wurde verworfen.** Die Positivkontrolle
(`google:80`) schlug fehl, womit „Port 80 dicht" nichts bewiesen hätte — der
messende Host hätte selbst kein Port-80-Egress haben können. Nachgefasst:

```
Positivkontrolle Port-80-Egress vom Messhost:
  http://example.com   -> 200, connect 0.019s     OK
  http://neverssl.com  -> 200, connect 0.213s     OK
```

Erst damit ist die eigentliche Messung belastbar:

```
                       IPv4              IPv6
  Port 22              OPEN              OPEN
  Port 80              filtered          filtered
  Port 443             OPEN              filtered

  http://office...:80  -> code 000, connect 0.000s, TIMEOUT nach 15s
  https://office...:443 v4 -> code 302, connect 0.002s
  https://office...:443 v6 -> code 000
  ICMPv6 ping          -> 3/3 empfangen, 0% Verlust
  ICMPv4 ping          -> 3/3 empfangen, 0% Verlust
```

IPv6-Positivkontrolle des Messhosts: `https://ipv6.google.com` → 200. Sein
IPv6 funktioniert also.

Lokal auf der Maschine dagegen:

```
ss -ltnp:   0.0.0.0:80  [::]:80  (nginx)   0.0.0.0:443  [::]:443 (docker-proxy)
curl http://localhost/          -> 200
curl https://office...:443      -> 302
```

### Die Auswertung

**Timeout, kein Connection-Refused.** Ein lokal lauschender Dienst, dessen Port
von außen in einen Timeout läuft, bedeutet **DROP**, nicht REJECT. Das Paket
wird verworfen, bevor der Kernel-TCP-Stack antwortet.

**Es ist eine Cloud-Maschine, keine Robot-Dedicated.** Damit ist eine
Hetzner-Cloud-Firewall überhaupt erst möglich:

```
DMI:      Hetzner / vServer / KVM        systemd-detect-virt: kvm
eth0:     78.47.156.115/32               MAC 96:00:00:35:ce:aa
Route:    default via 172.31.1.1
```

`/32`-Adresse mit Gateway `172.31.1.1` und MAC-Präfix `96:00:00` sind die
Hetzner-Cloud-Signatur.

**🔴 Eine eigene Hypothese musste ich verwerfen.** Ich vermutete, die
IPv4/IPv6-Asymmetrie auf 443 komme daher, dass Docker `--ip6tables` per Default
nicht setzt. Am Binary geprüft:

```
/usr/bin/dockerd --help  (Docker 27.4.1)
  --ip6tables   Enable addition of ip6tables rules (default true)
  --iptables    Enable addition of iptables rules  (default true)
```

**Default ist `true`, die Hypothese war falsch.** Die echte Ursache steht
woanders:

```
ip -6 addr show docker0  ->  NUR fe80::42:feff:fe5c:2f0d/64  (link-local!)
cat /proc/sys/net/ipv6/conf/all/forwarding  ->  0
cat /proc/sys/net/ipv4/ip_forward           ->  1
```

`docker0` hat **kein globales IPv6-Subnetz** und IPv6-Forwarding ist **aus**.
`docker-proxy` lauscht zwar auf `[::]:443`, kann aber nicht an den Container
(172.17.0.2, reines IPv4) weiterleiten. **Die v6-Asymmetrie auf 443 ist also
kein Firewall-Effekt, sondern fehlende IPv6-Container-Konnektivität** — ein
eigenständiger, bisher nicht erfasster Befund.

### Der Schluss auf Port 80

Damit bleibt für Port 80 nur eine Erklärung, und sie ist auf **beiden** Familien
konsistent:

| Beobachtung | Was sie ausschließt |
|---|---|
| v4:443 **offen**, v4:80 **dicht** | Kein pauschaler v4-Block. Es wird **portselektiv** gefiltert |
| ICMPv6 kommt durch, TCPv6:80 nicht | Kein pauschaler v6-Block auf Netzebene |
| v6:22 **offen**, v6:80 **dicht** | Auch auf v6 wird portselektiv gefiltert, nicht familienweit |
| Timeout statt Refused, beide Familien | DROP vor der Maschine, nicht durch den lokalen Stack |
| nginx lauscht auf `0.0.0.0:80` UND `[::]:80`, localhost liefert 200 | Der Dienst selbst ist einwandfrei |
| `docker0` ohne globales v6, forwarding=0 | Erklärt v6:443, aber **nicht** Port 80 (dort lauscht nginx nativ) |

Port 80 ist auf **beiden** Familien portselektiv gedroppt, obwohl nginx auf
beiden nativ lauscht und lokal antwortet. Ein lokaler Paketfilter, der genau so
konfiguriert wäre, müsste sowohl in `iptables` als auch in `ip6tables` dieselbe
Port-80-Regel führen und dabei 22 und v4:443 durchlassen.

**Befund: Der Filter sitzt mit hoher Wahrscheinlichkeit VOR der Maschine —
Hetzner Cloud Firewall.** Das passt zur Regelform einer typischen
Cloud-Firewall (Allowlist 22 + 443, alles andere DROP) und zur
`ICMP-durch/TCP-80-weg`-Signatur.

### 🔴 Die Beweislücke, offen benannt

Ich kann das **nicht abschließend beweisen**, und das ist wichtig:

- `nft` und `iptables` sind als uid `birk` nicht aufrufbar (`command not found`).
- `/etc/ufw/user.rules`, `before.rules`, `/etc/iptables/rules.v4|v6`: alle
  `root:root 0640`, **Permission denied**.
- `sudo` ist im Gateway-Kontext funktionsunfähig (`effective uid is not 0`),
  `systemd-run --user` scheitert an fehlendem Bus.
- Der Cloud-Metadatendienst (169.254.169.254) ist durch die Sicherheitsprüfung
  gesperrt — zu Recht, dort liegen Instanz-Credentials.

Nachweisbar ist nur: **`ufw` ist aktiv** (`/etc/ufw/ufw.conf: ENABLED=yes`).
Ob dessen Regeln Port 80 zusätzlich blocken, ist von hier aus nicht lesbar.

**Die zwei Befehle, die die Lücke schließen** (als `admin`, dort läuft `sudo`
passwortlos):

```bash
# 1. Lokaler Filter — blockt ufw/nft Port 80?
sudo ufw status verbose && sudo nft list ruleset | grep -A5 -B5 'dport.*80'

# 2. Cloud-Firewall — in der Hetzner-Konsole unter dem Server nachsehen:
#    console.hetzner.com -> Server -> Firewalls -> Eingehende Regeln
```

Zeigt (1) keine Port-80-Blockade, ist es (2) — und dann ist Weg (a) ein reiner
Konsolen-Klick.

---

## 4. Wie gelang die Erneuerung am 2026-08-18? — BEANTWORTET

Das war die offene Frage aus t_d17a974c. Sie ist ohne `/var/log/letsencrypt`
beantwortbar, über Zeitstempel:

```
Zertifikat notBefore  : 2026-08-18 16:46:18 UTC  = 18:46:18 CEST
/etc/ufw/user.rules   : 2026-08-18 19:45:42 CEST   (mtime = ctime)
/etc/ufw/user6.rules  : 2026-08-18 19:45:42 CEST
nginx access.log.4.gz : 2026-08-18 19:45:41 CEST
```

Und die Gegenprobe — **es sind die einzigen beiden Dateien unter `/etc`, die an
diesem Tag geändert wurden**:

```
find /etc -maxdepth 3 -newermt '2026-08-18 00:00' ! -newermt '2026-08-19 00:00'
  /etc/ufw/user.rules
  /etc/ufw/user6.rules
```

**Die Rekonstruktion:** Um 18:46 wurde das Zertifikat ausgestellt — Port 80 war
zu diesem Zeitpunkt von außen erreichbar. Rund **59 Minuten später**, um 19:45,
wurden die ufw-Regeln geändert und nginx rotierte im selben Sekundenbereich
seinen Log. Seither ist Port 80 dicht.

Die damalige `authenticator = standalone`-Config funktionierte also nicht
„trotz" dichtem Port 80, sondern **weil er zu dem Zeitpunkt noch offen war**.
Die Sperre kam danach.

⚠️ **Was diese Indizienkette NICHT beweist:** dass die ufw-Änderung selbst die
Ursache ist. Der Inhalt von `user.rules` ist nicht lesbar; die zeitliche Nähe
kann auch bedeuten, dass an dem Abend generell an der Firewall gearbeitet wurde
— inklusive einer Cloud-Firewall-Regel, die keine Spur im Dateisystem
hinterlässt. Die Zeitachse belegt **den Wechsel offen→dicht an diesem Abend**,
nicht die genaue Stelle.

**Der Befehl für die volle Bestätigung** (als `admin`):

```bash
sudo zgrep -hE 'standalone|webroot|Timeout during connect|nginx' \
  /var/log/letsencrypt/letsencrypt.log* | grep -A3 -B3 '2026-08-18' | head -60
```

---

## 5. Ablaufmonitor — gebaut, scharf, mutationsgeprüft

**Skript:** `~/.hermes/profiles/birk/scripts/cert_expiry_watch.py` (0755)
**Cronjob:** `tls-expiry-office` (`27ce2711747e`), täglich `0 7 * * *`,
`no_agent`, `deliver: telegram`

Im **Profil `birk`** angelegt, nicht in `ops`. Das ist nicht kosmetisch:

```
architekt  No such file        coder  No such file        ops  No such file
birk       2026-09-07 08:23:01  (52 s alt)                default  2026-06-30 (tot)
```

Nur `birk` hat einen frischen `cron/ticker_heartbeat`. Ein in `ops` angelegter
Wächter wäre nie gefeuert und seine Stille hätte wie „alles in Ordnung"
ausgesehen.

### Was er misst

Die **live am Port 443 ausgelieferte** Kette, nicht die Datei unter
`/etc/letsencrypt/live/`. Das ist bewusst: Der Deploy-Hook ist nachweislich noch
nie gelaufen (t_d17a974c). Ein erneuertes Zertifikat, das nicht in den Container
gelangt, sähe auf der Platte frisch aus und liefe am Port trotzdem ab — genau
der Fehlermodus, der hier zählt.

Das DER wird **unverifiziert** geholt (`CERT_NONE`). Sonst könnte der Wächter
ausgerechnet dann kein Datum mehr lesen, wenn die Verifikation *wegen Ablaufs*
scheitert — er würde im Ernstfall verstummen.

### Verhalten

- **≥ 21 Tage Rest** → leere Ausgabe, exit 0 → stumm
- **< 21 Tage** → eine Meldung, danach 24 h Cooldown
- **Eskalation** (21/14/7/3/1/0 Tage) durchbricht den Cooldown sofort — sonst
  verschluckt ein 24-h-Fenster den Übergang „noch 3 Tage" → „abgelaufen"
- **Messung scheitert** → Text auf stderr, **exit 1** → Fehleralarm.
  Eine gescheiterte Messung ist nie „alles gut".
- Kein `DONE`-Stempel: der Wächter macht sich **nie** endgültig stumm.

### Isolierte Prüfung — 11/11

`scripts/verify_cert_expiry_watch.py`, Wegwerf-Harness mit gemocktem Netz:

```
[OK] Normalfall (70 Tage Rest) -> STUMM
[OK] Mutation (5 Tage Rest) -> ALARM
[OK] Grenzfall exakt 21.5 Tage -> STUMM
[OK] Grenzfall 20.5 Tage -> ALARM
[OK] Abgelaufen (-2 Tage) -> ALARM 'ABGELAUFEN'
[OK] Messung gescheitert -> exit 1, laut
[OK] Cooldown 1. Lauf (10 Tage) -> ALARM
[OK] Cooldown 2. Lauf (10 Tage, gleiche Stufe) -> STUMM
[OK] Eskalation (10 -> 2 Tage) durchbricht Cooldown -> ALARM
[OK] Datumsparser 'Nov 16 ... 2026 GMT'
[OK] Muell-Datum -> ProbeError

11/11 Faelle wie erwartet.
```

Der Datumsparser ist bewusst **ohne `strptime('%b')`** gebaut: `%b` ist
locale-abhängig und würde unter deutscher Locale scheitern.

### Mutationsprobe am ECHTEN Ziel

Ein grüner Harness beweist nur die Logik. Deshalb beide Zustände live:

```
Normalfall (Schwelle 21):        (leer)                        exit 0
Mutation  (Schwelle 999):        ⚠️ ... laeuft in 70.4 Tagen ab
                                  Ablauf: 2026-11-16 17:46 CET  exit 0
Fail-Loud, Port 9:               MESSUNG GESCHEITERT: Connection refused  exit 1
Fail-Loud, Host unauflösbar:     MESSUNG GESCHEITERT: Name or service not known  exit 1
```

Die gemessenen 70,4 Tage / 2026-11-16 17:46 decken sich exakt mit dem
unabhängig per `openssl s_client` gelesenen `notAfter=Nov 16 16:46:17 2026 GMT`.

### Und die Probe über den ECHTEN Zustellweg

Das ist der Teil, der zählt — zweimal `hermes cron run`, einmal normal, einmal
mit temporär auf 999 mutierter Schwelle. Aus `logs/agent.log`:

```
08:26:48  cron.scheduler: Job '27ce2711747e' (no_agent): empty stdout — silent run
08:26:48  cron.scheduler: Job '27ce2711747e': agent returned [SILENT] — skipping delivery
08:27:41  cron.scheduler: Job '27ce2711747e': delivered to telegram:570261709
```

**Stumm im Normalfall, zugestellt bei Mutation** — nicht am Skript gemessen,
sondern am Scheduler. `cron runs` zeigt für beide nur `completed` und hätte den
Unterschied verdeckt.

Danach: Mutation zurückgenommen (`diff` gegen Backup → identisch), das
Cooldown-Testartefakt entfernt (sonst hätte der erste echte Alarm 24 h lang
fälschlich geschwiegen), Kontrolllauf wieder stumm.

**Ein Messfehler von mir, protokolliert:** Ein `export CERT_WATCH_VAR` aus der
Fail-Loud-Probe wirkte in der Terminal-Session nach und ließ einen mutierten
Lauf fälschlich schweigen. Kein Skript-Bug — aber genau der Fehlertyp, der
sonst als „Wächter kaputt" fehldiagnostiziert wird.

`var/` ist über `var/*` in `.gitignore` abgedeckt, es bleibt nichts untracked.

---

## 6. Entscheidungsvorlage — (a) gegen (b)

**Die Karte entscheidet nicht.** Beide Wege sind gangbar; sie unterscheiden sich
in Aufwand, Angriffsfläche und Abhängigkeiten.

### Weg (a) — Port 80 öffnen, bei webroot bleiben

| | |
|---|---|
| **Neu einzurichten** | Nichts. Die renewal-Config steht bereits auf `webroot`. Nur `/var/www/html/.well-known/acme-challenge/` muss existieren |
| **Aufwand** | Minuten. Eine Firewall-Regel, ggf. nur ein Klick in der Cloud-Konsole |
| **Wer** | Birk als `admin` bzw. in der Hetzner-Konsole. Agent kann es nicht |
| **Risiko** | Port 80 ist wieder erreichbar. nginx liefert dort den Default-vhost. Klein, aber Angriffsfläche > 0 |
| **Abhängigkeit** | Keine externe. Kein Token, keine Fremdsoftware |
| **Bricht wenn** | jemand die Firewall-Regel erneut entfernt — dann still, wie jetzt |
| **Voraussetzung** | Die offene Beweislücke aus §3 muss zuerst geschlossen werden: erst wissen, WO gefiltert wird |

Nebenbedingung: Ein Redirect 80→443 ist üblich, darf aber `/.well-known/acme-challenge/`
**nicht** mit umleiten, sonst scheitert webroot weiterhin.

### Weg (b) — DNS-01 über Hetzner Cloud DNS

| | |
|---|---|
| **Neu einzurichten** | **Zonen-Umzug** Robot-DNS → Cloud DNS (NS-Wechsel beim Registrar Key-Systems). Cloud-Projekt. API-Token. Snap-Plugin `certbot-dns-hetzner-cloud`. Credentials-Datei `0600`. renewal-Config von `webroot` auf `dns-hetzner-cloud` zurückstellen |
| **Aufwand** | Deutlich höher. Der Zonen-Umzug ist der Brocken: alle Records müssen mit, und eine unvollständige Migration nimmt E-Mail und Web mit runter |
| **Wer** | Birk. Registrar-Zugang + Cloud-Konsole + `admin` auf der Maschine |
| **Risiko** | **Projektweiter Read-&-Write-Token** — feiner geht es laut Doku nicht (§2d). Plugin-Publisher `validation: unproven`. Läuft als root und liest den Token |
| **Abhängigkeit** | Zwei fremde: Hetzner Cloud DNS + ein Snap von einem Einzelentwickler |
| **Vorteil** | Braucht **keinen** offenen Port. Funktioniert auch bei komplett dichter Firewall. Kann Wildcards |
| **Bricht wenn** | Token abläuft/rotiert, Plugin nicht mehr gepflegt wird, oder Hetzner die API ändert |

### Was aus der Faktenlage folgt — ohne Empfehlung

- **Weg (b) ist heute NICHT verfügbar.** Auf dem aktuellen Nameserver gibt es
  keine TXT-API. Er wird erst nach einem Zonen-Umzug möglich. Das ist keine
  Bewertung, sondern eine Reihenfolge.
- **Der Handlungsdruck ist real, aber nicht akut.** 70 Tage Restlaufzeit. Die
  ersten stillen Fehlschläge von `snap.certbot.renew.timer` beginnen ab ca.
  **2026-10-17** (30 Tage vor Ablauf). Der Ablaufmonitor schlägt spätestens am
  **2026-10-26** an (21 Tage) — mit dann noch drei Wochen Vorlauf.
- **Beide Wege lösen die Schlüssel-Kompromittierung nicht.** Der als
  kompromittiert geltende Schlüssel ist weiter im Einsatz. Der ACME-Weg ist die
  Voraussetzung für die Neuausstellung, nicht deren Ersatz. Diese bleibt an
  t_d17a974c.
- **Eine Zwischenlösung existiert:** Port 80 nur für die Dauer einer
  Neuausstellung öffnen, danach wieder schließen. Löst das Schlüsselproblem
  sofort und lässt die Grundsatzentscheidung offen — verschiebt sie aber nur um
  90 Tage, weil jede Erneuerung dasselbe Fenster bräuchte.

### Was Birk als Nächstes braucht

Der einzige echte Blocker ist **die Beweislücke aus §3**: solange nicht
feststeht, ob lokal oder in der Cloud gefiltert wird, ist der Aufwand von Weg
(a) nicht bezifferbar. Ein Befehl als `admin`:

```bash
sudo ufw status verbose && sudo nft list ruleset | grep -B5 -A5 'dport.*80'
```

Kommt dort keine Port-80-Blockade zum Vorschein, sitzt sie in der
Hetzner-Cloud-Firewall — und Weg (a) ist ein Klick in der Konsole.

---

## Anhang — Werkzeuglage auf der Maschine

Für spätere Sessions, damit nicht erneut geraten wird:

| Werkzeug | Status |
|---|---|
| `dig`, `nslookup`, `whois`, `nft`, `iptables`, `ufw` | **fehlen** als uid `birk` |
| `host` | **vorhanden** (`/usr/bin/host`) — löst die DNS-Aufgabe |
| `curl`, `openssl`, `python3`, `ss`, `ps`, `snap` | vorhanden |
| Egress | Default-deny-Allowlist. `google.com` gesperrt, `github.com` erlaubt. RDAP/DoH von hier aus tot |
| Zweiter Host | Als Messpunkt nutzbar — hat v4+v6 und offenes Port-80-Egress |
| `/var/log/letsencrypt`, `/etc/ufw/*.rules`, `/etc/letsencrypt/live` | root-only, nicht lesbar |
| `sudo` | Im Gateway-Kontext funktionsunfähig; `systemd-run --user` mangels Bus auch |

**Regel, die sich hier zweimal bewährt hat:** Wenn ein Werkzeug fehlt, erst die
Geschwister prüfen (`dig` fehlt → `host` da) und Messungen von einem zweiten Host
gegenprüfen — mit **Positivkontrolle**, sonst misst man die eigene Blockade.
