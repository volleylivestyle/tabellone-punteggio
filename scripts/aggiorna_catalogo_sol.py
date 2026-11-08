#!/usr/bin/env python3
"""Sincronizza TUTTI i campionati, tutte le fasi e tutti i gironi di Milano-Monza-Lecco dal portale SOL.

SOL usa Blazor Server e Radzen. Per ogni campionato lo script ricava l'ID
tecnico dalla route /calendarioris/{id}/{stagione}; per ogni girone legge la
scheda Radzen già renderizzata. Il pulsante con icona "print" e title
"Calendario" è un comando di stampa, non un passaggio per aprire i dati:
le partite sono già presenti nella pagina dopo la selezione del girone.

La sincronizzazione è transazionale: nessun dato SOL viene sostituito finché
non sono stati letti e validati tutti i campionati e tutti i gironi scoperti.
Se un singolo girone fallisce, il catalogo precedente resta intatto e il job
termina FALLITO. Gli ID locali dei gironi sono SOL-{idCampionato}-{girone}.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "catalogo-fipav-v11.json"
SOL_URL = "https://www.sol.milano.federvolley.it/campionatiris"
SEASON_ROUTE = "2026"  # stagione 2026/2027, documentata nelle catture HAR del 03-04/11/2026
EXPECTED_COMPETITIONS = 18  # minimo atteso nel catalogo Milano 2026/2027
NAV_TIMEOUT_MS = 60_000
ELEMENT_TIMEOUT_MS = 35_000


def clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def parse_date(value: str) -> str:
    m = re.search(r"\b(\d{1,2}/\d{1,2}/\d{4})\b", value)
    if not m:
        return ""
    d, mo, y = m.group(1).split("/")
    return f"{int(d):02d}/{int(mo):02d}/{y}"


def parse_time(value: str) -> str:
    m = re.search(r"\b([01]?\d|2[0-3])[.:]([0-5]\d)\b", value)
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else ""


def normalize_group_label(value: Any) -> str:
    """Restituisce la sigla del girone da tab come A, A1 o "Girone A"."""
    label = clean(value).upper()
    match = re.fullmatch(r"(?:GIRONE\s*)?([A-Z0-9]{1,3})", label)
    return match.group(1) if match else ""


def parse_match_cells(cells: list[str], round_no: int, group_id: str) -> dict[str, Any] | None:
    """Normalizza le sei colonne della griglia Calendario Ufficiale SOL."""
    if len(cells) < 6:
        return None
    game_match = re.search(r"\d+", clean(cells[0]))
    date = parse_date(cells[1])
    time = parse_time(cells[1])
    teams = [clean(x) for x in cells[2].splitlines() if clean(x)]
    if len(teams) < 2:
        # Ripiega sul testo completo, nel caso in cui Radzen separi i nomi
        # tramite spazi o elementi inline invece di newline.
        teams = [clean(x) for x in re.split(r"\s{2,}", cells[2]) if clean(x)]
    if not game_match or not date or not teams:
        return None
    home = teams[0]
    away = teams[1] if len(teams) > 1 else ""
    if not home or not away:
        return None

    set_values = [int(x) for x in re.findall(r"\b[0-3]\b", cells[3])]
    result = set_values[:2] if len(set_values) >= 2 else None
    status_text = clean(cells[4])
    partials = re.findall(r"\b\d{1,2}/\d{1,2}\b", status_text)
    status = re.sub(r"\b\d{1,2}/\d{1,2}\b", " ", status_text)
    status = clean(status)

    venue_lines = [clean(x) for x in cells[5].splitlines() if clean(x)]
    venue_name = venue_lines[0] if venue_lines else ""
    city = ""
    address = ""
    if len(venue_lines) > 1:
        tail = " · ".join(venue_lines[1:])
        if " - " in tail:
            city, address = [clean(x) for x in tail.split(" - ", 1)]
        else:
            city = venue_lines[1]
            address = " · ".join(venue_lines[2:]) if len(venue_lines) > 2 else ""
    venue = " · ".join(x for x in (venue_name, city, address) if x)

    return {
        "gameNo": int(game_match.group(0)),
        "round": int(round_no or 0),
        "date": date,
        "time": time,
        "home": home,
        "away": away,
        "result": result,
        "partials": partials,
        "status": status,
        "venue": venue,
        "city": city,
        "address": address,
        "venueDetails": {"name": venue_name, "city": city, "address": address},
    }


async def wait_for_rows(page) -> None:
    """Attende le righe della lista SOL dopo aver selezionato la stagione."""
    await page.wait_for_selector("tr.rz-data-row", state="visible", timeout=ELEMENT_TIMEOUT_MS)


async def select_season_2026_2027(page) -> None:
    """Seleziona la stagione 2026/2027 con il ciclo reale Radzen/Blazor.

    SOL usa Radzen 8.7.5. Il click Blazor può provocare un nuovo render e
    rigenerare GetId()/PopupID. Per questo il popup trovato prima del click
    non è necessariamente quello ancora montato dopo il callback server.

    Strategia:
    1) click sul trigger visibile, così Blazor esegue l'eventuale callback Open;
    2) rilettura del root e del popup *dopo* il round-trip Blazor;
    3) chiamata idempotente a Radzen.openPopup sul popup corrente, usando
       l'API JS effettivamente presente in Radzen.Blazor.js 8.7.5;
    4) attesa delle opzioni realmente renderizzate prima di selezionare.

    Non si usa togglePopup: se Blazor ha già aperto il vecchio popup, toggle
    rischierebbe di richiuderlo. openPopup invece imposta display=block.
    """
    dropdown = page.locator("div.rz-dropdown:has(input#stagione)").first
    await dropdown.wait_for(state="visible", timeout=ELEMENT_TIMEOUT_MS)
    label_locator = dropdown.locator(".rz-dropdown-label").first
    season_pattern = re.compile(r"2026\s*(?:/|-|–)\s*2027|2026\s*(?:/|-|–)\s*27", re.I)

    current_label = clean(await label_locator.inner_text()) if await label_locator.count() else ""
    if season_pattern.search(current_label):
        print(f"SOL: stagione già selezionata: {current_label}", flush=True)
        return

    before = await page.evaluate("""() => {
        const d = document.querySelector('input#stagione')?.closest('.rz-dropdown');
        const p = d ? (document.getElementById(`popup-${d.id}`) || d.querySelector('.rz-dropdown-panel')) : null;
        return {
            rootId: d?.id || '', rootClass: d?.className || '',
            label: d?.querySelector('.rz-dropdown-label')?.innerText || '',
            trigger: !!d?.querySelector('.rz-dropdown-trigger'),
            popupId: p?.id || '', popupDisplay: p?.style.display || '',
            items: p?.querySelectorAll('.rz-dropdown-item,[role=option]').length || 0,
            radzenOpenPopup: typeof window.Radzen?.openPopup === 'function'
        };
    }""")
    print(f"SOL: dropdown stagione pronto; stato iniziale={before}", flush=True)

    if not before.get("trigger"):
        raise RuntimeError(f"SOL: trigger Radzen stagione non trovato. Stato DOM: {before}")
    if not before.get("radzenOpenPopup"):
        raise RuntimeError("SOL: Radzen.openPopup non disponibile; Radzen.Blazor.js 8.7.5 non inizializzato.")

    # Il click sul trigger è il gesto utente previsto dal componente Radzen.
    # Non cliccare l'input readonly (tabindex=-1) né il root sopra il suo centro.
    trigger = dropdown.locator(".rz-dropdown-trigger").first
    await trigger.click(timeout=15_000)

    # Attendi il round-trip Blazor: la callback Open può rigenerare il root e
    # il relativo PopupID. Non riutilizzare riferimenti DOM/ID precedenti.
    try:
        await page.wait_for_function("""(oldId) => {
            const d = document.querySelector('input#stagione')?.closest('.rz-dropdown');
            const p = d ? (document.getElementById(`popup-${d.id}`) || d.querySelector('.rz-dropdown-panel')) : null;
            const n = p?.querySelectorAll('.rz-dropdown-item,[role=option]').length || 0;
            return !!d && (d.id !== oldId || n > 0 || p?.style.display === 'block');
        }""", arg=before.get("rootId", ""), timeout=10_000)
    except PlaywrightTimeoutError:
        # Non è ancora un errore definitivo: alcune pagine non cambiano GetId
        # quando il callback termina. Proseguiamo rileggendo il DOM corrente.
        pass

    after_click = await page.evaluate("""() => {
        const d = document.querySelector('input#stagione')?.closest('.rz-dropdown');
        const p = d ? (document.getElementById(`popup-${d.id}`) || d.querySelector('.rz-dropdown-panel')) : null;
        return {
            rootId: d?.id || '', rootClass: d?.className || '',
            label: d?.querySelector('.rz-dropdown-label')?.innerText || '',
            popupId: p?.id || '', popupDisplay: p?.style.display || '',
            popupClass: p?.className || '', popupParent: p?.parentElement?.tagName || '',
            items: p?.querySelectorAll('.rz-dropdown-item,[role=option]').length || 0,
            popupText: (p?.innerText || '').trim().slice(0, 300),
            radzenOpenPopup: typeof window.Radzen?.openPopup === 'function'
        };
    }""")
    print(
        "SOL: dopo click Blazor; "
        f"root prima={before.get('rootId')} root corrente={after_click.get('rootId')}; "
        f"stato={after_click}",
        flush=True,
    )

    # Recupera il popup ricreato dal render Blazor e aprilo con la funzione
    # ufficiale Radzen 8.7.5. openPopup (non togglePopup) è intenzionalmente
    # usato per rendere l'operazione idempotente.
    opened = await page.evaluate("""() => {
        const d = document.querySelector('input#stagione')?.closest('.rz-dropdown');
        if (!d) return {ok:false, reason:'root stagione assente'};
        const id = `popup-${d.id}`;
        const p = document.getElementById(id) || d.querySelector('.rz-dropdown-panel') || document.querySelector('.rz-dropdown-panel');
        if (!p) return {ok:false, reason:'pannello stagione assente', rootId:d.id};
        if (typeof window.Radzen?.openPopup !== 'function')
            return {ok:false, reason:'Radzen.openPopup non disponibile', rootId:d.id, popupId:p.id};
        if (p.style.display !== 'block' && !p.classList.contains('rz-open')) {
            window.Radzen.openPopup(d, p.id, true, null, null, null, null, null, true, false);
        }
        return {
            ok: p.style.display === 'block' || p.classList.contains('rz-open'),
            rootId:d.id, popupId:p.id, display:p.style.display || '',
            className:p.className || '', parent:p.parentElement?.tagName || '',
            items:p.querySelectorAll('.rz-dropdown-item,[role=option]').length || 0,
            text:(p.innerText || '').trim().slice(0,300)
        };
    }""")
    print(f"SOL: apertura diretta Radzen sul popup corrente: {opened}", flush=True)
    if not opened.get("ok"):
        raise RuntimeError(f"SOL: Radzen.openPopup non è riuscito ad aprire il popup corrente: {opened}")

    # La callback Open del componente può popolare la lista lato server dopo
    # il primo render. Aspettiamo gli item, non solo il pannello grafico.
    try:
        await page.wait_for_function("""() => {
            const d = document.querySelector('input#stagione')?.closest('.rz-dropdown');
            const p = d ? (document.getElementById(`popup-${d.id}`) || document.querySelector('.rz-dropdown-panel')) : null;
            return !!p && (p.style.display === 'block' || p.classList.contains('rz-open')) &&
                p.querySelectorAll('.rz-dropdown-item,[role=option]').length > 0;
        }""", timeout=20_000)
    except PlaywrightTimeoutError as exc:
        details = await page.evaluate("""() => {
            const d = document.querySelector('input#stagione')?.closest('.rz-dropdown');
            const p = d ? (document.getElementById(`popup-${d.id}`) || document.querySelector('.rz-dropdown-panel')) : null;
            return {
                rootId:d?.id || '', rootClass:d?.className || '',
                label:d?.querySelector('.rz-dropdown-label')?.innerText || '',
                expanded:d?.getAttribute('aria-expanded') || null,
                popupId:p?.id || '', display:p?.style.display || '',
                popupClass:p?.className || '', parent:p?.parentElement?.tagName || '',
                html:p?.innerHTML?.slice(0,1800) || '',
                items:p?.querySelectorAll('.rz-dropdown-item,[role=option]').length || 0,
                radzen:typeof window.Radzen, ws:typeof window.Blazor
            };
        }""")
        try:
            await page.screenshot(path=str(ROOT / "sol-debug-season-items.png"), full_page=True)
            (ROOT / "sol-debug-season-items.html").write_text(await page.content(), encoding="utf-8")
        except Exception:
            pass
        raise RuntimeError(
            "SOL: popup Radzen aperto, ma il server Blazor non ha fornito opzioni stagione. "
            f"Diagnostica DOM: {details}. Catalogo precedente conservato."
        ) from exc

    options = page.locator("div.rz-dropdown-panel .rz-dropdown-item, div.rz-dropdown-panel [role='option']")
    option_labels = [clean(x) for x in await options.all_inner_texts()]
    print(f"SOL: opzioni stagione disponibili ({len(option_labels)}): {option_labels}", flush=True)

    target_index = next((i for i, value in enumerate(option_labels) if season_pattern.search(value)), None)
    if target_index is None:
        target_index = next((i for i, value in enumerate(option_labels)
                             if re.search(r"^\s*2026(?:\D|$)", value)), None)
    if target_index is None:
        raise RuntimeError(
            "Stagione 2026/2027 non trovata nel menu SOL. "
            f"Opzioni disponibili: {option_labels or 'nessuna'}. Catalogo precedente conservato."
        )

    await options.nth(target_index).click(timeout=15_000)
    await page.wait_for_function(
        """() => {
            const d=document.querySelector('input#stagione')?.closest('.rz-dropdown');
            const t=d?.querySelector('.rz-dropdown-label')?.innerText || '';
            const rows=Array.from(document.querySelectorAll('tr.rz-data-row')).filter(r=>r.getClientRects().length);
            return /2026/.test(t) && rows.length > 0;
        }""",
        timeout=ELEMENT_TIMEOUT_MS,
    )
    selected_label = clean(await label_locator.inner_text()) if await label_locator.count() else option_labels[target_index]
    print(f"SOL: stagione selezionata e griglia caricata: {selected_label}", flush=True)


async def open_selection_page(page) -> None:
    print("SOL: apertura elenco campionati...", flush=True)
    # Registra il listener prima di goto: il WebSocket può aprirsi rapidamente.
    ready = asyncio.Event()
    state = {"seen": False, "received": 0}

    def on_websocket(ws) -> None:
        if "/_blazor" not in ws.url:
            return
        state["seen"] = True

        def on_frame(_payload) -> None:
            state["received"] += 1
            if state["received"] >= 5:
                ready.set()

        ws.on("framereceived", on_frame)

    page.on("websocket", on_websocket)
    try:
        await page.goto(SOL_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        await page.locator("input#stagione").wait_for(state="attached", timeout=ELEMENT_TIMEOUT_MS)
        try:
            await asyncio.wait_for(ready.wait(), timeout=45)
        except asyncio.TimeoutError as exc:
            raise RuntimeError(
                "Circuito Blazor SOL non interattivo prima della selezione stagione "
                f"(websocket={state['seen']}, frame ricevuti={state['received']})."
            ) from exc
        print(f"SOL: primo render Blazor completato ({state['received']} frame ricevuti)", flush=True)
    finally:
        page.remove_listener("websocket", on_websocket)

    await select_season_2026_2027(page)
    await wait_for_rows(page)
    await page.wait_for_function(
        "() => document.querySelectorAll('tr.rz-data-row').length >= 10",
        timeout=ELEMENT_TIMEOUT_MS,
    )
    print(f"SOL: elenco campionati pronto ({await page.locator('tr.rz-data-row').count()} righe).", flush=True)


async def read_competitions(page, existing: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Legge le righe della pagina campionati usando le colonne reali SOL.

    Il DOM già osservato espone quattro celle: icona genere, sigla, nome,
    fase. Non limitiamo la scansione ai soli codici già presenti nel JSON:
    così una nuova competizione comparsa su SOL viene acquisita ugualmente.
    """
    rows = page.locator("tr.rz-data-row")
    known_by_code = {
        clean(c.get("code")): c for c in existing
        if isinstance(c, dict) and clean(c.get("code"))
    }
    out: list[dict[str, str]] = []
    seen: set[str] = set()

    for index in range(await rows.count()):
        row = rows.nth(index)
        if not await row.is_visible():
            continue
        cells = [clean(x) for x in await row.locator(":scope > td").all_inner_texts()]
        if len(cells) < 3:
            continue

        # Nella riga SOL: td[1] = sigla; td[2] = denominazione; td[3] = fase.
        code = clean(cells[1]) if len(cells) > 1 else ""
        if not re.fullmatch(r"[A-Za-z0-9]{2,5}", code):
            # Fallback per piccole variazioni del markup, mantenendo i codici
            # del catalogo esistente come criterio di riconoscimento.
            row_text = clean(" ".join(cells))
            code = next((candidate for candidate in known_by_code
                         if re.search(rf"(?<![A-Za-z0-9]){re.escape(candidate)}(?![A-Za-z0-9])", row_text, re.I)), "")
        if not code:
            continue
        if code in seen:
            raise RuntimeError(f"SOL: sigla competizione duplicata nella pagina campionati: {code}")
        seen.add(code)

        old = known_by_code.get(code, {})
        name = clean(cells[2]) if len(cells) > 2 else ""
        phase = clean(cells[3]) if len(cells) > 3 else ""
        if not name:
            name = clean(old.get("name"))
        if not phase:
            phase = clean(old.get("phase"))

        gender = clean(old.get("gender"))
        try:
            image = row.locator(":scope > td").nth(0).locator("img").first
            image_src = clean(await image.get_attribute("src")) if await image.count() else ""
            image_alt = clean(await image.get_attribute("alt")) if await image.count() else ""
            image_hint = f"{image_src} {image_alt}".lower()
            if "female" in image_hint or "femmin" in image_hint:
                gender = "femminile"
            elif "male" in image_hint or "masch" in image_hint:
                gender = "maschile"
        except Exception:
            pass
        if not gender:
            gender = "femminile" if re.search(r"femminile", name, re.I) else "maschile"

        if not name:
            raise RuntimeError(f"SOL: nome mancante per la competizione {code}; riga={cells}")
        out.append({
            "code": code,
            "name": name,
            "phase": phase,
            "gender": gender,
            "_rowIndex": str(index),
        })

    if len(out) < max(EXPECTED_COMPETITIONS, len(known_by_code)):
        raise RuntimeError(
            f"SOL: pagina campionati incompleta: lette {len(out)} competizioni; "
            f"attese almeno {max(EXPECTED_COMPETITIONS, len(known_by_code))}. "
            f"Codici rilevati: {[x['code'] for x in out]}"
        )
    return out


