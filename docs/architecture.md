# Arkkitehtuuriviite

**Kohdeyleisö:** kehittäjät, integraattorit ja ylläpitäjät, joiden täytyy ymmärtää miten alusta liittyy yhteen - ei vain mitä ajetaan.  
**Laajuus:** monikerroksinen simulaatiomoottori plugin-koodista OPC UA -päätepisteeseen jonka CODESYS selaa.

*Miten alustaa ylläpidetään*: katso [operations.md](operations.md). *Miten sitä käytetään selaimesta*: katso [quickstart.md](quickstart.md).

## 1. Yhden lauseen suunnittelu

> Jokainen käynnissä oleva simulaatio altistetaan itseään kuvaavana alipuuna polussa `Objects/Gateway/Sessions/<session_name>/` **yhdellä OPC UA -päätepisteellä**, ja alusta pitää tämän alipuun oikeana ja tavoitettavissa riippumatta siitä miten istunnot käynnistyvät, pysähtyvät, kaatuvat tai luodaan uudelleen.

Kaikki muu tässä dokumentissa on olemassa palvellakseen tuota lausetta.

## 2. Kerrokset ylhäältä alas

| Kerros | Komponentit | Vastuu |
|---|---|---|
| **Asiakaskerros** | CODESYS, SCADA/HMI, selain, `tools/opcua_cli.py` | Käyttää OPC UA -päätepistettä portissa 8080 tai Django-käyttöliittymää ja REST-rajapintaa portissa 8000. |
| **Palvelinkerros** | `gateway` ja `control_plane` | Gateway kokoaa kaikkien työläisten OPC UA -puut yhdeksi päätepisteeksi; Django tarjoaa käyttöliittymän, REST-rajapinnan, WebSocket-reaaliajan ja kojelautavälityksen. |
| **Viestiväylä** | Redis | Välittää komennot, tapahtumat, sydämenlyönnit ja porttivaraukset palvelinkerroksen ja ajonaikaisen kerroksen välillä. |
| **Ajonaikainen kerros** | `worker_pool.daemon` ja työläisprosessit | Daemon varaa portit, käynnistää ja valvoo työläisiä sekä sovittaa tilan uudelleenkäynnistysten jälkeen. Jokainen istunto saa oman työläisprosessin. |
| **Pluginkerros** | `sim_plugins/*/simulation.py` | Sisältää itse simulaatiologiikan: mittaukset, komennot, tilat ja fysiikan. |

### Viestiväylän avaimet

| Avain | Tarkoitus |
|---|---|
| `cip:cmd` | Komentovirta Djangosta daemonille. |
| `cip:evt` | Tapahtumavirta daemonilta ja työläisiltä muille komponenteille. |
| `cip:hb:<uuid>` | Työläisen sydämenlyönti ja elossaolon osoitus. |
| `cip:idle:<uuid>` | Viimeisin asiakasaktiviteetti, jota käytetään joutoajan valvontaan. |
| `cip:ports` | Porttivaraukset: portti → istunto. |

### Työläisprosessit

Jokainen käynnissä oleva istunto saa oman työläisprosessin. Työläinen tarjoaa:

- OPC UA -palvelimen loopback-portissa, oletusalueella 5000–5100.
- HTTP-kojelaudan loopback-portissa, joka on OPC UA -portti + `HTTP_PORT_OFFSET` (oletus 1000).
- Pluginin toteuttaman simulaatiologiikan.

**Pluginkerros** on ainoa osa, jonka toimialan asiantuntija kirjoittaa. Kaikki sen yläpuolella on infrastruktuuria, jota plugin ei koskaan näe.

## 3. Plugin-sopimus

Simulaatio on `sim_runtime.base.BaseSimulation`-luokan aliluokka, jonka `simulation.py` on `sim_plugins/`-hakemiston alla olevassa paketissa. **Löytäminen tapahtuu konvention mukaan** - pudota hakemisto paikalleen, aja `manage.py sync_catalog`, ja se näkyy käyttöliittymässä. Rekisteritiedostoa ei muokata.

Sopimus on tarkoituksella pieni:

```python
class BaseSimulation(ABC):
    SIMULATION_ID: ClassVar[str]          # esim. "cip"
    SIMULATION_NAME: ClassVar[str]        # esim. "Clean-In-Place"
    SIMULATION_VERSION: ClassVar[str]     # esim. "2.0.0"

    @classmethod
    def commands(cls)     -> dict[str, CommandSpec]: ...
    @classmethod
    def measurements(cls) -> dict[str, MeasurementSpec]: ...
    @classmethod
    def status(cls)       -> dict[str, StatusSpec]: ...   # valinnainen

    def __init__(self, config: SimulationConfig) -> None: ...
    def step(self, dt: float) -> None: ...
    def snapshot(self) -> dict[str, Any]: ...              # {"measurements": {...}, "status": {...}}
    def write_command(self, name: str, value: Any) -> None: ...
    def on_client_activity(self) -> None: ...              # valinnainen
```

