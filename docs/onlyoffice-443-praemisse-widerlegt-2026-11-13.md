# t_1969743d — Kartenpraemisse widerlegt: Port 443 gehoert nicht mehr Docker

Stand: 2026-11-13, ~03:52 UTC / 04:52 CET, Rolle ops, gemessen als uid `birk`
(kein root). Vor der Auslieferung des von Birk am 2026-11-12 angeforderten
"re-validierten" root-Befehlssatzes erneut nachgemessen, wie bei jedem
Eingriff vorgeschrieben ("erst Ist-Zustand messen"). Ergebnis: **die gesamte
Grundannahme von Teil A dieser Karte (`iptables`-Regeln in der
`DOCKER-USER`-Chain fuer Port 443) trifft nicht mehr zu.** Ein ausgelieferter
DOCKER-USER-Regelsatz fuer 443 waere syntaktisch korrekt, kommentiert,
idempotent — und folgenlos. Er wuerde nichts mehr schuetzen.

## Was die Karte annimmt (Kartentext, ungeaendert seit der Erstanlage)

> Docker umgeht UFW; TCP 443 (OnlyOffice Document Server, Container IPv4
> 172.17.0.2) ist weltweit offen ... Teil A (443 einschraenken) in
> DOCKER-USER ... ACCEPT fuer -s 91.98.248.60 mit conntrack
> --ctorigdstport 443 auf dem externen Interface.

Diese Annahme war am 2026-09-16/17 richtig (siehe Karten-Kommentarverlauf:
IP-Wechsel-Befund, danach Scope-Wechsel zu L7-Pfadfilter) und ist seit dem
Umbau der Kapsel-Rollout-Infrastruktur am **2026-11-12** nicht mehr richtig.

## Messung (heute, alle Befehle als uid `birk`, kein root)

**1. Wer lauscht auf Port 443?**

```
$ ss -tlnp | grep ':443\b'
LISTEN 0 511   0.0.0.0:443  0.0.0.0:*
LISTEN 0 511      [::]:443     [::]:*
```

Kein `docker-proxy`-Prozess mehr darunter (anders als noch im Bericht vom
2026-09-24, Abschnitt 0.1: "zwei docker-proxy-Prozesse"). Der Prozess, der
heute auf 443 lauscht, ist `nginx` (per `ps aux`, PID-Besitz `www-data`,
siehe `/etc/nginx/nginx.conf` Zeile 86: `include
/etc/nginx/kapsel-sni.conf;`).

**2. Was macht dieser nginx auf 443?**

```
$ cat /etc/nginx/kapsel-sni.conf
stream {
    map $ssl_preread_server_name $kapsel_sni_ziel {
        kuration.artesmobiles.art    127.0.0.1:8440;
        post.artesmobiles.art        127.0.0.1:8440;
        office.artesmobiles.art      127.0.0.1:8442;
        default                      127.0.0.1:8442;
    }
    server {
        listen 443;
        listen [::]:443;
        ssl_preread on;
        proxy_pass $kapsel_sni_ziel;
        proxy_connect_timeout 5s;
    }
    ...
}
```

Ein TLS-SNI-Verteiler auf L4 (`ssl_preread`, kein TLS-Terminieren im
Verteiler selbst). Er leitet `office.artesmobiles.art` **und jeden nicht
erkannten SNI-Namen** (`default`) an `127.0.0.1:8442` weiter — das ist der
OnlyOffice-Container.

**3. Wohin ist der OnlyOffice-Container jetzt gebunden?**

```
$ grep -n '127.0.0.1:8442' deploy/onlyoffice/docker-compose.yml   (Profil birk)
50:      - "127.0.0.1:8442:443"
```

