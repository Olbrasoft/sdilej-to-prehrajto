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
5. Teprve po obou úspěšných pilotech nastav repository variable
   `CONTINUOUS_ENABLED=true`. Volitelná `CONTINUOUS_BATCH_SIZE` může být 1–50,
   výchozí dávka je 25 filmů každých šest hodin.

Kontinuální workflow bez explicitní hodnoty `CONTINUOUS_ENABLED=true` upload
vůbec nespustí. Po přípravě cílového videa, chybě a dokončeném uploadu se stav
okamžitě commitne a pushne, aby přerušení runneru nezpůsobilo tichý duplicitní
upload.

Po úspěšném uploadu se stabilní detailová URL vybraného souboru zapíše do
`manifests/selected-sources.jsonl`. Manifest neobsahuje dočasný autorizovaný
download odkaz. Při budoucím uploadu na jiný účet se detail znovu načte a
aktuální odkaz „Stáhnout rychle“ se vyřeší znovu.

## Lokální ověření

```bash
python -m venv .venv
.venv/bin/pip install ".[test]"
.venv/bin/pytest
```

Pro vytvoření plánu je navíc potřeba FFmpeg, `faster-whisper` a stejné čtyři
proměnné prostředí jako v GitHub Secrets.
