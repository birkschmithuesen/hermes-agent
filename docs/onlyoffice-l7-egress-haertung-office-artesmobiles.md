# OnlyOffice Document Server (office.artesmobiles.art) — L7-Pfad-Allowlist, SSRF, Update-Stand, Docker-Egress-Sperre, IPv6

Karte `t_1969743d` (Kontext: Elternkarte `t_7869d241`). Stand: 2026-09-24, Rolle ops.

🔴 **Scope-Wechsel (Birk 2026-09-24, siehe Kartenkommentare):** Die urspruengliche
L3-Quell-IP-Allowlist auf 443 ist VERWORFEN. Grund: Nextcloud-Clients (Browser
der Nutzerinnen) sprechen den Document Server DIREKT an — Editor-Assets
(`api.js`), Editor-WebSocket, sdkjs. Eine Quell-IP-Beschraenkung auf 443 haette
das Dokument-Oeffnen fuer alle Beteiligten zerstoert. Primaerquelle:
ONLYOFFICE-Nextcloud-README ("You will need an instance of ONLYOFFICE Docs
that is resolvable and connectable both from Nextcloud and any end clients")
und die API-Doku "Opening file" (Browser laedt `api.js` vom Document Server
und der Editor verbindet sich mit demselben Dienst).

**Neuer Scope, aus Birks Entscheidung uebernommen, nicht selbst gewaehlt:**
1. L7-Pfad-Allowlist im nginx des Containers: Server-zu-Server-Endpunkte nur
   von den Nextcloud-IPs, Browser-Pfade bleiben offen.
2. SSRF: `allowPrivateIPAddress`/`allowMetaIPAddress` pruefen und auf `false`
   setzen, Wirkung nachweisen.
3. Update-Stand: Version vs. aktuellem Release, offene CVEs benennen.
4. Docker-Egress-Sperre fuer den Container (Muster `hermes_docker_egress`,
   OpenDesign-Analogie) — ERST MESSEN (LOG, nicht blockierend), dann sperren.
5. IPv6:443 freischalten (Entscheidung auf `t_fedc3249`, Variante a) — NACH
   Punkt 1, damit v6 nicht schutzloser ist als v4.

Alle Root-Befehle unten sind **ENTWORFEN, NICHT AUSGEFUEHRT**. `iptables`/
`nft`/`docker`/Datei-Zugriff auf den Container sind als uid `birk` gesperrt
(`docker.sock`: `permission denied`; Container-Rootfs unter `/proc/<pid>/root/`
ebenfalls `Permission denied` — geprueft, nicht vermutet).

---

## 0. Was ohne root gemessen werden konnte

### 0.1 Container-Aufbau (rekonstruiert ohne Docker-Socket, siehe `docker-compose.yml`
    unten und `/tmp/rv/sbx-1789562449/p/deploy/onlyoffice/` aus einer Vorarbeit)

```
Image            : onlyoffice/documentserver:7.1.1 (build:23, ds_release_date 2022-05-30)
Container-IP     : 172.17.0.2 (docker0-Bruecke)
Port-Publish     : 443:443 (v4 UND v6, zwei docker-proxy-Prozesse)
Bind-Mounts      : /app/onlyoffice/DocumentServer/data -> /var/www/onlyoffice/Data (Certs)
                    /app/onlyoffice/DocumentServer/sdkjs-plugins -> .../sdkjs-plugins
Named/anonyme    : oo_log, oo_lib, oo_postgres, oo_rabbitmq, oo_redis, oo_fonts (alle anonym,
                    64-stellige Hex-IDs — siehe Bewertung t_34388bdd Upgrade-Doku)
JWT              : AKTIV, extern belegt (ConvertService -> {"error":-8}, CommandService ->
                    {"error":6} = "Invalid token" laut api.onlyoffice.com/docs/docs-api/
                    additional-api/command-service/)
```

### 0.2 Von aussen erreichbare Pfade (gemessen 2026-09-24, `https://office.artesmobiles.art`)

| Pfad | HTTP-Status | Einordnung |
|---|---|---|
| `/` | 200 | Browser — Startseite |
| `/healthcheck` | 200 | Monitoring — MUSS offen bleiben (Birks Liste) |
| `/welcome/` | 200 | Browser |
| `/web-apps/apps/api/documents/api.js` | 200 | Browser — Editor-Bootstrap, MUSS offen bleiben |
| `/example/` | 502 | kein aktiver Upstream (Example-App laeuft hier nicht) |
| `/info/info.json` | **403** | **bereits eingeschraenkt** (nicht durch uns) |
| `/cache/files/` | **403** | bereits eingeschraenkt |
| `/sdkjs/` | **403** | bereits eingeschraenkt (Verzeichnislisting, einzelne Dateien vermutlich per `api.js`-Referenz weiter erreichbar — nicht abschliessend geprueft) |
| `/internal/cluster` | **403** | bereits eingeschraenkt |
| `/coauthoring/CommandService.ashx` | 200 (JSON `{"error":6}`, JWT greift) | Server-zu-Server — SOLL eingeschraenkt werden |
| `/ConvertService.ashx` | 200 (`{"error":-8}`, JWT greift) | Server-zu-Server — SOLL eingeschraenkt werden |

**Wichtiger Befund:** Ein Teil der Server-zu-Server-Pfade ist bereits per
nginx-Konfiguration (Werksstandard von ONLYOFFICE, nicht unsere Aenderung) auf
403 gesetzt (`info/info.json`, `internal/cluster`, `cache/files/`). Das ist die
Standard-nginx-Config von `onlyoffice/documentserver`, die diese Pfade schon
werkseitig auf lokale Anfragen beschraenkt (`allow 127.0.0.1; deny all;` ist das
bekannte Muster aus der Upstream-Config). **`CommandService.ashx` und
`ConvertService.ashx` sind NICHT eingeschraenkt** — JWT ist die einzige
Zugriffskontrolle, die dort heute greift. Das ist der Luecken-Teil, den diese
Karte schliesst: JWT schuetzt vor *inhaltlichem* Missbrauch (kein gueltiges
Kommando ohne Secret), aber nicht davor, dass jede beliebige IP weltweit
ueberhaupt Anfragen an diese Endpunkte stellen und damit z. B. Brute-Force
gegen das JWT-Secret oder Lastangriffe fahren kann.