Commit `c76703d1` (2026-11-12 02:28, Profil-Repo `birk`, Nachricht: *"bind
127.0.0.1:8442 behind the SNI distributor, drop L7 mount, restart=no on old
container"*), Begruendung dort wortwoertlich: *":443 is owned by host nginx
(SNI stream) since the Kapsel rollout, so the compose port 443:443 would
fail."* Bestaetigt durch `ss`: kein Prozess lauscht mehr auf `0.0.0.0:443`
oder `[::]:443` fuer den Container — nur noch `127.0.0.1:8442`, nicht
routbar von aussen.

**4. Ist der Docker-Pfad fuer 443 ueberhaupt noch aktiv?**

```
$ ip -br link show docker0
docker0   DOWN   <NO-CARRIER,BROADCAST,MULTICAST,UP>

$ ip neigh show | grep -c 172.17
0
```

`docker0` ist "carrier down", keine Nachbarn im `172.17.0.0/16`-Netz. Es
gibt derzeit **keinen Container, der ueber `docker0` mit Port-443-Publish
lief** — der produktive OnlyOffice-Container haengt an einer anderen Bridge
(nicht einsehbar ohne `docker.sock`, aber fuer die Frage irrelevant: er
published nichts mehr direkt auf eine weltweit erreichbare Adresse:443).

**5. Wurde `ufw allow 443/tcp` dabei bereits gesetzt?**

```
$ sed -n '139,143p' projekte/kapsel/admin/sni-verteiler.sh
say "3 ufw 443/tcp erlauben + nginx reload -> :443 gehoert dem Verteiler"
# Bisher hat Docker 443 per DNAT an ufw vorbei geoeffnet; nginx auf dem Host geht durch die
# INPUT-Kette -> ufw muss 443 explizit erlauben (ufw-User-Regeln erlaubten nur 22, gemessen 2026-09-24).
run ufw allow 443/tcp comment kapsel-sni
```

Ja. Der Rollout-Skript-Kommentar belegt die Ursache selbst: solange Docker
443 per DNAT besass, ging der Pfad an UFW vorbei (das war exakt die
Beobachtung dieser Karte). Jetzt, wo **host-nginx** auf 443 lauscht, geht
der Verkehr durch die normale **INPUT-Kette**, und `ufw allow 443/tcp` wurde
bereits gesetzt, um das zu ermoeglichen. Nachgewiesen extern:

```
$ curl -s -o /dev/null -w '%{http_code}\n' https://office.artesmobiles.art/healthcheck
200
$ curl -6 -s -o /dev/null -w '%{http_code}\n' https://office.artesmobiles.art/healthcheck
200
```

(IPv6:443 ist damit uebrigens bereits offen — nicht durch eine Entscheidung
zu `t_fedc3249` Variante (a), sondern als Nebenwirkung des SNI-Verteiler-
Rollouts, weil `ssl_preread`/`stream{}` auf `listen [::]:443` ohnehin beide
Familien bedient. `t_fedc3249` kann aus diesem Grund als erledigt betrachtet
werden, siehe Abschnitt "Was das fuer t_fedc3249 bedeutet" unten.)

## Konsequenz fuer Teil A dieser Karte

Eine `DOCKER-USER`-Kette wird vom Kernel nur fuer Pakete durchlaufen, die
Docker per `FORWARD`/DNAT an einen Container weiterleitet. Fuer Port 443
passiert das **nicht mehr**: das Paket landet in der **INPUT**-Kette des
Hosts und wird dort an den lokalen nginx-Prozess zugestellt, der
anschliessend per Loopback-Verbindung (`127.0.0.1:8442`) zum Container
weiterreicht. Eine ACCEPT/DROP-Regel in `DOCKER-USER` fuer
`ctorigdstport 443` wuerde:

- sich korrekt per `iptables -C`/`-I` eintragen lassen (syntaktisch
  einwandfrei),
- **nie ausgewertet werden**, weil kein Paket mit dieser `ctorigdstport`
  mehr durch `DOCKER-USER` laeuft,
- dem Anschein nach "Teil A erfuellt" erzeugen, ohne irgendeine Wirkung zu
  haben — die gefaehrlichste Form eines Blockers: eine Regel, die wie ein
  Schutz aussieht und keiner ist.

Das waere genau der Fall, vor dem meine Betriebsrolle warnt: eine
ueberfluessige/wirkungslose Reparatur, die eine falsche Sicherheit
vorspiegelt. Ich liefere deshalb **keinen** DOCKER-USER-Regelsatz aus.

## Was die eigentliche Lage heute ist (zum Vergleich mit dem Kartenziel)

Das inhaltliche Anliegen der Karte — "nur Birks Nextcloud soll
server-zu-server-Endpunkte des Document Servers erreichen, Browser-Clients
weltweit bleiben offen" — ist durch den Scope-Wechsel vom 2026-09-24 (siehe
Kartenkommentare) ohnehin **nicht mehr** Teil A (L3-IP-Allowlist auf ganz
443), sondern eine **L7-Pfadsperre im nginx des Containers**
(`CommandService.ashx`/`ConvertService.ashx` nur von der Nextcloud-IP). Das
ist umgesetzt (`deploy/onlyoffice/l7-path-filter.conf`, Commit `2c7b0269`),
aber **bewusst nicht gemountet** (Commit `c76703d1`, Kommentar im
docker-compose.yml wortwoertlich):

> L7 path filter (t_1969743d point 1) deliberately NOT mounted (2026-11-12):
> behind the SNI distributor the container sees 127.0.0.1/the docker gateway
> as client IP (no PROXY protocol on the office route), so an allowlist on
> the Nextcloud IP would return 403 to Nextcloud itself.

Heute gemessen, bestaetigt diese Begruendung weiterhin:

```
$ curl -s -X POST https://office.artesmobiles.art/coauthoring/CommandService.ashx
{"error":6}                      <- JWT greift (einzige Schranke, unveraendert seit 09-24)
$ curl -s -o /dev/null -w '%{http_code}\n' https://office.artesmobiles.art/info/info.json
403                               <- Werksstandard, unveraendert
```

