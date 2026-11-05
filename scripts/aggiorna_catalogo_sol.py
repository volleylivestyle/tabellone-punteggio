#!/usr/bin/env python3
"""Sincronizza il Comitato Territoriale Milano-Monza-Lecco dal portale SOL.

SOL è un'applicazione Blazor Server. Il suo catalogo/calendario non è esposto
come un feed REST pubblico equivalente a FIPAVOnline: la sincronizzazione
interagisce con le pagine pubbliche SOL tramite Chromium/Playwright, legge i
campionati, le schede dei gironi e le tabelle del calendario ufficiale.

Regole di sicurezza dei dati:
- se il portale non risponde, non cancella alcun dato già acquisito;
- una competizione viene aggiornata soltanto dopo aver identificato il suo ID
  tecnico nella route ufficiale /calendarioris/{id}/{stagione};
- un calendario viene sostituito soltanto se sono state lette partite valide;
- gli ID dei gironi sono chiavi locali stabili (SOL-{idCampionato}-{girone}),
  non ID ufficiali SOL.
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
EXPECTED_COMPETITIONS = 18
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
    """Seleziona 2026/2027 dal Radzen DropDown, senza dipendere dagli ID runtime."""
    dropdown = page.locator("div.rz-dropdown:has(input#stagione)").first
    await dropdown.wait_for(state="visible", timeout=ELEMENT_TIMEOUT_MS)
    label_locator = dropdown.locator(".rz-dropdown-label").first
    season_pattern = re.compile(r"2026\s*(?:/|-|–)\s*2027|2026\s*(?:/|-|–)\s*27", re.I)

    current_label = clean(await label_locator.inner_text()) if await label_locator.count() else ""
    if season_pattern.search(current_label):
        print(f"SOL: stagione già selezionata: {current_label}", flush=True)
        return

    # Radzen inizialmente renderizza il popup dentro il dropdown, ma quando
    # Radzen.openPopup viene eseguito lo sposta con document.body.appendChild.
    # Per questo il locator relativo al dropdown smette di trovare il pannello
    # proprio dopo il click. Ricaviamo l'ID DAL DOM corrente dopo il primo
    # render Blazor e poi usiamo un locator globale, valido prima e dopo lo
    # spostamento del popup nel body.
    nested_panel = dropdown.locator(".rz-dropdown-panel").first
    await nested_panel.wait_for(state="attached", timeout=ELEMENT_TIMEOUT_MS)
    root_id = await dropdown.get_attribute("id") or "(assente)"
    panel_id = await nested_panel.get_attribute("id") or ""
    if not panel_id:
        if root_id == "(assente)":
            raise RuntimeError("SOL: impossibile ricavare l'ID dinamico del dropdown stagione.")
        panel_id = f"popup-{root_id}"
    panel = page.locator(f'[id="{panel_id}"]').first
    await panel.wait_for(state="attached", timeout=ELEMENT_TIMEOUT_MS)
    print(
        f"SOL: dropdown stagione pronto; root={root_id}, popup={panel_id} "
        "(locator globale: Radzen sposta il popup in document.body)",
        flush=True,
    )

    async def open_menu() -> None:
        trigger = dropdown.locator(".rz-dropdown-trigger").first
        await trigger.click(timeout=15_000)
        try:
            await panel.wait_for(state="visible", timeout=8_000)
            return
        except PlaywrightTimeoutError:
            pass
        # Fallback 1: etichetta visibile del componente.
        await dropdown.locator(".rz-dropdown-label").first.click(timeout=10_000)
        try:
            await panel.wait_for(state="visible", timeout=8_000)
            return
        except PlaywrightTimeoutError:
            pass
        # Fallback 2: comportamento da tastiera accessibile Radzen.
        await dropdown.focus()
        await dropdown.press("ArrowDown")
        await panel.wait_for(state="visible", timeout=12_000)

    await open_menu()
    popup_state = await page.evaluate(
        "(id) => { const p=document.getElementById(id); return p ? "
        "{display:getComputedStyle(p).display, parent:p.parentElement?.tagName, "
        "open:p.classList.contains('rz-open'), text:(p.innerText||'').trim().slice(0,300)} : null; }",
        panel_id,
    )
    print(f"SOL: stato popup stagione dopo apertura: {popup_state}", flush=True)
    options = panel.locator(".rz-dropdown-item, [role='option']")
    try:
        await options.first.wait_for(state="visible", timeout=12_000)
    except PlaywrightTimeoutError:
        option_labels = [clean(x) for x in await options.all_inner_texts()]
        try:
            await page.screenshot(path=str(ROOT / "sol-debug-season.png"), full_page=True)
            (ROOT / "sol-debug-season.html").write_text(await page.content(), encoding="utf-8")
        except Exception:
            pass
        raise RuntimeError(
            "Il menu stagione SOL si è aperto ma non contiene opzioni visibili. "
            f"root={root_id}, popup={panel_id}, opzioni={option_labels or 'nessuna'}."
        )

    option_labels = [clean(x) for x in await options.all_inner_texts()]
    print(f"SOL: opzioni stagione disponibili ({len(option_labels)}): {option_labels}", flush=True)
    target_index = next((i for i, value in enumerate(option_labels) if season_pattern.search(value)), None)
    if target_index is None:
        target_index = next((i for i, value in enumerate(option_labels)
                             if re.search(r"^\s*2026(?:\D|$)", value)), None)
    if target_index is None:
        try:
            await page.screenshot(path=str(ROOT / "sol-debug-season.png"), full_page=True)
            (ROOT / "sol-debug-season.html").write_text(await page.content(), encoding="utf-8")
        except Exception:
            pass
        raise RuntimeError(
            "Stagione 2026/2027 non trovata nel menu SOL. "
            f"Opzioni visibili: {option_labels or 'nessuna'}. Catalogo precedente conservato."
        )

    # La locator è relativa al popup vivo, quindi resta valida anche se Blazor
    # sostituisce il markup durante il ciclo di render.
    await options.nth(target_index).click(timeout=15_000)
    await page.wait_for_function(
        "() => { const d=document.querySelector('input#stagione')?.closest('.rz-dropdown'); "
        "const t=d?.querySelector('.rz-dropdown-label')?.innerText || ''; "
        "const rows=Array.from(document.querySelectorAll('tr.rz-data-row')).filter(r=>r.getClientRects().length); "
        "return /2026/.test(t) && rows.length > 0; }",
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
    rows = await page.locator("tr.rz-data-row").all()
    known_by_code = {clean(c.get("code")): c for c in existing if clean(c.get("code"))}
    out: list[dict[str, str]] = []
    for index, row in enumerate(rows):
        cells = [clean(x) for x in await row.locator(":scope > td").all_inner_texts()]
        text = clean(" ".join(cells))
        code = next((token for token in re.findall(r"\b[A-Z0-9]{2,4}\b", text)
                     if token in known_by_code), "")
        if not code:
            # Le sigle può essere già presente come label di prima colonna.
            for candidate in known_by_code:
                if re.search(rf"\b{re.escape(candidate)}\b", text):
                    code = candidate
                    break
        if not code:
            continue
        old = known_by_code[code]
        name = clean(cells[2]) if len(cells) > 2 else clean(old.get("name"))
        phase = clean(cells[3]) if len(cells) > 3 else clean(old.get("phase"))
        # L'ordine delle colonne è icona-genere, sigla, campionato, fase.
        if not name or name == code:
            name = clean(old.get("name"))
        if not phase:
            phase = clean(old.get("phase"))
        out.append({
            "code": code,
            "name": name,
            "phase": phase,
            "gender": clean(old.get("gender")) or ("femminile" if "Femminile" in name else "maschile"),
            "_rowIndex": str(index),
        })
    # Fallback: non facciamo scomparire le competizioni storiche se la griglia
    # del portale restituisce momentaneamente solo una pagina parziale.
    if len(out) < min(12, len(existing)):
        raise RuntimeError(f"SOL ha restituito soltanto {len(out)} competizioni: catalogo non aggiornato")
    return out


async def click_competition(page, row_index: int) -> tuple[str, str]:
    rows = page.locator("tr.rz-data-row")
    row = rows.nth(row_index)
    try:
        await row.locator(":scope > td").nth(2).click(timeout=10_000)
    except Exception:
        await row.click(timeout=10_000)
    await page.wait_for_function(
        "() => /\\/calendarioris\\/\\d+\\/\\d+/.test(location.pathname)",
        timeout=ELEMENT_TIMEOUT_MS,
    )
    m = re.search(r"/calendarioris/(\d+)/(\d+)", page.url)
    if not m:
        raise RuntimeError(f"SOL non ha aperto la scheda del campionato (URL: {page.url})")
    return m.group(1), m.group(2)


async def discover_group_tabs(page) -> list[str]:
    """Individua i gironi nelle schede SOL A/B/C... dopo il render Blazor.

    L'HAR del 04/11 mostra le schede A-F come <li> in .rz-tabview-nav.
    L'elenco Squadre contiene inoltre una tabella Radzen con la colonna Gir.
    che costituisce un fallback affidabile se le schede non sono ancora visibili.
    """
    try:
        await page.wait_for_function(
            "() => Array.from(document.querySelectorAll('.rz-tabview-nav > li'))"
            ".some(el => /^[A-Z0-9]{1,3}$/.test((el.innerText || '').trim().toUpperCase()))",
            timeout=12_000,
        )
    except PlaywrightTimeoutError:
        # Si può ancora ricavare il gruppo dalla tabella Squadre.
        pass

    groups: list[str] = []
    tabs = page.locator(".rz-tabview-nav > li, ul[role='tablist'] > li, [role='tablist'] > li")
    for raw in await tabs.all_inner_texts():
        label = clean(raw).upper()
        match = re.fullmatch(r"(?:GIRONE\s*)?([A-Z0-9]{1,3})", label)
        if match:
            group = match.group(1)
            if group not in groups:
                groups.append(group)

    # Fallback: la tabella Squadre mostra le colonne Logo, Gir., Squadra,
    # Società e Comune. Le righe non hanno la classe rz-data-row.
    if not groups:
        tables = page.locator("table.rz-grid-table")
        for ti in range(await tables.count()):
            table = tables.nth(ti)
            headers = [clean(x).lower() for x in await table.locator("thead th").all_inner_texts()]
            if not any("gir" in h for h in headers) or not any("squadra" in h for h in headers):
                continue
            for row in await table.locator("tbody tr:not(.rz-datatable-emptymessage-row)").all():
                cells = [clean(x) for x in await row.locator(":scope > td").all_inner_texts()]
                if len(cells) >= 3 and re.fullmatch(r"[A-Z0-9]{1,3}", cells[1].upper()):
                    letter = cells[1].upper()
                    if letter not in groups:
                        groups.append(letter)
        groups.sort()

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
    """Apre una scheda girone e poi il relativo Calendario ufficiale."""
    await page.goto(route_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    tab_selector = ".rz-tabview-nav > li"
    tab = page.locator(tab_selector).filter(
        has_text=re.compile(rf"^\s*{re.escape(group_letter)}\s*$", re.I)
    ).first
    # La route cambia prima che Blazor completi il render delle schede A-F.
    await tab.wait_for(state="visible", timeout=ELEMENT_TIMEOUT_MS)
    await tab.click(timeout=15_000)
    await page.wait_for_function(
        "letter => Array.from(document.querySelectorAll('.rz-tabview-nav > li'))"
        ".some(el => el.classList.contains('rz-tabview-selected') && "
        "(el.innerText || '').trim().toUpperCase() === letter)",
        arg=group_letter.upper(),
        timeout=ELEMENT_TIMEOUT_MS,
    )

    calendar_button = page.locator("button.rz-button").filter(
        has_text=re.compile(r"^\s*Calendario\s*$", re.I)
    ).first
    await calendar_button.wait_for(state="visible", timeout=ELEMENT_TIMEOUT_MS)
    await calendar_button.click(timeout=15_000)
    print(f"SOL: attendo righe calendario per Girone {group_letter}...", flush=True)
    await page.wait_for_function(
        "() => document.body.innerText.includes('Calendario Ufficiale') && "
        "Array.from(document.querySelectorAll('table.rz-grid-table tbody tr:not(.rz-datatable-emptymessage-row)'))"
        ".some(row => row.querySelectorAll(':scope > td.rz-cell-data').length >= 6 && "
        "!!(row.getClientRects().length && getComputedStyle(row).visibility !== 'hidden'))",
        timeout=ELEMENT_TIMEOUT_MS,
    )


async def scrape_calendar_rows(page, group_letter: str, group_id: str) -> list[dict[str, Any]]:
    raw_rows = await page.evaluate("""() => {
      const headers = Array.from(document.querySelectorAll('div.bg-primary.text-white.text-center.fw-bold'));
      const visible = el => !!(el && el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden');
      const rows = Array.from(document.querySelectorAll('table.rz-grid-table tbody tr:not(.rz-datatable-emptymessage-row)')).filter(row => visible(row) && row.querySelectorAll(':scope > td.rz-cell-data').length >= 6);
      return rows.map(row => {
        const cells = Array.from(row.querySelectorAll(':scope > td')).map(td => (td.innerText || '').trim());
        const grid = row.closest('.rz-data-grid') || row.closest('table');
        const previous = headers.filter(h => (h.compareDocumentPosition(grid) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0);
        const heading = previous.length ? (previous[previous.length - 1].innerText || '') : '';
        const match = heading.match(/Giornata\\s*n\\.?\\s*(\\d+)/i);
        return { cells, round: match ? Number(match[1]) : 0 };
      });
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
    previous_groups = committee.get("groups", [])
    previous_snapshots = committee.setdefault("groupSnapshots", {})
    previous_by_code = {clean(c.get("code")): c for c in previous_groups if isinstance(c, dict)}
    warnings: list[str] = []
    groups_updated = 0
    snapshots_updated = 0

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = await browser.new_context(locale="it-IT", timezone_id="Europe/Rome", viewport={"width": 1440, "height": 1200})
        page = await context.new_page()
        page.set_default_timeout(ELEMENT_TIMEOUT_MS)
        try:
            await open_selection_page(page)
            competitions = await read_competitions(page, previous_groups)
            print(f"SOL: individuate {len(competitions)} competizioni nella pagina pubblica", flush=True)
            if len(competitions) < EXPECTED_COMPETITIONS:
                raise RuntimeError(
                    f"Catalogo SOL incompleto: {len(competitions)}/{EXPECTED_COMPETITIONS} competizioni; "
                    "nessuna sostituzione del catalogo."
                )

            new_competitions: list[dict[str, Any]] = []
            for comp_index, comp in enumerate(competitions, start=1):
                code = comp["code"]
                old = previous_by_code.get(code, {})
                print(f"[{comp_index}/{len(competitions)}] {code} · {comp['name']} · {comp['phase']}", flush=True)
                try:
                    await open_selection_page(page)
                    # Legge di nuovo la riga per indice, perché dopo il click
                    # la griglia viene ricreata dal circuito Blazor.
                    row = page.locator("tr.rz-data-row").filter(has_text=re.compile(rf"\b{re.escape(code)}\b")).first
                    await row.wait_for(state="visible", timeout=ELEMENT_TIMEOUT_MS)
                    try:
                        await row.locator(":scope > td").nth(2).click(timeout=10_000)
                    except Exception:
                        await row.click(timeout=10_000)
                    await page.wait_for_function("() => /\\/calendarioris\\/\\d+\\/\\d+/.test(location.pathname)", timeout=ELEMENT_TIMEOUT_MS)
                    route_match = re.search(r"/calendarioris/(\d+)/(\d+)", page.url)
                    if not route_match:
                        raise RuntimeError("ID campionato non ricavato dalla route SOL")
                    competition_id, season = route_match.group(1), route_match.group(2)
                    route_url = page.url
                    tab_letters = await discover_group_tabs(page)
                    if not tab_letters:
                        raise RuntimeError("SOL non ha esposto schede di girone per questa competizione")
                    print(f"  ID SOL {competition_id}; gironi: {', '.join(tab_letters)}", flush=True)

                    group_entries: list[dict[str, Any]] = []
                    for group_letter in tab_letters:
                        group_id = f"SOL-{competition_id}-{group_letter}"
                        group_entries.append({
                            "id": group_id,
                            "name": f"Girone {group_letter}",
                            "label": f"Girone {group_letter}",
                            "officialGroup": group_letter,
                            "competitionId": competition_id,
                            "sourceUrl": route_url,
                        })
                        try:
                            await open_group_calendar(page, route_url, group_letter)
                            matches = await scrape_calendar_rows(page, group_letter, group_id)
                            if not matches:
                                raise RuntimeError("nessuna partita valida nella tabella Calendario Ufficiale")
                            snapshot = make_snapshot(comp, competition_id, season, group_letter, group_id, matches)
                            previous_snapshots[group_id] = snapshot
                            snapshots_updated += 1
                            print(f"    {group_letter}: {len(matches)} gare, {len(snapshot['teams'])} squadre, {snapshot['counts']['rounds']} giornate", flush=True)
                        except Exception as exc:
                            warnings.append(f"{code} Girone {group_letter}: {exc}")
                            print(f"    {group_letter}: calendario non aggiornato ({exc}); conservo eventuale snapshot precedente", flush=True)

                    # Mantiene anche i vecchi gruppi noti se una cattura parziale
                    # ne ha omesso qualcuno.
                    known = {g.get("id"): g for g in old.get("groups", []) if isinstance(g, dict)}
                    known.update({g["id"]: g for g in group_entries})
                    new_comp = {
                        "code": code,
                        "name": comp["name"],
                        "phase": comp["phase"],
                        "gender": comp["gender"],
                        "sourceId": competition_id,
                        "season": season,
                        "groups": list(known.values()),
                    }
                    new_competitions.append(new_comp)
                    groups_updated += len(group_entries)
                except Exception as exc:
                    warnings.append(f"{code}: {exc}")
                    print(f"  ERRORE competizione {code}: {type(exc).__name__}: {exc}; mantengo i dati già presenti", flush=True)
                    # Allega elementi diagnostici al job Actions, così un errore
                    # non viene più nascosto dietro il vecchio snapshot.
                    try:
                        await page.screenshot(path=str(ROOT / f"sol-debug-{code}.png"), full_page=True)
                        (ROOT / f"sol-debug-{code}.html").write_text(await page.content(), encoding="utf-8")
                    except Exception as debug_exc:
                        print(f"  Diagnostica non disponibile: {debug_exc}", flush=True)
                    if old:
                        new_competitions.append(old)

            competitions_with_groups = sum(
                1 for item in new_competitions if isinstance(item, dict) and item.get("groups")
            )
            if len(new_competitions) < EXPECTED_COMPETITIONS or competitions_with_groups < EXPECTED_COMPETITIONS or groups_updated < EXPECTED_COMPETITIONS:
                raise RuntimeError(
                    "Scansione SOL incompleta: "
                    f"{competitions_with_groups}/{len(new_competitions)} campionati con gironi; "
                    f"{groups_updated} gironi individuati. Attesi almeno {EXPECTED_COMPETITIONS} campionati "
                    "con almeno un girone ciascuno. Catalogo precedente conservato."
                )

            # Non eliminare competizioni non viste durante un caricamento parziale.
            new_by_code = {c.get("code"): c for c in new_competitions}
            for old in previous_groups:
                if old.get("code") not in new_by_code:
                    new_competitions.append(old)
            committee["groups"] = new_competitions
            committee["source"] = "sol-blazor-snapshot"
            committee["season"] = "2026/2027"
            committee["snapshotDate"] = datetime.now(timezone.utc).date().isoformat()
            committee["snapshotCapturedAt"] = datetime.now(timezone.utc).isoformat()
            committee["snapshotNote"] = (
                "Campionati, fasi, gironi e calendari letti dal portale ufficiale SOL tramite browser server-side. "
                "I gironi/calendari non raggiungibili conservano l'ultimo snapshot valido."
            )
            print(f"SOL riepilogo: {len(new_competitions)} campionati, {groups_updated} gironi rilevati, {snapshots_updated} calendari aggiornati", flush=True)
        finally:
            await context.close()
            await browser.close()

    return groups_updated, snapshots_updated, warnings


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
        print("I dati già presenti nel catalogo sono stati conservati; il job deve risultare FALLITO.", file=sys.stderr)
        return 1

    catalog["snapshotDate"] = datetime.now(timezone.utc).date().isoformat()
    catalog["version"] = "volley-live-v11.3.7-fix-radzen-popup-portal"
    tmp_path = CATALOG_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(CATALOG_PATH)
    if warnings:
        print(f"SOL: {len(warnings)} avvisi. I dati validi e gli snapshot precedenti sono stati conservati.")
    print(f"SOL: sincronizzazione completata; {groups} gironi, {snapshots} calendari aggiornati.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
