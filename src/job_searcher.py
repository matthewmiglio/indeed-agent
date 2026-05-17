"""Indeed job search navigation, pagination, and job card extraction.

Handles searching Indeed with filters, paginating through results, collecting
job card data, and extracting full job details from individual posting pages.
"""

import urllib.parse
from playwright.async_api import Page
from browser import human_delay, INDEED_URL, dump_page


async def search_indeed(page: Page, query: str, location: str = None,
                        job_type: str = None, days_posted: int = 14,
                        easy_apply_only: bool = True):
    """Navigate to Indeed search results with the given filters.

    Args:
        page: Playwright page instance.
        query: Job title/keywords to search for.
        location: City, state, or "remote".
        job_type: One of fulltime, parttime, contract, internship, or None.
        days_posted: Only show jobs posted within this many days.
        easy_apply_only: If True, filter for Easy Apply / "Easily apply" jobs.
    """
    params = {"q": query}
    if location:
        params["l"] = location
    if days_posted:
        params["fromage"] = str(days_posted)

    # Job type filter
    jt_map = {
        "fulltime": "fulltime",
        "parttime": "parttime",
        "contract": "contract",
        "internship": "internship",
    }
    if job_type and job_type in jt_map:
        params["jt"] = jt_map[job_type]

    # NOTE: Indeed's URL-level "Easily apply" filter sc=0kf:attr(DSQF7); is
    # over-restrictive — for many query/city combos it returns ONLY sponsored
    # Easy Apply slots (the same handful of employers, regardless of query).
    # We deliberately do NOT apply it here. Instead, the per-card `easy_apply`
    # flag (computed in collect_job_listings from the visible "Easily apply"
    # badge) is the source of truth and gets filtered downstream.

    search_url = f"{INDEED_URL}/jobs?" + urllib.parse.urlencode(params)

    # Soft throttle — a 15-30s pre-search idle dramatically lowers the rate at
    # which Indeed/Cloudflare flags the session as bot-like across many runs.
    print(f"  [search] Humanization delay before search...")
    await human_delay(15, 30)
    print(f"  [search] Navigating to: {search_url}")
    await page.goto(search_url, wait_until="domcontentloaded")
    await human_delay(2, 4)

    # Detect Cloudflare interstitial so we fail loud instead of silently
    # returning 0 cards.
    try:
        title = await page.title()
        if "just a moment" in title.lower() or "verifying" in title.lower():
            print(f"  [search] WARNING: Cloudflare challenge detected (title={title!r}).")
            print(f"  [search] Pause and pass the verification in the browser, "
                  f"or wait 15-30 min for the rate-limit to relax.")
            # Give the user up to 60s to click the challenge manually before continuing.
            await page.wait_for_function(
                "document.title && !document.title.toLowerCase().includes('just a moment') "
                "&& !document.title.toLowerCase().includes('verifying')",
                timeout=60_000,
            )
            print(f"  [search] Challenge cleared — continuing.")
    except Exception as e:
        print(f"  [search] Challenge check error: {e}")

    # URL-level filter applied above; no chip click needed (Indeed removed the visible chip).
    # Listings are still hard-filtered by easy_apply flag downstream as a belt-and-suspenders.

    print(f"  [search] Search page loaded: {page.url}")


