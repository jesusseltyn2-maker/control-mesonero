"""
Visita la app de Streamlit con un navegador real (headless) y, si
está dormida ("Zzz"), hace clic en el botón de despertarla.
Se ejecuta automáticamente cada 6 horas vía GitHub Actions.
"""

import asyncio

from playwright.async_api import async_playwright

APP_URL = "https://control-mesonero.streamlit.app/"


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()

        await page.goto(APP_URL, wait_until="domcontentloaded", timeout=120_000)
        await page.wait_for_timeout(5000)

        boton = page.get_by_role("button", name="Yes, get this app back up!")
        if await boton.count() > 0:
            print("La app estaba dormida. Despertando...")
            await boton.click()
            await page.wait_for_timeout(30_000)
            print("Listo, la app debería estar despertando.")
        else:
            print("La app ya estaba despierta. Nada que hacer.")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