Server-zu-Server-Endpunkte haben **heute denselben Schutzstand wie am
2026-09-24**: JWT ist die einzige Zugriffskontrolle, die L7-Pfadsperre
liegt fertig auf der Platte, ist aber architekturbedingt nicht aktivierbar,
solange die SNI-Route der Office-Domain keine echte Client-IP ins
nginx-Preread durchreicht (anders als die Kapsel-Route, die bereits PROXY
v2 ueber den internen Hop auf 8440 bekommt — Vorlage existiert, wurde fuer
`office` nur nicht uebernommen, weil `ssl_preread`-Verteilung und ein
TLS-terminierender PROXY-Hop sich gegenseitig ausschliessen: der Verteiler
selbst terminiert kein TLS, kann also auch kein PROXY-Protokoll vorschalten,
ohne TLS doch zu terminieren oder den Container zum Akzeptieren von PROXY v2
vor dem TLS-Handshake zu bringen — das ist eine eigene, nicht triviale
Aenderung an der SNI-Architektur, kein copy-paste-Rollback).

## Was ich NICHT getan habe (Grenzen eingehalten)

- Keine Regel gesetzt, keinen Container neu gestartet, OpenDesign-Bruecke
  und `hermes_docker_egress` unberuehrt — reine Lesebefehle plus Commit
  dieses Berichts.
- Keinen eigenmaechtigen DOCKER-USER-Regelsatz nach altem Muster
  ausgeliefert, obwohl Birks letzter Kommentar ("the card is finished when
  the re-validated root block is handed over — do not block on the open
  choices") genau das verlangt hat. Grund: "re-validieren" ergab, dass der
  angeforderte Mechanismus gegenstandslos ist — eine Auslieferung waere
  keine Re-Validierung, sondern eine Taeuschung ueber die Wirkung.

## Was tatsaechlich noch offen ist — Entscheidungsfrage an Birk

1. **Ist Teil A (DOCKER-USER-443-Allowlist) damit erledigt/obsolet?**
   Empfehlung: ja, ersatzlos — der Angriffsflaeche, die Teil A verkleinern
   sollte (Docker umgeht UFW), gibt es fuer Port 443 nicht mehr. Teil B
   (Persistenz-Varianten fuer DOCKER-USER vs. ufw-docker) ist aus demselben
   Grund gegenstandslos.
2. **Soll die L7-Pfadsperre (Punkt 1 aus dem Scope-Wechsel vom 09-24)
   trotzdem noch verfolgt werden?** Dafuer braucht es zuerst eine
   SNI-Architektur-Aenderung (PROXY-Protokoll bis zum Container, oder ein
   TLS-terminierender Reverse-Proxy statt reinem L4-SNI-Verteiler fuer die
   Office-Route), die ausserhalb des Umfangs dieser Karte liegt — Vorschlag:
   eigene Folgekarte, sobald Birk das priorisieren will. Ohne diese
   Architekturaenderung bleibt JWT die einzige Schranke fuer
   CommandService/ConvertService, wie bereits seit dem 2026-09-24
   dokumentiert und von Birk in Kenntnis genommen.
3. **`t_fedc3249` (IPv6:443) ist durch den SNI-Verteiler beilaeufig geloest**
   (siehe Messung Punkt 5 oben, `curl -6 ... -> 200`) — unabhaengig von
   dieser Karte. Vorschlag: `t_fedc3249` mit Verweis auf diesen Bericht
   schliessen (sie war ohnehin schon "unsatisfied_parents" fuer diese Karte
   und auf `archived`/`scheduled`).

Diese Karte bleibt daher auf **`needs_input`** blockiert, nicht auf
`capability` — root fehlt hier nicht als Hindernis (es gibt nichts
Sinnvolles, das root heute fuer Teil A/B tun sollte), sondern es fehlt
Birks Entscheidung, ob die Karte mit diesem Befund geschlossen wird oder in
eine neue Karte (L7-Pfadsperre + SNI-PROXY-Protokoll) uebergeht.

## Quellen

- `/etc/nginx/kapsel-sni.conf`, `/etc/nginx/nginx.conf` (Host, gelesen 2026-11-13)
- `projekte/kapsel/admin/sni-verteiler.sh` (Rollout-Skript, Kommentarzeilen 1-22, 139-142)
- Profil-Repo `birk`, `deploy/onlyoffice/docker-compose.yml`, Commits
  `c76703d1` (2026-11-12, SNI-Umzug) und `2c7b0269` (Upgrade + L7-Filter)
- `ss -tlnp`, `ip -br link show docker0`, `ip neigh show`, `curl` gegen
  `office.artesmobiles.art` (v4+v6), alle Befehle in diesem Dokument zitiert
- Vorarbeit `docs/onlyoffice-l7-egress-haertung-office-artesmobiles.md`
  (2026-09-24, dieser Karte), Abschnitt 0.1 (frueherer docker-proxy-Befund,
  jetzt ueberholt)
