"""
Personal price-tracking scraper for a single Masoutis storefront on e-food.gr.

Note: e-food.gr's Terms of Service likely restrict automated access. This is
intended for low-frequency, personal, non-commercial use against one store
page -- not for bulk/repeated crawling. Respect the site's robots.txt and
rate limits, and stop using this if e-food asks you to.

The CSS selectors below are placeholders. e-food renders its supermarket
catalog with auto-generated class names that change over time, so you must
inspect the live page (right-click a product -> Inspect) and update
PRODUCT_CARD_SELECTOR / TITLE_SELECTOR / PRICE_SELECTOR before this will
find anything.
"""

import asyncio
from playwright.async_api import async_playwright

URL = "https://www.e-food.gr/delivery/xalandri/masoytis-9038526"

PRODUCT_CARD_SELECTOR = ".product-card"
TITLE_SELECTOR = ".product-title"
PRICE_SELECTOR = ".product-price"


async def autoscroll(page, step=1000, pause_ms=500, max_steps=40):
    """Scroll down incrementally so lazy-loaded products render."""
    last_height = 0
    for _ in range(max_steps):
        await page.mouse.wheel(0, step)
        await page.wait_for_timeout(pause_ms)
        height = await page.evaluate("document.body.scrollHeight")
        if height == last_height:
            break
        last_height = height


async def crawl_masoutis():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            locale="el-GR",
        )
        page = await context.new_page()

        print(f"Navigating to {URL}...")
        response = await page.goto(URL, wait_until="networkidle")

        if response and response.status != 200:
            print(f"Warning: received HTTP {response.status}")

        print("Page loaded. Scrolling to load the full catalog...")
        await autoscroll(page)

        print("Looking for products...")
        try:
            await page.wait_for_selector(PRODUCT_CARD_SELECTOR, timeout=10000)
            products = await page.query_selector_all(PRODUCT_CARD_SELECTOR)
            print(f"Found {len(products)} products.\n")

            for item in products:
                title_element = await item.query_selector(TITLE_SELECTOR)
                price_element = await item.query_selector(PRICE_SELECTOR)

                title = await title_element.inner_text() if title_element else "Unknown Item"
                price = await price_element.inner_text() if price_element else "Unknown Price"

                print(f"- {title.strip()} | {price.strip()}")

        except Exception as e:
            print(
                "Extraction failed. e-food's DOM structure likely requires "
                "updated selectors, or the page didn't finish loading."
            )
            print(f"Error details: {e}")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(crawl_masoutis())
