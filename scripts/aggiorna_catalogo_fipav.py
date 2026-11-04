#!/usr/bin/env python3
"""Aggiorna i calendari FIPAVOnline del Comitato Territoriale Varese.

Il catalogo include anche Milano-Monza-Lecco: i dati SOL già acquisiti sono
conservati come snapshot e non vengono alterati da questo processo, perché
SOL usa una pagina Blazor Server e non è documentato qui un endpoint calendario
JSON equivalente all'API FIPAVOnline.

Il programma gira in GitHub Actions (server-side), quindi non è soggetto al
blocco CORS che impedisce al browser GitHub Pages di leggere l'API.
Non usa login, cookie, scraping dell'HTML o dati inseriti a mano.
"""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "catalogo-fipav-v11.json"
API_BASE = "https://data.fipavonline.it/api/v2/calendar/0/{}"
USER_AGENT = "VolleyLive-FIPAV-calendar-sync/1.0 (+https://volleylivestyle.github.io/tabellone-punteggio/)"
REQUEST_HEADERS = {
    "Accept": "application/json",
    "User-Agent": USER_AGENT,
}
TIMEOUT_SECONDS = 35
RETRIES = 3
DELAY_BETWEEN_REQUESTS = 0.4
CONSECUTIVE_FAILURE_LIMIT = 3


def fix_mojibake(value: Any) -> str:
    text = str(value or "").strip()
    # Alcuni nomi restituiti dall'API possono arrivare con UTF-8 interpretato
    # come Latin-1 (es. "AGORÃ " invece di "AGORÀ"). Correggiamo solo
    # i casi riconoscibili, lasciando invariato il testo già corretto.
    if any(marker in text for marker in ("Ã", "Â", "â€")):
        try:
            repaired = text.encode("latin-1").decode("utf-8")
            if repaired:
                # Se il nome era scritto in maiuscolo e soltanto il carattere
                # accentato è diventato minuscolo, ripristina la maiuscola.
                if re.search(r"[A-Z]", repaired) and not re.search(r"[a-z]", re.sub(r"[àèéìòù]", "", repaired)):
                    repaired = repaired.upper()
                return repaired
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return text


def clean(value: Any) -> str:
    return fix_mojibake(value)


def as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return clean(value).lower() in {"1", "true", "yes", "si", "sì"}


def fetch_calendar(group_id: str) -> dict[str, Any]:
    url = API_BASE.format(group_id)
    last_error: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        request = Request(url, headers=REQUEST_HEADERS, method="GET")
        try:
            with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                if response.status != 200:
                    raise RuntimeError(f"HTTP {response.status} per girone {group_id}")
                raw = response.read()
            payload = json.loads(raw.decode("utf-8-sig"))
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, dict):
                raise ValueError("Risposta JSON senza oggetto data")
            returned_id = clean(data.get("id"))
            if returned_id and returned_id != str(group_id):
                raise ValueError(f"ID restituito {returned_id}, atteso {group_id}")
            matches = data.get("matches")
            if not isinstance(matches, list) or not matches:
                raise ValueError("Calendario vuoto o campo matches assente")
            return data
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, RuntimeError) as exc:
            last_error = exc
            if attempt < RETRIES:
                wait = 2 ** attempt
                print(f"  tentativo {attempt}/{RETRIES} non riuscito: {exc}; riprovo tra {wait}s", flush=True)
                time.sleep(wait)
    raise RuntimeError(f"Impossibile acquisire girone {group_id}: {last_error}")


def match_to_snapshot(match: dict[str, Any]) -> dict[str, Any] | None:
    if clean(match.get("type")) not in {"match", ""}:
        return None
    team1 = match.get("team1") if isinstance(match.get("team1"), dict) else {}
    team2 = match.get("team2") if isinstance(match.get("team2"), dict) else {}
    home = clean(team1.get("title"))
    away = clean(team2.get("title"))
    if not home or not away:
        return None

    day_text = clean(match.get("day"))
    round_match = re.search(r"(\d+)", day_text)
    round_no = int(round_match.group(1)) if round_match else 0

    played = as_bool(match.get("played"))
    result = None
    if played:
        result = [as_int(match.get("team1-setwin")), as_int(match.get("team2-setwin"))]

    points_a = match.get("pt_a") if isinstance(match.get("pt_a"), list) else []
    points_b = match.get("pt_b") if isinstance(match.get("pt_b"), list) else []
    partials: list[str] = []
    for a, b in zip(points_a, points_b):
        a_text, b_text = clean(a), clean(b)
        if a_text and b_text:
            partials.append(f"{a_text}/{b_text}")

    game_no_raw = match.get("ng")
    game_no = as_int(game_no_raw, -1)
    out: dict[str, Any] = {
        "date": clean(match.get("date")),
        "time": clean(match.get("time")),
        "round": round_no,
        "gameNo": game_no if game_no >= 0 else None,
        "venue": clean(match.get("stadium")),
        "home": home,
        "away": away,
        "result": result,
        "partials": partials,
    }
    return out


