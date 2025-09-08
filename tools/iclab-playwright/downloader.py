import asyncio
from playwright.async_api import async_playwright, Playwright
import aiofiles
from tqdm.asyncio import tqdm

sem = asyncio.Semaphore(10)

async def browse_url(browser, url: str):
    async with sem:
        try:
            page = await browser.new_page()
            await page.goto(url)
        except Exception as e:
            print(f"Error browsing {url}: {e}")
        finally:
            await page.close()

async def process_urls_from_file(input_file: str):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=False)
        context = await browser.new_context(
            accept_downloads=True,
            ignore_https_errors=True,
        )
        context.route("**/*.{png,jpg,jpeg,webp,heic,js,css}", lambda route, request: route.abort())
        with open(input_file, 'r') as file:
            urls = [line.strip() for line in file.readlines()]
        tasks = [browse_url(context, url) for url in urls]
        await tqdm.gather(*tasks, total=len(urls))
        await browser.close()


async def main():
    input_file = "urls.txt"  # File containing URLs, one per line
    await process_urls_from_file(input_file)

if __name__ == "__main__":
    asyncio.run(main())