### Miksi skeema on luokkatasolla

OPC UA -osoiteavaruus rakennetaan ennen kuin yhtään instanssia on olemassa, `commands()` / `measurements()` / `status()`-metodeista. Tämä mahdollistaa:

- gatewayn löytää työläisen muodon purkamatta elävää oliota;
- katalogin synkronoinnin (`sync_catalog`) luetella pluginin ilman niiden konstruointia;
- CODESYS Data Sources -kohteiden upottaa muodon generoituihin struct-kenttinimiin - muodosta tulee osa PLC-projektia, joten se ei saa riippua instanssin tilasta.

### Miksi `step()` ja `snapshot()` ovat erillisiä

`step(dt)` edistää fysiikkaa taajuudella `PHYSICS_HZ` (10 Hz). `snapshot()` lukee tilaa. Niitä kutsutaan eri säikeistä: `step` ajetaan työläisen asyncio-silmukassa; `snapshot` ajetaan myös HTTP-kojelautasäikeessä. Pluginit joiden tila muuttuu täytyy lukita. `CIPSimulation` käyttää `threading.Lock`:ia; `Blinker` luottaa harvojen attribuuttiensa atomisuuteen.

`write_command()` saatetaan kutsua OPC UA -setterin takaisinkutsusta (myös asyncio-säikeessä mutta ei `step`-kutsun sisällä), joten sen täytyy ottaa sama lukko.

### Pulssikomentojen konventio

`CommandSpec(pulse=True)` tarkoittaa että arvo nollautuu itsestään yhden tikin jälkeen. Adapteri hoitaa nollauksen: se hyväksyy kirjoituksen, lähettää sen `write_command`-metodille ja kirjoittaa seuraavassa `_push_values()`-kutsussa oletusarvon takaisin. Tämä antaa CODESYS:in laukaista kertaluontoisen resetin ilman alustan kanssa koordinointia.

## 4. Ajonaikainen moottori

Jokainen työläisprosessi on `worker_pool.worker.main`. Se:

- Lataa pluginin `SimulationRegistry`-rekisteristä.
- Rakentaa OPC UA -osoiteavaruuden skeemasta `sim_runtime.opcua_adapter.OPCUAAdapter`-luokan kautta.
- Käynnistää HTTP-kojelaudan osoitteessa `127.0.0.1:(opcua_port + HTTP_PORT_OFFSET)` taustasäikeessä.
- Kirjoittaa sydämenlyöntejä avaimeen `cip:hb:<uuid>` joka `HEARTBEAT_INTERVAL` sekunti vanhenemisajalla `HEARTBEAT_TTL`.
- Julkaisee `SessionReadyEvent`-tapahtuman adapterin `on_ready`-takaisinkutsusta - eli sen jälkeen kun OPC UA -portti on tosiasiallisesti sidottu. Tämä välttää kilpailun jossa peili yrittää yhdistää socketiin jota ei vielä kuunnella.
- Ajaa OPC UA -tikkisilmukkaa (`adapter.run(stop)`) SIGTERM:iin asti.
- Siivoaa sydämenlyönti- ja joutoaikaisavaimet sekä PID-tiedoston.

### Sidonta vs. mainostus - hienovarainen yksityiskohta

`asyncua` yhdistää URL:n johon palvelin sitoutuu URL:ään jonka se mainostaa. Työläiset ovat sisäisiä - gateway on niiden ainoa asiakas ja yhdistää loopbackin kautta - joten `bind_host` on oletuksena `127.0.0.1`. Jos asiakkaan täytyy tavoittaa työläinen suoraan, `--advertise-host` ja `--bind-host` eroavat, ja adapteri kirjoittaa mainostetut URL:t uudelleen `Server.endpoints`-listaan `init()`-kutsun jälkeen, jotta löytövastaukset kantavat oikean isäntänimen. Katso `OPCUAAdapter._rewrite_advertised_urls`.

### Pulssikomennot ja arvon asettajat

OPC UA -komennon asettaja on synkroninen asyncua-sopimuksen mukaan. Se kutsuu `write_command`-metodia ja - jos arvo hyväksytään - tallentaa hyväksytyn `DataValue`-arvon kohtaan `node_data.attributes[attribute].value`. Tämä sijoitus on commit-piste: asyncua välittää sen tilaajille. Jos `write_command` heittää `ValueError`-poikkeuksen, adapteri heittää `ServiceError(BadOutOfRange)`-poikkeuksen ja asiakas näkee asianmukaisen tilakoodin.