def make_snapshot(competition: str, gender: str, group_name: str, group_id: str,
                  data: dict[str, Any]) -> dict[str, Any]:
    api_matches = data.get("matches", [])
    matches = [converted for item in api_matches
               if isinstance(item, dict)
               for converted in [match_to_snapshot(item)] if converted is not None]
    if not matches:
        raise ValueError("Nessuna partita valida dopo la conversione")

    team_logos: dict[str, str] = {}
    teams: set[str] = set()
    for item in api_matches:
        if not isinstance(item, dict):
            continue
        for key in ("team1", "team2"):
            team = item.get(key)
            if not isinstance(team, dict):
                continue
            name = clean(team.get("title"))
            logo = clean(team.get("logo"))
            if name:
                teams.add(name)
                if logo.startswith("https://"):
                    team_logos[name] = logo

    rounds = {m["round"] for m in matches if isinstance(m.get("round"), int) and m["round"] > 0}
    completed = sum(1 for item in api_matches if isinstance(item, dict) and as_bool(item.get("played")))
    return {
        "competition": competition,
        "gender": gender,
        "group": group_name,
        "groupId": str(group_id),
        "sourceUrl": f"https://public.fipavonline.it/gironi/{group_id}",
        "teams": sorted(teams, key=str.casefold),
        "matches": matches,
        "counts": {
            "totalMatches": len(matches),
            "completedMatches": completed,
            "upcomingMatches": max(0, len(matches) - completed),
            "rounds": len(rounds),
        },
        "teamLogos": team_logos,
    }


def all_catalog_groups(committee: dict[str, Any]) -> list[dict[str, str]]:
    """Restituisce i gironi FIPAV di un singolo comitato."""
    items: list[dict[str, str]] = []
    for competition in committee.get("groups", []):
        if not isinstance(competition, dict):
            continue
        comp_name = clean(competition.get("name"))
        gender = clean(competition.get("gender"))
        for group in competition.get("groups", []):
            if not isinstance(group, dict):
                continue
            group_id = clean(group.get("id"))
            if group_id:
                items.append({
                    "id": group_id,
                    "name": clean(group.get("name")),
                    "competition": comp_name,
                    "gender": gender,
                })
    return items


