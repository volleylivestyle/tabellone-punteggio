#!/usr/bin/env python3
"""Test isolato: verifica se UserSession basta a caricare il catalogo SOL."""
import asyncio
import json
import os
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

URL = "https://www.sol.milano.federvolley.it/campionatiris"
EXPECTED = 18

async def main():
    session = os.environ.get("SOL_USER_SESSION", "").strip()
    if not session:
        raise RuntimeError(
            "Secret SOL_USER_SESSION assente o vuoto. "
            "Il test non ha avviato alcuna sincronizzazione."
        )

    errors = []
    websocket = {"seen": False, "frames": 0}

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

        origin = json.dumps("https://www.sol.milano.federvolley.it")
        token = json.dumps(session)
        await context.add_init_script(
            script=(
                "(() => { if (location.origin === " + origin + ") { "
                "localStorage.setItem('UserSession', " + token + "); "
                "} })();"
            )
        )

        page = await context.new_page()
        page.on("pageerror", lambda e: errors.append("pageerror: " + str(e)[:250]))
        page.on(
            "console",
            lambda m: errors.append("console: " + m.text[:250])
            if m.type == "error" else None,
        )

        def on_ws(ws):
            if "/_blazor" in ws.url:
                websocket["seen"] = True
                ws.on("framereceived", lambda _: websocket.__setitem__(
                    "frames", websocket["frames"] + 1
                ))

        page.on("websocket", on_ws)

        try:
            print("TEST SOL: apertura pagina elenco...", flush=True)
            await page.goto(URL, wait_until="domcontentloaded", timeout=60000)

            try:
                await page.wait_for_function(
                    """expected => {
                        const d = document.querySelector('input#stagione')?.closest('.rz-dropdown');
                        const label = (d?.querySelector('.rz-dropdown-label')?.innerText || '').trim();
                        const rows = Array.from(document.querySelectorAll(
                            'table.rz-grid-table tbody tr.rz-data-row'
                        )).filter(r => r.getClientRects().length > 0);
                        return /2026\\s*(?:\\/|-|–)\\s*2027/i.test(label) && rows.length >= expected;
                    }""",
                    arg=EXPECTED,
                    timeout=90000,
                )
            except PlaywrightTimeoutError:
                pass

            result = await page.evaluate("""() => {
                const input = document.querySelector('input#stagione');
                const d = input?.closest('.rz-dropdown');
                const rows = Array.from(document.querySelectorAll(
                    'table.rz-grid-table tbody tr.rz-data-row'
                )).filter(r => r.getClientRects().length > 0);
                const visible = el => !!(el && el.getClientRects().length &&
                    getComputedStyle(el).visibility !== 'hidden' &&
                    getComputedStyle(el).display !== 'none');
                const err = document.querySelector('#blazor-error-ui');
                return {
                    season: d?.querySelector('.rz-dropdown-label')?.innerText?.trim() || '',
                    options: document.querySelectorAll(
                        '.rz-dropdown-panel .rz-dropdown-item, .rz-dropdown-panel [role="option"]'
                    ).length,
                    competitions: rows.length,
                    spinnerVisible: Array.from(document.querySelectorAll('.spinner')).some(visible),
                    blazorError: err && visible(err) ? (err.innerText || '').trim() : '',
                    readyState: document.readyState
                };
            }""")
            print(
                "TEST SOL RESULT: "
                + json.dumps({
                    **result,
                    "UserSessionPresent": True,
                    "UserSessionLength": len(session),
                    "websocketSeen": websocket["seen"],
                    "websocketFrames": websocket["frames"],
                    "pageErrors": errors[:10],
                }, ensure_ascii=False),
                flush=True,
            )

            if not (
                "2026" in result["season"]
                and result["competitions"] >= EXPECTED
            ):
                Path("sol-session-test.html").write_text(
                    await page.content(), encoding="utf-8"
                )
                await page.screenshot(
                    path="sol-session-test.png", full_page=True
                )
                raise RuntimeError(
                    "TEST FALLITO: il solo UserSession non ha prodotto "
                    "la stagione 2026/2027 e almeno 18 competizioni. "
                    "Vedere il risultato e gli artifact diagnostici."
                )

            print(
                "TEST SUPERATO: il solo UserSession ha caricato "
                f"{result['season']} e {result['competitions']} competizioni. "
                "Nessun catalogo è stato letto o modificato.",
                flush=True,
            )
        finally:
            await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