async def click_competition(page, code: str) -> tuple[str, str, str]:
    """Apre la competizione individuando la sigla nella seconda colonna."""
    rows = page.locator("tr.rz-data-row")
    target = None
    for index in range(await rows.count()):
        row = rows.nth(index)
        cells = row.locator(":scope > td")
        if await cells.count() < 3:
            continue
        actual_code = clean(await cells.nth(1).inner_text())
        if actual_code == code:
            target = row
            break
    if target is None:
        raise RuntimeError(f"SOL: riga della competizione {code} non trovata nell'elenco aggiornato")

    # Il portale rende cliccabile la denominazione nella terza cella.
    target_cell = target.locator(":scope > td").nth(2)
    clickable = target_cell.locator(".cursor-pointer").first
    if await clickable.count():
        await clickable.click(timeout=15_000)
    else:
        await target_cell.click(timeout=15_000)

    await page.wait_for_function(
        "() => /\\/calendarioris\\/\\d+\\/\\d+/.test(location.pathname)",
        timeout=ELEMENT_TIMEOUT_MS,
    )
    match = re.search(r"/calendarioris/(\d+)/(\d+)", page.url)
    if not match:
        raise RuntimeError(f"SOL: route del calendario non riconosciuta dopo il click su {code}: {page.url}")
    competition_id, season = match.group(1), match.group(2)
    route_url = f"https://www.sol.milano.federvolley.it/calendarioris/{competition_id}/{season}"
    return competition_id, season, route_url


