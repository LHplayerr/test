# Pikaopas - CIP-simulaatioalusta

Tämä opas on tarkoitettu **opiskelijoille ja käyttäjille**, jotka haluavat
käynnistää simulaation ja liittää sen CODESYS:iin. Sinun ei tarvitse avata
päätettä lainkaan - kaikki tapahtuu selaimessa.

---

## 1. Avaa selain

Siirry osoitteeseen:
http://<palvelimen-ip>:8000/

Esimerkki: `http://10.120.32.67:8000/`

Näet listan käynnissä olevista istunnoista. Jos lista on tyhjä, se on
normaalia - kukaan ei ole vielä käynnistänyt simulaatiota.

---

## 2. Luo uusi istunto

1. Klikkaa **"New session"** -painiketta oikeassa yläkulmassa.
2. Valitse **Simulation**-pudotusvalikosta haluamasi simulaatio
   (esim. *Clean-In-Place* tai *Blinking Light*).
3. **Slug** (valinnainen mutta suositeltu):
   - Slug on nimi, jonka CODESYS näkee OPC UA -puussa.
   - Käytä pieniä kirjaimia, numeroita ja alaviivoja, aloita kirjaimella.
   - Esimerkki: `cip_live`, `ryhma3_cip`
   - **Jos jätät slug-kentän tyhjäksi**, simulaatio saa satunnaisen
     UUID-tunnisteen, joka **vaihtuu joka kerta**. CODESYS:n Data Sources
     -puu on silloin luotava uudelleen jokaisen käynnistyksen jälkeen.
4. **Label** (valinnainen): vapaamuotoinen kuvaus, esim. *"Ryhmä 3 harjoitus"*.
5. Klikkaa **"Start session"**.

**Huom:** Jos käytät slugia, joka on jo käytössä, vanha istunto
**pysäytetään automaattisesti** ja uusi ottaa nimen haltuunsa.

Odota noin 2–5 sekuntia. Sinut ohjataan istunnon yksityiskohtasivulle.

---

## 3. Yhdistä CODESYS:sta

Istunnon yksityiskohtasivulla näet:

- **Endpoint:** `opc.tcp://<palvelin>:8080/gateway/`
- **Browse to:** `Objects -> Gateway -> Sessions -> <slug>`

### CODESYS:ssa:

1. Lisää **OPC UA Data Source** osoitteella
   `opc.tcp://<palvelin>:8080/gateway/`
2. Selaa puuta: `Objects -> Gateway -> Sessions -> <slug>`
3. Lue mittaus: `Measurements.LevelPercent` (pitäisi olla `0.0`)
4. Kirjoita komento: `Commands.WaterValveCmd = 1`
5. Katso kuinka `LevelPercent` alkaa nousta kohti 40:tä.

**Data Sources -puu säilyy** istuntojen uudelleenkäynnistysten yli, koska
slug pysyy samana. Sinun ei tarvitse selata puuta uudelleen.

---

## 4. Live-näkymä selaimessa

Istunnon yksityiskohtasivulla on linkki **"Open live dashboard"**.
Se avaa työntekijän reaaliaikaisen kojelaudan, jossa näet:

- Mittaukset päivittyvät 4 kertaa sekunnissa
- Tilakaaviot ja trendit
- Mahdolliset hälytykset

Kojelauta ei vaadi erillistä kirjautumista - se on suojattu istunnon
tokenilla, joka on osa URL-osoitetta.

---

## 5. Pysäytä istunto

Kun olet valmis:

1. Palaa istunnon yksityiskohtasivulle.
2. Klikkaa punaista **"Stop session"** -painiketta.
3. Vahvista.

Istunto siirtyy tilaan `stopping` ja lopulta `stopped`. CODESYS menettää
yhteyden kyseiseen kansioon.

---

## 6. Ongelmatilanteet

Sivu ei avaudu:
Tarkista että olet samassa intranetissä. Kysy adminilta palvelimen IP.

"No simulations are available":
Adminin pitää ajaa `python manage.py sync_catalog` palvelimella.

Istunto jää tilaan `starting`:
Odota 10 sekuntia. Jos ei etene, ota yhteyttä adminiin.

CODESYS ei näe istuntoa:
Tarkista että endpoint on oikein ja että istunto on tilassa `running`.

Kojelauta ei päivity:
Tarkista verkkoyhteys. Sivu yrittää yhdistää uudelleen automaattisesti.

---

## Pikaohje yhdellä silmäyksellä

1. Avaa selain -> http://<palvelin>:8000/

2. "New session" -> valitse simulaatio + slug -> "Start session"

3. CODESYS -> opc.tcp://<palvelin>:8080/gateway/
    -> Objects -> Gateway -> Sessions -> <slug> 

Lue Measurements.* / kirjoita Commands.*

"Stop session" kun valmis