### 0.3 Der Nginx-Container-Dateisystempfad ist NICHT lesbar

```
$ ls /proc/4561/root/etc/nginx/
ls: cannot access '/proc/4561/root/etc/nginx/': Permission denied
```
(`4561` = `docservice`-Prozess im Container, gehoert uid 109 im Container-Namespace,
nicht uid `birk` auf dem Host.) **Der exakte Inhalt der nginx-Config im Container
(`ds.conf` + Includes) ist als uid `birk` nicht einsehbar** — auch nicht ueber
`/proc/<pid>/root/`, obwohl dieser Pfad fuer andere Prozesse (eigene) funktioniert.

Deshalb: Die konkreten `location`-Bloecke unten sind aus der **oeffentlich
dokumentierten** ONLYOFFICE-Pfadstruktur (API-Doku, GitHub-Issues, siehe
Quellenliste) abgeleitet, NICHT aus der realen Datei zitiert. **Schritt 1 im
Root-Ablauf unten ist deshalb Pflicht, nicht optional** — er liest die echte
Config, bevor irgendetwas ueberschrieben wird.

---

## 1. L7-Pfad-Allowlist im nginx

### 1.1 Server-zu-Server-Pfade (nur von den Nextcloud-IPs)

Aus der ONLYOFFICE-API-Dokumentation und dem Nettitude-Advisory zu
CVE-2021-43444/43449 (Server-zu-Server-Endpunkte des Document Server):

```
/coauthoring/CommandService.ashx     <- Command-Service, Versionen VOR 8.2 (7.1.1 ist davor)
/ConvertService.ashx                 <- Convert-Service
/info/info.json                      <- bereits 403 (Werksstandard, siehe 0.2 — gegenpruefen)
/example/                            <- bereits 502 (kein Upstream, siehe 0.2 — gegenpruefen)
/internal/*                          <- bereits 403 (Werksstandard, siehe 0.2 — gegenpruefen)
```

Fuer die beiden **tatsaechlich offenen** Server-zu-Server-Pfade
(`CommandService.ashx`, `ConvertService.ashx`) fehlt die IP-Einschraenkung.

### 1.2 Browser-Pfade (bleiben offen — NICHT anfassen)

```
/                                     Startseite
/welcome/                             Willkommensseite
/web-apps/apps/api/documents/api.js   Editor-Bootstrap (Browser laedt das direkt)
/web-apps/                            Editor-UI-Assets
/sdkjs/                               Editor-Rendering (teils bereits 403 auf Verzeichnislisting,
                                       einzelne Dateien via api.js-Referenz — NICHT weiter einschraenken)
/doc/                                 WebSocket-Verbindung fuer Coauthoring (Browser)
/fonts/, /cache/                      Browser-Assets
/healthcheck                          Monitoring, MUSS offen bleiben (Birks Vorgabe)
```

### 1.3 Root-Ablauf (ANWENDEN-Block, kommentiert, idempotent)

🔴 **Schritt 1 ist Pflicht vor Schritt 2.** Ohne die echte Config zu lesen, ist
nicht bekannt, ob die Server-zu-Server-Pfade als `location =` (exakt), als
Praefix, oder als Regex im laufenden `ds.conf` stehen — und davon haengt ab,
welche Spezifitaet unsere Override-Regel braucht, um zu greifen (nginx: `location =`
> `location ^~` > laengster Praefix > Regex in Deklarationsreihenfolge).

```bash
# ============================================================
# SCHRITT 1 — NUR LESEN. Ermittelt die reale nginx-Config im
# Container, damit Schritt 2 nicht gegen die falsche Struktur baut.
# ============================================================
CID=$(docker ps -q --filter "publish=443" | head -1)
echo "Container: $CID"

# Vollstaendige, generierte Config (ONLYOFFICE generiert ds.conf aus Templates
# beim Container-Start via documentserver-generate-allfonts.sh / gen-conf).
docker exec "$CID" cat /etc/nginx/conf.d/ds.conf
docker exec "$CID" nginx -T 2>&1 | grep -A3 -E 'location.*(CommandService|ConvertService|info|internal|example)'

# local.json fuer SSRF (Schritt 2 unten) UND fuer den JWT-Header-Namen (falls
# spaeter relevant).
docker exec "$CID" cat /etc/onlyoffice/documentserver/local.json

# Version + CVE-Referenzpunkt (Schritt 3).
docker exec "$CID" cat /var/www/onlyoffice/documentserver/server/DocService/sources/version.json 2>/dev/null \
  || docker exec "$CID" grep -r '"version"' /var/www/onlyoffice/documentserver/ 2>/dev/null | head -3
```

