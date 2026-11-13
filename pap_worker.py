"""Collect PAP rental advertisements for the Marseille housing project.

This file is generated directly from the notebook. It runs separately because
sync_playwright() conflicts with the asyncio loop used by Jupyter on Windows.

The code keeps the visible Chromium setup from the version that worked locally.
Only residential listings are saved to the dataset.
"""

import argparse
import re
import time
from pathlib import Path
from urllib.parse import urljoin

import pandas as pd
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright._impl._errors import TargetClosedError

BASE_URL = "https://www.pap.fr/annonce/locations-marseille-13-g12024"
PAP_DOMAIN = "https://www.pap.fr"

# These words describe the property category in an ad title.
# We check the title, not the full description: an apartment may contain a desk.
RESIDENTIAL_TYPES = {
    "appartement": "appartement", "studio": "studio",
    "maison": "maison", "villa": "maison",
    "loft": "loft", "duplex": "duplex",
    "chambre": "chambre", "colocation": "colocation",
}
NON_RESIDENTIAL_TYPES = {
    "bureau", "bureaux", "commerce", "commerces", "commercial",
    "local", "locaux", "boutique", "boutiques", "parking",
    "garage", "garages", "entrepôt", "entrepot", "entrepôts",
    "entrepots", "terrain", "terrains", "atelier", "ateliers",
}

# Recognize French prices such as 850 €, 1 200 €, 1.035 € and 1 250,50 €.
EURO_PATTERN = r"(?<!\d)(\d{1,3}(?:[.\s\u00a0\u202f]\d{3})+|\d+)(?:,(\d{1,2}))?\s*€"


def euro_value(match):
    """Convert a matched French price to a number, leaving missing prices empty."""
    if match is None:
        return None
    whole = re.sub(r"[.\s\u00a0\u202f]", "", match.group(1))
    decimals = match.group(2)
    return float(whole + ("." + decimals if decimals else ""))


def is_access_check(page):
    """Recognize PAP access-check pages in French and English."""
    title = page.title().casefold()
    return any(word in title for word in (
        "un instant", "just a moment", "cloudflare", "access denied"
    ))


def wait_for_access(page, selector, timeout_ms=60000):
    """Wait if an access check appears; never parse it as a real advertisement.

    Chromium remains visible for permitted human interaction. A website can
    still refuse automated access even after the waiting period.
    """
    if not is_access_check(page):
        return True

    print("  Website access check displayed in Chromium.", flush=True)
    try:
        page.wait_for_selector(selector, timeout=timeout_ms)
    except TargetClosedError:
        print("  Chromium was closed during the access check.", flush=True)
        return False
    except PlaywrightTimeoutError:
        print("  The website did not show the expected content in time.", flush=True)
        return False

    if is_access_check(page):
        print("  The website is still displaying an access check.", flush=True)
        return False
    return True


def property_type_from_title(soup):
    """Read the listing's <h1> and keep only recognizable residential types.

    'Bureaux à Marseille' is rejected. 'Appartement avec bureau' stays an
    apartment, because the property category is 'appartement'.
    """
    title_tag = soup.find("h1")
    title = title_tag.get_text(" ", strip=True) if title_tag else ""
    words = re.findall(r"[a-zà-ÿ]+", title.casefold())

    # Explicitly commercial titles should never enter a housing dataset.
    if "bureaux" in words or "commerces" in words:
        return title, None
    if "commercial" in words and ("local" in words or "locaux" in words):
        return title, None

    # The first stated property category determines the listing type.
    for word in words:
        if word in NON_RESIDENTIAL_TYPES:
            return title, None
        if word in RESIDENTIAL_TYPES:
            return title, RESIDENTIAL_TYPES[word]

    # Without a clear residential category, we do not guess.
    return title, None


def collect_listing_urls(page, max_pages):
    """Collect unique individual ad links from PAP search result cards."""
    all_urls = set()
    print("Collecting PAP advertisement links...", flush=True)

    for page_num in range(1, max_pages + 1):
        url = BASE_URL if page_num == 1 else f"{BASE_URL}-{page_num}"
        print(f"Search page {page_num}/{max_pages}", flush=True)

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            time.sleep(2)  # Allow the result page to finish loading.

            # Accept the cookie banner if it is displayed.
            try:
                page.locator("#didomi-notice-agree-button").click(timeout=1000)
            except PlaywrightTimeoutError:
                pass

            if not wait_for_access(page, ".search-list-item"):
                # Keep URLs collected on previous pages if access stops here.
                print("  Keeping the links collected so far.", flush=True)
                break

            html = page.content()
        except TargetClosedError:
            print("  Chromium was closed. Stopping URL collection.", flush=True)
            break
        except Exception as error:
            print(f"  Search page could not be loaded: {error}", flush=True)
            continue

        # Keep the card-based extraction used in the working script.
        soup = BeautifulSoup(html, "html.parser")
        ad_containers = soup.find_all(
            "div", class_=re.compile(r"search-list-item|annonce-item")
        )

        if not ad_containers:
            print("  No result cards found on this page.", flush=True)
            break

        new_urls = 0
        for ad in ad_containers:
            link_tag = ad.find("a", href=True)
            if link_tag is None:
                continue

            href = link_tag["href"]
            if "/annonces/" not in href:
                continue

            full_url = urljoin(PAP_DOMAIN, href)
            if full_url not in all_urls:
                all_urls.add(full_url)
                new_urls += 1

        print(f"  Found {new_urls} new links.", flush=True)
        time.sleep(1)

    print(f"Collected {len(all_urls)} unique advertisement links.", flush=True)
    return sorted(all_urls)

