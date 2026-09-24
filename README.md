# invisible_puppeteer

pyppeteer's API on a patched Firefox with a deterministic stealth profile.
Change the import, keep the script.

```bash
uv venv
uv pip install -e .
uv run invisible-puppeteer fetch
```

```python
import asyncio
from invisible_puppeteer import launch

async def main():
    browser = await launch(seed=42)
    page = await browser.newPage()
    await page.goto("https://example.com")
    await page.click("a")
    await browser.close()

asyncio.run(main())
```

```python
browser = await launch(
    seed=42,                        # same seed, same fingerprint
    proxy={"server": "socks5://host:1080", "username": "u", "password": "p"},
    headless=True,                  # hidden desktop, real rendering
    userDataDir="./profile",        # cookies and storage survive the run
)
```

```bash
uv run pytest -q                      # unit
uv run pytest -q -m e2e               # against the real browser
```