```bash
# ============================================================
# SCHRITT 2 — ANWENDEN. Pfad-Allowlist als EIGENE, gemountete Datei
# — NICHT als In-Place-Edit der Image-Config (die geht beim naechsten
# `docker pull`/Neuanlegen verloren, das ist explizit Birks Vorgabe).
#
# Vorgehen: Datei auf den Host schreiben, per BIND-MOUNT in den Container
# einhaengen, ds.conf um ein `include` ergaenzen ODER die Override-Datei so
# platzieren, dass sie im bereits vorhandenen Include-Verzeichnis landet
# (die generierte ds.conf inkludiert typischerweise
# /etc/onlyoffice/documentserver/nginx/includes/*.conf — das ist die
# ANNAHME aus 0.3, die Schritt 1 bestaetigen oder widerlegen muss).
#
# Ein NEUER Bind-Mount bedeutet: der Container muss mit einem zusaetzlichen
# `-v` neu angelegt werden (`docker run` nicht `docker restart`) — das ist
# eine Dienstunterbrechung. Gehoert ins vereinbarte Nachtfenster 03:00-05:00.
# ============================================================

# 2a. Host-seitige Override-Datei anlegen (ausserhalb des Containers, ueberlebt
#     jeden Container-Neubau von selbst, solange der Mount-Pfad gleich bleibt).
install -d -m 0755 /app/onlyoffice/DocumentServer/nginx-overrides
cat > /app/onlyoffice/DocumentServer/nginx-overrides/l7-restrict-internal.conf <<'EOF'
# hermes t_1969743d — L7-Pfad-Allowlist fuer Server-zu-Server-Endpunkte.
# Browser-Pfade (/, /web-apps, /sdkjs, /doc, /healthcheck, /fonts, /cache)
# werden HIER NICHT beruehrt.
#
# WICHTIG: exakte Praefixe ("location = /pfad" bzw. "location ^~ /pfad"),
# damit die Regel Vorrang vor generischen `location /` Regeln aus der
# Original-Config hat. Reihenfolge/Spezifitaet gegen Schritt 1 gegenpruefen.

geo $nc_source {
    default 0;
    91.98.254.144/32 1;                     # Nextcloud v4 (Stand 2026-09-24, Karte t_1969743d)
    2a01:4f8:bacc:2:200::1710/128 1;         # Nextcloud v6 (Stand 2026-09-24, Karte t_1969743d)
    # 🔴 Diese Adresse wird vom IP-Watcher (Punkt "IP watcher" unten) beobachtet.
    # Aendert sie sich (wie am 2026-09-16 bereits einmal geschehen), MUSS diese
    # Datei nachgezogen werden, sonst bricht die Nextcloud-Anbindung.
}

location = /coauthoring/CommandService.ashx {
    if ($nc_source = 0) { return 403; }
    include /etc/onlyoffice/documentserver/nginx/includes/ds-docservice.conf;  # ANNAHME, per Schritt 1 verifizieren
}

location = /ConvertService.ashx {
    if ($nc_source = 0) { return 403; }
    include /etc/onlyoffice/documentserver/nginx/includes/ds-docservice.conf;  # ANNAHME, per Schritt 1 verifizieren
}

# /info/info.json, /internal/*, /example/ sind laut Messung 0.2 bereits 403
# bzw. ohne Upstream (502) — NICHT nochmals anfassen, nur gegenpruefen
# (Schritt 1), dass sich daran nichts geaendert hat.
EOF

# 2b. Container MIT dem neuen Mount neu anlegen (Nachtfenster!).
#     Restlicher docker run exakt wie der laufende Container — Werte aus
#     0.1 oben, .env-JWT_SECRET vom Original uebernehmen (siehe
#     Upgrade-Vorarbeit env.example.txt: docker inspect --format
#     '{{range .Config.Env}}{{println .}}{{end}}' $CID | grep JWT_SECRET).
docker stop onlyoffice-documentserver
docker rename onlyoffice-documentserver onlyoffice-documentserver.bak-$(date +%s)   # Rueckbau-Anker, siehe unten
docker run -d --name onlyoffice-documentserver --restart always \
  -p 443:443 \
  -e JWT_ENABLED=true \
  -e JWT_SECRET="$(cat /app/onlyoffice/DocumentServer/.jwt-secret)" \
  -v /app/onlyoffice/DocumentServer/data:/var/www/onlyoffice/Data \
  -v /app/onlyoffice/DocumentServer/sdkjs-plugins:/var/www/onlyoffice/documentserver/sdkjs-plugins \
  -v /app/onlyoffice/DocumentServer/nginx-overrides/l7-restrict-internal.conf:/etc/nginx/conf.d/zz-l7-restrict-internal.conf:ro \
  onlyoffice/documentserver:7.1.1
  # 🔴 Die alten anonymen Volumes (oo_log/oo_lib/oo_postgres/oo_rabbitmq/
  # oo_redis/oo_fonts) werden NICHT uebernommen — neuer Container bekommt
  # frische. Laut Upgrade-Vorarbeit (t_34388bdd) unkritisch: Dokumente liegen
  # in Nextcloud, DB/Queue sind nur Cache. AUSSER /usr/share/fonts/truetype/
  # custom enthaelt eigene Schriften — VOR dem Neubau pruefen:
  #     docker exec onlyoffice-documentserver ls -la /usr/share/fonts/truetype/custom

# 2c. Nachweis: greift die Regel, ohne Browser-Pfade zu treffen?
curl -s -o /dev/null -w "CommandService (fremde IP)  -> %{http_code}\n" \
  -X POST https://office.artesmobiles.art/coauthoring/CommandService.ashx
# ERWARTUNG: 403 (vorher: 200 mit {"error":6})
curl -s -o /dev/null -w "healthcheck (Browser-Pfad)  -> %{http_code}\n" \
  https://office.artesmobiles.art/healthcheck
# ERWARTUNG: unveraendert 200
curl -s -o /dev/null -w "api.js (Browser-Pfad)       -> %{http_code}\n" \
  https://office.artesmobiles.art/web-apps/apps/api/documents/api.js
# ERWARTUNG: unveraendert 200
```

```bash
# ============================================================
# RUECKBAU (falls der Nachtest scheitert oder Birk Dokumente in der
# Nextcloud nicht mehr oeffnen kann)
# ============================================================
docker stop onlyoffice-documentserver
docker rm onlyoffice-documentserver
docker rename onlyoffice-documentserver.bak-<timestamp> onlyoffice-documentserver
docker start onlyoffice-documentserver
# Nachweis: Original-Verhalten wieder da.
curl -s -o /dev/null -w "%{http_code}\n" https://office.artesmobiles.art/healthcheck   # -> 200
```

