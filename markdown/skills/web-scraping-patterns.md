# Web Scraping Patterns Skill

You have been given this skill because the user's query involves web scraping, data extraction, or provider configuration for automated data collection. Follow these guidelines when advising on scraping strategies and extraction patterns.

## Extraction Priority Hierarchy

1. **JSON-LD (primary)** — Standards-based structured data embedded in HTML:
   - Easiest to parse (JSON, not HTML)
   - Most reliable (vendor-neutral schema.org)
   - Future-proof (rarely changes compared to DOM)
   - Use PyLD library for proper semantic processing (normalize → expand → compact → frame)
2. **Microdata (secondary)** — Schema.org markup via HTML attributes (itemscope, itemtype)
   - Similar reliability to JSON-LD, more verbose
   - Parse with BeautifulSoup or structured extraction tools
3. **CSS selectors (fallback)** — Only when structured data unavailable
   - Brittle (DOM changes break extraction)
   - Requires reverse-engineering site design
   - Document heavily and add validation
4. **Regex (last resort)** — Only for unstructured text extraction
   - Rarely reliable for structured data
   - Use only if no other option

## Anti-Detection Measures

1. **User-Agent rotation** — Randomize across real browser agents:
   ```python
   # Good: rotate real browser UA strings
   user_agents = [
       "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36...",
       "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)...",
   ]
   # Bad: static UA or obviously bot UA
   ```
2. **Request delays** — Add random delays between requests:
   - 1-5 second minimum between requests to same domain
   - Exponential backoff on 429/503 responses
   - Vary delay (not exact 2 seconds every time)
3. **Fingerprint randomization** — Use Playwright anti-detection:
   - Hide `navigator.webdriver`
   - Spoof Chrome/WebGL/canvas
   - Randomize timezone, language, screen dimensions
   - WebRTC IP leak protection
4. **Residential proxy** — For Akamai/Cloudflare, use proxy only as last resort
   - Expensive and slow
   - Try headed mode + warm-up navigation first

## Playwright Patterns

1. **Wait strategies** — Critical for dynamic content:
   ```python
   # Good: wait for actual content
   await page.wait_for_selector('div[data-product-price]', timeout=10000)
   await page.locator('text="In Stock"').wait_for()

   # Avoid: arbitrary sleep
   await asyncio.sleep(5)
   ```
2. **Network interception** — Reduce noise, optimize performance:
   ```python
   await page.route('**/*.{png,jpg,gif,svg}', lambda r: r.abort())  # Block images
   ```
3. **Headed vs headless**:
   - Headed mode (display shown): Required for Akamai-protected sites
   - Headless (no display): Faster, works for standard sites
   - Always use Xvfb virtual display in Docker when running headed
4. **Page interactions** — Click, scroll, submit forms with Playwright:
   ```python
   await page.click('button[data-add-to-cart]')
   await page.fill('input[name="email"]', 'user@example.com')
   ```

## Data Extraction Patterns

1. **CSS selectors** — Most common DOM approach:
   ```python
   # Good: specific, unlikely to break
   price = await page.inner_text('span.product-price[data-currency="USD"]')

   # Avoid: generic, brittle
   price = await page.inner_text('span')  # May get wrong element
   ```
2. **XPath** — More powerful but harder to read:
   ```python
   # Good: select by text content
   await page.click('//button[contains(text(), "Add to Cart")]')
   ```
3. **BeautifulSoup patterns** — For HTML parsing (use after fetching HTML):
   ```python
   soup = BeautifulSoup(html, 'lxml')
   products = soup.select('div.product[data-id]')
   for p in products:
       name = p.select_one('h2.name').text
       price = p.select_one('.price').text
   ```
4. **Attribute extraction** — Always extract data attributes preferentially:
   ```python
   # Good: structured data
   product_id = element.get('data-product-id')

   # Avoid: extracting from text
   product_id = re.search(r'ID:\s*(\d+)', element.text)
   ```

## Error Handling & Resilience

1. **Retry with backoff** — Transient failures common in web scraping:
   ```python
   from tenacity import retry, stop_after_attempt, wait_exponential

   @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2))
   async def fetch_page(url):
       async with session.get(url) as resp:
           return await resp.text()
   ```
2. **Partial extraction** — Don't fail entire job for one missing field:
   ```python
   product = {
       'name': extract_name(html),
       'price': extract_price(html),  # May be None
       'description': extract_description(html),
   }
   # Validate price is present before using, but don't abandon product
   ```