### Joutoajan havainnointi

Adapteri tilaa asyncua:n `PostRead`- ja `PostWrite`-palvelintakaisinkutsut. Jokainen ulkoinen asiakasluku tai -kirjoitus päivittää `_last_external_activity`-kentän ja kasvattaa `_external_request_count`-laskuria. Sydämenlyöntisilmukka lukee laskurin, ja kun se muuttuu, päivittää avaimen `cip:idle:<uuid>` nykyisellä seinäkellon ajalla. Daemonin joutoaikavahti vertaa aikaleimaa `WORKER_IDLE_TIMEOUT`-asetukseen; istunto jolla ei ole asiakkaita koko joutoaikana pysäytetään syyllä `reason="idle_timeout"`.

Tämä tekee alustasta turvallisen sisäverkossa jossa opiskelijat unohtavat sulkea selainvälilehtensä - istunto päättyy itsestään sen sijaan että pitäisi portin varaamattomana loputtomiin.

## 5. Työläisaltaan daemon

`worker_pool.daemon` on pitkäikäinen palvelu joka:

- omistaa porttien varaajan (`PortAllocator`, Redis-hash `HSETNX`-pohjaisella atomaarisella varauksella ja ehdollisella `HDEL`-Lua-skriptillä vapautukselle);
- omistaa työläishallinnan (`WorkerPoolManager`, `SpawnedWorker`-tietueiden sanakirja avaimena `session_id`);
- ajaa Redis-siltaa (`RedisBridge`, `XREADGROUP` avaimella `cip:cmd` ja `XAUTOCLAIM`-palautuksella hylätyille viesteille);
- ajaa reaperia 5 sekunnin välein - jokainen työläinen jonka PID ei enää vastaa signaaliin 0 siivotaan ja `SessionFailedEvent` julkaistaan;
- ajaa joutoaikavahtia 60 sekunnin välein - kuvattu yllä;
- sovittaa käynnistyksen yhteydessä `cip:hb:*`-avaimiin, jotta daemonin uudelleenkäynnistyksen yli selvinneet työläiset adoptoidaan ja vanhentuneet porttivaraukset pudotetaan.

### Miksi Redis Streams, ei pub/sub

Daemonin uudelleenkäynnistys ei saa hukata `start_session`-komentoa joka julkaistiin sen ollessa alhaalla. Streamit pitävät kuittaamattomat viestit kuluttajaryhmän odotuslistalla ja toimittavat ne uudelleen. Pub/sub pudottaisi ne hiljaa. Vähintään kerran -toimitus tarkoittaa että komentokäsittelijöiden täytyy olla idempotentteja: `start_worker` jo seuratulle istunnolle heittää `AlreadyRunningError`-poikkeuksen ja silta kirjaa ja jättää huomiotta; `stop_worker` seuraamattomalle istunnolle palauttaa `False` eikä `SessionStoppedEvent`-tapahtumaa julkaista.

### Miksi `session_id` (UUID) ja `session_name` (slug) ovat erillisiä

`session_id` on identiteetti. Se avaintaa tietokantarivin, Rediksen sydämenlyönnin, porttivarauksen, hallinnan sanakirjan ja peilin sanakirjan.

`session_name` on näyttönimi - slug jos asetettu, muuten UUID-merkkijono. Se on se mikä näkyy OPC UA -kansion polussa.

Kaksi eroaa täsmälleen silloin kun slug käytetään uudelleen. Istunnon luominen jo käytetyllä slugilla pysäyttää edellisen istunnon ja ottaa nimen haltuunsa. CODESYS:n näkökulmasta mikään ei muuttunut - kansion polku on vakaa - mutta alustan sisällä kaksi ovat erillisiä entiteettejä ja niiden tapahtumavirrat eivät koskaan kohtaa.

## 6. OPC UA -gateway

`gateway.main` ajaa yhtä asyncua-`Server`-oliota portissa `:8080`. Se omistaa aggregoidun osoiteavaruuden. Jokainen aktiivinen istunto on yksi `WorkerMirror` polun `Objects/Gateway/Sessions/<session_name>/` alla.

`WorkerMirror`:

- Yhdistää työläiseen loopbackin kautta (`opc.tcp://127.0.0.1:<port>/<sim_id>/`).
- Löytää simulaation juuren käymällä `Objects`-haaran läpi ja etsimällä solmun jolla on `SimulationId`-ominaisuus. Lukee `NamespaceUri` samasta solmusta. Ei kovakoodattua karttaa - uudet pluginit näkyvät gatewayn puussa ilman muutoksia gatewayhin.
- Rakentaa peilatun alipuun: jokaiselle `Commands`-, `Measurements`-, `Status`-kansiolle lisää kansion ja yhden muuttujan per solmu, pakottaen työläisen arvon gatewayn omaan nimiavaruuteen.
- Tilaa jokaisen peilatun työläissolmun `create_subscription(500, handler)`-kutsulla. `datachange_notification` työntää uuden arvon vastaavaan gateway-solmuun.
- Komentosolmuille asentaa arvon asettajan joka asettaa `(session_id, command_name, value)`-tuplen gatewayn lähtevään kirjoitusjonoon. Erillinen `_drain_writes`-tehtävä purkaa jonon ja kutsuu `mirror.forward_command`-metodia, joka kirjoittaa takaisin työläiselle.