---

## 2. SSRF — `allowPrivateIPAddress` / `allowMetaIPAddress`

### 2.1 Warum das noetig ist, auch ohne aktives CVE

Der Document Server laedt Dokumente per Convert/Command-Service von beliebigen
URLs herunter (Kern-Funktionalitaet, kein Bug). Ohne Einschraenkung kann dieser
Pfad genutzt werden, um interne Adressen abzufragen (Cloud-Metadata-Dienste,
`172.17.0.1` = Docker-Host, andere Container). CVE-2021-43449 (SSRF, GENAU
dieser Mechanismus) betraf Versionen **bis einschliesslich 7.0.0.49** — unser
Stand **7.1.1** liegt DARUEBER und ist laut NVD-Versionsangabe (`versionEndIncluding:
7.0.0.49`) fuer dieses konkrete CVE bereits gepatcht. Die Konfigurationsoption
existiert aber unabhaengig vom CVE-Fix weiter und ist Verteidigung in der
Tiefe — deshalb Birks Auftrag, sie explizit zu pruefen statt sich auf den
CVE-Patch-Stand zu verlassen.

Konfigurationspfad (bestaetigt ueber mehrere GitHub-Issues von ONLYOFFICE
selbst, z. B. `ONLYOFFICE/onlyoffice-nextcloud#930`, `#803`,
`ONLYOFFICE/Docker-DocumentServer#837`):

```json
{
  "services": {
    "CoAuthoring": {
      "request-filtering-agent": {
        "allowPrivateIPAddress": false,
        "allowMetaIPAddress": false
      }
    }
  }
}
```

### 2.2 Root-Ablauf

```bash
# ============================================================
# SCHRITT 1 — LESEN. Ist-Zustand ermitteln (Teil von Schritt 1 oben, hier
# nochmal isoliert fuer diesen Abschnitt).
# ============================================================
CID=$(docker ps -q --filter "publish=443" | head -1)
docker exec "$CID" cat /etc/onlyoffice/documentserver/local.json
# ERWARTUNG laut GitHub-Issues: der Schluessel existiert oft NICHT im
# Default (= Bibliotheksdefault greift, i. d. R. request-filtering-agent
# blockt private Ranges bereits standardmaessig -- aber das ist NICHT
# verifiziert fuer 7.1.1 und MUSS an dieser Ausgabe abgelesen werden,
# nicht angenommen werden).
```

```bash
# ============================================================
# SCHRITT 2 — ANWENDEN, nur falls Schritt 1 zeigt, dass
# allowPrivateIPAddress/allowMetaIPAddress fehlen oder auf true stehen.
# local.json liegt im Image, NICHT auf einem Host-Bind (siehe 0.1) ->
# per Bind-Mount ueberschreiben, damit ein Image-Update sie nicht killt.
# ============================================================
install -d -m 0755 /app/onlyoffice/DocumentServer/config-overrides
docker exec "$CID" cat /etc/onlyoffice/documentserver/local.json \
  > /app/onlyoffice/DocumentServer/config-overrides/local.json.orig   # Sicherung des Ist-Zustands

python3 - <<'PYEOF'
import json
p = "/app/onlyoffice/DocumentServer/config-overrides/local.json.orig"
out = "/app/onlyoffice/DocumentServer/config-overrides/local.json"
with open(p) as f:
    cfg = json.load(f)
cfg.setdefault("services", {}).setdefault("CoAuthoring", {}).setdefault(
    "request-filtering-agent", {}
)
cfg["services"]["CoAuthoring"]["request-filtering-agent"]["allowPrivateIPAddress"] = False
cfg["services"]["CoAuthoring"]["request-filtering-agent"]["allowMetaIPAddress"] = False
with open(out, "w") as f:
    json.dump(cfg, f, indent=2)
print("geschrieben:", out)
PYEOF

# In den Container-Neubau aus Schritt 1.3 mit aufnehmen (zusaetzliche -v-Zeile):
#   -v /app/onlyoffice/DocumentServer/config-overrides/local.json:/etc/onlyoffice/documentserver/local.json:ro
```

```bash
# ============================================================
# NACHWEIS — eine Conversion mit privatem Ziel MUSS fehlschlagen.
# Docker-Host-Gateway aus dem Container: 172.17.0.1
# ============================================================
curl -s -X POST https://office.artesmobiles.art/ConvertService.ashx \
  -H "Content-Type: application/json" \
  -d '{"async":false,"filetype":"txt","outputtype":"docx","key":"ssrf-probe","url":"http://172.17.0.1:28764/"}'
# ERWARTUNG: Fehler (kein erfolgreicher Download/Convert-Response mit Inhalt
# von 172.17.0.1). Ohne gueltiges JWT liefert der Aufruf zwar zuerst
# {"error":-8} (Invalid token) -- fuer den ECHTEN Nachweis braucht es ein
# gueltiges Token (aus lokalem JWT_SECRET signiert), sonst wird der
# SSRF-Filter durch den Token-Check gar nicht erreicht. Signierbefehl
# (Node, im Container vorhanden):
docker exec "$CID" node -e '
const jwt = require("/var/www/onlyoffice/documentserver/server/Common/node_modules/jsonwebtoken");
const secret = process.env.JWT_SECRET;
const payload = {url:"http://172.17.0.1:28764/", filetype:"txt", outputtype:"docx", key:"ssrf-probe"};
console.log(jwt.sign(payload, secret));
'
# den Token dann als {"token": "<...>"} mitsenden -- Anleitung analog zur
# ONLYOFFICE Command-Service-Doku oben.
```

```bash
# ============================================================
# RUECKBAU
# ============================================================
# local.json-Override-Mount entfernen bzw. auf local.json.orig zeigen lassen
# und Container neu anlegen (analog Schritt 1.3 Rueckbau).
```

