# HOTFIX SOL: readiness su tab Squadre + gironi (AST V2)
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
import os
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
SOL_READY_TIMEOUT_MS = 90_000


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


async def wait_for_selection_ready(page, websocket_state: dict[str, Any] | None = None) -> None:
    """Ripristina la sessione SOL e seleziona 2026/2027 prima di leggere la griglia.

    La pagina SOL interroga localStorage.getItem("UserSession") durante
    l'inizializzazione Blazor Server. Il browser reale che ha prodotto il HAR
    aveva un valore già presente; il context Playwright effimero di GitHub
    Actions parte invece senza localStorage. La chiave viene quindi inizializzata
    da SOL_USER_SESSION (GitHub Actions secret), senza stamparne mai il valore.

    Il menu stagione può essere già selezionato dal server. Se resta sul
    placeholder, si apre il vero trigger Radzen e si sceglie l'opzione 2026/2027
    quando il server l'ha effettivamente renderizzata. Solo dopo si attende la
    griglia delle competizioni.
    """
    dropdown = page.locator("div.rz-dropdown:has(input#stagione)").first
    await dropdown.wait_for(state="visible", timeout=ELEMENT_TIMEOUT_MS)
    season_pattern = re.compile(r"2026\s*(?:/|-|–)\s*2027|2026\s*(?:/|-|–)\s*27", re.I)

    def _label_locator():
        return dropdown.locator(".rz-dropdown-label").first

    async def current_label() -> str:
        loc = _label_locator()
        return clean(await loc.inner_text()) if await loc.count() else ""

    label = await current_label()
    if not season_pattern.search(label):
        # Aspetta che il server abbia terminato il primo caricamento delle
        # opzioni oppure che abbia già impostato la stagione di default.
        try:
            await page.wait_for_function(
                r"""() => {
                    const d = document.querySelector('input#stagione')?.closest('.rz-dropdown');
                    const label = (d?.querySelector('.rz-dropdown-label')?.innerText || '').trim();
                    const options = Array.from(document.querySelectorAll(
                        '.rz-dropdown-panel .rz-dropdown-item, .rz-dropdown-panel [role="option"], .rz-dropdown-items .rz-dropdown-item'
                    ));
                    return /2026\s*(?:\/|-|–)\s*2027/i.test(label) ||
                        options.some(x => /2026\s*(?:\/|-|–)\s*2027/i.test(x.innerText || ''));
                }""",
                timeout=30_000,
            )
        except PlaywrightTimeoutError:
            pass

        label = await current_label()
        if not season_pattern.search(label):
            trigger = dropdown.locator(".rz-dropdown-trigger").first
            if await trigger.count() == 0:
                raise RuntimeError("SOL: trigger del menu stagione non trovato.")
            await trigger.click(timeout=15_000)

            try:
                await page.wait_for_function(
                    r"""() => Array.from(document.querySelectorAll(
                        '.rz-dropdown-panel .rz-dropdown-item, .rz-dropdown-panel [role="option"], .rz-dropdown-items .rz-dropdown-item'
                    )).some(x => /2026\s*(?:\/|-|–)\s*2027/i.test(x.innerText || ''))""",
                    timeout=30_000,
                )
            except PlaywrightTimeoutError as exc:
                details = await page.evaluate("""() => {
                    const input = document.querySelector('input#stagione');
                    const d = input?.closest('.rz-dropdown');
                    const all = Array.from(document.querySelectorAll(
                        '.rz-dropdown-panel, .rz-dropdown-items, .rz-dropdown-item, [role="option"]'
                    ));
                    const visible = el => !!(el && el.getClientRects().length &&
                        getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none');
                    return {
                        seasonValue: input?.value || '',
                        seasonLabel: d?.querySelector('.rz-dropdown-label')?.innerText?.trim() || '',
                        popup: all.filter(visible).slice(0, 20).map(x => ({
                            tag: x.tagName, cls: String(x.className || ''),
                            text: (x.innerText || '').trim().slice(0, 120)
                        })),
                        userSessionPresent: !!localStorage.getItem('UserSession'),
                        userSessionLength: (localStorage.getItem('UserSession') || '').length,
                        visibleSpinners: Array.from(document.querySelectorAll('.spinner')).filter(visible).length,
                        blazorError: (() => { const e=document.querySelector('#blazor-error-ui'); return e && visible(e) ? (e.innerText||'').trim() : ''; })(),
                        pageText: (document.body.innerText || '').trim().slice(0, 1200)
                    };
                }""")
                try:
                    await page.screenshot(path=str(ROOT / "sol-debug-season-options-empty.png"), full_page=True)
                    (ROOT / "sol-debug-season-options-empty.html").write_text(
                        await page.content(), encoding="utf-8"
                    )
                except Exception:
                    pass
                frames = (websocket_state or {}).get("received", "n/d")
                raise RuntimeError(
                    "SOL: il server non ha renderizzato l'opzione 2026/2027. "
                    f"Frame WebSocket ricevuti={frames}; DOM={details}. "
                    "Verificare il segreto SOL_USER_SESSION; nessun dato è stato modificato."
                ) from exc

            options = page.locator(
                '.rz-dropdown-panel .rz-dropdown-item, '
                '.rz-dropdown-panel [role="option"], '
                '.rz-dropdown-items .rz-dropdown-item'
            )
            option_index = None
            for i in range(await options.count()):
                if season_pattern.search(clean(await options.nth(i).inner_text())):
                    option_index = i
                    break
            if option_index is None:
                raise RuntimeError("SOL: opzione 2026/2027 rilevata ma non individuabile nel menu Radzen.")
            await options.nth(option_index).click(timeout=15_000)

    try:
        await page.wait_for_function(
            r"""() => {
                const d = document.querySelector('input#stagione')?.closest('.rz-dropdown');
                const label = (d?.querySelector('.rz-dropdown-label')?.innerText || '').trim();
                const rows = Array.from(document.querySelectorAll(
                    'table.rz-grid-table tbody tr.rz-data-row'
                )).filter(row => row.getClientRects().length > 0);
                return /2026\s*(?:\/|-|–)\s*2027/i.test(label) && rows.length >= 18;
            }""",
            timeout=SOL_READY_TIMEOUT_MS,
        )
    except PlaywrightTimeoutError as exc:
        details = await page.evaluate("""() => {
            const input = document.querySelector('input#stagione');
            const root = input?.closest('.rz-dropdown');
            const rows = Array.from(document.querySelectorAll(
                'table.rz-grid-table tbody tr.rz-data-row'
            )).filter(row => row.getClientRects().length > 0);
            const visible = el => !!(el && el.getClientRects().length &&
                getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none');
            const err = document.querySelector('#blazor-error-ui');
            return {
                url: location.href,
                readyState: document.readyState,
                seasonValue: input?.value || '',
                seasonLabel: root?.querySelector('.rz-dropdown-label')?.innerText?.trim() || '',
                dropdownOptions: document.querySelectorAll(
                    '.rz-dropdown-panel .rz-dropdown-item, .rz-dropdown-panel [role="option"], .rz-dropdown-items .rz-dropdown-item'
                ).length,
                competitionRows: rows.length,
                visibleSpinners: Array.from(document.querySelectorAll('.spinner')).filter(visible).length,
                userSessionPresent: !!localStorage.getItem('UserSession'),
                userSessionLength: (localStorage.getItem('UserSession') || '').length,
                blazorError: err && visible(err) ? (err.innerText || '').trim() : '',
                gridHeaders: Array.from(document.querySelectorAll('table.rz-grid-table thead'))
                    .map(x => (x.innerText || '').trim()).slice(0, 2),
                pageText: (document.body.innerText || '').trim().slice(0, 1800)
            };
        }""")
        try:
            await page.screenshot(path=str(ROOT / "sol-debug-selection-not-ready.png"), full_page=True)
            (ROOT / "sol-debug-selection-not-ready.html").write_text(
                await page.content(), encoding="utf-8"
            )
        except Exception:
            pass
        frames = (websocket_state or {}).get("received", "n/d")
        errors = (websocket_state or {}).get("errors", [])
        raise RuntimeError(
            "SOL: catalogo 2026/2027 non disponibile dopo il ripristino della sessione. "
            f"Frame WebSocket ricevuti={frames}; errori pagina={errors}; DOM={details}. "
            "Catalogo precedente conservato."
        ) from exc

    state = await page.evaluate("""() => {
        const root = document.querySelector('input#stagione')?.closest('.rz-dropdown');
        const label = root?.querySelector('.rz-dropdown-label')?.innerText?.trim() || '';
        const rows = Array.from(document.querySelectorAll(
            'table.rz-grid-table tbody tr.rz-data-row'
        )).filter(row => row.getClientRects().length > 0);
        return {season: label, competitions: rows.length};
    }""")
    frames = (websocket_state or {}).get("received")
    suffix = f"; frame WebSocket ricevuti={frames}" if frames is not None else ""
    print(
        f"SOL: catalogo Blazor pronto: stagione {state['season']}, "
        f"{state['competitions']} competizioni{suffix}.",
        flush=True,
    )


