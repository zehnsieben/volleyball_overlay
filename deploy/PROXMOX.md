# Proxmox: eigene VM für das Overlay

Diese Schritte werden auf deinem Server ausgeführt. Die Dateien allein richten
noch keine VM, Firewall oder öffentliche Adresse ein.

## 1. VM erstellen

In Proxmox eine neue **Debian-VM** erstellen, nicht im Proxmox-Host oder in der
NAS-/Home-Assistant-Instanz installieren. Für den Anfang: 1 vCPU, 1 GB RAM,
8 GB Disk, Netzwerkmodell VirtIO. Debian minimal installieren, Zeitsynchronisierung
aktiv lassen und unter VM → Options „Start at boot“ aktivieren.
Keine Host-Verzeichnisse, NAS-Freigaben oder Geräte durchreichen.

## 2. Repository installieren

Die vorbereiteten Projektdateien zunächst in dein Git-Repository committen und pushen.
In der **Debian-VM**, über die Proxmox-Konsole, als root:

```sh
apt-get update
apt-get install -y git
git clone DEINE_REPOSITORY_URL /opt/volleyball-overlay
cd /opt/volleyball-overlay
sh deploy/install.sh
```

`DEINE_REPOSITORY_URL` ersetzen. Alternativ nur main.py, requirements.txt,
templates/ und deploy/ nach `/opt/volleyball-overlay` kopieren. Keine lokale
`.venv`, `.idea`, Beispiel-JSONs oder Laufzeitdateien kopieren.

Der Dienst startet automatisch. Beim ersten Start holt er den Spielplan selbst;
danach freitags um 00:00 Europe/Berlin. Bei einem Fehler wiederholt er den Abruf
nach 15 Minuten. Ohne Spiele wartet er bis zum nächsten Freitag.

Code und Python-Umgebung bleiben root-eigen. Der Dienst läuft als `volleyball`
und darf nur `/var/lib/volleyball-overlay` sowie sein privates temporäres Verzeichnis
beschreiben. Spielplan und Laufzustand liegen außerhalb des Repositorys.
Der Webserver lauscht ausschließlich auf `127.0.0.1:10000`; nur `/` und
`/api/match` werden bereitgestellt. Er erlaubt GET/HEAD, keine Schreibmethoden.
Besucher erreichen weiterhin Flask/Waitress; dies ist kein separater statischer
Dateiserver. Die VM-Firewall ist deshalb eine notwendige zusätzliche Schutzgrenze.

Prüfen:

```sh
systemctl status volleyball-overlay
journalctl -u volleyball-overlay -n 30 --no-pager
curl http://127.0.0.1:10000/api/match
curl -i -X POST http://127.0.0.1:10000/api/match
```

GET muss JSON liefern, POST den Status 405.

## 3. VM vom Heimnetz abschotten – vor Veröffentlichung

Die Regeln **außerhalb der VM in Proxmox** setzen. Nur die neue VM einschränken,
nicht unvorbereitet die Host-Managementregeln ändern. Netzwerkgerät → Firewall
aktivieren und prüfen, dass die Firewall auf Datacenter- und VM-Ebene aktiv ist.
Die benötigten Aktivierungsschritte hängen von deiner Proxmox-Version ab.

VM-Firewall: eingehend DROP, ausgehend DROP; gezielte Ausnahmen in dieser Reihenfolge:

1. DNS nur zu einem festgelegten Resolver erlauben (UDP/TCP 53). Falls das die
   FRITZ!Box ist, nur deren konkrete IP und Port 53 freigeben.
2. Alle internen Netze sowie sämtliche IPs von Proxmox und deinen anderen Geräten
   ausgehend sperren. IPv4 **und IPv6** berücksichtigen: private IPv4-Netze,
   `100.64.0.0/10`, Link-Local, IPv6-ULA und insbesondere das globale IPv6-Präfix
   deines Heimnetzes. Eine öffentliche IPv6-Adresse ist trotzdem intern erreichbar!
3. Danach TCP 443 für SAMS/Updates, TCP 80 für Debian-Paketquellen, UDP 123 für
   Zeitsynchronisierung und UDP/TCP 7844 für Cloudflare Tunnel erlauben.
4. Bei DHCP die notwendigen DHCP-Regeln aktivieren; bei IPv6 auch die erforderlichen
   ICMPv6-Nachbarschaftsregeln. Keine pauschale Freigabe zum gesamten Heimnetz.

Die VM darf das Gateway zur Weiterleitung ins Internet benutzen; das bedeutet
nicht, dass dessen Verwaltungsoberfläche erreichbar sein muss. Einrichtung und
Wartung zunächst über die Proxmox-Konsole, ohne SSH-Freigabe.
Eine separate Bridge/VLAN mit gefiltertem Routing ist zusätzlich möglich; eine
Bridge allein stellt weder Internetzugang noch eine Firewall bereit.

Vor dem Tunnel aus der VM verifizieren: SAMS per HTTPS erreichbar, aber Verbindungen
zu den realen Proxmox-/NAS-/Home-Assistant-Adressen und Ports gesperrt, auch per IPv6.
Bei deaktivierter Firewall wäre die Abschottung nicht gegeben.

## 4. Öffentliche Adresse ohne Anmeldung

Eine Domain auf Cloudflare und einen **Cloudflare Tunnel** anlegen. Die im Dashboard
für Debian angegebenen Installationsschritte für `cloudflared` in dieser VM ausführen
und den Tunnel als Dienst installieren. Das Tunnel-Token nicht im Repository speichern.

Eine einzige „Published application“-Route anlegen:

- Hostname: deine gewünschte Subdomain, beispielsweise `overlay.deinedomain.de`
- Service: `http://127.0.0.1:10000`
- Keine private Netzwerkroute und keine Cloudflare-Access-Anmeldung konfigurieren.

Keine Ports an der FRITZ!Box öffnen. Besucher öffnen die HTTPS-Adresse direkt.
Der Verkehr läuft über Cloudflare. `/api/match` nicht cachen; der Server liefert
bereits `Cache-Control: no-store`, also keine überschreibende Cache-Regel hinzufügen.

Abschließend über ein fremdes Netz prüfen: Website und Spielstand erreichbar,
POST abgewiesen, andere Pfade (z. B. `/main.py`, `/runtime.json`) liefern 404.
VM neu starten und beide Dienste kontrollieren.

## Wartung

Code aktualisieren (als root, bei sauberem Checkout):

```sh
cd /opt/volleyball-overlay
git pull --ff-only
sh deploy/install.sh
```

Die Laufzeitdaten in `/var/lib/volleyball-overlay` bleiben erhalten. Debian,
Python-Abhängigkeiten, cloudflared und Proxmox regelmäßig aktualisieren.

Herstellerdokumentation:
- [Proxmox-Firewall](https://pve.proxmox.com/pve-docs/chapter-pve-firewall.html)
- [Cloudflare Tunnel einrichten](https://developers.cloudflare.com/tunnel/get-started/)
