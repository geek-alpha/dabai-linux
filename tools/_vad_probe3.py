import asyncio
import json
import os

from playwright.async_api import async_playwright

CANDIDATES = ['/opt/ms-playwright/chromium-1208/chrome-linux64/chrome', '/usr/bin/chromium', '/usr/bin/google-chrome']
EXE = next((c for c in CANDIDATES if os.path.exists(c)), None)


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=EXE, args=['--no-sandbox'])
        page = await (await browser.new_context()).new_page()
        page.on('console', lambda m: print('[console]', m.type, m.text[:200], flush=True))
        page.on('pageerror', lambda e: print('[pageerror]', str(e)[:300], flush=True))
        page.on('response', lambda r: print('[404]', r.url[:130], flush=True) if r.status == 404 else None)
        await page.goto('http://127.0.0.1:8088/_silero_probe3.html')
        done = False
        for i in range(20):
            await asyncio.sleep(5)
            txt = (await page.inner_text('#log')).replace('\n', ' | ')
            print(f'[{i * 5}s] {txt[-260:]}', flush=True)
            r = await page.evaluate('window.__result')
            if r is not None:
                print('RESULT', json.dumps(r, ensure_ascii=False), flush=True)
                done = True
                break
        if not done:
            print('NO RESULT after 100s', flush=True)
        await browser.close()


asyncio.run(main())