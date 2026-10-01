# Sdílej.cz → Přehraj.to

Pipeline vybere filmy z prioritizovaného backlogu, na Sdílej.cz ověří správný
titul, rok, délku, původní rozlišení a jazyk zvuku a originální soubor průběžně
přepošle do multipart uploadu Přehraj.to. Celý film se na runner neukládá.

## Bezpečné spuštění

1. Nastav GitHub Secrets `SDILEJ_EMAIL`, `SDILEJ_PASSWORD`,
   `PREHRAJTO_EMAIL` a `PREHRAJTO_PASSWORD`.
2. Spusť `pilot-plan` s velikostí 1, zkontroluj report a zkopíruj SHA plánu.
3. Spusť `pilot-upload` se stejnou velikostí a schváleným SHA.
4. Po kontrole cílového videa zopakuj plán a upload s velikostí 10.
5. Nech workflow `prepare-sources` průběžně plnit frontu ověřených zdrojů.
6. Teprve po obou úspěšných pilotech nastav repository variable
   `CONTINUOUS_ENABLED=true`. Volitelná `CONTINUOUS_BATCH_SIZE` může být 1–50,
   výchozí dílčí dávka je 25 filmů.

Příprava a upload jsou dva nezávislé dlouhodobé procesy. Producer opakuje
vyhledávání a jazykové ověřování po dobu až 330 minut jednoho runneru a může
postupně připravit celý backlog. Pět rychlých přípravných workerů kontroluje jen
první tři kandidáty jednoho filmu. Kandidátsky náročný film pak uloží do trvalé
hluboké fronty, kterou souběžně zpracovává šestý worker bez omezení počtu
kandidátů. Uploader po stejnou dobu opakovaně odebírá připravené dávky nejvýše po třech
přenosech současně. Pokud je fronta krátce prázdná, čeká 30 sekund a znovu
načte nové checkpointy produceru. Oba hodinové triggery se díky vlastním
concurrency skupinám průběžně střídají bez vzájemného blokování.

Kontinuální workflow bez explicitní hodnoty `CONTINUOUS_ENABLED=true` upload
vůbec nespustí. Lokální stav se zapisuje atomicky. Úspěšné uploady a nové zdroje
se checkpointují po čtyřech, běžné negativní výsledky po 250 změnách, předání
do hluboké fronty po deseti a uploadové chyby po 25 změnách. Vše se ještě jednou
uloží na konci workflow, aby Git historie nerostla o commit pro každý claim.
Před opakováním se přesný název ověří v nahraných videích, takže
ani poslední necommitnutá dávka po pádu nevytvoří tichý duplicitní upload.
Upload běží nejvýše ve třech nezávislých workerech (výchozí hodnota je tři).
Parametr `--workers` i proměnná `UPLOAD_WORKERS` přijímají pouze hodnoty 1–3.
Před převzetím filmu
worker atomicky uloží šestihodinový lease; ostatní workery jej přeskočí. Úspěch
claim odstraní, běžná chyba jej okamžitě uvolní a po pádu procesu jej lze znovu
převzít až po vypršení lease.

Jakmile je zdroj ověřen, jeho stabilní detailová URL se atomicky zapíše do
jediného `manifests/selected-sources.jsonl`. Během workflow funguje jako
append-only žurnál a na konci se atomicky zkompaktuje na právě jeden řádek na
film. Manifest neobsahuje dočasný autorizovaný download odkaz. Při budoucím
uploadu na jiný účet se detail znovu načte a aktuální odkaz „Stáhnout rychle“ se
vyřeší znovu.

Výběr zdroje nepoužívá pravidlo „největší soubor je nejlepší“. Sdílej.cz se
prohledává po kvalitativních třídách 4K, 1080p a 720p, vždy mezi videosoubory
seřazenými od nejmenšího. Po ověření filmu, délky a jazyka se spočítá průměrný
datový tok z velikosti a délky. Kandidáti pod minimem pro své rozlišení a kodek
se odmítnou a z ostatních se vezme nejmenší. Pro H.265/HEVC a AV1 platí nižší
minimum než pro výslovně rozpoznané H.264 nebo VC-1. Neznámý kodek u 4K se
posuzuje kompaktním limitem, aby se moderní 4K zdroj bez kodeku v názvu
nesprávně nezahodil. Před rozhodnutím `ffprobe` načte z autorizovaného
originálního streamu pomocí HTTP rozsahů skutečný kodek, rozlišení a délku;
celý film se kvůli kontrole nestahuje. Stabilní manifest obsahuje také
verzi této výběrové politiky; položky vytvořené starším pravidlem se před dalším
uploadem musí znovu vyhodnotit producerem.