---

## 3. Update-Stand und offene CVEs

Laufend: **onlyoffice/documentserver:7.1.1** (build 23, `ds_release_date`
2022-05-30). Aktuell (Stand 2026-09-24, GitHub-Tags): **v10.0.0.112** (zuletzt
mit `v10.0.0.*`-Schema — die alte `X.Y.Z`-Zaehlung endete bei etwa 9.4, siehe
ONLYOFFICE-Docs-Ankuendigung "ONLYOFFICE Docs 9.4 released" auf der API-Doku).
**Der Versionsrueckstand betraegt mehrere Major-Versionen (7.1.1 -> ~9.4/10.x),
nicht ein einzelnes Sicherheitsupdate.**

Aus NVD (`services.nvd.nist.gov`, Stichwort "ONLYOFFICE Document Server",
CPE `cpe:2.3:a:onlyoffice:document_server`) sind fuer 7.1.1 relevant:

| CVE | Schwere | Betrifft 7.1.1? | Kurzbeschreibung |
|---|---|---|---|
| CVE-2023-30188 | **HIGH** (CVSS 3.1) | **JA** (4.0.3–7.3.2) | Memory-Exhaustion / DoS via manipuliertes JavaScript-Makro |
| CVE-2023-46988 | MEDIUM | **JA** (vor 8.0.1) | Path Traversal ueber `fileExt`-Parameter — Datei-Kopie ausserhalb des vorgesehenen Verzeichnisses |
| CVE-2021-43449 | HIGH | **NEIN** (bis 7.0.0.49, wir sind druber) | SSRF ueber den Editor-Service — bereits gepatcht, siehe Abschnitt 2 fuer den Konfig-Teil unabhaengig davon |
| CVE-2022-24229 | MEDIUM | **NEIN** (Example-App vor 7.0.0) | XSS in der Example-App — bei uns ohnehin 502/kein aktiver Upstream (0.2) |
| CVE-2020-11534/35/36/37 | CRITICAL | **NEIN** (nur 5.5.0) | RCE/SQLi-Kette, laengst hinter uns |

**Zwei offene, unbehobene CVEs also: CVE-2023-30188 (HIGH, DoS) und
CVE-2023-46988 (MEDIUM, Path Traversal).** Keine der beiden ist durch die
L7-Allowlist oder die SSRF-Konfiguration aus Abschnitt 1/2 gemindert — sie
sind Bugs im Convert/Rendering-Pfad selbst, nicht am Netzwerkzugang.

**Update ist laut Birks Auftrag ein SEPARATER Admin-Schritt**, kein Teil des
ANWENDEN-Blocks hier — ein Major-Versionssprung 7.1.1 -> 9.x/10.x ist ein
eigenes Projekt (neues Datenbankschema, ggf. neue JWT-Handhabung ab 8.2, neue
Nginx-Struktur — siehe die bereits vorliegende `UPGRADE.md` aus der
Vorarbeit `/tmp/rv/sbx-1789562449/p/deploy/onlyoffice/UPGRADE.md`, die genau
diesen Weg fuer den Sprung auf 9.4 vorbereitet hat). Diese Karte dokumentiert
den Rueckstand, entscheidet aber nicht ueber das Upgrade.

---

## 4. Docker-Egress-Sperre fuer den OnlyOffice-Container

### 4.1 Muster: `hermes_docker_egress` / OpenDesign-Analogie

Vorbild ist die bereits produktive Sperre fuer `od_isolated`
(`/etc/nftables-docker-egress.conf`, Tabelle `hermes_docker_egress`,
`hermes-od-egress.service`, `Before=docker.service`). Dieselbe Struktur, neue
Tabelle (eigener Namensraum, um die bestehende OpenDesign-Regel NICHT
anzufassen — Kartenvorgabe).

🔴 **ERST MESSEN, DANN SPERREN — LOG-Regel zuerst, nicht blockierend.** Die
Annahme "nur Nextcloud wird gebraucht" ist laut Birks Anweisung UNVERIFIZIERT:
Plugins (z. B. Google-Translate-Plugin, siehe CVE-2021-40864 oben in der
Vorarbeit t_fedc3249 nicht erwaehnt, aber ein bekanntes DS-Plugin), Schriften-
Nachladen, Lizenz-/Update-Checks koennten weitere Ziele brauchen.

### 4.2 Root-Ablauf — Phase 1: Messen (LOG only, keine Blockade)

```bash
# ============================================================
# PHASE 1 — nur protokollieren, NICHTS blockieren. Ueber mehrere Stunden
# laufen lassen, waehrend Birk normal in der Nextcloud Dokumente bearbeitet
# (Skill llm-free-watchdog-jobs, Abschnitt "Messfenster": mindestens so lang,
# dass seltene Pfade — Lizenzcheck, Update-Ping — eine Chance haben
# aufzutauchen; ein Tag ist der Mindestwert, kein Fensterschluss vor Ablauf).
# ============================================================
cat > /etc/nftables-onlyoffice-egress-MESSPHASE.conf <<'EOF'
#!/usr/sbin/nft -f
# Messphase fuer OnlyOffice-Container-Egress (172.17.0.2). NUR LOG, kein DROP.
table inet oo_egress_probe
delete table inet oo_egress_probe

table inet oo_egress_probe {
    chain forward {
        type filter hook forward priority -5; policy accept;
        ip saddr 172.17.0.2 ct state new log prefix "oo-egress-probe " counter
    }
}
EOF
nft -c -f /etc/nftables-onlyoffice-egress-MESSPHASE.conf   # Syntax-Check
nft -f /etc/nftables-onlyoffice-egress-MESSPHASE.conf      # anwenden, NICHTS wird geblockt

# Nach dem Messfenster (>= 24h, mit mind. einer aktiven Bearbeitungssession
# durch Birk in der Nextcloud):
journalctl -k --since "-24h" | grep 'oo-egress-probe' | \
  awk '{for(i=1;i<=NF;i++) if($i ~ /^DST=/) print $i}' | sort -u
# -> Liste ALLER Ziel-IPs, die der Container tatsaechlich angefragt hat.
# Gegen die Nextcloud-IPs (91.98.254.144 / 2a01:4f8:bacc:2:200::1710) und
# ggf. weitere Ziele abgleichen -- JEDES zusaetzliche Ziel ist ein Befund,
# der VOR der Sperre gepruesft werden muss (legitim = Font-CDN/Lizenzserver?
# oder verdaechtig?), nicht blind in die Allowlist uebernommen.

# Messphase abbauen, sobald ausgewertet:
nft delete table inet oo_egress_probe
```

