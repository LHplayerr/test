# Ylläpitokäsikirja

**Kohdeyleisö:** järjestelmänvalvoja, joka ottaa palvelimen haltuunsa luovutuksen jälkeen.  
**Laajuus:** CIP-simulaatioalustan ajaminen, valvonta, varmuuskopiointi ja vianetsintä tuotannossa.

Jos etsit loppukäyttäjän ohjetta ("miten istunto luodaan?"), katso [quickstart.md](quickstart.md).  
Jos etsit suunnitteluperusteluja, katso [architecture.md](architecture.md).

## 1. Mitä palvelimella pyörii

Alusta koostuu viidestä systemd-palvelusta, Rediksestä ja SQLite-tietokannasta. Kaikkia hallinnoidaan systemd:llä - **älä käynnistä niitä käsin** äläkä käytä `scripts/stack.sh`-skriptiä tuotantopalvelimella.

| Yksikkö | Tehtävä | Portti(t) |
|---|---|---|
| `cip-control-plane.service` | Django HTTP + WebSocket -palvelin (Uvicorn) | `8000/tcp` (vain sisäverkko) |
| `cip-worker-pool.service` | Käynnistää ja seuraa simulaatiotyöläisprosesseja | - |
| `cip-event-consumer.service` | Lukee Rediksen elinkaaritapahtumat, päivittää tietokannan, työntää reaaliaikapäivitykset selaimiin | - |
| `cip-gateway.service` | Kokoaa kaikkien työläisten OPC UA -palvelimet yhdeksi päätepisteeksi | `8080/tcp` (vain sisäverkko) |
| `cip-purge-events.timer` | Yöllinen yli 30 päivää vanhojen istuntotapahtumien siivous | - |

Tukevat komponentit:

| Komponentti | Tehtävä |
|---|---|
| `redis-server.service` | Viestiväylä (streamit + sydämenlyönnit + porttivaraukset). Aptin hallinnoima, ei CIP-asentajan. |
| `/var/lib/cip-sim/db.sqlite3` | Django-tietokanta (istunnot, katalogi, tapahtumaloki). |
| `/etc/cip-sim/env` | Kaikki ajonaikainen konfiguraatio. **Omistaja root, ryhmä `cip`, tila 0640.** Älä muokkaa `cip`-käyttäjänä. |
| `/var/log/cip-sim/` | Varattu tiedostopohjaisille lokeille (tällä hetkellä käyttämätön; lokit menevät journaldiin). |
| `/opt/cip-sim/` | Sovellushakemisto ja sen virtualenv. |

## 2. Päivittäinen palveluhallinta

Kaikki alla olevat komennot olettavat rootin tai salasanattoman `sudo`-oikeuden.

### Tila

```bash
sudo systemctl status cip-control-plane cip-worker-pool cip-event-consumer cip-gateway
sudo systemctl status cip-purge-events.timer
sudo systemctl is-active cip-*
sudo systemctl is-enabled cip-control-plane cip-worker-pool cip-event-consumer cip-gateway cip-purge-events.timer
```

### Käynnistys, pysäytys ja uudelleenkäynnistys

Yhden palvelun uudelleenkäynnistys (turvallista - muut istunnot jatkavat):

```bash
sudo systemctl restart cip-control-plane
```

Kaikkien uudelleenkäynnistys (lyhyt OPC UA -katko yhdistetyille PLC:ille):

```bash
sudo systemctl restart cip-control-plane cip-event-consumer cip-worker-pool cip-gateway
```

**Älä** pysäytä `cip-worker-pool`-palvelua ellet aio myös tappaa kaikkia käynnissä olevia simulaatioita. Työläiset ovat daemonin prosessiryhmän lapsia; `systemctl stop cip-worker-pool` lähettää SIGTERM:in daemonille, joka sitten sammuttaa jokaisen seuraamansa työläisen siististi (enintään `WORKER_STOP_GRACE` sekuntia per työläinen).

### Konfiguraation uudelleenlataus ilman täyttä uudelleenkäynnistystä

Ympäristötiedosto luetaan prosessin käynnistyessä. Live-latausta ei ole - **uudelleenkäynnistys vaaditaan**:

```bash
sudoedit /etc/cip-sim/env
sudo systemctl restart cip-control-plane cip-event-consumer cip-worker-pool cip-gateway
```