async def wait_for_competition_route_ready(page) -> None:
    \
    """Attende che il dettaglio SOL abbia renderizzato Squadre e i gironi.

    Il calendario non è ancora presente all'apertura della route: viene
    caricato soltanto dopo la selezione di una scheda-girone.
    """
    try:
        await page.wait_for_function(
            r"""() => {
                const visible = el => !!(el && el.getClientRects().length &&
                    getComputedStyle(el).display !== 'none' &&
                    getComputedStyle(el).visibility !== 'hidden' && !el.hidden);
                const routeOk = /\/calendarioris\/\d+\/\d+\/?$/.test(location.pathname);
                const tabs = Array.from(document.querySelectorAll('li[role="tab"]')).filter(visible);
                const labels = tabs.map(el => (el.innerText || '').trim().replace(/\s+/g, ' ').toUpperCase());
                const hasTeams = labels.includes('SQUADRE');
                const hasGroup = labels.some(label => /^[A-Z][A-Z0-9]{0,2}$/.test(label));
                const spinnerVisible = Array.from(document.querySelectorAll('.spinner')).some(visible);
                const errorBox = document.querySelector('#blazor-error-ui');
                const blazorErrorVisible = visible(errorBox) && /unhandled error/i.test(errorBox.innerText || '');
                return routeOk && hasTeams && hasGroup && !spinnerVisible && !blazorErrorVisible;
            }""",
            timeout=ELEMENT_TIMEOUT_MS,
        )
    except Exception as exc:
        try:
            state = await page.evaluate(r"""() => {
                const visible = el => !!(el && el.getClientRects().length &&
                    getComputedStyle(el).display !== 'none' &&
                    getComputedStyle(el).visibility !== 'hidden' && !el.hidden);
                return {
                    url: location.href,
                    readyState: document.readyState,
                    tabs: Array.from(document.querySelectorAll('li[role="tab"]')).filter(visible).map(el => (el.innerText || '').trim()),
                    spinnerVisible: Array.from(document.querySelectorAll('.spinner')).some(visible),
                    blazorErrorVisible: visible(document.querySelector('#blazor-error-ui')),
                    bodyTitle: (document.querySelector('.rz-panel-titlebar h6')?.innerText || '').trim()
                };
            }""")
        except Exception as diagnostic_exc:
            state = {"diagnosticError": str(diagnostic_exc)}
        raise RuntimeError(
            f"SOL: pagina dettaglio non pronta prima della selezione del girone; "
            f"stato={state}; causa={exc}"
        ) from exc

    print("SOL: pagina dettaglio pronta (tab Squadre e schede-girone renderizzate).", flush=True)