### 4.3 Root-Ablauf — Phase 2: Sperren (erst nach Auswertung von Phase 1)

```bash
# ============================================================
# PHASE 2 — ANWENDEN, NACHDEM Phase 1 ausgewertet ist. Die Menge
# <GEMESSENE_ZIELE> unten ist ein PLATZHALTER -- mit den echten IPs aus
# Phase 1 befuellen, NICHT nur mit der Nextcloud-Vermutung.
# ============================================================
cat > /etc/nftables-onlyoffice-egress.conf <<'EOF'
#!/usr/sbin/nft -f
# EGRESS-Sperre fuer den OnlyOffice-Container (172.17.0.2 auf docker0).
# Analog hermes_docker_egress (OpenDesign) -- EIGENE Tabelle, ruehrt die
# bestehende Regel fuer od_isolated NICHT an.
table inet oo_egress
delete table inet oo_egress

table inet oo_egress {
    chain forward {
        type filter hook forward priority -10; policy accept;
        ip saddr 172.17.0.2 jump oo_egress_out
    }

    # Host selbst darf ebenfalls nichts fuer den Container weiterreichen.
    chain output {
        type filter hook output priority -10; policy accept;
        ip daddr 172.17.0.2 ct state new tcp dport { 53 } drop
    }

    chain oo_egress_out {
        # Antworten auf EINGEHENDE Verbindungen (der veroeffentlichte Port
        # 443) muessen zurueckkoennen -- sonst stirbt der Dienst selbst.
        ct state established,related accept

        # DNS: noetig, wenn der Container Nextcloud/Update-Server per Namen
        # anspricht. Auf den Docker-internen Resolver beschraenken, NICHT
        # global oeffnen.
        udp dport 53 ip daddr 127.0.0.11 accept
        tcp dport 53 ip daddr 127.0.0.11 accept
        udp dport 53 drop
        tcp dport 53 drop

        # Erlaubte Ziele aus der Messphase (Phase 1) + Nextcloud v4/v6.
        # <GEMESSENE_ZIELE> durch die echte Liste ersetzen.
        ip daddr { 91.98.254.144 } tcp dport 443 accept
        ip6 daddr { 2a01:4f8:bacc:2:200::1710 } tcp dport 443 accept
        # <GEMESSENE_ZIELE>   # z.B. weitere IPs aus Phase 1, je mit Kommentar wofuer

        # KEIN accept Richtung 172.17.0.1 (Docker-Host) oder andere
        # Host-Dienste (100.75.24.33:8088/:9119 sind Tailnet-gebunden,
        # liegen NICHT auf docker0 -- diese Regel beruehrt sie nicht, weil
        # sie nur fuer Pakete MIT saddr 172.17.0.2 gilt, nicht fuer
        # eingehenden Tailnet-Verkehr).

        # Log + Drop GETRENNT, limit ZUERST (sonst faellt Verkehr ueber der
        # Rate durch auf policy accept -- fail-open-Falle, siehe
        # OpenDesign-Vorlage-Kommentar in nftables-docker-egress.conf).
        limit rate 5/minute log prefix "oo-egress-deny "
        drop
    }
}
EOF
nft -c -f /etc/nftables-onlyoffice-egress.conf
nft -f /etc/nftables-onlyoffice-egress.conf
```

### 4.4 Persistenz — systemd-Unit (Muster `hermes-od-egress.service`)

```bash
cat > /etc/systemd/system/hermes-oo-egress.service <<'EOF'
[Unit]
Description=OnlyOffice container egress lock -- nftables default-deny fuer 172.17.0.2
Documentation=file:///mnt/HC_Volume_106183673/hermes/hermes-agent/docs/onlyoffice-l7-egress-haertung-office-artesmobiles.md
# VOR docker.service: der Container startet mit restart=always von selbst,
# ohne diese Reihenfolge gaebe es ein Zeitfenster mit voll offenem Egress.
Before=docker.service
After=network-pre.target
Wants=network-pre.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStartPre=/usr/sbin/nft -c -f /etc/nftables-onlyoffice-egress.conf
ExecStart=/usr/sbin/nft -f /etc/nftables-onlyoffice-egress.conf
# KEIN ExecStop -- ein `systemctl stop` soll die Sperre NICHT entfernen,
# waehrend der Container weiterlaeuft (gleiche Begruendung wie hermes-od-egress).

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable hermes-oo-egress.service
systemctl restart hermes-oo-egress.service
systemctl is-enabled hermes-oo-egress.service
systemctl is-active hermes-oo-egress.service

nft list table inet oo_egress | grep -E 'saddr|drop|dport'
```

### 4.5 Nachweis