### Käynnistyksen yhteydessä

Asentaja ottaa kaiken käyttöön. Tarkista huoltotoimenpiteen jälkeen:

```bash
sudo systemctl is-enabled cip-control-plane cip-worker-pool cip-event-consumer cip-gateway cip-purge-events.timer
```

## 3. Lokit

Kaikki palvelut kirjaavat journaldiin **rivinvaihdolla eroteltuna JSON:na** (yksi objekti per rivi, kentät `ts`, `level`, `logger`, `msg` ja mahdolliset kutsujan lisäämät `extra`-kentät - tyypillisesti `session_id`, `session_name`, `port`, `pid`).

### Palvelun seuraaminen

```bash
sudo journalctl -u cip-control-plane -f
sudo journalctl -u cip-worker-pool -f
sudo journalctl -u cip-event-consumer -f
sudo journalctl -u cip-gateway -f
```

Kaikkien CIP-palveluiden seuraaminen yhdessä:

```bash
sudo journalctl -u 'cip-*' -f
```

### Historian tarkastelu

Viimeiset 200 riviä:

```bash
sudo journalctl -u cip-worker-pool -n 200
```

Tietystä ajasta alkaen (ISO 8601; palvelimen paikallinen aika ellei perään lisätä `Z`):

```bash
sudo journalctl -u cip-control-plane --since '2026-11-08 09:00' --until '2026-11-08 11:00'
```

Vain virheet:

```bash
sudo journalctl -u cip-control-plane -p err
```

### Rakenteelliset haut

Koska hyötykuorma on JSON:ia, putkita `jq`:lle kenttien suodattamiseksi. Näytä yhden istunnon tapahtumat:

```bash
sudo journalctl -u cip-worker-pool -o cat | jq -c 'select(.session_id == "9c1b...uuid...")'
```

Näytä jokainen tilasiirtymä tietokannasta (ohittaa lokit ja on usein nopeampaa):

```bash
sudo -u cip /opt/cip-sim/.venv/bin/python /opt/cip-sim/manage.py shell -c 'from control_plane.apps.sessions_mgr.models import SessionEvent; print("\n".join(f"{e.occurred_at.isoformat()} {e.event:10s} {e.detail}" for e in SessionEvent.objects.filter(session_id="<uuid>").order_by("occurred_at")))'
```

### Lokien säilytys

journald:n oletukset Ubuntu 22.04:llä ovat anteliaat (yleensä ~10 % `/var/log`:sta). Tarkista:

```bash
journalctl --disk-usage
grep -E '^(SystemMaxUse|MaxRetentionSec)' /etc/systemd/journald.conf
```

Jos tarvitsee käyttäjän olevan adm ryhmässä:
```bash
sudo usermod -aG adm $USER
```
jonka jälkeen kirjaudu ulos ja sisään.

Jos säilytystä pitää säätää, muokkaa `/etc/systemd/journald.conf` ja aja `sudo systemctl restart systemd-journald`.

## 4. Konfiguraatio

**Yksi totuuden lähde:** `/etc/cip-sim/env`. Jokainen palvelu lukee sen `EnvironmentFile=`-direktiivillä. Muutokset astuvat voimaan vasta kun asianomainen palvelu käynnistetään uudelleen.

### Nykyisen konfiguraation katselu

```bash
sudo cat /etc/cip-sim/env
```

### Arvon muuttaminen

```bash
sudoedit /etc/cip-sim/env
```

Muokkaa, tallenna, sitten:

```bash
sudo systemctl restart cip-control-plane cip-event-consumer cip-worker-pool cip-gateway
```

### Yleisiä muutoksia

**Mainostetun OPC UA -isäntänimen vaihto** (esim. laatikko sai uuden IP:n):

```bash
sudoedit /etc/cip-sim/env
```

Aseta `GATEWAY_PUBLIC_HOST=10.0.0.42:8080`. Sitten:

```bash
sudo sed -i 's/^Environment=ADVERTISE_HOST=.*/Environment=ADVERTISE_HOST=10.0.0.42/' /etc/systemd/system/cip-worker-pool.service.d/advertise.conf /etc/systemd/system/cip-gateway.service.d/advertise.conf
sudo systemctl daemon-reload
sudo systemctl restart cip-control-plane cip-worker-pool cip-gateway
```