Repozitář nevytváří soubor pro každý film. Provozní `state/sync.json` obsahuje
jen krátké uploadové stavy, nejvýše tři poslední chyby a dočasné claimy;
`state/source-scan.json` odděleně drží cooldown neúspěšných hledání. Po dokončení
celé migrace se zdrojový manifest a uploadové příznaky sloučí do jediného výsledného
JSONL katalogu a provozní stav se odstraní. Při současné velikosti záznamů má
výsledný katalog pro 28 775 filmů odhad přibližně 26 MB, tedy hluboko pod limitem
100 MB na jeden GitHub soubor.

Jednorázový příkaz `sdilej-sync export-results` vytvoří výsledný katalog
`manifests/film-results.jsonl` sloučením ověřených zdrojů a uploadových stavů.
Odstranění provozních souborů se provede až po kontrole úplnosti celé migrace.

## Lokální ověření

```bash
python -m venv .venv
.venv/bin/pip install ".[test]"
.venv/bin/pytest
```

Pro vytvoření plánu je navíc potřeba FFmpeg, `faster-whisper` a stejné čtyři
proměnné prostředí jako v GitHub Secrets.

## Doplňování českých titulků

Workflow `backfill-subtitles` běží každou hodinu, vždy pouze v jednom workeru.
Nezastavuje nahrávání filmů a nemění jeho limit tří souběžných přenosů.
Po dávkách prochází stránky `CZ Titulky` od nejstarších, aby zpracovávaná
videa neblokovala dokončená. Kontroluje i položky mimo původní titulkovou frontu;
bez doložené vazby na původní zdroj je ale automaticky neupravuje.

- Přehled k ručnímu doplnění: [reports/subtitle-backfill.md](reports/subtitle-backfill.md).
- Strojový stav a kurzor pro další běh: `state/subtitle-backfill.json`.
- Zpracovávaná videa a existující české titulky se přeskakují. Neznámý jazyk
  existující stopy vyžaduje kontrolu; stopa se nemaže ani nepřepisuje.
- Používá se pouze přesně zaznamenaný zdroj na Sdílej.cz. Nově dokončené
  přenosy ukládají zdroj do titulkové fronty; dohledání existujícího videa podle
  názvu se nepovažuje za důkaz, že pochází ze právě vybraného zdroje.
- České textové stopy se extrahují nebo převedou na UTF-8 SRT s CRLF. SRT,
  WebVTT a ASS/SSA jsou podporované, obrazové ani natvrdo vložené titulky ne.
  Nic se nepřekládá, negeneruje, nečte pomocí OCR ani nehledá u jiné verze filmu.
- Chybějící česká stopa je v přehledu uvedená samostatně, s názvem filmu,
  odkazem na video i původní zdroj a časem kontroly. Nová kontrola nejdříve za
  sedm dní; dočasné chyby mají kratší interval a vlastní oddíl.
- Před vložením se znovu ověří přesné ID videa a do Gitu uloží záměr včetně
  hashe a jedinečného názvu SRT. Nejasná odpověď serveru vede pouze k ověřování,
  nikdy k druhému POST. Úspěch vyžaduje novou odpovídající stopu v seznamu.

První běh lze spustit ručně; výchozí dávka prohlédne 20 stránek a nejvýše
12 zdrojů, vloží nejvýše tři titulkové soubory **postupně**. Celý zdrojový film
se neukládá na disk, ale extrakce vnitřních titulků může vyžadovat jeho přečtení
po síti. FFmpeg má pevný časový limit; tajné odkazy ani text titulků se do Gitu
neukládají. Pro diagnostiku bez změn na webu použij samostatný lokální stav:

```bash
python -m sdilej_to_prehrajto.subtitle_backfill --dry-run \
  --state /tmp/subtitle-inspection.json --report /tmp/subtitle-inspection.md \
  --max-pages 2 --max-sources 4
```