### Peilin elinkaari

Peilit lisätään kahdella tavalla ja poistetaan yhdellä:

- **Gatewayn käynnistyessä** - `reconcile_existing()` skannaa `cip:hb:*`-avaimet ja peilaa jokaisen elävän työläisen. Tämä on totuuden lähde sille "mitkä työläiset ovat elossa juuri nyt", eikä se riipu `SessionReadyEvent`-tapahtuman näkemisestä.
- **Ajonaikana** - `run_event_loop()` kuluttaa `cip:evt`-virtaa kuluttajaryhmän kautta (id="0" jotta ennen gatewayn käynnistymistä saapuneet tapahtumat eivät jää väliin). `SessionReadyEvent`-tapahtumasta se kutsuu `_add_mirror`-metodia; `SessionStoppedEvent`- tai `SessionFailedEvent`-tapahtumasta `_remove_mirror`-metodia.

Molemmat polut yhtyvät samoihin `_add_mirror`- ja `_remove_mirror`-metodeihin, joten kaksi sisääntuloa käyttäytyvät identtisesti.

`_add_mirror` yrittää uudelleen viiveellä (5 yritystä, `min(0.3 * attempt, 1.5)` s välissä), koska gatewayn joka sovittaa sydämenlyönnin sekunnin murto-osan ennen kuin työläinen lopettaa sidonnan, ei pidä luovuttaa pysyvästi. Jokaista yritystä varten konstruoidaan tuore `WorkerMirror` - epäonnistunut yhteys voi jättää puoliavoimen asyncua-asiakkaan joka epäonnistuu oudosti uudelleenkäytössä.

### Slug-uudelleenkäytön tunnettu ongelma

`WorkerMirror.disconnect()` tällä hetkellä purkaa tilauksen ja asiakkaan mutta ei poista istuntokansion solmua OPC UA -puusta. Uusi istunto joka ottaa saman slugin haltuunsa osuu siksi `BadNodeIdExists`-virheeseen yrittäessään `add_folder`-kutsua. Tämä on aikataulutettu korjattavaksi P3:ssa (poista kansio rekursiivisesti disconnect-metodissa).

Kunnes P3 laskeutuu, `cip-gateway`:n uudelleenkäynnistys istunnon pysäytyksen jälkeen on kiertotie.

## 7. Control plane

`control_plane` on Django 5 -sovellus jota Uvicorn ajaa `control_plane.asgi`-kautta. Se käsittelee HTTP:n ja WebSocketin saman ASGI-sovelluksen kautta. Kolme vastuuta:

### 7.1 Istuntorajapinta

`control_plane/apps/sessions_mgr/views.py` tarjoaa:

| Metodi | Polku | Tunnistautuminen | Mitä tekee |
|---|---|---|---|
| `GET` | `/api/simulations/` | ei | Listaa aktiiviset katalogirivit |
| `GET` | `/api/sessions/` | ei | Listaa ei-päättyneet istunnot (yhteenveto - ei tokenia) |
| `POST` | `/api/sessions/` | ei | Luo istunnon; palauttaa tokenin kerran |
| `GET` | `/api/sessions/<token>/` | kyvykkyystoken | Istunnon täydet tiedot |
| `DELETE` | `/api/sessions/<token>/` | kyvykkyystoken | Pysäyttää istunnon |
| `GET` | `/api/sessions/<token>/events/` | kyvykkyystoken | Viimeiset 100 tapahtumariviä |

Kyvykkyystoken generoidaan luontihetkellä (`secrets.token_urlsafe(32)`, 256 bittiä), tallennetaan malliin, ja on ainoa tunnistautuminen jota tarvitaan istunnon näkemiseen tai pysäyttämiseen. Kuka tahansa URL:n haltija voi pysäyttää istunnon. Tämä on tahallista: ei kirjautumista, ei käyttäjiä, ei salasanan nollausvirtaa - vain URL joka käyttäytyy kuin bearer-token. Listapäätepiste ei koskaan sisällä sitä; se on pakotettu käyttämällä `SessionSummarySerializer`-serialisoijaa siellä ja `SessionDetailSerializer`-serialisoijaa kaikkialla muualla.

