#!/usr/bin/env python3
"""NFL Careers Web Scraper - scrapes job listings from all 32 NFL team career pages."""

import asyncio
import csv
import re
from datetime import datetime
from urllib.parse import urljoin, urlparse
from playwright.async_api import async_playwright, Page, BrowserContext, TimeoutError as PlaywrightTimeoutError

NFL_CLUBS_URL = "https://www.nfl.com/careers/clubs"
OUTPUT_FILE = f"nfl_jobs_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
TIMEOUT = 30_000

CSV_FIELDS = ["team", "title", "department", "location", "job_type", "url", "source_url"]

# Regex to spot "View Jobs", "See Open Positions", etc.
JOB_LINK_RE = re.compile(
    r'\b(view|see|explore|find|browse|search|open|current|available)\b.{0,20}'
    r'\b(jobs?|positions?|openings?|opportunities?|roles?|listings?)\b',
    re.IGNORECASE,
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# Cookie / consent banner dismissal
# ---------------------------------------------------------------------------

# Vendor-specific selectors tried first (fastest path)
_COOKIE_SELECTORS = [
    "#onetrust-accept-btn-handler",                                    # OneTrust
    ".onetrust-accept-btn-handler",
    "#truste-consent-button",                                          # TrustArc
    ".truste-button-2",
    "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",          # Cookiebot
    "#CybotCookiebotDialogBodyButtonAccept",
    "[id*='cookie'][id*='accept']",
    "[id*='consent'][id*='accept']",
    "[class*='cookie-accept']",
    "[class*='accept-cookie']",
    "button[data-testid*='cookie'][data-testid*='accept']",
]

# Short, exact button labels that unambiguously mean "accept all"
_COOKIE_TEXT_RE = re.compile(
    r'^(accept all( cookies?)?|accept cookies?|allow all( cookies?)?'
    r'|i agree|agree|got it|ok|okay|close)$',
    re.IGNORECASE,
)


async def dismiss_cookie_banner(page: Page) -> None:
    """Click through any cookie/GDPR consent banner. Silent no-op if none present."""
    for sel in _COOKIE_SELECTORS:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=800):
                await btn.click(timeout=3_000)
                await page.wait_for_load_state("networkidle", timeout=5_000)
                return
        except Exception:
            continue

    # Text-match fallback: scan visible buttons
    try:
        for btn in await page.query_selector_all("button, a[role='button']"):
            if not await btn.is_visible():
                continue
            text = (await btn.inner_text()).strip()
            if _COOKIE_TEXT_RE.match(text):
                await btn.click()
                await page.wait_for_load_state("networkidle", timeout=5_000)
                return
    except Exception:
        pass


# ---------------------------------------------------------------------------
# ATS detection
# ---------------------------------------------------------------------------

def detect_ats(url: str) -> str:
    u = url.lower()
    if "myworkdayjobs.com" in u or "workday.com" in u:
        return "workday"
    if "greenhouse.io" in u:
        return "greenhouse"
    if "lever.co" in u:
        return "lever"
    if "jobvite.com" in u:
        return "jobvite"
    if "bamboohr.com" in u:
        return "bamboohr"
    if "taleo.net" in u or "oracle.com/taleo" in u:
        return "taleo"
    if "icims.com" in u:
        return "icims"
    if "teamtailor.com" in u:
        return "teamtailor"
    if "smartrecruiters.com" in u:
        return "smartrecruiters"
    if "teamworkonline.com" in u:
        return "teamworkonline"
    return "generic"


# ---------------------------------------------------------------------------
# ATS-specific scrapers
# ---------------------------------------------------------------------------

async def _all_items_loaded(page: Page, selector: str, stable_ms: int = 2000) -> None:
    """Wait until a selector stops changing count (for lazy-load / infinite scroll)."""
    prev = 0
    while True:
        await asyncio.sleep(stable_ms / 1000)
        cur = len(await page.query_selector_all(selector))
        if cur == prev and cur > 0:
            break
        prev = cur