**Joutoajan pidentäminen** (oletus: 30 minuuttia ilman OPC UA -asiakasaktiviteettia ennen kuin daemon pysäyttää istunnon):

```bash
sudoedit /etc/cip-sim/env
```

Aseta `WORKER_IDLE_TIMEOUT=3600`. Sitten:

```bash
sudo systemctl restart cip-worker-pool
```

**Django-salaisen avaimen vaihto** (mitätöi kaikki olemassa olevat CSRF-tokenit - harmitonta koska kirjautumista ei ole, mutta tee rauhallisena hetkenä):

```bash
NEW=$(/opt/cip-sim/.venv/bin/python -c 'import secrets; print(secrets.tokenurlsafe(64))')
sudo sed -i "s|^DJANGO_SECRET_KEY=.*|DJANGO_SECRET_KEY=${NEW}|" /etc/cip-sim/env
sudo systemctl restart cip-control-plane cip-event-consumer
```

**OPC UA -porttialtaan kasvatus** (oletus 5000–5100 = 101 porttia). Jos odotat yli 101 samanaikaista istuntoa:

```bash
sudoedit /etc/cip-sim/env
```

Aseta `OPCUA_PORT_START=5000` ja `OPCUA_PORT_END=6000`. Sitten:

```bash
sudo systemctl restart cip-worker-pool
```

Nykyiset istunnot pitävät varauksensa; uudet istunnot käyttävät laajempaa aluetta.

**Älä** muokkaa `/etc/cip-sim/env`-tiedostoa `cip`-käyttäjänä. Tiedosto on tila 0640 ryhmällä `cip` syystä - palvelut voivat lukea sitä, mutta vain root voi kirjoittaa. Jos `sudoedit` kieltäytyy käynnistämästä editoria, aseta `EDITOR` ensin: `sudo EDITOR=nano -e /etc/cip-sim/env`.

## 5. Rutiinihuolto

### Yöllinen tapahtumien siivous

`cip-purge-events.timer` ajaa `manage.py purge_events --days 30` kerran päivässä enintään tunnin satunnaisviiveellä. Tarkista että se on ajastettu ja milloin viimeksi ajettu:

```bash
systemctl list-timers cip-purge-events.timer
sudo journalctl -u cip-purge-events.service -n 5
```

Säilytysajan muuttaminen:

```bash
sudoedit /etc/systemd/system/cip-purge-events.service
```

Vaihda `--days 30` arvoon `--days 7`. Sitten:

```bash
sudo systemctl daemon-reload
```

### Sovelluspäivitysten asentaminen

Käytä toimitettua deploy-skriptiä. Se ajaa `git pull`, asentaa riippuvuudet uudelleen, migroi tietokannan, synkronoi katalogin uudelleen ja käynnistää jokaisen palvelun uudelleen. Se ei koske `/etc/cip-sim/env`-tiedostoon eikä tietokantaan.

```bash
cd /opt/cip-sim && sudo chmod +x deploy.sh && sudo ./deploy.sh
```

Jos uudelleenkäynnistys epäonnistuu, `deploy.sh` poistuu ei-nollalla ja tulostaa epäonnistuneen yksikön. Tarkista `journalctl -u <unit> -n 100`. Palautus:

```bash
sudo -u cip git -C /opt/cip-sim reset --hard <edellinen-sha>
sudo ./deploy.sh
```

### Rediksen kasvu

Redis-streamien pituus on rajattu (`STREAM_MAXLEN = 10 000` per stream, likimääräinen trim), joten se ei kasva rajatta. Vahvista nykyinen muistinkäyttö:

```bash
redis-cli INFO memory | grep -E 'used_memory_human|maxmemory_human'
```

Jos se näyttää joskus suurelta, `redis-cli CONFIG SET maxmemory-policy allkeys-lru` on turvallinen viimeinen keino - mitään pitkäikäistä ei ole tallennettu Redikseen.

## 6. Vianetsintä

Etene ylhäältä alas. Useimmat "se on rikki" -raportit ovat jokin kolmesta ensimmäisestä rivistä.