### 7.2 Selainkäyttöliittymä ja kojelaudan välityspalvelin

`control_plane/apps/dashboard/views.py` renderöi:

- `/` - istuntolista (reaaliaikaisesti WebSocketin kautta)
- `/sessions/rows/` - taulukon fragmentti (haetaan uudelleen jokaisesta muutoksesta)
- `/sessions/new/` - luontilomake
- `/sessions/<token>/` - tietosivu pysäytyspainikkeella
- `/s/<name>/...` - kojelautavälitys työläisen loopback-HTTP-palvelimelle

Välitys on ainoa polku selaimesta työläisen kojelautaan. Työläiset sitovat HTTP-palvelimensa `127.0.0.1`:een, joten selain ei tavoita niitä suoraan. Django hakee selaimen puolesta ja välittää vastauksen 404/409/502-semantiikalla:

- `404` - istuntoa ei löydy
- `409` - istunto on olemassa mutta ei ole `running`, tai sillä ei ole vielä `http_port`-arvoa
- `502` - työläisen HTTP ei ole tavoitettavissa 2 sekunnissa

Välitys on async-näkymä. ASGI:n alla Django ajaa synkroniset näkymät yhdellä jaetulla säikeellä, ja jokainen avoin kojelauta kysyy työläistään 4 kertaa sekunnissa. Yksi jumissa oleva työläinen jumittaisi koko käyttöliittymän timeoutin ajaksi jos välitys olisi synkroninen. `asyncio.to_thread(_fetch, ...)` pitää tapahtumasilmukan reagoivana muille selaimille.

`sim_runtime/`-hakemiston `shell.html` hakee tilansa `fetch('api/state')`-kutsulla - suhteellisella, ei absoluuttisella - joten sama HTML toimii sekä suoraan käytettynä (kehityksessä, kun työläisen loopback on tavoitettavissa) että välitettynä polun `/s/<slug>/` alla.

### 7.3 Tapahtumakuluttaja ja WebSocket-hajautus

`control_plane/apps/sessions_mgr/management/commands/run_event_consumer.py` on pitkäikäinen prosessi. Se:

- Sovittaa käynnistyksen yhteydessä - jokainen ei-päättynyt istunto jonka sydämenlyöntiavain `cip:hb:<uuid>` puuttuu ja jonka `created_at` on vanhempi kuin `HEARTBEAT_TTL`, suljetaan: `stopping` → `stopped`, kaikki muu → `failed`. Tämä on mekanismi joka siivoaa "kummitusrivit" jotka kaatuminen tai `stack.sh reset` jättää jälkeen.
- Kuluttaa `cip:evt`-virtaa kuluttajaryhmän kautta ja soveltaa jokaisen tapahtuman tietokantaan (`SessionStartedEvent` → `running`, `SessionReadyEvent` → `http_port` asetettu jne.).
- Ilmoittaa jokaisen muutoksen `sessions`-kanavaryhmään `group_send`-kutsulla.
- `XACK`:aa jokaisen viestin.

`control_plane/apps/dashboard/consumers.py` (`SessionsConsumer`) liittyy `sessions`-ryhmään ja välittää jokaisen viestin selaimelle JSON:na. Lista ja tietosivu päättävät itse mitä piirtävät uudelleen - lista hakee uudelleen `/sessions/rows/`, tietosivu hakee itsensä uudelleen. Molemmat yhdistävät uudelleen eksponentiaalisella viiveellä (1 s → 15 s katto) ja uudelleenyhdistämisen yhteydessä käsittelevät yhteyden "piirrä kaikki uudelleen" -signaalina, koska offline-tilassa tehdyt muutokset jäivät väliin.

Token ei koskaan ylitä WebSocketia. Ryhmä lähetetään jokaiselle avoimelle listasivulle, joten `_summary(session)` jättää sen tarkoituksella pois.

## 8. Tietomalli

Kolme taulua tiedostossa `control_plane/apps/sessions_mgr/models.py`:

- `simulation_catalog` - yksi rivi per levyllä löydetty plugin. Päivitetään `sync_catalog`-komennolla. `is_active=False` piilottaa pluginin luontilomakkeesta poistamatta sen historiaa.
- `sessions` - yksi rivi per istunto.
- `session_events` - vain lisättävä elinkaarisiirtymien loki. Tapahtumakuluttaja kirjoittaa, `purge_events` trimmaa (30 päivän oletus), käyttöliittymä näyttää istunnon historian. Ei koskaan muuteta.

### `sessions`-taulun kentät