async def scrape_workday(page: Page, team: str) -> list[dict]:
    jobs: list[dict] = []
    try:
        # Workday may paginate; keep clicking "Load more" if present
        while True:
            await page.wait_for_load_state("networkidle", timeout=TIMEOUT)
            items = await page.query_selector_all('[data-automation-id="jobItem"]')
            for item in items:
                title_el = await item.query_selector('[data-automation-id="jobTitle"]')
                loc_el = await item.query_selector('[data-automation-id="jobLocations"]')
                type_el = await item.query_selector('[data-automation-id="time"]')
                link_el = await item.query_selector("a")
                title = (await title_el.inner_text()).strip() if title_el else ""
                loc = (await loc_el.inner_text()).strip() if loc_el else ""
                jtype = (await type_el.inner_text()).strip() if type_el else ""
                href = await link_el.get_attribute("href") if link_el else ""
                if href and not href.startswith("http"):
                    href = urljoin(page.url, href)
                if title:
                    jobs.append({"team": team, "title": title, "department": "",
                                 "location": loc, "job_type": jtype,
                                 "url": href, "source_url": page.url})
            # Check for next-page button
            next_btn = await page.query_selector('[data-uxi-widget-type="selectNextButton"]:not([disabled])')
            if not next_btn:
                break
            await next_btn.click()
    except Exception as e:
        print(f"    [workday] {e}")
    return jobs


async def scrape_greenhouse(page: Page, team: str) -> list[dict]:
    jobs: list[dict] = []
    try:
        await page.wait_for_load_state("networkidle", timeout=TIMEOUT)
        # Greenhouse boards use .opening rows
        for opening in await page.query_selector_all(".opening"):
            link_el = await opening.query_selector("a")
            loc_el = await opening.query_selector(".location")
            dept_el = await opening.query_selector(".department")
            title = (await link_el.inner_text()).strip() if link_el else ""
            href = await link_el.get_attribute("href") if link_el else ""
            loc = (await loc_el.inner_text()).strip() if loc_el else ""
            dept = (await dept_el.inner_text()).strip() if dept_el else ""
            if title:
                jobs.append({"team": team, "title": title, "department": dept,
                             "location": loc, "job_type": "",
                             "url": href, "source_url": page.url})
    except Exception as e:
        print(f"    [greenhouse] {e}")
    return jobs


async def scrape_lever(page: Page, team: str) -> list[dict]:
    jobs: list[dict] = []
    try:
        await page.wait_for_load_state("networkidle", timeout=TIMEOUT)
        for posting in await page.query_selector_all(".posting"):
            title_el = await posting.query_selector("h5")
            dept_el = await posting.query_selector(".posting-category-title")
            loc_el = await posting.query_selector("[class*='sort-by-location']")
            link_el = await posting.query_selector("a.posting-title")
            title = (await title_el.inner_text()).strip() if title_el else ""
            dept = (await dept_el.inner_text()).strip() if dept_el else ""
            loc = (await loc_el.inner_text()).strip() if loc_el else ""
            href = await link_el.get_attribute("href") if link_el else ""
            if title:
                jobs.append({"team": team, "title": title, "department": dept,
                             "location": loc, "job_type": "",
                             "url": href, "source_url": page.url})
    except Exception as e:
        print(f"    [lever] {e}")
    return jobs


async def scrape_icims(page: Page, team: str) -> list[dict]:
    jobs: list[dict] = []
    try:
        await page.wait_for_load_state("networkidle", timeout=TIMEOUT)
        for row in await page.query_selector_all(".iCIMS_JobsTable .iCIMS_TableRow"):
            title_el = await row.query_selector(".iCIMS_JobsTable .iCIMS_ColContent a")
            loc_el = await row.query_selector(".iCIMS_JobLocation")
            title = (await title_el.inner_text()).strip() if title_el else ""
            href = await title_el.get_attribute("href") if title_el else ""
            loc = (await loc_el.inner_text()).strip() if loc_el else ""
            if title:
                jobs.append({"team": team, "title": title, "department": "",
                             "location": loc, "job_type": "",
                             "url": href, "source_url": page.url})
    except Exception as e:
        print(f"    [icims] {e}")
    return jobs