async def open_selection_page(page) -> None:
    print("SOL: apertura elenco campionati...", flush=True)
    state: dict[str, Any] = {"seen": False, "received": 0, "errors": []}

    def on_websocket(ws) -> None:
        if "/_blazor" not in ws.url:
            return
        state["seen"] = True

        def on_frame(_payload) -> None:
            state["received"] += 1

        ws.on("framereceived", on_frame)

    def on_page_error(error) -> None:
        state["errors"].append(f"pageerror: {str(error)[:300]}")

    def on_console(message) -> None:
        if message.type == "error":
            state["errors"].append(f"console: {message.text[:300]}")

    page.on("websocket", on_websocket)
    page.on("pageerror", on_page_error)
    page.on("console", on_console)
    try:
        await page.goto(SOL_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        await wait_for_selection_ready(page, state)
    finally:
        page.remove_listener("websocket", on_websocket)
        page.remove_listener("pageerror", on_page_error)
        page.remove_listener("console", on_console)

    await wait_for_rows(page)
    row_count = await page.locator("table.rz-grid-table tbody tr.rz-data-row").count()
    if row_count < EXPECTED_COMPETITIONS:
        raise RuntimeError(
            f"SOL: elenco competizioni incompleto dopo il render Blazor: "
            f"{row_count}/{EXPECTED_COMPETITIONS} righe."
        )
    print(
        f"SOL: elenco campionati pronto ({row_count} righe; "
        f"WebSocket={'connesso' if state['seen'] else 'non rilevato'}, "
        f"frame={state['received']}).",
        flush=True,
    )


# HOTFIX SOL: identificazione univoca tramite riga e sourceId
async def read_competitions(page, existing: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Legge tutte le righe SOL; la sigla non è trattata come chiave univoca."""
    rows = page.locator("tr.rz-data-row")
    known_by_code: dict[str, list[dict[str, Any]]] = {}
    known_codes: dict[str, str] = {}
    for item in existing:
        if not isinstance(item, dict):
            continue
        known_code = clean(item.get("code"))
        if known_code:
            known_by_code.setdefault(known_code.casefold(), []).append(item)
            known_codes.setdefault(known_code.casefold(), known_code)

    out: list[dict[str, str]] = []
    for index in range(await rows.count()):
        row = rows.nth(index)
        if not await row.is_visible():
            continue
        cells = [clean(x) for x in await row.locator(":scope > td").all_inner_texts()]
        if len(cells) < 3:
            continue

        code = clean(cells[1]) if len(cells) > 1 else ""
        if not re.fullmatch(r"[A-Za-z0-9]{2,5}", code):
            row_text = clean(" ".join(cells))
            code = next((candidate for candidate in known_codes.values()
                         if re.search(rf"(?<![A-Za-z0-9]){re.escape(candidate)}(?![A-Za-z0-9])", row_text, re.I)), "")
        if not code:
            continue

        name = clean(cells[2]) if len(cells) > 2 else ""
        phase = clean(cells[3]) if len(cells) > 3 else ""
        old_candidates = known_by_code.get(code.casefold(), [])
        exact_old = [item for item in old_candidates
                     if (not name or clean(item.get("name")).casefold() == name.casefold())
                     and (not phase or clean(item.get("phase")).casefold() == phase.casefold())]
        old = exact_old[0] if len(exact_old) == 1 else (
            old_candidates[0] if len(old_candidates) == 1 else {}
        )

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

        # L'indice identifica la riga concreta anche quando la sigla è condivisa.
        out.append({
            "code": code,
            "name": name,
            "phase": phase,
            "gender": gender,
            "_rowIndex": str(index),
        })

    expected_min = max(EXPECTED_COMPETITIONS, len(existing))
    if len(out) < expected_min:
        raise RuntimeError(
            f"SOL: pagina campionati incompleta: lette {len(out)} competizioni; "
            f"attese almeno {expected_min}. Codici rilevati: {[x['code'] for x in out]}"
        )
    return out


async def click_competition(page, comp: dict[str, str]) -> tuple[str, str, str]:
    """Apre la riga SOL esatta, verificandone sigla, denominazione e fase."""
    code = clean(comp.get("code"))
    name = clean(comp.get("name"))
    phase = clean(comp.get("phase"))
    try:
        row_index = int(comp.get("_rowIndex", "-1"))
    except (TypeError, ValueError):
        row_index = -1

    rows = page.locator("tr.rz-data-row")

    async def row_matches(row) -> bool:
        if not await row.is_visible():
            return False
        cells = row.locator(":scope > td")
        if await cells.count() < 3:
            return False
        actual_code = clean(await cells.nth(1).inner_text())
        actual_name = clean(await cells.nth(2).inner_text())
        actual_phase = clean(await cells.nth(3).inner_text()) if await cells.count() > 3 else ""
        return (
            actual_code.casefold() == code.casefold()
            and actual_name.casefold() == name.casefold()
            and (not phase or actual_phase.casefold() == phase.casefold())
        )

    target = None
    count = await rows.count()
    if 0 <= row_index < count:
        candidate = rows.nth(row_index)
        if await row_matches(candidate):
            target = candidate

    # Se SOL ha riordinato la griglia dopo il render, usa la tripla completa,
    # mai la sola sigla. Se resta ambiguità, interrompe senza scegliere a caso.
    if target is None:
        matches = []
        for index in range(count):
            candidate = rows.nth(index)
            if await row_matches(candidate):
                matches.append(candidate)
        if len(matches) == 1:
            target = matches[0]
        elif not matches:
            raise RuntimeError(
                f"SOL: riga non trovata per {code} · {name} · {phase} (indice originario {row_index})"
            )
        else:
            raise RuntimeError(
                f"SOL: più righe identiche per {code} · {name} · {phase}; "
                "impossibile scegliere in sicurezza."
            )

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
        raise RuntimeError(
            f"SOL: route calendario non riconosciuta dopo il click su {code} · {name}: {page.url}"
        )
    competition_id, season = match.group(1), match.group(2)
    route_url = f"https://www.sol.milano.federvolley.it/calendarioris/{competition_id}/{season}"
    await wait_for_competition_route_ready(page)
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


# HOTFIX SOL: accetta Calendario Provvisorio (AST V1)
async def open_group_calendar(page, route_url: str, group_letter: str) -> None:
    """Seleziona la scheda del girone e attende il calendario già renderizzato.

    Evidenza DOM verificata: la pagina mostra già "Calendario Ufficiale" e le
    righe delle partite dopo il render della scheda. Il pulsante
    button[title="Calendario"] include l'icona "print" ed è quindi un comando
    di stampa: NON va cliccato per accedere alle partite.
    """
    await page.goto(route_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    await wait_for_competition_route_ready(page)
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
        "   return hasRows && (hasHeaders || /(?:calendario ufficiale|calendario provvisorio)/i.test(body));"
        " });"
        " return groupHeading && /(?:calendario ufficiale|calendario provvisorio)/i.test(body) && ok;"
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
    def comp_identity(item: dict[str, Any]) -> tuple[str, str, str]:
        return tuple(clean(item.get(key)).casefold() for key in ("code", "name", "phase"))

    previous_by_source_id: dict[str, dict[str, Any]] = {}
    previous_by_identity: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for item in previous_groups:
        if not isinstance(item, dict):
            continue
        source_id = clean(item.get("sourceId"))
        if source_id:
            previous_by_source_id[source_id] = item
        previous_by_identity.setdefault(comp_identity(item), []).append(item)
    warnings: list[str] = []
    staged_competitions: list[dict[str, Any]] = []
    staged_snapshots: dict[str, dict[str, Any]] = {}
    groups_updated = 0

    sol_user_session = os.environ.get("SOL_USER_SESSION", "").strip()
    if not sol_user_session:
        raise RuntimeError(
            "Segreto GitHub SOL_USER_SESSION mancante. Il portale SOL legge "
            "localStorage.UserSession durante l'avvio Blazor; il browser GitHub Actions "
            "è effimero e non eredita la sessione del browser personale. "
            "Configurare SOL_USER_SESSION nelle Actions secrets."
        )

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
        # Non stampare né salvare il token. Viene inizializzato prima di ogni script
        # della pagina e soltanto per l'origine ufficiale SOL.
        origin_json = json.dumps("https://www.sol.milano.federvolley.it")
        token_json = json.dumps(sol_user_session)
        await context.add_init_script(
            script=(
                "(() => { if (location.origin === " + origin_json + ") { "
                "try { localStorage.setItem('UserSession', " + token_json + "); } catch (_) {} "
                "} })();"
            )
        )
        print(
            f"SOL: sessione browser predisposta da secret GitHub (UserSession presente; "
            f"lunghezza={len(sol_user_session)} caratteri; valore non registrato).",
            flush=True,
        )
        page = await context.new_page()
        page.set_default_timeout(ELEMENT_TIMEOUT_MS)
        try:
            await open_selection_page(page)
            competitions = await read_competitions(page, previous_groups)
            print(f"SOL: individuate {len(competitions)} competizioni nella pagina pubblica", flush=True)

            for comp_index, comp in enumerate(competitions, start=1):
                code = comp["code"]
                identity = comp_identity(comp)
                old_candidates = previous_by_identity.get(identity, [])
                old = old_candidates[0] if len(old_candidates) == 1 else {}
                print(
                    f"[{comp_index}/{len(competitions)}] {code} · {comp['name']} · {comp['phase']}",
                    flush=True,
                )
                try:
                    # Si riapre l'elenco stagione per ogni competizione: il
                    # click della riga porta a una route differente e non si
                    # dipende dal pulsante Back dell'interfaccia.
                    await open_selection_page(page)
                    competition_id, season, route_url = await click_competition(page, comp)
                    old = previous_by_source_id.get(competition_id, old)
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
    current_source_ids = {clean(item.get("sourceId")) for item in staged_competitions if clean(item.get("sourceId"))}
    current_identities = {comp_identity(item) for item in staged_competitions}
    for old in previous_groups:
        if not isinstance(old, dict):
            continue
        old_source_id = clean(old.get("sourceId"))
        if old_source_id:
            preserve_old = old_source_id not in current_source_ids
        else:
            preserve_old = comp_identity(old) not in current_identities
        if preserve_old:
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
    catalog["version"] = "volley-live-v11.3.14-sol-duplicate-codes-by-row-and-source-id"
    tmp_path = CATALOG_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(CATALOG_PATH)
    if warnings:
        print(f"SOL: {len(warnings)} avvisi. I dati validi e gli snapshot precedenti sono stati conservati.")
    print(f"SOL: sincronizzazione completa; tutti i campionati e i {groups} gironi aggiornati ({snapshots} calendari).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