| Kenttä | Tarkoitus |
|---|---|
| `id` (UUID, PK) | Identiteetti kaikkialla - Redis-avaimet, hallinnan sanakirja, peilin sanakirja, tapahtumareititys. |
| `token` | Kyvykkyystoken. Palautetaan kerran luonnissa; vaaditaan tieto- ja pysäytyspäätepisteissä. |
| `simulation_id`, `simulation_version` | Katalogin tilannekuva luontihetkellä. |
| `slug` | Ihmisluettava nimi joka näkyy OPC UA -kansion polussa. Yksilöllinen kun ei-null. |
| `label` | Vapaa tekstihuomautus. Ei koskaan poistu käyttöliittymästä. |
| `status` | Tilakone (`pending → starting → running → stopping → stopped`; mikä tahansa tila → `failed`). |
| `port`, `http_port`, `pid`, `advertised_endpoint` | Ajonaikaisten tilojen peilejä siitä mitä Redis/prosessit todella tietävät. Tapahtumakuluttaja päivittää. |
| `created_at`, `started_at`, `stopped_at` | Aikajana. |

Tietokanta on historia; Redis on totuus siitä mikä on käynnissä nyt. Kaksi sovitetaan tapahtumakuluttajan käynnistyessä, jotta tietokanta ei näytä istuntoa elävänä kun sen työläinen on poissa.

## 9. Pyyntöjen elinkaari: täysi kierros

Nähdäksesi miten kerrokset toimivat yhdessä, seuraa yhtä istuntoa klikkauksesta ensimmäiseen lukuun:

1. **Luonti.** Selain POSTaa `/api/sessions/`-päätepisteeseen `{simulation_id, slug, label}`.
2. **Django.** `views._create_session` validoi, pysäyttää mahdollisen aiemman samaa slugia käyttävän istunnon (`_reclaim_slug`), luo `Session`-rivin, kirjaa tapahtuman ja julkaisee `StartSessionCommand`-komennon avaimeen `cip:cmd` `orchestrator.request_start`-kutsulla. Se palauttaa `SessionDetailSerializer`-serialisoinnin - token mukaan lukien.
3. **Daemon.** `RedisBridge._handle_start` kutsuu `manager.start_worker`, joka vuokraa portin `PortAllocator`-luokalta, haarauttaa `worker_pool.worker.main`-prosessin `session_id`-, `session_name`-, `simulation_id`-, portti- ja advertise host -argumenteilla ja odottaa 1 s varmistaakseen ettei lapsi kaatunut heti. Manager julkaisee `SessionStartedEvent`-tapahtuman avaimeen `cip:evt`.
4. **Tapahtumakuluttaja.** Lukee `SessionStartedEvent`-tapahtuman, päivittää rivin `running`-tilaan, kirjaa tapahtuman, ilmoittaa muutoksesta. Selaimen listarivi päivittyy. Istunto ei ole vielä tavoitettavissa OPC UA:n kautta - vain daemon on vahvistanut prosessin olemassaolon.
5. **Työläinen.** Rakentaa OPC UA -puun, astuu `async with server:`-kontekstiin (socket nyt sidottu), laukaisee `on_ready`-takaisinkutsun joka julkaisee `SessionReadyEvent`-tapahtuman port- ja `http_port`-kentillä. Sitten käynnistää sydämenlyöntisilmukan ja tikkisilmukan.
6. **Tapahtumakuluttaja, uudelleen.** Lukee `SessionReadyEvent`-tapahtuman, päivittää `http_port`-arvon, kirjaa tapahtuman, ilmoittaa muutoksesta. Tietosivun "Avaa live-kojelaudan" -linkki tulee käytettäväksi. Gateway lukee myös tämän tapahtuman ja konstruoi `WorkerMirror`-olion.
7. **Gateway, peilin rakennus.** `_discover_simulation_root` käy työläisen `Objects`-haaran läpi ja löytää solmun jolla on `SimulationId`-ominaisuus. Lukee `NamespaceUri`-arvon, tilaa jokaisen peilatun solmun ja asentaa komentojen asettajat. Kansio `Gateway/Sessions/<slug>` on nyt CODESYS:ille olemassa.
8. **CODESYS selaa.** Lukee `Measurements.LevelPercent`-arvon - asyncua kutsuu gatewayn tilausten välimuistia, kierrosta työläiselle ei tarvita ellei tilaus ole kylmä. Arvo palaa.
9. **CODESYS kirjoittaa** `Commands.WaterValveCmd = 1`. Gatewayn asettaja laukeaa synkronisesti: `_enqueue_write(session_id, "WaterValveCmd", True)`. `_drain_writes`-tehtävä purkaa jonon, kutsuu `mirror.forward_command`-metodia, joka pakottaa arvon ja kirjoittaa sen työläisen `Commands.WaterValveCmd`-solmuun. Työläisen `OPCUAAdapter._make_command_setter` laukeaa, validoi, kutsuu pluginin `write_command`-metodia. Plugin päivittää tilansa ja palaa. Adapteri hyväksyy kirjoituksen; asyncua välittää sen tilaajille. Muutos tulee näkyväksi gatewayn peilatussa solmussa seuraavalla tilaussyklillä (≤ 500 ms).
10. **Pysäytys.** Selain DELETE:aa `/api/sessions/<token>/`. Django asettaa rivin `stopping`-tilaan, kirjaa tapahtuman, julkaisee `StopSessionCommand`-komennon. Daemon kutsuu `manager.stop_worker`-metodia, lähettää SIGTERM:in, odottaa poistumista (enintään `WORKER_STOP_GRACE`), vapauttaa portin, julkaisee `SessionStoppedEvent`-tapahtuman. Tapahtumakuluttaja päivittää rivin `stopped`-tilaan, kirjaa tapahtuman, ilmoittaa muutoksesta. Gateway lukee tapahtuman, kutsuu `_remove_mirror`-metodia, katkaisee tilauksen ja asiakkaan. Kansio katoaa puusta. (Slug-uudelleenkäyttö vaatii tällä hetkellä gatewayn uudelleenkäynnistyksen - katso kohta 6.)