async def scrape_teamworkonline(page: Page, team: str) -> list[dict]:
    jobs: list[dict] = []
    try:
        while True:
            await page.wait_for_load_state("networkidle", timeout=TIMEOUT)
            containers = await page.query_selector_all(".organization-portal__job-container")
            for container in containers:
                title_el = await container.query_selector(".organization-portal__job-title a")
                loc_el   = await container.query_selector(".organization-portal__job-location")
                dept_el  = await container.query_selector(".organization-portal__job-category")
                type_el  = await container.query_selector(".organization-portal__job__career-level")

                title = (await title_el.inner_text()).strip() if title_el else ""
                href  = (await title_el.get_attribute("href") or "").strip() if title_el else ""
                loc   = (await loc_el.inner_text()).strip() if loc_el else ""
                dept  = (await dept_el.inner_text()).strip() if dept_el else ""
                jtype = (await type_el.inner_text()).strip() if type_el else ""

                if href and not href.startswith("http"):
                    href = f"https://www.teamworkonline.com{href}"

                if title:
                    jobs.append({"team": team, "title": title, "department": dept,
                                 "location": loc, "job_type": jtype,
                                 "url": href, "source_url": page.url})

            # TeamWork Online uses a "Load more" button for pagination
            load_more = await page.query_selector("button.load-more, a.load-more, [data-action*='load']")
            if load_more and await load_more.is_visible():
                await load_more.click()
            else:
                break
    except Exception as e:
        print(f"    [teamworkonline] {e}")
    return jobs


# Selector list tried in order for the generic scraper
GENERIC_JOB_SELECTORS = [
    "[class*='job-card']",
    "[class*='job-item']",
    "[class*='job-listing']",
    "[class*='career-listing']",
    "[class*='position-card']",
    "[class*='opening']",
    "li[class*='job']",
    "tr[class*='job']",
    "article[class*='job']",
]


async def scrape_generic(page: Page, team: str) -> list[dict]:
    jobs: list[dict] = []
    try:
        await page.wait_for_load_state("networkidle", timeout=TIMEOUT)
        for sel in GENERIC_JOB_SELECTORS:
            items = await page.query_selector_all(sel)
            if not items:
                continue
            for item in items:
                text = (await item.inner_text()).strip()
                link_el = await item.query_selector("a")
                href = await link_el.get_attribute("href") if link_el else ""
                if href and not href.startswith("http"):
                    href = urljoin(page.url, href)
                lines = [l.strip() for l in text.splitlines() if l.strip()]
                if lines:
                    jobs.append({
                        "team": team,
                        "title": lines[0],
                        "department": lines[1] if len(lines) > 1 else "",
                        "location": lines[2] if len(lines) > 2 else "",
                        "job_type": "",
                        "url": href,
                        "source_url": page.url,
                    })
            break  # stop at first selector that matched
    except Exception as e:
        print(f"    [generic] {e}")
    return jobs


ATS_SCRAPERS = {
    "workday": scrape_workday,
    "greenhouse": scrape_greenhouse,
    "lever": scrape_lever,
    "icims": scrape_icims,
    "teamworkonline": scrape_teamworkonline,
}


# ---------------------------------------------------------------------------
# Navigation helpers
# ---------------------------------------------------------------------------

async def resolve_jobs_url(page: Page) -> str | None:
    """
    Given we're on a team landing page (possibly still on nfl.com),
    find and return the href of the 'View Jobs' / 'See Openings' link.
    Returns None if no such link is found.
    """
    # 1. Try anchor/button text matching
    for el in await page.query_selector_all("a, button"):
        try:
            text = (await el.inner_text()).strip()
        except Exception:
            continue
        if JOB_LINK_RE.search(text):
            href = await el.get_attribute("href")
            if href:
                return href

    # 2. Fallback: any href that contains job-like keywords
    for el in await page.query_selector_all("a[href]"):
        href = (await el.get_attribute("href") or "").lower()
        if any(kw in href for kw in ("jobs", "careers", "openings", "positions")):
            # Exclude obvious nav links back to nfl.com generic pages
            if "nfl.com/careers/clubs" not in href:
                return await el.get_attribute("href")

    return None