async def collect_job_listings(page: Page, target_count: int = 10,
                               location_filter: str | None = None) -> list[dict]:
    """Collect job card data from search results across multiple pages.

    Paginates through Indeed's search results (15 per page) until we have
    enough listings or run out of pages.

    Args:
        target_count: How many listings to collect.
        location_filter: If provided, drop any card whose visible location
            doesn't contain the user-typed city (e.g. "ann arbor"). This
            keeps sponsored cross-city slots out of the results.

    Returns:
        List of dicts with: title, company, location, salary, job_url, job_key, easy_apply
    """
    all_jobs = []
    page_num = 0

    # Pass the full location string lowercased (e.g. "ann arbor, mi") so the JS
    # can match against state code, not just city — Indeed expands the search
    # radius around the requested city, returning relevant nearby listings.
    city_filter = location_filter.lower() if location_filter else ""

    while len(all_jobs) < target_count:
        print(f"  [search] Extracting job cards from results page {page_num + 1}...")

        # Extract job cards from current page via JS evaluation
        cards = await page.evaluate("""(cityFilter) => {
            const jobs = [];
            const seenKeys = new Set();
            // Anchor the search on the primary results container, fall back to whole doc.
            const root = document.querySelector('#mosaic-provider-jobcards') || document;
            // Use the single canonical card wrapper — every result has one job_seen_beacon.
            const cardElements = root.querySelectorAll('div.job_seen_beacon');

            cardElements.forEach(card => {
                try {
                    // Skip hidden template / stub cards (Indeed ships invisible placeholders
                    // with sequential fake job keys like 0123456789abcdef in the DOM).
                    const rect = card.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) return;
                    if (card.offsetParent === null) return;
                    // Job title - usually in an anchor with data-jk attribute or h2 > a
                    const titleLink = card.querySelector(
                        'a[data-jk], ' +
                        'h2.jobTitle a, ' +
                        'a[id^="job_"], ' +
                        'a.jcs-JobTitle'
                    );
                    if (!titleLink) return;

                    const title = titleLink.textContent.trim();
                    let href = titleLink.getAttribute('href') || '';
                    if (href.startsWith('/')) href = 'https://www.indeed.com' + href;

                    // Job key (Indeed's unique job ID)
                    const jobKey = titleLink.getAttribute('data-jk') ||
                                   href.match(/jk=([a-f0-9]+)/)?.[1] || '';

                    // Company name
                    const companyEl = card.querySelector(
                        'span[data-testid="company-name"], ' +
                        'span.companyName, ' +
                        'span.company'
                    );
                    const company = companyEl ? companyEl.textContent.trim() : '';

                    // Location
                    const locationEl = card.querySelector(
                        'div[data-testid="text-location"], ' +
                        'div.companyLocation, ' +
                        'span.companyLocation'
                    );
                    const location = locationEl ? locationEl.textContent.trim() : '';

                    // Salary (not always present)
                    const salaryEl = card.querySelector(
                        'div[data-testid="attribute_snippet_testid"], ' +
                        'div.salary-snippet-container, ' +
                        'span.salaryText, ' +
                        'div.metadata.salary-snippet-container'
                    );
                    const salary = salaryEl ? salaryEl.textContent.trim() : '';

                    // Easy Apply badge — check by text content (no :has-text in native CSS)
                    const cardText = card.textContent || '';
                    const easyApply = cardText.includes('Easily apply') || cardText.includes('Easy Apply');

                    // Skip sponsored cards. Indeed renders ALL cards through its
                    // tracking framework (data-mobtk, /pagead/clk are on every
                    // card now), so those markers are useless. Only the visible
                    // "Sponsored" badge / specific testid identifies a paid slot.
                    const sponsored = (
                        card.querySelector('span[data-testid="sponsoredJob"]') !== null ||
                        card.querySelector('[data-testid="jobsearch-AttributeContainer-sponsored"]') !== null ||
                        // visible label inside a span (avoids matching script/JSON blobs)
                        Array.from(card.querySelectorAll('span,div'))
                            .some(el => el.children.length === 0 && el.textContent.trim() === 'Sponsored')
                    );
                    if (sponsored) return;

                    // Location filter — Indeed expands the geo radius around the
                    // searched city, so exact-city match drops valid nearby results.
                    // Require state-level match instead (e.g. ", MI") so we still
                    // exclude sponsored cross-region bleed (e.g. searching MI but
                    // showing TX jobs) without rejecting in-metro listings.
                    if (cityFilter && location) {
                        // Pull a 2-letter state code out of the searched location
                        // (passed in as "ann arbor, mi" lowercased).
                        const stateMatch = (cityFilter || '').match(/,\s*([a-z]{2})\b/);
                        const userState = stateMatch ? stateMatch[1] : '';
                        if (userState) {
                            const cardStateRe = new RegExp(',\\s*' + userState + '\\b', 'i');
                            if (!cardStateRe.test(location)) return;
                        }
                    }

                    if (title && href && jobKey && !seenKeys.has(jobKey) && easyApply) {
                        seenKeys.add(jobKey);
                        // Build a stable viewjob URL from the job key so navigation
                        // hits the real posting (not a /pagead/clk sponsored redirect).
                        const canonicalUrl = 'https://www.indeed.com/viewjob?jk=' + jobKey;
                        jobs.push({
                            title: title,
                            company: company,
                            location: location,
                            salary: salary,
                            job_url: canonicalUrl,
                            job_key: jobKey,
                            easy_apply: easyApply
                        });
                    }
                } catch (e) {
                    // Skip broken cards
                }
            });
            return jobs;
        }""", city_filter)

        if not cards:
            print(f"  [search] No job cards found on page {page_num + 1}, stopping pagination")
            await dump_page(page, f"no-job-cards-page{page_num + 1}", force=True)
            break

        # Deduplicate by job_url
        existing_urls = {j["job_url"] for j in all_jobs}
        new_cards = [c for c in cards if c["job_url"] not in existing_urls]
        all_jobs.extend(new_cards)
        print(f"  [search] Found {len(new_cards)} new jobs (total: {len(all_jobs)})")

        if len(all_jobs) >= target_count:
            break

        # Try to go to the next page
        next_page = await _click_next_page(page)
        if not next_page:
            print(f"  [search] No more pages available")
            break

        page_num += 1
        await human_delay(2, 4)

    return all_jobs[:target_count]