```bash
CID=$(docker ps -q --filter "publish=443" | head -1)

# 1. Dokument-Oeffnen/-Speichern in der Nextcloud funktioniert weiterhin
#    (Birk testet persoenlich, wie bei Teil A des urspruenglichen Plans).

# 2. Verbotene Ziele scheitern von INNEN dem Container:
docker exec "$CID" curl -m 5 -s -o /dev/null -w "%{http_code}\n" https://api.github.com \
  || echo "BLOCKIERT (Timeout/Fehler) -- korrekt"
docker exec "$CID" curl -m 5 -s -o /dev/null -w "%{http_code}\n" http://1.1.1.1 \
  || echo "BLOCKIERT (Timeout/Fehler) -- korrekt"
docker exec "$CID" curl -m 5 -s -o /dev/null -w "%{http_code}\n" http://172.17.0.1:28764 \
  || echo "BLOCKIERT (Timeout/Fehler) -- korrekt"

# 3. Erlaubtes Ziel funktioniert weiterhin (Nextcloud v4):
docker exec "$CID" curl -m 5 -sk -o /dev/null -w "%{http_code}\n" https://91.98.254.144/status.php
```

### 4.6 Rueckbau

```bash
systemctl disable --now hermes-oo-egress.service
nft delete table inet oo_egress
```

---

## 5. IPv6:443 freischalten (Entscheidung auf `t_fedc3249`, Variante a)

### 5.1 Ursache, aus `t_fedc3249` uebernommen (nicht neu gemessen, Quelle: Kommentar
    "CAUSE FOUND", Robo 2026-09-24, root-Dump vom 2026-09-24 18:23)

```
TCPv6:443 wird LOKAL von ufw geblockt, nicht von einem Hetzner-Filter:
  ip6 INPUT -> ufw6-user-input erlaubt nur 22 -> policy drop
Der v4-Pfad umgeht ufw, weil Docker 443 in PREROUTING per DNAT auf
172.17.0.2 umschreibt (FORWARD/DOCKER-USER, bisher ACCEPT fuer ALLE
Quellen auf eth0 tcp/443 -- keine Nextcloud-Allowlist vorhanden).
Fuer v6 gibt es KEIN DNAT (ip6 DOCKER-USER liefert nur RETURN), das Paket
landet also in INPUT auf docker-proxy [::]:443 und wird dort von ufw
verworfen.
```

`sudo ufw allow 443/tcp` legt bei `IPV6=yes` (bestaetigt in
`/etc/default/ufw`, siehe `t_fedc3249`-Bericht) **beide** Familien an. Fuer
v4 hat das **keine Wirkung** (DNAT greift vor INPUT). Fuer v6 oeffnet es
**weltweit**, weil es keine Quell-Einschraenkung kennt.

🔴 **Deshalb MUSS Schritt 1 dieses Abschnitts NACH Abschnitt 1 (L7-Pfad-Allowlist)
angewendet sein** — sonst waere v6:443 fuer den Moment zwischen `ufw allow` und
der Pfad-Sperre WENIGER geschuetzt als v4 (das schon eine L7-Beschraenkung hat).
Reihenfolge im ANWENDEN-Block also: **Abschnitt 1 -> hier.**

### 5.2 Root-Ablauf

```bash
# Reihenfolge: NACH Abschnitt 1 (L7-Pfad-Allowlist bereits aktiv).
sudo ufw allow 443/tcp
sudo ufw status verbose
# ERWARTUNG: 443/tcp ALLOW Anywhere UND 443/tcp (v6) ALLOW Anywhere (v6)
```

### 5.3 Nachweis (von herkules, 91.98.143.165 -- selbst KEIN v6-Egress auf
    dem vServer, siehe `t_fedc3249`-Befund 1.1)

```bash
# Von herkules (hat funktionierendes v6-Egress) oder ueber den
# Broker-Browser-Kontext:
curl -6 -sk -o /dev/null -w "%{http_code}\n" https://office.artesmobiles.art/healthcheck
# ERWARTUNG: 200 (vorher: Timeout/kein Connect)

curl -6 -sk -o /dev/null -w "CommandService v6 fremde Quelle -> %{http_code}\n" \
  -X POST https://office.artesmobiles.art/coauthoring/CommandService.ashx
# ERWARTUNG: 403 (die L7-Allowlist aus Abschnitt 1 wirkt unabhaengig von der
# IP-Familie, weil sie auf L7/nginx sitzt, nicht auf L3/iptables)
```

Nach erfolgreichem Nachweis: **`t_fedc3249` schliessen** (Birks Vorgabe) mit
Verweis auf diesen Bericht und die Nachweiswerte.

### 5.4 Rueckbau

```bash
sudo ufw delete allow 443/tcp
sudo ufw status verbose   # 443 nicht mehr in der Liste
```

---

## 6. IP-Watcher fuer `cloud.artesmobiles.art` (deckt Punkt 1 UND Punkt 4/5 ab)

Bereits einmal ist die Nextcloud-IP gewechselt (`91.98.248.60` ->
`91.98.254.144`, dokumentiert im Kommentarverlauf dieser Karte,
2026-09-16/17). Die L7-Allowlist (Abschnitt 1) und die Egress-Sperre
(Abschnitt 4) haengen BEIDE an dieser IP. Ein weiterer, unbemerkter Wechsel
wuerde beide Regeln lautlos falsch machen -- Nextcloud koennte dann weder
CommandService/ConvertService erreichen noch der Container zur Nextcloud
raus.

Nach Skill `llm-free-watchdog-jobs`: `no_agent=True`, LLM-frei, Vergleich
gegen den zuletzt bekannten Wert, still im Normalfall, Alarm bei Abweichung.
Deckt Punkt 1 (allgemeine IP-Beobachtung) UND die Abhaengigkeit aus Punkt
4/5 (dieselbe Adresse, keine zwei Waechter noetig) in einem Job ab.