## 10. Vikatilat ja palautuminen

Jokainen komponentti on suunniteltu palautumaan ilman operaattorin väliintuloa yleisimmistä vikatiloistaan.

| Vikatila | Palautuminen |
|---|---|
| Control plane kaatuu | systemd käynnistää sen uudelleen 5 sekunnissa. WebSocket-asiakkaat yhdistävät uudelleen viiveellä. Ei tiedonmenetystä. |
| Tapahtumakuluttaja kaatuu | systemd käynnistää sen uudelleen. Sen kuluttajaryhmä säilyttää kuittaamattomat viestit; `XAUTOCLAIM` palauttaa ne. Käynnistyksen sovitus sulkee kummitusrivit. |
| Gateway kaatuu | systemd käynnistää sen uudelleen. `reconcile_existing()` skannaa `cip:hb:*`-avaimet ja rakentaa jokaisen peilin uudelleen. CODESYS yhdistää uudelleen samaan päätepisteeseen ja näkee samat kansiot. |
| Työläinen kaatuu | Reaper huomaa 5 sekunnissa, vapauttaa portin, julkaisee `SessionFailedEvent`-tapahtuman. Tietokantarivi menee `failed`-tilaan. |
| Daemon kaatuu | systemd käynnistää sen uudelleen. `reconcile_on_startup()` adoptoi työläiset jotka selvisivät (ne ovat omassa prosessiryhmässään, joten ne selviävät) sydämenlyönnin ja PID-tarkistuksen kautta. Vanhentuneet porttivaraukset pudotetaan. |
| Redis kaatuu | systemd käynnistää sen uudelleen. Kaikki mikä oli kesken streamissä menetetään, mutta mitään Rediksessä ei ole uudelleenrakentamatonta. Jos työläinen on yhä käynnissä, sen seuraava sydämenlyönti rekisteröi sen uudelleen; daemon adoptoi sen seuraavalla sovituskierroksellaan. |
| Koko laatikko käynnistyy uudelleen | systemd käynnistää `redis-server`:in ja sitten CIP-palvelut (`After=` / `Requires=` pakottaa järjestyksen). Olemassa olevat istuntorivit sovitetaan `failed`-tilaan koska niiden sydämenlyönnit ovat poissa. Istunnot eivät selviä uudelleenkäynnistyksestä - suunnittelun mukaan. |
| Mainostettu isäntänimi muuttuu (laatikko sai uuden IP:n) | Muokkaa `GATEWAY_PUBLIC_HOST`-asetusta ja palvelukohtaisia `ADVERTISE_HOST`-ohituksia (katso operations §4). Käynnistä uudelleen. Olemassa olevat istunnot pitävät vanhan päätepisteensä kunnes ne pysäytetään. |

## 11. Turvamalli

Alusta on suunniteltu luotettuun sisäverkkoon, ja jokainen alla oleva suunnittelupäätös heijastaa tätä rajoitetta tarkoituksella.