async def discover_group_tabs(page) -> list[str]:
    """Legge tutte le schede-girone dalle tab Radzen reali (A, A1, A2, ...)."""
    tabs = page.locator('li[role="tab"]')
    await tabs.first.wait_for(state="visible", timeout=ELEMENT_TIMEOUT_MS)
    labels = await tabs.all_inner_texts()
    groups: list[str] = []
    for raw in labels:
        label = normalize_group_label(raw)
        # Esclude Squadre e Classifica Gir. X; include sigle alfanumeriche
        # osservate nel DOM come A1 e A2, oltre alle lettere B...K.
        if label and label not in groups:
            groups.append(label)

    if not groups:
        # Fallback dalle righe della scheda Squadre, se le tab non sono ancora
        # state renderizzate dal server Blazor.
        tables = page.locator("table.rz-grid-table")
        for ti in range(await tables.count()):
            table = tables.nth(ti)
            headers = [clean(x).lower() for x in await table.locator("thead th").all_inner_texts()]
            if not any("gir" in h for h in headers) or not any("squadra" in h for h in headers):
                continue
            for row in await table.locator("tbody tr").all():
                cells = [clean(x) for x in await row.locator(":scope > td").all_inner_texts()]
                if len(cells) >= 3 and re.fullmatch(r"[A-Z0-9]{1,3}", cells[1].upper()):
                    label = cells[1].upper()
                    if label not in groups:
                        groups.append(label)
    return groups