```python
#!/usr/bin/env python3
"""IP-Watcher cloud.artesmobiles.art -- Karte t_1969743d.
no_agent=True. Vergleicht gegen den zuletzt gespeicherten Wert (v4+v6).
Stumm bei Uebereinstimmung, Alarm bei Abweichung -- inkl. Hinweis, WELCHE
Regeln (Abschnitt 1 L7-Allowlist, Abschnitt 4 Egress-Sperre) betroffen sind.
"""
import socket
import sys
from pathlib import Path

STATE = Path.home() / ".hermes" / "profiles" / "birk" / "var" / "oo-nc-ip-watch" / "last.txt"
STATE.parent.mkdir(parents=True, exist_ok=True)

def resolve(host, family):
    try:
        infos = socket.getaddrinfo(host, None, family)
        return sorted({i[4][0] for i in infos})
    except socket.gaierror:
        return None

v4 = resolve("cloud.artesmobiles.art", socket.AF_INET)
v6 = resolve("cloud.artesmobiles.art", socket.AF_INET6)

if v4 is None and v6 is None:
    sys.exit(1)  # Messfehler -- fail loud, nicht stumm

current = f"v4={','.join(v4 or [])} v6={','.join(v6 or [])}"

if not STATE.exists():
    STATE.write_text(current)
    sys.exit(0)  # erster Lauf, kein Vergleich moeglich, still

last = STATE.read_text().strip()
if current == last:
    sys.exit(0)  # unveraendert, still

STATE.write_text(current)
print(
    "Nextcloud-IP fuer cloud.artesmobiles.art hat sich geaendert!\n"
    f"  vorher: {last}\n"
    f"  jetzt : {current}\n"
    "Betroffen: L7-Pfad-Allowlist im OnlyOffice-nginx (docs/onlyoffice-l7-"
    "egress-haertung-office-artesmobiles.md Abschnitt 1) UND die Docker-"
    "Egress-Sperre (Abschnitt 4) -- beide muessen auf die neue IP "
    "nachgezogen werden, sonst bricht entweder die Nextcloud-Anbindung "
    "oder die alte IP bleibt faelschlich zugelassen."
)
```

```bash
HERMES_HOME=~/.hermes/profiles/birk hermes cron create "0 */4 * * *" \
  "IP-Watcher cloud.artesmobiles.art fuer OnlyOffice L7/Egress-Regeln" \
  --name oo-nc-ip-watch --script oo_nc_ip_watch.py --no-agent --deliver telegram
```

---

## 7. Zusammenfassung — was Birk entscheiden muss

| # | Entscheidung | Empfehlung |
|---|---|---|
| 1 | L7-Pfad-Allowlist anwenden (Container-Neubau im Nachtfenster) | Ja — schliesst die einzige unbeschraenkte Server-zu-Server-Luecke |
| 2 | SSRF-Konfiguration setzen | Ja — Verteidigung in der Tiefe, geringer Aufwand |
| 3 | CVE-2023-30188 / CVE-2023-46988 offen lassen oder Upgrade-Projekt anstossen | Getrennter Admin-Schritt, nicht Teil dieser Karte — siehe vorliegende `UPGRADE.md` |
| 4 | Egress-Sperre: erst Messphase (>= 24h), dann sperren | Ja, aber ERST nach ausgewerteter Messphase |
| 5 | IPv6:443 freischalten (`ufw allow 443/tcp`) | Ja, aber NUR nach Abschnitt 1 (Reihenfolge bindend) |
| 6 | IP-Watcher einrichten | Ja, unabhaengig von den anderen Punkten |

Alle Container-Neubauten (Abschnitt 1, 2, ggf. 4 falls der Egress-Filter
Container-seitig statt nur netzwerkseitig gebraucht wird) gehoeren ins
vereinbarte Nachtfenster 03:00-05:00 mit Vorankuendigung im Admin-Topic
(bestehende Vereinbarung aus `t_7869d241`).

---

## Quellen

- ONLYOFFICE Nextcloud README: "You will need an instance of ONLYOFFICE Docs
  that is resolvable and connectable both from Nextcloud and any end clients."
- ONLYOFFICE API-Doku, Command Service: <https://api.onlyoffice.com/docs/docs-api/additional-api/command-service/>
- Nettitude/PwnlyOffice-Advisory (CVE-2021-43444–43449, SSRF/RCE-Kette):
  <https://labs.nettitude.com/blog/exploiting-onlyoffice-web-sockets-for-unauthenticated-remote-code-execution/>
- NVD CVE-Suche `ONLYOFFICE Document Server`: <https://services.nvd.nist.gov/rest/json/cves/2.0?keywordSearch=ONLYOFFICE%20Document%20Server>
  (CVE-2020-11534/35/36/37, CVE-2021-3199, CVE-2021-25829, CVE-2021-40864,
  CVE-2021-43449, CVE-2022-24229, CVE-2022-29776/77, CVE-2023-30188,
  CVE-2023-46988)
- `allowPrivateIPAddress`/`allowMetaIPAddress`-Konfigurationspfad bestaetigt in:
  `ONLYOFFICE/onlyoffice-nextcloud#930`, `#803`, `ONLYOFFICE/Docker-DocumentServer#837`
- GitHub-Repo `ONLYOFFICE/Docker-DocumentServer` (Tags, aktuell v10.0.0.112)
- OpenDesign-Egress-Vorlage: `hermes-od-egress.service`,
  `/etc/nftables-docker-egress.conf`, `persist-od-egress.sh` (lokale Vorlage,
  nicht extern)
- Skill `llm-free-watchdog-jobs` (IP-Watcher-Muster)
- Kartenkommentare `t_1969743d`, `t_7869d241`, `t_fedc3249`, Vorarbeiten
  `t_34388bdd` (Bewertung DOCKER-USER vs. ufw-docker),
  `t_c133cfa3` (Befund-Reproduktion)
- Eigene Messung 2026-09-24: HTTP-Statusmatrix der Pfade (Abschnitt 0.2),
  `/proc/<pid>/root/`-Leseversuche (Abschnitt 0.3, `Permission denied`
  tatsaechlich erzeugt, nicht vermutet)