| Oire | Ensimmäinen tarkistus | Todennäköinen syy |
|---|---|---|
| Selainkäyttöliittymä on tyhjä / `502` | `systemctl status cip-control-plane`; `journalctl -u cip-control-plane -n 100` | Django kaatui; yleensä huono ympäristömuutos tai jumissa oleva migraatio |
| Käyttöliittymä latautuu mutta istunto jää `pending`-tilaan | `systemctl status cip-worker-pool`; `journalctl -u cip-worker-pool -n 100` | Daemon alhaalla, tai porttiallas täynnä |
| CODESYS ei näe päätepistettä | `ss -tlnp \| grep 8080`; `systemctl status cip-gateway`; `journalctl -u cip-gateway -n 100` | Gateway ei käynnissä, väärä mainostettu isäntänimi, tai UFW estää |
| Istunto näyttää `running` mutta kojelauta puuttuu | `curl -s http://127.0.0.1:<http_port>/health` | Työläisen HTTP-säie kuoli mutta OPC UA -silmukka on yhä elossa - reaper merkitsee istunnon epäonnistuneeksi |
| Istunto jumissa `stopping`-tilassa minuutteja | `ps -eo pid,ppid,cmd \| grep worker_pool.worker` | Työläinen jättää SIGTERM:in huomiotta; `WORKER_STOP_GRACE`-ajan jälkeen daemon lähettää SIGKILL:in |
| Kaikki näyttää hyvältä mutta mikään ei päivity reaaliajassa | `redis-cli ping` | Redis alhaalla; sekä event consumer että control plane palautuvat itsestään kun Redis palaa |
| `BadNodeIdExists` slugia uudelleenkäytettäessä | Tunnettu ongelma, korjataan P3:ssa | Kunnes P3 laskeutuu: käynnistä `cip-gateway` uudelleen edellisen istunnon pysäytyksen jälkeen |

### Istunto jumissa `pending`- tai `starting`-tilassa

Daemon hyväksyi käynnistyskomennon mutta työläinen ei koskaan noussut. Lue daemonin viimeaikainen loki:

```bash
sudo journalctl -u cip-worker-pool -o cat | jq -c 'select(.session_id=="<uuid>")' | tail -40
```

Etsi `worker spawned` (hyvä) tai `worker for ... exited during startup` (huono - yleensä puuttuva plugin tai porttikonflikti). Tarkista sitten Redis suoraan:

```bash
redis-cli EXISTS cip:hb:<uuid>
redis-cli HGETALL cip:ports
redis-cli XRANGE cip:evt - + COUNT 20
```

Jumissa olevan istunnon pakotettu siivous:

```bash
sudo -u cip /opt/cip-sim/.venv/bin/python /opt/cip-sim/manage.py shell -c 'from control_plane.apps.sessions_mgr.models import Session; s = Session.objects.get(id="<uuid>"); s.status = Session.STATUS_FAILED; s.save(update_fields=["status"]); print("forced to failed:", s)'
```

Sitten `sudo systemctl restart cip-event-consumer`, jotta muutos ilmoitetaan avoimille selaimille.

### Gateway ei käynnisty

```bash
sudo journalctl -u cip-gateway -n 100
```

Jos viesti on `Address already in use`, edellistä gateway-prosessia ei ole siivottu. Etsi ja tapa se:

```bash
sudo ss -tlnp 'sport = :8080'
sudo kill <pid>
```

Jos viesti on `PermissionError` polulla `/var/lib/cip-sim`, tarkista että tilahakemisto on `cip`-käyttäjän omistuksessa (`stat -c '%U %a' /var/lib/cip-sim` - odotettu `cip 750`).

### Porttiallas täynnä

`journalctl -u cip-worker-pool -p err` näyttää `PortPoolExhausted`. Tarkista mitä tällä hetkellä on varattu:

```bash
redis-cli HGETALL cip:ports
```

Vapauta varaukset joiden istuntoja ei enää ole:

```bash
sudo systemctl restart cip-worker-pool
```

`reconcile_on_startup()` pudottaa orvot varaukset automaattisesti. Nosta kattoa pysyvästi (kohta 4).

### Selainten reaaliaikapäivitykset lakkasivat toimimasta uudelleenkäynnistyksen jälkeen

Channels pitää yhteytensä Redikseen; jos Redis käynnistettiin uudelleen palveluiden ollessa käynnissä, kanavakerroksen pitäisi yhdistää automaattisesti. Jos ei:

```bash
sudo systemctl restart cip-control-plane cip-event-consumer
```

### Palomuuri

Vahvista mitä UFW:llä on auki:

```bash
sudo ufw status numbered
```

Lisää alue (esim. toinen sisäverkko):

```bash
sudo ufw allow from 10.0.1.0/24 to any port 8000 proto tcp
```

**Älä koskaan** avaa portteja 5000–5100 tai kojelaudan HTTP-portteja muualle kuin loopbackiin. Nuo portit isännöivät raakoja työläisten OPC UA -palvelimia ja istuntokohtaisia kojelautoja; ainoa laillinen asiakas on `cip-gateway` loopbackin kautta.

## 7. Hätätoimenpiteet

### "Pysäytä kaikki, nyt"

```bash
sudo systemctl stop cip-gateway cip-event-consumer cip-control-plane cip-worker-pool
```

Daemon sammuttaa jokaisen työläisen SIGTERM:llä. Kokonaisaika on rajattu `WORKER_STOP_GRACE`-aikaan (10 s per työläinen, rinnakkain).

### "Työläinen käyttäytyy huonosti ja minun täytyy tappaa se käsin"

Hae PID sydämenlyönnistä:

```bash
redis-cli GET cip:hb:<uuid> | jq .pid
```

Tapa se. Daemonin reaper huomaa 5 sekunnin kuluessa ja julkaisee `session_failed`-tapahtuman:

```bash
sudo kill -9 <pid>
```

### "Redis on täynnä / korruptoitunut"

Kaikki Rediksessä on uudelleenrakennettavissa. Tyhjennys on turvallista niin kauan kuin mitään istuntoja ei tarvitse säilyttää:

```bash
redis-cli FLUSHDB
sudo systemctl restart cip-worker-pool cip-control-plane cip-event-consumer cip-gateway
```

Kaikki daemonin alla yhä käynnissä olevat työläiset adoptoidaan `reconcile_on_startup()`-kutsulla, koska niiden sydämenlyönnit kirjoitetaan uudelleen `HEARTBEAT_INTERVAL`-ajan kuluessa.

## 8. Viite

### Portit

| Portti | Sitoo | Sidontarajapinta | Tavoitettavissa |
|---|---|---|---|
| `6379/tcp` | redis-server | `127.0.0.1` (Ubuntun oletus) | vain paikallinen |
| `8000/tcp` | cip-control-plane | `10.120.36.0` | sisäverkko (UFW) |
| `8080/tcp` | cip-gateway | `10.120.36.0` | sisäverkko (UFW) |
| `5000–5100/tcp` | työläiset | `127.0.0.1` | vain gateway (loopback) |
| `6000–6100/tcp` | työläisten HTTP-kojelauta | `127.0.0.1` | vain control plane (loopback) |

HTTP-kojelautaportit ovat `OPCUA_PORT + HTTP_PORT_OFFSET` (oletus offset 1000).

### Tiedostot ja hakemistot

```text
/opt/cip-sim/                     sovellushakemisto (omistaja: cip)
/opt/cip-sim/.venv/               virtualenv (omistaja: cip)
/opt/cip-sim/deploy/              systemd-yksiköt, palomuuriskripti (vain luku, root)
/opt/cip-sim/docs/                tämä käsikirja ja kumppanit
/opt/cip-sim/install.sh           alkuasentaja (aja kerran rootina)
/opt/cip-sim/deploy.sh            päivitysajuri (aja rootina)
/etc/cip-sim/env                  ajonaikainen konfiguraatio (root:cip 0640)
/etc/systemd/system/cip-*.service
/etc/systemd/system/cip-*.timer
/var/lib/cip-sim/db.sqlite3       SQLite-tietokanta (cip:cip 0640)
/var/lib/cip-sim/pids/            työläisten PID-tiedostot (neuvonanto)
/var/log/cip-sim/                 varattu; lokit menevät tällä hetkellä journaldiin
```

### Hallintakomennot jotka kannattaa tuntea

Katalogin synkronointi kun plugin pudotetaan `sim_plugins/`-hakemistoon:

```bash
sudo -u cip /opt/cip-sim/.venv/bin/python /opt/cip-sim/manage.py sync_catalog --settings=control_plane.settings.prod
```

Yli N päivää vanhojen istuntotapahtumien siivous (normaalisti ajastin hoitaa):

```bash
sudo -u cip /opt/cip-sim/.venv/bin/python /opt/cip-sim/manage.py purge_events --days 30 --settings=control_plane.settings.prod
```

