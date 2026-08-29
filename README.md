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
postupně připravit celý backlog. Uploader po stejnou dobu opakovaně odebírá
připravené dávky po čtyřech přenosech současně. Pokud je fronta krátce prázdná,
čeká dvě minuty a znovu načte nové checkpointy produceru. Oba hodinové triggery
se díky vlastním concurrency skupinám průběžně střídají bez vzájemného blokování.

Kontinuální workflow bez explicitní hodnoty `CONTINUOUS_ENABLED=true` upload
vůbec nespustí. Lokální stav se zapisuje atomicky a Git checkpointy se slučují
po 25 změnách plus jednou na konci workflow, aby Git historie nerostla o commit
pro každý claim. Před opakováním se přesný název ověří v nahraných videích, takže
ani poslední necommitnutá dávka po pádu nevytvoří tichý duplicitní upload.
Upload běží ve čtyřech nezávislých workerech. Před převzetím filmu
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
minimum než pro H.264, VC-1 nebo neznámý kodek. Stabilní manifest obsahuje také
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
