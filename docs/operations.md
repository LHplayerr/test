# Ylläpitäjän käsikirja — CIP-simulaatioalusta

Tämä dokumentti on tarkoitettu **järjestelmänvalvojalle**, joka hallinnoi
palvelinta, jolla CIP-alusta pyörii. Kaikki komennot ajetaan palvelimella
`root`-oikeuksin tai `sudo`-etuliitteellä.

---

## 1. Palvelun rakenne

Alusta koostuu neljästä pitkäkestoisesta systemd-palvelusta ja yhdestä
ajastetusta tehtävästä:

| Yksikkö | Tehtävä | Portti |
|---|---|---|
| `cip-control-plane.service` | Django-palvelin (HTTP + WebSocket) | 8000 |
| `cip-worker-pool.service` | Työntekijäprosessien hallinta (daemon) | — |
| `cip-event-consumer.service` | Redis-tapahtumien kirjoitus tietokantaan | — |
| `cip-gateway.service` | OPC UA -yhdyskäytävä | 8080 |
| `cip-purge-events.timer` | Vanhojen tapahtumien siivous (päivittäin) | — |

Lisäksi `redis-server.service` on riippuvuus — se käynnistyy ennen
muita.

---

## 2. Palveluiden hallinta

### Tilanne

```bash```
systemctl status cip-control-plane cip-worker-pool cip-event-consumer cip-gateway

Lyhyt tarkistus:
systemctl is-active cip-control-plane cip-worker-pool cip-event-consumer cip-gateway

Käynnistys / pysäytys / uudelleenkäynnistys
sudo systemctl restart cip-control-plane
sudo systemctl restart cip-worker-pool
sudo systemctl restart cip-event-consumer
sudo systemctl restart cip-gateway

Kaikki kerralla:

sudo systemctl restart cip-control-plane cip-event-consumer cip-worker-pool cip-gateway