- **Ei tunnistautumista REST-rajapinnassa.** URL:n kyvykkyystoken on ainoa tunnistautuminen. Tämä tarkoittaa että URL:t ovat salaisuuksia - älä liitä istunnon tietosivulinkkejä chattiin tai sähköpostiin.
- **Ei TLS:ää.** Käyttöönotot odottavat olevansa sisäverkossa jossa siirtotie on luotettu. Jos alusta pitää joskus altistaa sisäverkon ulkopuolelle, se on projektitason päätös, ei konfiguraatiovipu.
- **OPC UA on anonyymi.** `SecurityPolicyType.NoSecurity` ja `AnonymousIdentityToken`, mikä vastaa alan normaalia sisäisissä simulaatioympäristöissä. `OPCUA_AUTH_MODE=per_session` on P3-toimitettava - putkitus on suunniteltu, ei vielä yhdistetty.
- **Palomuuri on ensisijainen kontrolli.** Asentajan `setup-ufw.sh` rajaa portit 22, 8000, 8080 sisäverkon CIDR:iin eikä mitään muuta. Työläisten OPC UA- ja kojelautaportit sitovat vain loopbackiin eikä niitä koskaan avata.
- **systemd-kovennus** (`NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`, `ReadWritePaths=/var/lib/cip-sim /var/log/cip-sim`) tarkoittaa että vaarantunut palvelu ei voi kirjoittaa kahta hakemistoaan ulkopuolelle tai nostaa oikeuksiaan.
- **Konfiguraatio on root:cip 0640.** Palvelut voivat lukea sen; vain root voi kirjoittaa. Django-salainen avain generoidaan asennuksen aikana.

## 12. Laajennuskohdat

Alusta on suunniteltu laajennettavaksi kolmella tietyllä tavalla ilman ydin­koodin muuttamista:

- **Lisää simulaatioplugin.** Pudota paketti `sim_plugins/`-hakemistoon `simulation.py`-tiedostolla (aliluokka `BaseSimulation`), valinnaisilla `body.html`- ja `ui.js`-tiedostoilla, sitten `manage.py sync_catalog`. Se näkyy käyttöliittymässä, on tavoitettavissa gatewayn kautta ja näkyy CODESYS:issä ilman muutoksia gatewayhin tai control planeen.
- **Lisää uusi asiakasrajapinta-protokolla.** Gateway omistaa aggregoidun osoiteavaruuden. Toinen gateway (esim. REST-julkisivu) voisi käyttää uudelleen `WorkerMirror`-luokan löytölogiikkaa - siksi löytö on erillinen metodi eikä leivottu konstruointiin.
- **Lisää toinen kone.** Daemon, gateway ja control plane ovat erillisiä prosesseja jotka kommunikoivat vain Rediksen kautta. Niiden ajaminen kolmella laatikolla vaatii vain kaikkien kolmen osoittamisen samaan `REDIS_URL`-osoitteeseen ja `GATEWAY_PUBLIC_HOST`-asetuksen oikean konfiguroinnin. Tätä ei testattu N>100:lla (se on P4), mutta kerrostus on olemassa rakenteellisesti.

Mitä alusta ei yritä tukea, ja mihin sitä ei pitäisi laajentaa ilman harkittua suunnittelukatsausta:

- Monen vuokralaisen eristäminen (token-malli olettaa yhden luottoalueen käyttöönoton)
- TLS-päättäminen sovelluksen sisällä (käytä käänteistä välityspalvelinta)
- Pluginien kuuma uudelleenlataus (käynnistä asianomainen työläinen tai daemon uudelleen)
- Pysyvä istuntohistoria uudelleenkäynnistysten yli (tietokanta tallentaa mitä tapahtui; se ei herätä istuntoja henkiin)

## 13. Mistä koodista etsiä

| Kerros | Aloituspiste |
|---|---|
| Plugin-sopimus | `sim_runtime/base.py` |
| Pluginien löytäminen | `sim_runtime/registry.py` |
| OPC UA -adapteri | `sim_runtime/opcua_adapter.py` |
| HTTP-kojelauta | `sim_runtime/http_adapter.py` |
| Työläisprosessi | `worker_pool/worker/main.py` |
| Työläisaltaan daemon | `worker_pool/daemon.py` |
| Työläishallinta | `worker_pool/manager.py` |
| Porttien varaaja | `worker_pool/port_allocator.py` |
| Redis-silta (komennot) | `worker_pool/redis_bridge.py` |
| OPC UA -gateway | `gateway/main.py`, `gateway/server.py`, `gateway/mirror.py` |
| Control plane -rajapinta | `control_plane/apps/sessions_mgr/views.py` |
| Control plane -UI | `control_plane/apps/dashboard/views.py` |
| Tapahtumakuluttaja | `control_plane/apps/sessions_mgr/management/commands/run_event_consumer.py` |
| WebSocket-kuluttaja | `control_plane/apps/dashboard/consumers.py` |
| Siirtoskeemat | `shared/schemas.py` |
| Läpileikkaavat vakiot | `shared/constants.py` |
| Rakenteellinen lokitus | `shared/logging.py` |

Jokaisen moduulin ylätason docstring ilmoittaa sen vastuun yhdessä kappaleessa. Jos tämän dokumentin suunnittelu ja koodi joskus eroavat, koodi voittaa - ja tämä dokumentti pitää päivittää vastaamaan sitä.