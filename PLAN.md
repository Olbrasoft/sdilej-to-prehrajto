# Plán projektu

Tento soubor slouží jako průběžný plán práce. Jednotlivé body budeme doplňovat, upřesňovat a označovat podle postupu.

## Úkoly

- [x] Založit e-mailovou schránku `sdilej.prehrajto@seznam.cz` na Seznamu.
- [x] Nastavit přeposílání příchozích e-mailů na primární adresu `tuma.rsrobot@gmail.com`.
- [x] Ověřit přeposílání: odeslat testovací e-mail z jiné adresy na `sdilej.prehrajto@seznam.cz` a potvrdit jeho doručení na primární adresu.
- [ ] Změnit heslo e-mailové schránky za náhodně vygenerované a jedinečné.
- [x] Založit účet na Sdílej.cz s e-mailovou adresou `sdilej.prehrajto@seznam.cz`.
- [x] Založit účet na Přehraj.to s uživatelským jménem `sdilej.prehrajto@seznam.cz`.
- [x] Založit lokální Git repozitář, nastavit základní strukturu a vytvořit první commit.
- [x] Vytvořit odpovídající repozitář na GitHubu a propojit jej s lokálním repozitářem.
- [x] Exportovat z produkční CR databáze read-only seznam filmů seřazený podle hodnocení.
- [x] Implementovat vyhledávání filmů na Sdílej.cz podle českého a původního názvu a roku.
- [x] Implementovat bezpečné ověření shody filmu podle názvu, roku, číselných částí názvu a délky.
- [x] Implementovat jazykovou kontrolu kandidátů pomocí Whisperu.
- [x] Implementovat výběr zdroje podle priority jazyka a následně kvality.
- [x] Implementovat tvorbu cílového názvu podle původu filmu a ověřeného jazyka.
- [x] Implementovat průchozí streamování ze Sdílej.cz do multipart uploadu Přehraj.to bez uložení celého filmu na disk.
- [x] Implementovat průběžnou přípravu ověřených zdrojů nezávisle na uploadu.
- [x] Implementovat šest paralelních upload workerů s atomickými lease claimy, okamžitým uvolněním po chybě a expirací po pádu procesu.
- [x] Implementovat oddělené GitHub Actions workflow pro plán, pilotní upload a explicitně povolený kontinuální provoz.
- [x] Ukládat po úspěšném uploadu stabilní detailovou URL vybraného zdroje pro budoucí upload na další účet.
- [x] Spustit a ručně ověřit pilot jednoho filmu.
- [x] Po úspěšném prvním pilotu spustit a ověřit pilot deseti filmů.
- [x] Po obou pilotech zapnout repository variable `CONTINUOUS_ENABLED=true`.

## Poznámky a rozhodnutí

