# CIP-simulaatioalusta

Monisessiollinen OPC UA -yhdyskäytävä ja ohjauspaneeli prosessisimulaatioille.  
Yksi OPC UA -päätepiste, monta rinnakkaista simulaatioistuntoa — CODESYS näkee jokaisen omana kansi­onaan.

## Pika-aloitus

### Palvelimella (Ubuntu 22.04 LTS)

```bash
# 1. Asenna alusta (kloonaa repo, asentaa riippuvuudet, migroi tietokannan)
git clone https://github.com/tomasmak-boop/SimulaattoriProjekti /opt/cip-sim
cd /opt/cip-sim
sudo INTRANET_CIDR=10.0.0.0/24 ./install.sh

# 2. Tarkista että palvelut käynnistyivät
sudo systemctl status cip-control-plane cip-worker-pool cip-event-consumer cip-gateway

# 3. Avaa selain
http://<palvelimen-ip>:8000/
```

## Päivitys

```bash
cd /opt/cip-sim
sudo ./deploy.sh
```

## Arkkitehtuuri lyhyesti

```text
CODESYS / SCADA ──┐
Selain ───────────┼──▶ OPC UA -yhdyskäytävä :8080 ──▶ Redis Streams ──▶ Worker Pool Dameon
opcua_cli ────────┘     Django Control Plane :8000                             │
                                                                               ▼
                                                                        Worker prosessit
                                                                        (OPC UA :5xxx, HTTP :6xxx)
```

- **Django-ohjauspaneeli (:8000)** — selain-UI, REST-rajapinta, WebSocket
- **OPC UA -yhdyskäytävä (:8080)** — yksi vakaa päätepiste kaikille istunnoille
- **Worker Pool Daemon** — luo ja valvoo simulaatioprosesseja
- **Redis Streams** — komentoväylä, tapahtumavirta, sydänlyönnit, porttivaraukset

## Hakemistorakenne

```text
cip-sim/
├── control_plane/       Django-ohjauspaneeli (UI + REST + WS)
├── gateway/             OPC UA -yhdyskäytävä
├── worker_pool/         Työntekijäprosessien hallinta
├── sim_runtime/         Simulaatioajonaika (OPC UA + HTTP -adapterit)
├── sim_plugins/         Simulaatioplugin­it (cip, blinker, ...)
├── deploy/              systemd-yksiköt ja palomuuri
├── docs/                Käyttö-, ylläpito- ja arkkitehtuuriohjeet
├── install.sh           Asennusskripti tuoreelle Ubuntu 22.04:lle
└── deploy.sh            Päivitysskripti tuotantopalvelimelle
```

## Dokumentaatio

| Tiedosto | Kohdeyleisö |
|---|---|
| [docs/quickstart.md](https://https://github.com/tomasmak-boop/SimulaattoriProjekti/tree/main/docs/quickstart.md) | Opiskelijat ja käyttäjät |
| [docs/operations.md](https://https://github.com/tomasmak-boop/SimulaattoriProjekti/tree/main/docs/operations.md) | Järjestelmänvalvojat |
| [docs/architecture.md](https://https://github.com/tomasmak-boop/SimulaattoriProjekti/tree/main/docs/architecture.md) | Kehittäjät ja arkki­tehdit |

## Vaatimukset

- Ubuntu 22.04 LTS (tai uudempi)
- Python 3.11+
- Redis 6.2+ (`XAUTOCLAIM`-tuki)
- SQLite (oletus) tai PostgreSQL
- Intranet-yhteys CODESYS-asiakkaisiin

## Testaus

```bash
python -m pytest tests/unit/ -v
python -m pytest control_plane/apps/dashboard/tests/ -v
```