Django-shellin avaus (tarkasta tai korjaa rivejä):

```bash
sudo -u cip /opt/cip-sim/.venv/bin/python /opt/cip-sim/manage.py shell --settings=control_plane.settings.prod
```

### `/etc/cip-sim/env`-muuttujat

| Muuttuja | Oletus | Merkitys |
|---|---|---|
| `DJANGO_SECRET_KEY` | - | **Pakollinen.** Istunnon/CSRF:n allekirjoitus. Vaihda jos vuotaa. |
| `DJANGO_DEBUG` | `0` | Tuotannossa pidettävä `0`:na. `1` vuotaa jäljitystiedot selaimeen. |
| `DJANGO_ALLOWED_HOSTS` | - | Pilkkuerotettu lista. Pyynnöt joiden `Host`-otsake ei ole listalla hylätään. |
| `DJANGO_HSTS` | `0` | Merkityksellinen vain TLS:n yli. Jätä `0`:ksi pelkän HTTP:n sisäverkossa. |
| `DATABASE_URL` | - | **Neljä vinoviivaa** absoluuttiselle SQLite-polulle: `sqlite:////var/lib/cip-sim/db.sqlite3`. |
| `REDIS_URL` | `redis://localhost:6379/0` | Pitää vastata asennettua Redistä. |
| `LOG_LEVEL` | `INFO` | `DEBUG` tuottaa erittäin puheliaat lokit. |
| `GATEWAY_PUBLIC_HOST` | - | `host:port` joka näytetään CODESYS/SCADA-asiakkaille. |
| `GATEWAY_OPCUA_PORT` | `8080` | Portti jonka gateway sitoo. |
| `OPCUA_PORT_START` / `_END` | `5000` / `5100` | Työläisten OPC UA -porttialue. Koko = samanaikaisten istuntojen enimmäismäärä. |
| `HTTP_PORT_OFFSET` | `1000` | Työläisen kojelauta = OPC UA -portti + tämä. |
| `WORKER_IDLE_TIMEOUT` | `1800` | Sekuntia ilman OPC UA -asiakasaktiviteettia ennen kuin istunto pysäytetään. |
| `WORKER_STOP_GRACE` | `10` | Sekuntia SIGTERM:in odottamiseen ennen SIGKILL:iä. |
| `HEARTBEAT_INTERVAL` | `10` | Työläisen sydämenlyöntien kirjoitustiheys. |
| `HEARTBEAT_TTL` | `30` | Sydämenlyöntiavaimen vanheneminen. Oltava > `HEARTBEAT_INTERVAL`. |

## 9. Luovutuksen tarkistuslista

Ennen kuin kutsut asennuksen "omistetuksi":

- [ ] `sudo systemctl status cip-*` - kaikki viisi yksikköä `active (running)`
- [ ] `curl -sS http://127.0.0.1:8000/health/` palauttaa `{"status": "ok"}`
- [ ] Selaa `http://<palvelin-ip>:8000/` työasemalta **sisäverkossa** - istuntolista renderöityy
- [ ] Luo istunto selaimesta - sen tila etenee `pending → starting → running` noin 5 sekunnissa
- [ ] Avaa sen tietosivu - kojelautalinkki toimii, päätepisteen URL on oikea
- [ ] CODESYS selaa `opc.tcp://<palvelin-ip>:8080/gateway/` ja näkee `Gateway/Sessions/<slug>/`
- [ ] Lue `Measurements.LevelPercent`; kirjoita `Commands.WaterValveCmd = 1`; näe tason nousevan
- [ ] Pysäytä istunto selaimesta; OPC UA -kansio katoaa
- [ ] `sudo ufw status verbose` - vain `22`, `8000`, `8080` ovat auki, kaikki sisäverkkoon rajattuja
- [ ] `sudo cat /etc/cip-sim/env` - `DJANGO_SECRET_KEY` **ei** ole paikkamerkkiteksti
- [ ] `sudo journalctl -u cip-control-plane -n 1` näyttää kelvollista JSON:ia, ei raakaa jäljitystä
- [ ] Varmuuskopiot: aja online-varmuuskopio kerran käsin, kirjaa polku
- [ ] Luovuta tämä tiedosto ja `/etc/cip-sim/env`:n sijainti ylläpitäjälle