def main() -> int:
    if not CATALOG_PATH.exists():
        print(f"ERRORE: non trovo {CATALOG_PATH}", file=sys.stderr)
        return 1
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))

    # V11.3 multi-comitato. Compatibilità con il vecchio schema V11.2.
    if isinstance(catalog.get("committees"), list):
        committees = catalog["committees"]
    else:
        legacy = {
            "id": catalog.get("committee", {}).get("id", "04020"),
            "name": catalog.get("committee", {}).get("name", "Comitato Territoriale Varese"),
            "source": "fipavonline",
            "selectionUrl": catalog.get("officialSelectionUrl"),
            "snapshotDate": catalog.get("snapshotDate"),
            "groups": catalog.get("groups", []),
            "groupSnapshots": catalog.get("groupSnapshots", {}),
        }
        committees = [legacy]
        catalog["committees"] = committees

    updated: list[str] = []
    failed: list[tuple[str, str]] = []
    skipped: list[str] = []
    sync_groups: list[tuple[dict[str, Any], dict[str, str]]] = []

    for committee in committees:
        if not isinstance(committee, dict):
            continue
        source = clean(committee.get("source"))
        name = clean(committee.get("name")) or clean(committee.get("id")) or "Comitato"
        if source == "fipavonline":
            groups = all_catalog_groups(committee)
            for group in groups:
                sync_groups.append((committee, group))
            print(f"{name}: {len(groups)} gironi FIPAVOnline da sincronizzare")
        elif source == "sol-blazor-snapshot":
            snapshots = committee.get("groupSnapshots", {})
            match_count = sum(len(g.get("matches", [])) for g in snapshots.values() if isinstance(g, dict))
            skipped.append(f"{name}: {match_count} partite SOL conservate dallo snapshot")
            print(f"{name}: conservo lo snapshot SOL ({match_count} partite); nessuna chiamata FIPAVOnline")
        else:
            skipped.append(f"{name}: sorgente {source or 'non specificata'} non configurata")
            print(f"{name}: sorgente {source or 'non specificata'} non configurata; dati invariati")

    if not sync_groups:
        print("ERRORE: nessun girone FIPAVOnline disponibile per la sincronizzazione.", file=sys.stderr)
        return 1

    total = len(sync_groups)
    consecutive_failures = 0
    for index, (committee, group) in enumerate(sync_groups, start=1):
        group_id = group["id"]
        committee_name = clean(committee.get("name")) or clean(committee.get("id"))
        print(f"[{index}/{total}] {committee_name} · {group['competition']} - {group['name']} ({group_id})", flush=True)
        try:
            data = fetch_calendar(group_id)
            snapshot = make_snapshot(
                group["competition"], group["gender"], group["name"], group_id, data
            )
            snapshots = committee.setdefault("groupSnapshots", {})
            snapshots[group_id] = snapshot
            updated.append(f"{committee_name}/{group_id}:{len(snapshot['matches'])}")
            consecutive_failures = 0
            print(f"  OK: {len(snapshot['matches'])} partite, {len(snapshot['teams'])} squadre, "
                  f"{snapshot['counts']['rounds']} giornate", flush=True)
        except Exception as exc:  # conserva lo snapshot precedente per questo girone
            failed.append((f"{committee_name}/{group_id}", str(exc)))
            consecutive_failures += 1
            print(f"  ERRORE: {exc}; mantengo i dati precedenti", flush=True)
            if consecutive_failures >= CONSECUTIVE_FAILURE_LIMIT:
                print(
                    f"API FIPAV non raggiungibile per {consecutive_failures} gironi consecutivi: "
                    "interrompo i tentativi Varese e passo al sincronizzatore SOL.",
                    file=sys.stderr, flush=True,
                )
                for _, pending_group in sync_groups[index:]:
                    pending_name = clean(pending_group.get("competition"))
                    pending_committee = clean(committee.get("name"))
                    failed.append((
                        f"{pending_committee}/{pending_group['id']}",
                        "tentativo non eseguito: sospeso dopo tre errori API consecutivi",
                    ))
                break
        if index < total and consecutive_failures < CONSECUTIVE_FAILURE_LIMIT:
            time.sleep(DELAY_BETWEEN_REQUESTS)

    if not updated:
        print("ATTENZIONE: nessun girone FIPAVOnline aggiornato in questa esecuzione.", file=sys.stderr)
        if failed:
            print(f"Richieste FIPAV non riuscite ({len(failed)}):", file=sys.stderr)
            for group_id, error in failed:
                print(f"  - {group_id}: {error}", file=sys.stderr)
        print("Conservo tutti gli snapshot FIPAV già presenti e proseguo con la sincronizzazione SOL di Milano-Monza-Lecco.", flush=True)
        # Un'indisponibilità temporanea dell'API di Varese non deve impedire
        # al workflow multi-comitato di eseguire lo scraper SOL.
        return 0

    today = datetime.now(timezone.utc).date().isoformat()
    for committee in committees:
        if isinstance(committee, dict) and clean(committee.get("source")) == "fipavonline":
            committee["snapshotDate"] = today
            committee["snapshotNote"] = (
                "Calendari acquisiti dal servizio JSON utilizzato dal sito pubblico FIPAVOnline; "
                "gli aggiornamenti sono eseguiti server-side da GitHub Actions. "
                "I gironi non raggiungibili mantengono l'ultimo snapshot valido."
            )
    catalog["snapshotDate"] = today
    catalog["version"] = "volley-live-v11.3"
    catalog["snapshotNote"] = (
        "Catalogo Volley Live per i soli Comitati Territoriali Varese e Milano-Monza-Lecco. "
        "Varese viene sincronizzato tramite API JSON FIPAVOnline. Milano-Monza-Lecco mantiene "
        "gli snapshot SOL acquisiti; il processo FIPAV non modifica tali dati."
    )

    tmp_path = CATALOG_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(CATALOG_PATH)

    print("\nRIEPILOGO")
    print(f"Gironi FIPAV aggiornati ({len(updated)}/{total}): " + ", ".join(updated))
    if skipped:
        print("Comitati/sorgenti mantenuti invariati:")
        for item in skipped:
            print(f"  - {item}")
    if failed:
        print(f"Gironi non aggiornati ({len(failed)}):")
        for group_id, error in failed:
            print(f"  - {group_id}: {error}")
        print("Per questi gironi resta disponibile l'ultimo snapshot valido, se presente.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