3. **Fallback chains** — Try multiple extraction methods:
   ```python
   price = (
       extract_from_json_ld(html) or
       extract_from_schema_markup(html) or
       extract_from_dom(html) or
       None
   )
   if price is None:
       logger.warning("Could not extract price from %s", url)
   ```

## Rate Limiting & Respectful Crawling

1. **robots.txt compliance** — Check and respect:
   ```python
   import urllib.robotparser
   rp = urllib.robotparser.RobotFileParser()
   rp.set_url("https://example.com/robots.txt")
   rp.read()
   if rp.can_fetch("*", url):
       # Safe to crawl
   ```
2. **Concurrent request limits** — Never spawn unlimited tasks:
   ```python
   semaphore = asyncio.Semaphore(5)  # Max 5 concurrent
   async with semaphore:
       await fetch_url(url)
   ```
3. **Request spacing** — Crawl interval between requests to same domain:
   - Minimum 1-2 seconds between requests
   - Spread requests across time (don't burst)
4. **Identify yourself** — In User-Agent, include contact info for large-scale crawls

## Data Validation

1. **Schema validation** — Define expected structure upfront:
   ```python
   from pydantic import BaseModel, field_validator

   class Product(BaseModel):
       name: str
       price: float
       availability: bool

       @field_validator('price')
       def price_positive(cls, v):
           if v <= 0:
               raise ValueError('Price must be positive')
           return v
   ```
2. **Type coercion** — Convert extracted strings to proper types:
   ```python
   price_str = "€19.99"
   price_float = float(price_str.replace('€', '').replace(',', '.'))
   ```
3. **Missing field handling** — Document expectations and defaults:
   ```python
   product = {
       'name': extract_name() or 'Unknown',  # Default to 'Unknown'
       'price': extract_price() or 0.0,      # Default to 0.0
       'in_stock': extract_availability() or False,  # Assume unavailable
   }
   ```

## Storage & Deduplication

1. **JSON-LD normalization** — Ensure consistency:
   - Normalize field names (camelCase → snake_case)
   - Deduplicate before storage
   - Include extraction metadata (source_url, extracted_at, confidence_score)
2. **Deduplication strategy** — Detect and skip duplicates:
   - Content hash (SHA256 of key fields) for exact duplicates
   - Semantic similarity (vector similarity) for near-duplicates (cosine > 0.95)
   - Track by product ID + timestamp to detect updates vs new items
3. **Incremental updates** — Only store what changed:
   - Compare new snapshot against last snapshot
   - If only price changed, only update price field and timestamp
   - Preserve history: keep all snapshots, don't overwrite
4. **Export to analytics format** — Parquet for columnar queries:
   - Time-series storage: snapshot_id, url, price, timestamp, source
   - Enables historical analysis and trend detection

## Bot Detection Awareness

1. **Akamai Bot Manager** — Deployed on high-value retailers (Jumbo, Albert Heijn):
   - Blocks headless browsers aggressively
   - **Bypass**: Use Firefox in headed mode + Xvfb + warm-up navigation
   - Detection indicators: 403 responses, empty content, CAPTCHA
2. **Cloudflare** — Deployed on many e-commerce sites:
   - JavaScript challenge required (Playwright handles transparently)
   - Sometimes: Human CAPTCHA (can't bypass programmatically)
   - If blocked: Try residential proxy or reduce request rate
3. **reCAPTCHA** — Can't bypass programmatically:
   - If encountered: Flag item for manual review
   - Alternative: Use reCAPTCHA solver service (expensive, ethical concerns)
   - Better: Adjust extraction strategy (JSON-LD may avoid CAPTCHA)
4. **Graceful degradation** — If detection encountered:
   - Log incident with timestamp and URL
   - Skip item and continue (don't retry aggressively)
   - Email alert if repeat offender
   - Consider provider change (different retailer for same product)

## Response Format

When advising on scraping tasks, structure your response as:
1. **Extraction strategy** — Which method(s) to use and why
2. **Implementation approach** — Code patterns with examples
3. **Resilience plan** — Retry logic, fallbacks, error handling
4. **Anti-detection measures** — Specific techniques for the target site
5. **Data validation** — Schema and validation code
6. **Complete implementation** — If significant work, provide full scraper code
