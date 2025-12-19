
import logging
import asyncio
import sys
from random import randint
from time import sleep
from subprocess import CalledProcessError

from playwright.async_api import async_playwright

from automation_server_client import AutomationServer, Workqueue


# ------------------------------
# Hjælpefunktion: installer Chromium-binær i den aktive venv
# ------------------------------
async def ensure_chromium_installed(logger: logging.Logger) -> None:
    """
    Sørger for, at Playwrights Chromium-binær er tilgængelig.
    Kalder 'python -m playwright install chromium' i den aktive venv,
    hvis launch fejler pga. manglende executable.

    Dette er den sikreste måde i Automation Servers isolerede venv.
    """
    # Prøv et hurtigt launch for at se om binær allerede findes
    try:
        async with async_playwright() as p:
            try:
                b = await p.chromium.launch(headless=True)
                await b.close()
                logger.info("Chromium binær fundet – ingen installation nødvendig.")
                return
            except Exception as e:
                if "Executable doesn't exist" in str(e):
                    logger.info("Chromium mangler – henter binær via 'playwright install chromium'...")
                else:
                    # En anden launch-fejl – re-raise så vi kan se den
                    raise

        # Kør installation (uden --with-deps; OS-deps ligger typisk i worker-image)
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "playwright", "install", "chromium",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        out, _ = await proc.communicate()
        logger.info(out.decode(errors="ignore"))
        if proc.returncode != 0:
            raise RuntimeError("playwright install chromium failed")
    except CalledProcessError as e:
        raise RuntimeError(f"Playwright install failed: {e}") from e


# ------------------------------
# Workqueue-population (første gang)
# ------------------------------
def populate_queue(workqueue: Workqueue):
    """Fylder køen med test-URLs (nyhedssites)."""
    news_sites = [
        "https://www.cnn.com",
        "https://www.bbc.com",
        "https://www.nytimes.com",
        "https://www.theguardian.com",
        "https://www.reuters.com",
        "https://www.washingtonpost.com",
        "https://www.aljazeera.com",
        "https://www.foxnews.com",
        "https://www.nbcnews.com",
        "https://www.usatoday.com",
    ]

    for i, site in enumerate(news_sites):
        data = {"url": site, "imagecount": 0, "hrefcount": 0}
        try:
            workqueue.add_item(data=data, reference=site)
        except Exception as e:
            print(f"[populate_queue] Fejl ved posting af item {i+1}: {e}")


# ------------------------------
# Selve proceskørslen: forbrug items i workqueue
# ------------------------------
async def process_workqueue(workqueue: Workqueue):
    logger = logging.getLogger(__name__)

    # Sørg for, at Chromium findes i venv (automatisk fallback-install)
    await ensure_chromium_installed(logger)

    async with async_playwright() as p:
        # Start Chromium headless
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-search-engine-choice-screen"]
        )
        page = await browser.new_page()

        for item in workqueue:
            # Workqueue-item context manager styrer status/logging for item
            with item:
                try:
                    # Åbn URL
                    await page.goto(item.data["url"], wait_until="domcontentloaded")

                    # Tæl <img> tags
                    images = await page.query_selector_all("img")
                    item.data["imagecount"] = len(images)

                    # Tæl ... links
                    links = await page.query_selector_all("a")
                    # get_attribute kan returnere None; filtrér dem fra
                    href_count = 0
                    for link in links:
                        href = await link.get_attribute("href")
                        if href:
                            href_count += 1
                    item.data["hrefcount"] = href_count

                    # Opdater item-data i køen
                    item.update(item.data)

                    logger.info(
                        f"Processed {item.data['url']} "
                        f"images={item.data['imagecount']} hrefs={item.data['hrefcount']}"
                    )
                except Exception as e:
                    logger.error(f"[process_workqueue] Fejl på {item.data.get('url')}: {e}")
                    item.data["hrefcount"] = -1
                    item.fail(str(e))

            # Let random pause for at undgå for aggressiv trafik
            delay = randint(10, 40)
            logger.info(f"Sleeping {delay} seconds")
            sleep(delay)

        await browser.close()


# ------------------------------
# Entrypoint
# ------------------------------
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )

    # Hent session/konfiguration fra miljøet (Automation Server)
    # Bem.: hvis server returnerer parameters=null, håndterer vi det som tom streng via CLI-args
    ats = AutomationServer.from_environment()
    workqueue = ats.workqueue()

    # Normaliser CLI-argumenter (Automation Server injecter "parameters" som ét argument)
    args = [arg for arg in sys.argv[1:] if arg is not None]
    # Pydantic v2 er strammere: hvis parameters var null, ender vi her med ingen args.
    # Vi tolker "ingen args" som "kør uden ekstra switches".
    # Hvis du vil populere køen: kør med parameters="--queue".

    # Populate køen ved '--queue'
    if "--queue" in args:
        # Ryd nye items og fyld med test-URLs
        workqueue.clear_workqueue("new")
        populate_queue(workqueue)
        # Afslut uden at forbruge (separat run for forbrug)
        sys.exit(0)

    # Ellers: forbrug køen