async def scrape_team(context: BrowserContext, team_name: str, start_url: str) -> list[dict]:
    page = await context.new_page()
    jobs: list[dict] = []

    try:
        print(f"  [{team_name}] → {start_url}")
        await page.goto(start_url, wait_until="networkidle", timeout=TIMEOUT)
        await dismiss_cookie_banner(page)
        current_url = page.url

        # If we landed on nfl.com, look for the outbound jobs link
        if "nfl.com" in urlparse(current_url).netloc:
            jobs_href = await resolve_jobs_url(page)
            if jobs_href:
                if not jobs_href.startswith("http"):
                    jobs_href = urljoin(current_url, jobs_href)
                print(f"  [{team_name}] Following jobs link → {jobs_href}")
                await page.goto(jobs_href, wait_until="networkidle", timeout=TIMEOUT)
                await dismiss_cookie_banner(page)
                current_url = page.url
            else:
                print(f"  [{team_name}] No outbound jobs link found on NFL page")

        ats = detect_ats(current_url)
        print(f"  [{team_name}] ATS={ats}  final_url={current_url}")

        scraper_fn = ATS_SCRAPERS.get(ats, scrape_generic)
        jobs = await scraper_fn(page, team_name)
        print(f"  [{team_name}] {len(jobs)} jobs found")

    except PlaywrightTimeoutError:
        print(f"  [{team_name}] TIMEOUT")
    except Exception as e:
        print(f"  [{team_name}] ERROR: {e}")
    finally:
        await page.close()

    return jobs


# ---------------------------------------------------------------------------
# Entry point: collect team links then scrape
# ---------------------------------------------------------------------------

async def get_team_links(page: Page) -> list[dict]:
    """Return list of {name, url} for all 32 teams on the clubs page.

    Targets elements that carry both a `title` and a `data-link_name` attribute —
    the pattern NFL.com uses for its alphabetically-listed team buttons.
    """
    print("Loading NFL clubs page...")
    await page.goto(NFL_CLUBS_URL, wait_until="networkidle", timeout=TIMEOUT)
    await dismiss_cookie_banner(page)

    # Primary: anchors with both title and data-link_name (NFL's team buttons)
    elements = await page.query_selector_all("a[title][data-link_name]")

    # Fallback: any clickable element with both attributes (covers button tags)
    if len(elements) < 20:
        elements = await page.query_selector_all("[title][data-link_name]")

    teams: list[dict] = []
    seen: set[str] = set()

    for el in elements:
        name = (await el.get_attribute("title") or "").strip()
        if not name:
            continue

        href = (await el.get_attribute("href") or "").strip()
        # Skip non-team links (e.g. generic nav items with the same attributes)
        if href.rstrip("/") in ("", "/careers/clubs", NFL_CLUBS_URL):
            continue
        if not href.startswith("http"):
            href = f"https://www.nfl.com{href}"
        if href in seen:
            continue

        seen.add(href)
        teams.append({"name": name, "url": href})

    # Page lists teams alphabetically; preserve that order
    teams.sort(key=lambda t: t["name"])
    return teams


async def main() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(user_agent=USER_AGENT)

        # Collect team links
        index_page = await context.new_page()
        teams = await get_team_links(index_page)
        await index_page.close()

        if not teams:
            print("ERROR: Could not find any team links. The page structure may have changed.")
            print("Try running with headless=False to inspect the page manually.")
            await browser.close()
            return

        print(f"\nFound {len(teams)} teams. Scraping jobs...\n")

        # Scrape each team sequentially (avoids rate-limiting / IP blocks)
        all_jobs: list[dict] = []
        for team in teams:
            jobs = await scrape_team(context, team["name"], team["url"])
            all_jobs.extend(jobs)

        await browser.close()

    # Write CSV
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(all_jobs)

    total_teams = len({j["team"] for j in all_jobs})
    print(f"\nDone — {len(all_jobs)} jobs across {total_teams} teams → {OUTPUT_FILE}")


if __name__ == "__main__":
    asyncio.run(main())