def pap_furnished(text):
    if re.search(r"\b(non[- ]meubl[ée]e?|pas meubl[ée]e?|sans meubles?|location nue)\b", text, re.I):
        return "Non"

    if re.search(r"\b(appartement|studio|logement|location|maison)\s+meubl[ée]e?\b", text, re.I):
        return "Oui"

    return None

def extract_listing(soup, ad_url, property_type, title):
    """Extract values from one residential listing and return one dictionary."""
    page_text = soup.get_text(separator=" ", strip=True)

    # Price: match French euro formats (including dots and spaces in thousands).
    price_match = re.search(EURO_PATTERN, page_text)
    price = euro_value(price_match)

    surface_match = re.search(
        r"(\d+(?:[.,]\d+)?)\s*(?:m²|m2)", page_text, re.IGNORECASE
    )
    rooms_match = re.search(r"(\d+)\s*pièce", page_text, re.IGNORECASE)
    bedrooms_match = re.search(r"(\d+)\s*chambre", page_text, re.IGNORECASE)
    zip_match = re.search(r"\b(130\d{2})\b", page_text)

    # Read fees and deposits near their own labels, not as the first price.
    charges_match = re.search(
        r"charges.{0,80}?" + EURO_PATTERN, page_text, re.IGNORECASE
    )
    deposit_match = re.search(
        r"garantie.{0,80}?" + EURO_PATTERN, page_text, re.IGNORECASE
    )
    dpe_match = re.search(r"DPE\s*:\s*([A-G])", page_text, re.IGNORECASE)
    ges_match = re.search(r"GES\s*:\s*([A-G])", page_text, re.IGNORECASE)

    return {
        "source_site": "PAP",
        "url": ad_url,
        "title": title,
        "property_type": property_type,
        "price_total_eur": price,
        "surface_m2": (
            float(surface_match.group(1).replace(",", "."))
            if surface_match else None
        ),
        "rooms": int(rooms_match.group(1)) if rooms_match else None,
        "bedrooms": int(bedrooms_match.group(1)) if bedrooms_match else None,
        "zipcode": zip_match.group(1) if zip_match else None,
        "charges_eur": euro_value(charges_match),
        "depot_garantie_eur": euro_value(deposit_match),
        "dpe_class": dpe_match.group(1).upper() if dpe_match else None,
        "ges_class": ges_match.group(1).upper() if ges_match else None,
        "is_furnished": pap_furnished(page_text),
        "has_elevator": "Oui" if re.search(r"\bascenseur\b", page_text, re.IGNORECASE) else "Non",
        "has_balcony_terrace": "Oui" if re.search(r"\b(balcon|terrasse)\b", page_text, re.IGNORECASE) else "Non",
        "raw_text": page_text[:2000],
        "honoraires_eur": 0,  # Project assumption: PAP rentals are person-to-person.
    }


def scrape_pap_deep_data(max_pages=20, max_ads=None):
    """Open PAP in Chromium, collect links and extract residential listings."""
    print("Launching Chromium browser...", flush=True)
    all_records = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
            )
            page = context.new_page()
            urls = collect_listing_urls(page, max_pages)

            print("Opening individual PAP advertisements...", flush=True)
            for index, ad_url in enumerate(urls, start=1):
                if max_ads is not None and len(all_records) >= max_ads:
                    break

                print(f"Listing {index}/{len(urls)}: {ad_url}", flush=True)
                try:
                    page.goto(
                        ad_url, wait_until="domcontentloaded", timeout=30000
                    )
                    time.sleep(1.5)  # Allow the page to load fully.

                    if not wait_for_access(page, ".item-title"):
                        print("  Skipping a page that did not become accessible.", flush=True)
                        continue

                    soup = BeautifulSoup(page.content(), "html.parser")
                except TargetClosedError:
                    print("  Chromium was closed. Keeping already extracted data.", flush=True)
                    break
                except Exception as error:
                    print(f"  Could not read this listing: {error}", flush=True)
                    continue

                # Decide whether this is housing BEFORE extracting any fields.
                title, property_type = property_type_from_title(soup)
                if property_type is None:
                    print(f"  Skipping non-residential/unknown title: {title!r}", flush=True)
                    continue

                record = extract_listing(soup, ad_url, property_type, title)
                all_records.append(record)
                print(f"  Saved residential listing #{len(all_records)}.", flush=True)

        finally:
            # Close the browser even if a page cannot be loaded.
            try:
                browser.close()
            except TargetClosedError:
                pass

    df = pd.DataFrame(all_records)
    if not df.empty:
        # A surface of zero cannot be used to calculate rent per square metre.
        valid = df["price_total_eur"].notna() & df["surface_m2"].gt(0)
        df.loc[valid, "price_per_m2"] = (
            df.loc[valid, "price_total_eur"] / df.loc[valid, "surface_m2"]
        ).round(2)

    return df


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scrape residential PAP rentals")
    parser.add_argument("--max-pages", type=int, default=2)
    parser.add_argument("--max-ads", type=int, default=10)
    parser.add_argument("--output", default="pap_marseille_deep_data.csv")
    args = parser.parse_args()

    limit = args.max_ads if args.max_ads > 0 else None
    df_pap = scrape_pap_deep_data(max_pages=args.max_pages, max_ads=limit)

    if df_pap.empty:
        print("No residential advertisements were extracted.", flush=True)
        raise SystemExit(1)

    output_file = Path(args.output).resolve()
    output_file.parent.mkdir(parents=True, exist_ok=True)
    df_pap.to_csv(output_file, index=False, encoding="utf-8-sig")
    print(f"Saved {len(df_pap)} PAP residential listings to {output_file}", flush=True)