- Pro získání původního souboru/streamu ze Sdílej.cz nelze spoléhat na URL z webového přehrávače, protože přehrávač může používat zmenšený transkódovaný stream.
- Download URL je potřeba hledat pod tlačítky `Stáhnout pomalu` nebo `Stáhnout rychle`. Varianta `Stáhnout rychle` vyžaduje zaplacený/premium přístup, aby šla plně ověřit a používat.
- Backlog obsahuje pouze filmy z tabulky `films` a získává se v databázové relaci s vynuceným read-only režimem.
- Pořadí backlogu používá IMDb hodnocení, při jeho absenci ČSFD a následně TMDB. Hodnocení je vážené počtem hlasů, aby několik jednotlivých hlasů neposouvalo obskurní titul před všeobecně uznávané filmy.
- Vyhledávání na Sdílej.cz musí používat český i původní název a rok. Fuzzy podobnost sama nestačí; číslované díly filmu se nesmí zaměnit a při dostupných datech se kontroluje také délka.
- Nejednoznačná shoda se automaticky nenahrává a musí skončit ve frontě k ruční kontrole.
- Jazyková priorita je potvrzená čeština, poté slovenština a nakonec jiný jazyk. Kvalita rozhoduje až mezi kandidáty stejné jazykové priority.
- V rámci stejného jazyka se bere nejvyšší dostupná třída rozlišení a následně nejmenší soubor, který splní minimální průměrný datový tok pro dané rozlišení a kodek. Nejmenší soubor bez kvalitativní hranice ani automaticky největší soubor se nevybírá.
- Kratší varianta se odmítne při zkrácení přibližně o pět procent nebo více proti katalogové délce. Delší oficiální sestřih může zůstat jako solidní shoda.
- Vyhledávání využívá video filtr Sdílej.cz, řazení od nejmenšího a postupné kvalitativní filtry 4K, 1080p a 720p. Údaje z výsledků se před výběrem znovu ověří na detailu.
- Jazyk se neodvozuje pouze z názvu souboru. Název slouží jako předběžný hint a skutečný jazyk zvuku ověřuje Whisper.
- Cílový název používá formát `Název filmu (rok) Rozlišení Jazyková varianta`. Český původní film nemá jazykový přídomek, zahraniční film s českým zvukem má `CZ Dabing`, slovenský původní film má `SK` a film s cizím zvukem má `CZ Titulky`.
- Rozlišení musí být součástí cílového názvu, protože Přehraj.to je v přehledu videí nezobrazuje tak jako Sdílej.cz. Uvádí se ověřené rozlišení původního souboru ze Sdílej.cz, nikoli rozlišení transkódovaného webového přehrávače.
- Normalizované štítky rozlišení jsou `4K` pro šířku alespoň 3840 px, `1440p` pro alespoň 2560 px, `1080p` pro alespoň 1920 px, `720p` pro alespoň 1280 px a `SD` pro nižší rozlišení.
- Backlog zahrnuje všech 28 775 filmů z tabulky `films`; předem se neomezuje podle obsahu jiných Přehraj.to účtů.
- Každý cizojazyčný zdroj bez českého dabingu dostane cílový název s příponou `CZ Titulky`, i když titulky ještě nejsou v okamžiku uploadu k dispozici. Chybějící titulky se musí zapsat do trvalé follow-up fronty a následně získat z jiného zdroje nebo vytvořit.
- Položka označená `CZ Titulky` není dokončená pouze uploadem videa. Za dokončenou se považuje až po úspěšném připojení českých titulků na Přehraj.to; do té doby musí zůstat ve stavu čekajícím na titulky.
- Celý zdrojový film se nemá ukládat na disk runneru. Runner funguje jako průchozí relé mezi autorizovaným download streamem Sdílej.cz a multipart uploadem Přehraj.to; před přenosem musí znát přesnou velikost, název a MIME typ.
- Průchozí přenos musí používat omezenou paměť, hlídat minimální rychlost a timeout a zapsat úspěch až po potvrzení dokončeného uploadu Přehraj.to.
- Produkční workflow poběží na GitHub Actions stejně jako předchozí synchronizační projekty. Přihlašovací údaje budou pouze v GitHub Secrets a zdrojový detail se znovu vyřeší těsně před přenosem.
- Po úspěšném uploadu se k filmu uloží stabilní detailová URL vybraného souboru ze Sdílej.cz (např. `https://sdilej.cz/32460472/...mkv`) do verzovaného manifestu. Dočasná URL z tlačítka `Stáhnout rychle` se nikdy neukládá; při uploadu na další účet se z detailu vyřeší znovu.
- Ověřený zdroj se ukládá atomicky ihned po výběru, ještě před uploadem. Samostatné přípravné workflow proto může kontinuálně plnit frontu, zatímco šest upload workerů současně přenáší už připravené položky.
- Producer i uploader jsou samostatné dlouhodobé smyčky. Producer může bez pevného počtu připravovat celý zbývající backlog; uploader opakovaně odebírá ověřenou frontu a při jejím krátkém vyprázdnění ji znovu načítá, aniž by ukončil runner.
- Každý upload worker musí před přenosem získat výhradní šestihodinový lease. Aktivní lease brání dvojímu převzetí, úspěch nebo zachycená chyba jej odstraní a po tvrdém pádu se položka odblokuje expirací.
- Repozitář nesmí vytvářet desetitisíce per-film souborů ani commit po každém claimu. Ověřené zdroje se vedou v jediném průběžně kompaktovaném JSONL manifestu. Zdroje a úspěšné uploady se checkpointují po čtyřech, méně důležité pokusy a chyby po 25 změnách. Po dokončení migrace se dočasný stav sloučí s manifestem do jednoho výsledného katalogu a odstraní.