async def extract_roster_teams(page, group_letter: str) -> list[str]:
    """Estrae i nomi squadra dalla griglia Squadre (se il markup li espone)."""
    result: list[str] = []
    try:
        tables = page.locator("table.rz-grid-table")
        count = await tables.count()
        for ti in range(count):
            table = tables.nth(ti)
            headers = [clean(x).lower() for x in await table.locator("thead th").all_inner_texts()]
            if not any("gir" in h for h in headers) or not any("squadra" in h for h in headers):
                continue
            rows = await table.locator("tbody tr:not(.rz-datatable-emptymessage-row)").all()
            for row in rows:
                cells = [clean(x) for x in await row.locator(":scope > td").all_inner_texts()]
                if len(cells) >= 3 and cells[1].upper() == group_letter:
                    # Nella griglia SOL la colonna squadra può contenere prima
                    # la denominazione visualizzata e poi lo sponsor/club.
                    name = cells[2].splitlines()[0].strip()
                    if name:
                        result.append(name)
            if result:
                break
    except Exception:
        pass
    return sorted(set(result), key=str.casefold)


async def open_group_calendar(page, route_url: str, group_letter: str) -> None:
    """Seleziona la scheda del girone e attende il calendario già renderizzato.

    Evidenza DOM verificata: la pagina mostra già "Calendario Ufficiale" e le
    righe delle partite dopo il render della scheda. Il pulsante
    button[title="Calendario"] include l'icona "print" ed è quindi un comando
    di stampa: NON va cliccato per accedere alle partite.
    """
    await page.goto(route_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    tabs = page.locator('li[role="tab"]')
    await tabs.first.wait_for(state="visible", timeout=ELEMENT_TIMEOUT_MS)

    tab = None
    for index in range(await tabs.count()):
        candidate = tabs.nth(index)
        label = normalize_group_label(await candidate.inner_text())
        if label == group_letter.upper():
            tab = candidate
            break
    if tab is None:
        labels = [clean(x) for x in await tabs.all_inner_texts()]
        raise RuntimeError(f"SOL: scheda Girone {group_letter} assente in {route_url}; schede={labels}")

    selected = "rz-tabview-selected" in (await tab.get_attribute("class") or "")
    if not selected:
        await tab.click(timeout=15_000)

    await page.wait_for_function(
        "letter => Array.from(document.querySelectorAll('li[role=\\\"tab\\\"]'))"
        ".some(el => el.classList.contains('rz-tabview-selected') && "
        "(el.innerText || '').trim().toUpperCase() === letter)",
        arg=group_letter.upper(),
        timeout=ELEMENT_TIMEOUT_MS,
    )

    # La pagina aggiorna il contenuto del pannello dopo il callback Blazor.
    # Individuiamo la griglia calendario dai suoi titoli (Gara/Data/Squadre/Campo)
    # e dalle righe che contengono un numero gara e una data.
    await page.wait_for_function(
        "letter => {"
        " const visible = el => !!(el && el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden');"
        " const tabs = Array.from(document.querySelectorAll('li[role=\\\"tab\\\"]'));"
        " const selected = tabs.find(el => el.classList.contains('rz-tabview-selected'));"
        " if (!selected || (selected.innerText || '').trim().toUpperCase() !== letter) return false;"
        " const body = (document.body.innerText || '').toUpperCase();"
        " const groupHeading = new RegExp('GIRONE\\\\s+' + letter + '(?:\\\\s|$)', 'i').test(body);"
        " const tables = Array.from(document.querySelectorAll('table.rz-grid-table')).filter(visible);"
        " const ok = tables.some(table => {"
        "   const head = (table.querySelector('thead')?.innerText || '').toLowerCase();"
        "   const rows = Array.from(table.querySelectorAll('tbody tr')).filter(visible);"
        "   const hasRows = rows.some(row => {"
        "     const cells = Array.from(row.querySelectorAll(':scope > td'));"
        "     return cells.length >= 6 && /^\\s*\\d+\\s*$/.test(cells[0]?.innerText || '') && /\\d{1,2}\\/\\d{1,2}\\/\\d{4}/.test(cells[1]?.innerText || '');"
        "   });"
        "   const hasHeaders = head.includes('gara') && head.includes('data') && head.includes('squadre') && head.includes('campo');"
        "   return hasRows && (hasHeaders || /calendario ufficiale/i.test(body));"
        " });"
        " return groupHeading && /calendario ufficiale/i.test(body) && ok;"
        "}",
        arg=group_letter.upper(),
        timeout=ELEMENT_TIMEOUT_MS,
    )
    print(f"SOL: Girone {group_letter} selezionato; calendario già presente nel pannello.", flush=True)


async def scrape_calendar_rows(page, group_letter: str, group_id: str) -> list[dict[str, Any]]:
    """Estrae tutte le tabelle del calendario, normalmente una per giornata."""
    raw_rows = await page.evaluate(r"""() => {
      const visible = el => !!(el && el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden');
      const tables = Array.from(document.querySelectorAll('table.rz-grid-table')).filter(visible);
      const rowHasMatch = row => {
        const cells = Array.from(row.querySelectorAll(':scope > td'));
        return cells.length >= 6 && /^\s*\d+\s*$/.test(cells[0]?.innerText || '') &&
          /\d{1,2}\/\d{1,2}\/\d{4}/.test(cells[1]?.innerText || '');
      };
      let calendarTables = tables.filter(table => {
        const head = (table.querySelector('thead')?.innerText || '').toLowerCase();
        return head.includes('gara') && head.includes('data') && head.includes('squadre') && head.includes('campo');
      });
      if (!calendarTables.length) {
        calendarTables = tables.filter(table => Array.from(table.querySelectorAll('tbody tr')).some(row => visible(row) && rowHasMatch(row)));
      }

      const headings = Array.from(document.querySelectorAll(
        'div.bg-primary.text-white.text-center.fw-bold, .bg-primary.text-white.text-center.fw-bold'
      )).filter(visible);
      const result = [];
      for (const table of calendarTables) {
        const grid = table.closest('.rz-data-grid') || table;
        const previous = headings.filter(h => (h.compareDocumentPosition(grid) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0);
        const heading = previous.length ? (previous[previous.length - 1].innerText || '') : '';
        const match = heading.match(/Giornata\s*n\.?\s*(\d+)/i);
        const rows = Array.from(table.querySelectorAll('tbody tr')).filter(row => visible(row) && rowHasMatch(row));
        for (const row of rows) {
          const cells = Array.from(row.querySelectorAll(':scope > td')).map(td => (td.innerText || '').trim());
          result.push({ cells, round: match ? Number(match[1]) : 0 });
        }
      }
      return result;
    }""")

    print(f"SOL: righe calendario DOM individuate per Girone {group_letter}: {len(raw_rows)}", flush=True)
    matches: list[dict[str, Any]] = []
    seen: set[tuple[int, str, str]] = set()
    fallback_round = 1
    for item in raw_rows:
        cells = item.get("cells") or []
        round_no = int(item.get("round") or 0) or fallback_round
        game = parse_match_cells(cells, round_no, group_id)
        if not game:
            continue
        key = (game["gameNo"], game["date"], game["home"])
        if key in seen:
            continue
        seen.add(key)
        matches.append(game)
        fallback_round = max(fallback_round, round_no)
    matches.sort(key=lambda x: (x["round"], x["gameNo"]))
    if raw_rows and len(matches) != len(raw_rows):
        raise RuntimeError(
            f"Girone {group_letter}: {len(matches)} partite interpretate su {len(raw_rows)} righe calendario visibili"
        )
    return matches


def make_snapshot(comp: dict[str, str], competition_id: str, season: str,
                  group_letter: str, group_id: str, matches: list[dict[str, Any]]) -> dict[str, Any]:
    teams = sorted({name for m in matches for name in (m["home"], m["away"]) if name}, key=str.casefold)
    completed = sum(1 for m in matches if m.get("result"))
    rounds = sorted({m["round"] for m in matches if m.get("round")})
    route = f"https://www.sol.milano.federvolley.it/calendarioris/{competition_id}/{season}"
    return {
        "competition": comp["name"],
        "phase": comp["phase"],
        "gender": comp["gender"],
        "group": f"Girone {group_letter}",
        "groupId": group_id,
        "competitionId": competition_id,
        "officialGroup": group_letter,
        "source": "sol-blazor",
        "sourceUrl": route,
        "snapshotDate": datetime.now(timezone.utc).date().isoformat(),
        "teams": teams,
        "matches": matches,
        "counts": {
            "totalMatches": len(matches),
            "completedMatches": completed,
            "upcomingMatches": len(matches) - completed,
            "rounds": len(rounds),
        },
        "teamLogos": {},
    }


async def sync_sol(committee: dict[str, Any]) -> tuple[int, int, list[str]]:
    """Sincronizzazione completa e transazionale di tutti i campionati SOL."""
    previous_groups = committee.get("groups", [])
    previous_snapshots = committee.get("groupSnapshots", {})
    previous_by_code = {
        clean(c.get("code")): c for c in previous_groups
        if isinstance(c, dict) and clean(c.get("code"))
    }
    warnings: list[str] = []
    staged_competitions: list[dict[str, Any]] = []
    staged_snapshots: dict[str, dict[str, Any]] = {}
    groups_updated = 0

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            locale="it-IT",
            timezone_id="Europe/Rome",
            viewport={"width": 1440, "height": 1200},
        )
        page = await context.new_page()
        page.set_default_timeout(ELEMENT_TIMEOUT_MS)
        try:
            await open_selection_page(page)
            competitions = await read_competitions(page, previous_groups)
            print(f"SOL: individuate {len(competitions)} competizioni nella pagina pubblica", flush=True)

            for comp_index, comp in enumerate(competitions, start=1):
                code = comp["code"]
                old = previous_by_code.get(code, {})
                print(
                    f"[{comp_index}/{len(competitions)}] {code} · {comp['name']} · {comp['phase']}",
                    flush=True,
                )
                try:
                    # Si riapre l'elenco stagione per ogni competizione: il
                    # click della riga porta a una route differente e non si
                    # dipende dal pulsante Back dell'interfaccia.
                    await open_selection_page(page)
                    competition_id, season, route_url = await click_competition(page, code)
                    tab_letters = await discover_group_tabs(page)
                    if not tab_letters:
                        raise RuntimeError(
                            f"SOL {code}: nessuna scheda-girone rilevata nella route {route_url}"
                        )
                    print(
                        f"  ID SOL {competition_id}; gironi rilevati ({len(tab_letters)}): "
                        f"{', '.join(tab_letters)}",
                        flush=True,
                    )

                    group_entries: list[dict[str, Any]] = []
                    for group_letter in tab_letters:
                        group_id = f"SOL-{competition_id}-{group_letter}"
                        await open_group_calendar(page, route_url, group_letter)
                        matches = await scrape_calendar_rows(page, group_letter, group_id)
                        if not matches:
                            raise RuntimeError(
                                f"{code} Girone {group_letter}: nessuna partita valida acquisita"
                            )
                        if len({m["gameNo"] for m in matches}) != len(matches):
                            raise RuntimeError(
                                f"{code} Girone {group_letter}: numeri gara duplicati nello snapshot"
                            )

                        snapshot = make_snapshot(
                            comp, competition_id, season, group_letter, group_id, matches
                        )
                        snapshot["sourceUrl"] = route_url
                        staged_snapshots[group_id] = snapshot
                        group_entries.append({
                            "id": group_id,
                            "name": f"Girone {group_letter}",
                            "label": f"Girone {group_letter}",
                            "officialGroup": group_letter,
                            "competitionId": competition_id,
                            "sourceUrl": route_url,
                        })
                        print(
                            f"    {group_letter}: {len(matches)} gare · "
                            f"{len(snapshot['teams'])} squadre · "
                            f"{snapshot['counts']['rounds']} giornate",
                            flush=True,
                        )

                    # Tutti i gironi di questa competizione sono stati letti.
                    staged_competitions.append({
                        **(old if isinstance(old, dict) else {}),
                        "code": code,
                        "name": comp["name"],
                        "phase": comp["phase"],
                        "gender": comp["gender"],
                        "sourceId": competition_id,
                        "season": season,
                        "groups": group_entries,
                    })
                    groups_updated += len(group_entries)
                except Exception as exc:
                    detail = f"{code}: {type(exc).__name__}: {exc}"
                    warnings.append(detail)
                    # Diagnostica allegata agli artefatti GitHub Actions.
                    safe_code = re.sub(r"[^A-Za-z0-9_-]", "_", code)
                    try:
                        await page.screenshot(
                            path=str(ROOT / f"sol-debug-{safe_code}.png"),
                            full_page=True,
                        )
                        (ROOT / f"sol-debug-{safe_code}.html").write_text(
                            await page.content(), encoding="utf-8"
                        )
                    except Exception as debug_exc:
                        print(f"  Diagnostica {code} non disponibile: {debug_exc}", flush=True)
                    raise RuntimeError(
                        f"Sincronizzazione Milano interrotta su {code}; "
                        "nessuno snapshot SOL è stato sostituito. " + detail
                    ) from exc
        finally:
            await context.close()
            await browser.close()

    if len(staged_competitions) != len(competitions):
        raise RuntimeError(
            f"SOL incompleta: {len(staged_competitions)}/{len(competitions)} campionati acquisiti"
        )
    if groups_updated != len(staged_snapshots):
        raise RuntimeError(
            f"SOL incompleta: {groups_updated} gironi rilevati ma "
            f"{len(staged_snapshots)} calendari acquisiti"
        )
    if groups_updated == 0:
        raise RuntimeError("SOL non ha restituito alcun girone; catalogo precedente conservato")

    # Conserva eventuali competizioni storiche non mostrate dal portale, ma
    # non sovrascrive né rimuove snapshot esistenti fino alla validazione totale.
    current_codes = {item["code"] for item in staged_competitions}
    for old in previous_groups:
        if isinstance(old, dict) and clean(old.get("code")) not in current_codes:
            staged_competitions.append(old)

    new_snapshots = dict(previous_snapshots) if isinstance(previous_snapshots, dict) else {}
    new_snapshots.update(staged_snapshots)

    # COMMIT unico: da qui in avanti tutti i campionati e tutti i gironi sono
    # stati acquisiti. Prima di questo punto il JSON resta invariato.
    committee["groups"] = staged_competitions
    committee["groupSnapshots"] = new_snapshots
    committee["source"] = "sol-blazor-snapshot"
    committee["season"] = "2026/2027"
    committee["snapshotDate"] = datetime.now(timezone.utc).date().isoformat()
    committee["snapshotCapturedAt"] = datetime.now(timezone.utc).isoformat()
    committee["snapshotNote"] = (
        f"Sincronizzazione completa SOL: {len(competitions)} campionati, "
        f"{groups_updated} gironi e {len(staged_snapshots)} calendari acquisiti. "
        "Ogni calendario è stato letto dalla scheda-girone già renderizzata; "
        "il comando di stampa 'Calendario' non viene cliccato."
    )
    print(
        f"SOL riepilogo: {len(competitions)} campionati, {groups_updated} gironi, "
        f"{len(staged_snapshots)} calendari aggiornati.",
        flush=True,
    )
    return groups_updated, len(staged_snapshots), warnings