async def _click_next_page(page: Page) -> bool:
    """Click the 'Next' pagination button. Returns True if successful."""
    try:
        next_btn = await page.query_selector(
            'a[data-testid="pagination-page-next"], '
            'a[aria-label="Next Page"], '
            'nav a:has-text("Next")'
        )
        if next_btn and await next_btn.is_visible():
            await next_btn.click()
            await human_delay(2, 3)
            return True
    except Exception as e:
        print(f"  [search] Pagination error: {e}")
    return False


async def extract_job_details(page: Page) -> dict:
    """Extract the full job posting details from a job detail page.

    Navigate to the job page first, then call this to pull structured data.

    Returns:
        Dict with: title, company, salary, location, job_type, description, benefits
    """
    # Wait for the job description to render
    try:
        await page.wait_for_selector(
            '#jobDescriptionText, div.jobsearch-JobComponent-description',
            timeout=10000
        )
        await human_delay(1, 2)
    except Exception:
        print("  [details] WARNING: job description did not appear in 10s")
        await dump_page(page, "job-detail-no-description", force=True)
        await human_delay(2, 3)

    data = await page.evaluate("""() => {
        const result = {
            title: '',
            company: '',
            salary: '',
            location: '',
            job_type: '',
            description: '',
            benefits: ''
        };

        // Title
        const titleEl = document.querySelector(
            'h1.jobsearch-JobInfoHeader-title, ' +
            'h2.jobsearch-JobInfoHeader-title, ' +
            'div[data-testid="jobsearch-JobInfoHeader-title"], ' +
            'h1[data-testid="jobTitle"]'
        );
        if (titleEl) result.title = titleEl.textContent.trim();

        // Company
        const companyEl = document.querySelector(
            'div[data-testid="inlineHeader-companyName"] a, ' +
            'div.jobsearch-InlineCompanyRating a, ' +
            'span.companyName a'
        );
        if (companyEl) result.company = companyEl.textContent.trim();

        // Salary
        const salaryEl = document.querySelector(
            '#salaryInfoAndJobType span, ' +
            'div[data-testid="attribute_snippet_testid"], ' +
            'span.salary-snippet'
        );
        if (salaryEl) result.salary = salaryEl.textContent.trim();

        // Location
        const locEl = document.querySelector(
            'div[data-testid="inlineHeader-companyLocation"], ' +
            'div.jobsearch-InlineCompanyRating + div'
        );
        if (locEl) result.location = locEl.textContent.trim();

        // Job type (full-time, part-time, etc.)
        const typeEl = document.querySelector(
            '#salaryInfoAndJobType span:last-child, ' +
            'div[data-testid="jobsearch-JobMetadataHeader-item"]'
        );
        if (typeEl) {
            const text = typeEl.textContent.trim();
            if (text.match(/full.?time|part.?time|contract|temporary|internship/i)) {
                result.job_type = text;
            }
        }

        // Full description
        const descEl = document.querySelector(
            '#jobDescriptionText, ' +
            'div.jobsearch-JobComponent-description'
        );
        if (descEl) result.description = descEl.innerText.trim();

        // Benefits section
        const benefitsEl = document.querySelector('#benefits');
        if (benefitsEl) result.benefits = benefitsEl.innerText.trim();

        return result;
    }""")

    data["job_url"] = page.url
    return data


async def navigate_to_job(page: Page, job_url: str):
    """Navigate to a specific job posting page."""
    print(f"  [browser] Navigating to: {job_url[:100]}")
    await page.goto(job_url, wait_until="domcontentloaded")
    await human_delay(1, 2)