def main() -> int:
    if not CATALOG_PATH.exists():
        print(f"ERRORE: catalogo assente: {CATALOG_PATH}", file=sys.stderr)
        return 1
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    committees = catalog.get("committees", [])
    committee = next((c for c in committees if str(c.get("id")) == "LM06"), None)
    if not committee:
        print("ERRORE: Comitato LM06 (Milano-Monza-Lecco) non presente nel catalogo", file=sys.stderr)
        return 1
    try:
        groups, snapshots, warnings = asyncio.run(sync_sol(committee))
    except Exception as exc:
        print(f"ERRORE: sincronizzazione SOL non completata: {exc}", file=sys.stderr)
        print("I dati SOL già presenti nel catalogo sono stati conservati; il job deve risultare FALLITO.", file=sys.stderr)
        return 1

    catalog["snapshotDate"] = datetime.now(timezone.utc).date().isoformat()
    catalog["version"] = "volley-live-v11.3.11-sol-all-milano-tabs-no-print-click"
    tmp_path = CATALOG_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(CATALOG_PATH)
    if warnings:
        print(f"SOL: {len(warnings)} avvisi. I dati validi e gli snapshot precedenti sono stati conservati.")
    print(f"SOL: sincronizzazione completa; tutti i campionati e i {groups} gironi aggiornati ({snapshots} calendari).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
