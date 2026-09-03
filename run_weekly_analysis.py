import os
import re
import csv
import json
import sys
import requests
from html.parser import HTMLParser
import smtplib
import subprocess
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import urljoin, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# FIX BUG-2: WORKSPACE_DIR must point to the directory that actually contains
# the cloned `awesome-semiconductor-startups` repo. The original constant
# pointed to `/home/anonymous/Downloads/agy-working/semiconductor-jobs-crawler`
# which never existed on this host. We resolve the path relative to this
# script's location so the script works in any checkout.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WORKSPACE_DIR = SCRIPT_DIR
REPO_DIR = os.path.join(WORKSPACE_DIR, "awesome-semiconductor-startups")
REPORT_PATH = os.path.join(WORKSPACE_DIR, "semiconductor_leadership_jobs_analysis.md")
BOARDS_DB_PATH = os.path.join(WORKSPACE_DIR, "all_discovered_boards.json")
ALUMNI_JSON_PATH = os.path.join(WORKSPACE_DIR, "all_alumni_boards.json")
ENV_PATH = os.path.join(WORKSPACE_DIR, ".env")

# Load environment variables from os.environ and .env if present
def get_smtp_config():
    cfg = dict(os.environ)
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, "r") as f:
            for line in f:
                if "=" in line and not line.startswith("#"):
                    key, val = line.strip().split("=", 1)
                    cfg[key.strip()] = val.strip()
    return cfg

smtp_config = get_smtp_config()

# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------
class MLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self.reset()
        self.strict = False
        self.convert_charrefs = True
        self.text = []

    def handle_data(self, d):
        self.text.append(d)

    def get_data(self):
        return ''.join(self.text)


def strip_tags(html):
    s = MLStripper()
    s.feed(html)
    return s.get_data()


# Regex patterns for finding job boards in HTML
GREENHOUSE_REGEX = re.compile(
    r'(?:boards\.greenhouse\.io|job-boards\.greenhouse\.io|boards-api\.greenhouse\.io)/([^/"\']\s>]+)',
    re.I,
)
LEVER_REGEX = re.compile(r'jobs\.lever\.co/([^/"\']\s>]+)', re.I)
ASHBY_REGEX = re.compile(r'jobs\.ashbyhq\.com/([^/"\']\s>]+)', re.I)
WORKDAY_REGEX = re.compile(r'([^/"\']\s>]+\.myworkdayjobs\.com/[^/"\']\s>]+)', re.I)
SMARTRECRUITERS_REGEX = re.compile(r'jobs\.smartrecruiters\.com/([^/"\']\s>]+)', re.I)
WORKABLE_REGEX = re.compile(r'apply\.workable\.com/([^/"\']\s>]+)', re.I)
BAMBOOHR_REGEX = re.compile(r'([^/"\']\s>]*\.bamboohr\.com/(?:jobs|careers)/?)', re.I)
BREEZY_REGEX = re.compile(r'([^/"\']\s>]*\.breezy\.hr/)', re.I)
JOBVITE_REGEX = re.compile(r'jobs\.jobvite\.com/([^/"\']\s>]+)', re.I)
JOBVITE_ALT_REGEX = re.compile(r'jobs\.jobvite\.[a-z.]+/([^/"\']\s>]+)', re.I)
PERSONIO_REGEX = re.compile(r'([a-zA-Z0-9-]+)\.jobs\.personio\.(de|com)', re.I)


class LinkParser(HTMLParser):
    def __init__(self, base_url):
        super().__init__()
        self.base_url = base_url
        self.links = []
        self.ignore_tags = {'script', 'style', 'svg', 'noscript'}
        self.ignored_depth = 0
        self.current_href = None
        self.current_title = None
        self.current_text = []

    def handle_starttag(self, tag, attrs):
        tag_lower = tag.lower()
        if tag_lower in self.ignore_tags:
            self.ignored_depth += 1
            return
        if self.ignored_depth > 0:
            return
        if tag_lower == 'a':
            attrs_dict = dict(attrs)
            href = attrs_dict.get('href')
            if href:
                full_url = urljoin(self.base_url, href)
                self.current_href = full_url
                self.current_title = attrs_dict.get('title', '').strip()
                self.current_text = []

    def handle_endtag(self, tag):
        tag_lower = tag.lower()
        if tag_lower in self.ignore_tags and self.ignored_depth > 0:
            self.ignored_depth -= 1
            return
        if self.ignored_depth > 0:
            return
        if tag_lower == 'a' and self.current_href:
            text = ' '.join(''.join(self.current_text).split())
            if not text and self.current_title:
                text = self.current_title
            if self.current_href:
                self.links.append((self.current_href, text or ''))
            self.current_href = None
            self.current_title = None
            self.current_text = []

    def handle_data(self, data):
        if self.ignored_depth == 0 and self.current_href:
            self.current_text.append(data)


def extract_links(html, base_url):
    parser = LinkParser(base_url)
    try:
        parser.feed(html)
    except Exception:
        pass
    return parser.links


# Known job-board domains for the link/URL filters
KNOWN_JOB_DOMAINS = {
    'greenhouse.io', 'boards.greenhouse.io', 'job-boards.greenhouse.io',
    'lever.co', 'jobs.lever.co',
    'ashbyhq.com', 'jobs.ashbyhq.com',
    'myworkdayjobs.com',
    'breezy.hr',
    'bamboohr.com',
    'rippling.com',
    'workable.com', 'apply.workable.com',
    'smartrecruiters.com', 'jobs.smartrecruiters.com',
    'personio.com', 'jobs.personio.com',
    'personio.de', 'jobs.personio.de',
    'ycombinator.com', 'workatastartup.com', 'wellfound.com',
    'recruitee.com', 'jobs.recruitee.com',
    'greetinghr.com',
    'jobvite.com', 'jobs.jobvite.com',
    'applytojob.com',
    'notion.site',
    'hire.lever.co',
    'eightfold.ai', 'pages.adobe.com',
    'teamtailor.com',
    'greenhouse.com',
}

NON_JOB_PATH_KEYWORDS = [
    '/news/', '/news-', '/article/', '/articles/', '/blog/', '/press/',
    '/press-release/', '/press-releases/', '/type/news/', '/interview/',
    '/interviews/', '/about/', '/team/', '/leadership/', '/management/',
    '/sharing/', '/share-offsite/', '/share/', '/events/', '/media/',
    '/privacy/', '/terms/', '/contact/', '/investors/', '/products/',
    '/podcast/', '/insights/', '/announcements/', '/publication/', '/publications/',
    '/case-study/', '/case-studies/', '/whitepaper/', '/whitepapers/',
]

NON_JOB_TITLE_KEYWORDS = [
    'appointed', 'appoints', 'joins', 'joining', 'announces', 'announced',
    'wins', 'named', 'promoted', 'award', 'press release', 'interview',
    'biography', 'profile', 'board director', 'working group chair',
    'keynote', 'speaker', 'panelist', 'speaker series', 'read more',
    'learn more', 'privacy policy', 'terms of use',
]

# Leadership keywords used to filter job titles. Order matters: longer phrases
# must be listed first so the regex boundary doesn't match a partial.
LEADERSHIP_KEYWORDS = [
    "vice president",
    "head of",
    "chief ",
    "chief",
    "director",
    "vp ",
    "vp,",
    "vp/",
    "vp,",
    "vp.",
    "(vp)",
    "vp",
    "architect",
    "fellow",
    "cto",
    "cio",
    "cfo",
    "coo",
    "cpo",
    "cmo",
    "svp",
    "evp",
    "avp",
    "principal engineer",
    "distinguished engineer",
    "lead ",
    "lead",
    "staff engineer",
    "senior director",
    "group manager",
    "head,",
    "head.",
]
LEADERSHIP_REGEX = re.compile(
    r'\b(' + '|'.join(re.escape(k.strip()) for k in LEADERSHIP_KEYWORDS if k.strip()) + r')\b',
    re.I,
)


def is_valid_job_link(link, title, company_url):
    """Return True if a link looks like an individual job posting."""
    if not link or not isinstance(link, str):
        return False
    parsed = urlparse(link)
    if parsed.scheme not in ('http', 'https'):
        return False
    netloc = parsed.netloc.lower().replace('www.', '')
    path = parsed.path.lower()
    title_lower = (title or '').lower().strip()

    if 'linkedin.com' in netloc:
        if '/in/' in path or '/sharing/' in path or '/share' in path or '/posts/' in path or '/company/' in path or '/pulse/' in path:
            return False
        if '/jobs/view/' not in path:
            return False

    non_job_domains = [
        'businesswire.com', 'einpresswire.com', 'prnewswire.com', 'globenewswire.com',
        'facebook.com', 'twitter.com', 'x.com', 'youtube.com', 'instagram.com',
        'medium.com', 'vimeo.com', 'github.com', 'wikipedia.org',
        't.co', 'bit.ly', 'tinyurl.com', 'goo.gl',
    ]
    if any(domain in netloc for domain in non_job_domains):
        return False

    path_with_slash = path if path.endswith('/') else path + '/'
    if any(kw in path_with_slash for kw in NON_JOB_PATH_KEYWORDS):
        return False

    if title_lower.startswith('{') or '@context' in title_lower or 'schema.org' in title_lower or 'javascript:' in title_lower:
        return False
    if any(phrase in title_lower for phrase in NON_JOB_TITLE_KEYWORDS):
        return False
    if len(title_lower) < 3 or title_lower in ['careers', 'jobs', 'home', 'about', 'contact', 'details', 'apply', 'view', 'apply now', 'read more', 'learn more']:
        return False
    return True


def scan_html_for_boards(html):
    """Try to find a known job-board URL embedded in the HTML."""
    gh = GREENHOUSE_REGEX.search(html)
    if gh:
        handle = gh.group(1).split('?')[0].strip()
        if handle and handle != "embed":
            return "greenhouse", {"handle": handle}

    lv = LEVER_REGEX.search(html)
    if lv:
        handle = lv.group(1).split('?')[0].strip()
        if handle:
            return "lever", {"handle": handle}

    ash = ASHBY_REGEX.search(html)
    if ash:
        handle = ash.group(1).split('?')[0].strip()
        if handle:
            return "ashby", {"handle": handle}

    wd = WORKDAY_REGEX.search(html)
    if wd:
        full_match = wd.group(1).strip()
        parts = [p for p in full_match.split('/') if p]
        if parts:
            host = parts[0]
            tenant = host.split('.')[0]
            if len(parts) > 2 and parts[1] in ("en-US", "en-GB", "zh-CN"):
                site = parts[-1]
            elif len(parts) > 1:
                site = parts[-1]
            else:
                site = tenant
            return "workday", {"host": host, "tenant": tenant, "site": site}

    sr = SMARTRECRUITERS_REGEX.search(html)
    if sr:
        handle = sr.group(1).split('?')[0].strip()
        if handle:
            return "smartrecruiters", {"handle": handle}

    wk = WORKABLE_REGEX.search(html)
    if wk:
        handle = wk.group(1).split('?')[0].strip().rstrip('/')
        if handle:
            return "workable", {"handle": handle}

    jv = JOBVITE_REGEX.search(html) or JOBVITE_ALT_REGEX.search(html)
    if jv:
        handle = jv.group(1).split('?')[0].strip()
        if handle:
            return "jobvite", {"handle": handle}

    bz = BREEZY_REGEX.search(html)
    if bz:
        return "breezy", {"url": bz.group(1).strip()}

    pers = PERSONIO_REGEX.search(html)
    if pers:
        handle = pers.group(1).split('?')[0].strip()
        tld = pers.group(2).lower()
        if handle:
            return "personio", {"handle": handle, "tld": tld}

    return None, None


def _extract_jobs_from_html(html, base_url, regex, seen_links):
    """Extract job titles from HTML using link text, headers, JSON-LD, table rows, and span classes.

    Returns a list of dicts: {title, link, location}.
    """
    jobs = []
    if not html:
        return jobs

    # 1) JSON-LD JobPosting blocks
    try:
        jsonld_blocks = re.findall(
            r'<script[^>]*type=[\"\']application/ld\+json[\"\'][^>]*>(.*?)</script>',
            html, re.DOTALL | re.IGNORECASE,
        )
        for blk in jsonld_blocks:
            try:
                data = json.loads(blk)
                items = data if isinstance(data, list) else [data]
                if isinstance(data, dict) and '@graph' in data:
                    items = data['@graph']
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    if it.get('@type') in ('JobPosting',) or 'title' in it:
                        title = it.get('title', '')
                        if title and regex.search(title):
                            loc = it.get('jobLocation', {})
                            if isinstance(loc, dict):
                                addr = loc.get('address', {})
                                if isinstance(addr, dict):
                                    loc_str = ', '.join(filter(None, [addr.get('addressLocality'), addr.get('addressRegion'), addr.get('addressCountry')]))
                                else:
                                    loc_str = str(loc)
                            else:
                                loc_str = 'N/A'
                            link = it.get('url') or it.get('@id') or base_url
                            jobs.append({'title': title, 'link': link, 'location': loc_str or 'N/A'})
            except Exception:
                pass
    except Exception:
        pass

    # 2) HTML elements that often contain job titles: h1-h4, .job-title, [data-job-title]
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, 'html.parser')
        # Look for elements whose direct text matches the leadership regex
        for el in soup.find_all(['h1', 'h2', 'h3', 'h4', 'a', 'span', 'div', 'td', 'li', 'p']):
            try:
                txt = el.get_text(' ', strip=True)
            except Exception:
                continue
            if not txt or len(txt) > 200 or len(txt) < 5:
                continue
            if not regex.search(txt):
                continue
            # Find a nearby link if any
            link = base_url
            anc = el if el.name == 'a' else el.find('a', href=True)
            if anc and anc.get('href'):
                href = anc['href']
                if href.startswith('http'):
                    link = href
                else:
                    link = urljoin(base_url, href)
            if link.split('#')[0].split('?')[0] in seen_links:
                continue
            # Skip if it looks like a generic word
            tlow = txt.lower()
            if any(generic in tlow for generic in ['view all', 'see all', 'browse all', 'all jobs', 'all openings', 'search jobs', 'all positions']):
                continue
            if not is_valid_job_link(link, txt, base_url):
                continue
            jobs.append({'title': txt[:200], 'link': link, 'location': 'Careers Page'})
            seen_links.add(link.split('#')[0].split('?')[0])
    except ImportError:
        pass

    # 3) Anchor tags whose text or path matches (the existing behavior)
    try:
        for link, text in extract_links(html, base_url):
            norm_link = link.split('#')[0].split('?')[0]
            if norm_link in seen_links:
                continue
            url_path = urlparse(link).path.lower()
            text_match = regex.search(text) if text else None
            url_match = regex.search(url_path)
            if not (text_match or url_match):
                continue
            text_strip = (text or '').strip()
            if text_match and len(text_strip) > 5 and text_strip.lower() not in ["details", "apply", "view", "apply now", "read more", "learn more"]:
                clean_title = text_strip
            else:
                clean_title = clean_title_from_url(link)
            if clean_title and is_valid_job_link(link, clean_title, base_url):
                jobs.append({'title': clean_title, 'link': link, 'location': 'Careers Page'})
                seen_links.add(norm_link)
    except Exception:
        pass

    return jobs


def _scrape_with_playwright(url, timeout=10):
    """Try headless browser scraping when static requests miss JS-rendered content."""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, wait_until="networkidle", timeout=timeout*1000)
            html = page.content()
            browser.close()
            if html and len(html) > 500:
                return html
    except Exception:
        pass
    return None


class _ShimResponse:
    """Minimal shim so we can return a Playwright-fetched HTML through the same path."""
    def __init__(self, html, status_code=200):
        self.text = html
        self.status_code = status_code


def _http_get(url, timeout=8, headers=None, try_playwright=False):
    """GET a URL with HTTPS→HTTP fallback; optionally try Playwright for JS pages."""
    if headers is None:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }
    r = None
    try:
        r = requests.get(url, timeout=timeout, headers=headers, allow_redirects=True)
        if r.status_code != 200 and url.startswith("https://"):
            r = requests.get(url.replace("https://", "http://"), timeout=timeout, headers=headers, allow_redirects=True)
    except Exception:
        try:
            r = requests.get(url.replace("https://", "http://"), timeout=timeout, headers=headers, allow_redirects=True)
        except Exception:
            r = None
    if r is not None and r.status_code == 200 and len(r.text) > 500:
        return r
    # If page looks sparse/empty and Playwright is enabled, try the browser
    if try_playwright:
        html = _scrape_with_playwright(url, timeout)
        if html:
            return _ShimResponse(html, 200)
    return r if (r is not None and r.status_code == 200) else None


def scrape_homepage_and_careers(url):
    """Visit a company homepage and a few career pages, looking for a known board."""
    if not url:
        return None, None
    if not url.startswith("http"):
        url = f"https://{url}"
    r = _http_get(url)
    if not r:
        return None, None
    html = r.text
    b_type, details = scan_html_for_boards(html)
    if b_type:
        return b_type, details

    # Find links that look like careers pages
    links = extract_links(html, url)
    career_urls = []
    career_keywords = ["career", "job", "work", "join", "position", "hiring", "openings", "vacanc"]
    orig_domain = urlparse(url).netloc.replace("www.", "")
    for full_url, text in links:
        parsed_url = urlparse(full_url)
        curr_domain = parsed_url.netloc.replace("www.", "")
        if orig_domain in curr_domain or curr_domain == "" or any(d in curr_domain for d in KNOWN_JOB_DOMAINS):
            path = parsed_url.path.lower()
            text_lower = text.lower() if text else ""
            if any(kw in path or kw in text_lower for kw in career_keywords):
                if full_url not in career_urls:
                    career_urls.append(full_url)

    for cu in career_urls[:4]:
        cr = _http_get(cu)
        if not cr:
            continue
        b_type, details = scan_html_for_boards(cr.text)
        if b_type:
            return b_type, details
    return None, None


def guess_from_apis(name, domain_name):
    """Try common name-based permutations against Greenhouse and Lever APIs."""
    candidates = []
    clean_name = re.sub(r'[^a-zA-Z0-9]', '', name).lower()
    candidates.append(clean_name)
    candidates.append(domain_name.lower())
    for suffix in ["semi", "semiconductor", "labs", "systems", "tech", "technology", "ai", "io"]:
        if clean_name.endswith(suffix) and len(clean_name) > len(suffix) + 2:
            candidates.append(clean_name[:-len(suffix)])

    candidates = list(dict.fromkeys([c for c in candidates if c]))
    headers = {"User-Agent": "Mozilla/5.0"}
    for candidate in candidates:
        try:
            r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{candidate}/jobs", timeout=4, headers=headers)
            if r.status_code == 200:
                data = r.json()
                if "jobs" in data:
                    return "greenhouse", {"handle": candidate}
        except Exception:
            pass
        try:
            r = requests.get(f"https://api.lever.co/v0/postings/{candidate}?limit=1", timeout=4, headers=headers)
            if r.status_code == 200:
                return "lever", {"handle": candidate}
        except Exception:
            pass
        try:
            r = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{candidate}", timeout=4, headers=headers)
            if r.status_code == 200:
                return "ashby", {"handle": candidate}
        except Exception:
            pass
    return None, None


def discover_board(row):
    """Try to find the job-board for a single company row."""
    name = row.get('Company', '').strip()
    website = row.get('Website', '').strip()
    if not name or not website:
        return name, website, None, None, "failed"
    domain_name = website.split('.')[0] if '.' in website else website
    b_type, details = scrape_homepage_and_careers(website)
    if b_type:
        return name, website, b_type, details, "scraped"
    # Additional guess_from_apis call for Google/Rowan to boost hit rate
    b_type, details = guess_from_apis(name, domain_name)
    if b_type:
        return name, website, b_type, details, "guessed"
    # After API failures, try generic scraping fallback for better coverage
    try:
        # Make one more attempt with the company name as potential board handle
        headers = {"User-Agent": "Mozilla/5.0"}
        # Try common Greenhouse permutations for known boards
        common_guesses = [
            name.replace(' ', '').lower(),
            name.replace(' ', '').lower() + '-careers',
            name.replace(' ', '').lower() + '-jobs',
        ]
        for candidate in common_guesses:
            for api_url, method in [
                (f"https://boards-api.greenhouse.io/v1/boards/{candidate}/jobs", "greenhouse"),
                (f"https://api.lever.co/v0/postings/{candidate}?limit=1", "lever"),
            ]:
                r = requests.get(api_url, timeout=4, headers=headers)
                if r.status_code == 200:
                    if method == "greenhouse":
                        data = r.json()
                        if "jobs" in data:
                            return name, website, "greenhouse", {"handle": candidate}, "guessed"
                    else:
                        return name, website, "lever", {"handle": candidate}, "guessed"
    except Exception:
        pass
    return name, website, None, None, "failed"


def clean_title_from_url(url):
    path = urlparse(url).path
    segments = [s for s in path.split('/') if s]
    if segments:
        last = segments[-1]
        if not last.isdigit() and len(last) > 3:
            last = last.split('.')[0]
            return last.replace('-', ' ').replace('_', ' ').title()
    return "Job Posting"


# ---------------------------------------------------------------------------
# Repo + CSV loading
# ---------------------------------------------------------------------------
def update_repo():
    """Clone the awesome-semiconductor-startups repo or pull the latest."""
    print("Updating awesome-semiconductor-startups repo...")
    try:
        if os.path.exists(os.path.join(REPO_DIR, ".git")):
            subprocess.run(["git", "-C", REPO_DIR, "pull"], check=False)
        else:
            os.makedirs(WORKSPACE_DIR, exist_ok=True)
            subprocess.run(
                ["git", "clone", "https://github.com/aolofsson/awesome-semiconductor-startups.git", REPO_DIR],
                check=False,
            )
    except Exception as e:
        print(f"update_repo warning: {e}")


def _norm_name(n):
    """Normalize a company name for fuzzy comparison."""
    n = (n or "").lower()
    n = "".join(c for c in n if c.isalnum() or c.isspace())
    n = n.replace(" ", "")
    for suffix in ["semiconductor", "semi", "labs", "systems", "technologies", "technology",
                   "inc", "ltd", "corp", "corporation", "co", "group", "computing",
                   "tech", "design", "ai", "io"]:
        if n.endswith(suffix) and len(n) > len(suffix) + 2:
            n = n[: -len(suffix)]
    return n.strip()


def _canonical_key(company_name, website):
    """A canonical key so duplicates collapse into one entry per company."""
    norm = _norm_name(company_name)
    if website:
        try:
            host = urlparse(website if website.startswith("http") else f"https://{website}").netloc.lower().replace("www.", "")
            host = host.split('.')[0]
            if host:
                return host
        except Exception:
            pass
    return norm


def load_and_update_discovered_boards(force_rediscover=False):
    """Build/refresh the discovered-boards DB by reading the repo CSVs and crawling missing companies."""
    boards_db = {}
    if os.path.exists(BOARDS_DB_PATH) and not force_rediscover:
        try:
            with open(BOARDS_DB_PATH, 'r') as f:
                raw_boards = json.load(f)
            seen_canon = set()
            for k, v in raw_boards.items():
                ck = _canonical_key(k, v.get('website', ''))
                bh = f"{v.get('board_type')}::{v.get('handle')}" if (v.get('board_type') and v.get('handle')) else None
                if (ck and ck in seen_canon) or (bh and bh in seen_canon):
                    continue
                boards_db[k] = v
                if ck:
                    seen_canon.add(ck)
                if bh:
                    seen_canon.add(bh)
        except Exception as e:
            print(f"Error loading {BOARDS_DB_PATH}: {e}")

    csv_path = os.path.join(REPO_DIR, "startups.csv")
    if not os.path.exists(csv_path):
        print(f"startups.csv not found at {csv_path}")
        return boards_db

    with open(csv_path, 'r') as f:
        all_companies = list(csv.DictReader(f))

    # Deduplicate by canonical key (e.g. "Cerebras" and "Cerebras Systems" share cerebras.net)
    canon_to_row = {}
    for row in all_companies:
        key = _canonical_key(row['Company'], row.get('Website', ''))
        if key and key not in canon_to_row:
            canon_to_row[key] = row

    deduped_companies = list(canon_to_row.values())

    # Load alumni → active companies (excluding shutdown)
    alumni_path = os.path.join(REPO_DIR, "alumni.csv")
    if os.path.exists(alumni_path):
        with open(alumni_path, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                comp = row.get('Company', '').strip()
                exit_type = row.get('Exit', '').strip().lower()
                if not comp or exit_type == 'shutdown':
                    continue
                # Skip if the active list already has this company
                if any(_canonical_key(comp, c.get('Website', '')) == _canonical_key(c['Company'], c.get('Website', ''))
                       for c in deduped_companies):
                    continue
                # Use a synthetic website (alumni rarely have one in the active list)
                website = (row.get('Website', '') or '').strip() or f"{_norm_name(comp)}.com"
                deduped_companies.append({
                    'Company': comp,
                    'Website': website,
                    'Technology': row.get('Technology', ''),
                    'Country': row.get('Country', ''),
                })

    # Determine which companies still need discovery
    to_discover = []
    known_keys = set()
    for name, entry in boards_db.items():
        key = _canonical_key(name, entry.get('website', ''))
        if key:
            known_keys.add(key)
    for row in deduped_companies:
        if _canonical_key(row['Company'], row.get('Website', '')) not in known_keys:
            to_discover.append(row)

    print(f"Boards DB: {len(boards_db)} entries; need to discover {len(to_discover)} new companies (out of {len(deduped_companies)})")
    if to_discover:
        discovered = 0
        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = {executor.submit(discover_board, row): row for row in to_discover}
            for future in as_completed(futures):
                row = futures[future]
                try:
                    name, website, b_type, details, method = future.result()
                except Exception as e:
                    print(f"discover_board error for {row.get('Company')}: {e}")
                    continue
                key = _canonical_key(name, website)
                if b_type:
                    entry = {"board_type": b_type}
                    entry.update(details)
                else:
                    entry = {"board_type": "generic_careers", "website": website}
                entry['tech'] = row.get('Technology', '')
                entry['country'] = row.get('Country', '')
                entry['website'] = website
                boards_db[name] = entry
                if key:
                    known_keys.add(key)
                discovered += 1
        with open(BOARDS_DB_PATH, 'w') as f:
            json.dump(boards_db, f, indent=2)
        print(f"Discovered {discovered} companies; saved {BOARDS_DB_PATH}")
    return boards_db


# ---------------------------------------------------------------------------
# Job fetching (one board type at a time)
# ---------------------------------------------------------------------------
def _get_ashby_jobs(handle, headers):
    """Ashby pagination. The default endpoint returns one page; we follow `nextPageCursor`."""
    jobs = []
    cursor = None
    for _ in range(50):  # safety cap
        url = f"https://api.ashbyhq.com/posting-api/job-board/{handle}"
        if cursor:
            url += f"?cursor={cursor}"
        try:
            r = requests.get(url, timeout=10, headers=headers)
            if r.status_code != 200:
                break
            data = r.json()
            jobs.extend(data.get('jobs', []))
            cursor = data.get('nextPageCursor')
            if not cursor:
                break
        except Exception:
            break
    return jobs


def fetch_jobs_for_company(comp_name, config, regex):
    b_type = config.get('board_type')
    jobs_list = []
    headers = {"User-Agent": "Mozilla/5.0"}
    if b_type == 'greenhouse':
        handle = config.get('handle')
        if not handle:
            return comp_name, jobs_list
        try:
            r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{handle}/jobs", timeout=12, headers=headers)
            if r.status_code == 200:
                for job in r.json().get('jobs', []):
                    title = job.get('title', '')
                    if regex.search(title):
                        jobs_list.append({
                            'title': title,
                            'location': job.get('location', {}).get('name', 'N/A') if isinstance(job.get('location'), dict) else (job.get('location') or 'N/A'),
                            'link': job.get('absolute_url', ''),
                        })
        except Exception as e:
            print(f"Greenhouse error for {comp_name}: {e}")

    elif b_type == 'lever':
        handle = config.get('handle')
        if not handle:
            return comp_name, jobs_list
        try:
            r = requests.get(f"https://api.lever.co/v0/postings/{handle}?mode=json", timeout=12, headers=headers)
            if r.status_code == 200:
                for job in r.json():
                    title = job.get('text', '')
                    if regex.search(title):
                        cats = job.get('categories', {}) or {}
                        loc = cats.get('location') or job.get('location', 'N/A')
                        jobs_list.append({
                            'title': title,
                            'location': loc,
                            'link': job.get('hostedUrl', ''),
                        })
        except Exception as e:
            print(f"Lever error for {comp_name}: {e}")

    elif b_type == 'ashby':
        handle = config.get('handle')
        if not handle:
            return comp_name, jobs_list
        try:
            for job in _get_ashby_jobs(handle, headers):
                title = job.get('title', '')
                if regex.search(title):
                    loc = job.get('location', 'N/A')
                    if isinstance(loc, dict):
                        loc = loc.get('name', 'N/A')
                    jobs_list.append({
                        'title': title,
                        'location': loc or 'N/A',
                        'link': job.get('jobUrl', ''),
                    })
        except Exception as e:
            print(f"Ashby error for {comp_name}: {e}")

    elif b_type == 'workday':
        host = config.get('host')
        tenant = config.get('tenant')
        site = config.get('site')
        if not (host and tenant and site):
            return comp_name, jobs_list
        try:
            url = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
            offset = 0
            limit = 20
            total = 1
            wd_headers = {'Content-Type': 'application/json', 'Accept': 'application/json', 'User-Agent': 'Mozilla/5.0'}
            while offset < total:
                payload = {'limit': limit, 'offset': offset, 'searchText': ''}
                r = requests.post(url, json=payload, headers=wd_headers, timeout=15)
                if r.status_code != 200:
                    break
                data = r.json()
                total = data.get('total', 0)
                postings = data.get('jobPostings', [])
                if not postings:
                    break
                for job in postings:
                    title = job.get('title', '')
                    if regex.search(title):
                        ext_path = job.get('externalPath', '')
                        jobs_list.append({
                            'title': title,
                            'location': job.get('locationsText', 'N/A'),
                            'link': f"https://{host}/en-US/{site}{ext_path}",
                        })
                offset += limit
        except Exception as e:
            print(f"Workday error for {comp_name}: {e}")

    elif b_type == 'smartrecruiters':
        handle = config.get('handle')
        if not handle:
            return comp_name, jobs_list
        try:
            r = requests.get(f"https://api.smartrecruiters.com/v1/companies/{handle}/postings?limit=100", timeout=12, headers=headers)
            if r.status_code == 200:
                for job in r.json().get('content', []):
                    title = job.get('name', '')
                    if regex.search(title):
                        jobs_list.append({
                            'title': title,
                            'location': (job.get('location') or {}).get('city', 'N/A') if isinstance(job.get('location'), dict) else job.get('location', 'N/A'),
                            'link': job.get('applyUrl') or job.get('ref', ''),
                        })
        except Exception as e:
            print(f"SmartRecruiters error for {comp_name}: {e}")

    elif b_type == 'workable':
        handle = config.get('handle')
        if not handle:
            return comp_name, jobs_list
        try:
            r = requests.get(f"https://apply.workable.com/api/v3/accounts/{handle}/jobs", timeout=12, headers=headers)
            if r.status_code == 200:
                for job in r.json().get('jobs', []) if isinstance(r.json(), dict) else []:
                    title = job.get('title') or job.get('full_title', '')
                    if regex.search(title):
                        loc = job.get('location') or {}
                        jobs_list.append({
                            'title': title,
                            'location': loc.get('city') or loc.get('country') or 'N/A',
                            'link': job.get('url') or job.get('shortlink', ''),
                        })
        except Exception as e:
            print(f"Workable error for {comp_name}: {e}")

    elif b_type == 'jobvite':
        handle = config.get('handle')
        if not handle:
            return comp_name, jobs_list
        try:
            r = requests.get(f"https://jobs.jobvite.com/{handle}/jobs", timeout=12, headers=headers)
            if r.status_code == 200:
                seen_links = set()
                jobs_list.extend(_extract_jobs_from_html(r.text, f"https://jobs.jobvite.com/{handle}/jobs", regex, seen_links))
        except Exception as e:
            print(f"Jobvite error for {comp_name}: {e}")

    elif b_type == 'breezy':
        url = config.get('url') or config.get('website')
        if not url:
            return comp_name, jobs_list
        if not url.startswith('http'):
            url = f"https://{url}"
        try:
            r = requests.get(url, timeout=12, headers=headers)
            if r.status_code == 200:
                seen_links = set()
                jobs_list.extend(_extract_jobs_from_html(r.text, url, regex, seen_links))
        except Exception as e:
            print(f"Breezy error for {comp_name}: {e}")

    elif b_type == 'personio':
        handle = config.get('handle')
        tld = config.get('tld', 'de')
        if not handle:
            return comp_name, jobs_list
        try:
            r = requests.get(f"https://{handle}.jobs.personio.{tld}/xml", timeout=12, headers=headers)
            if r.status_code == 200:
                import xml.etree.ElementTree as ET
                root = ET.fromstring(r.text)
                for pos in root.findall('.//position'):
                    title = (pos.findtext('name') or '').strip()
                    if regex.search(title):
                        pid = pos.findtext('id') or ''
                        office = pos.findtext('office') or 'N/A'
                        link = f"https://{handle}.jobs.personio.{tld}/job/{pid}?language=en" if pid else f"https://{handle}.jobs.personio.{tld}"
                        jobs_list.append({
                            'title': title,
                            'location': office,
                            'link': link,
                        })
        except Exception as e:
            print(f"Personio error for {comp_name}: {e}")

    elif b_type == 'generic_careers':
        website = config.get('website') or ''
        if not website:
            return comp_name, jobs_list
        url = website if website.startswith("http") else f"https://{website}"

        # Fetch homepage/career page
        r = _http_get(url, timeout=8, try_playwright=True)
        if not r:
            return comp_name, jobs_list
        html = r.text
        b_type2, details2 = scan_html_for_boards(html)
        if b_type2:
            sub_config = dict(config)
            sub_config['board_type'] = b_type2
            sub_config.update(details2)
            return fetch_jobs_for_company(comp_name, sub_config, regex)

        links = extract_links(html, url)
        career_urls = []
        career_keywords = ["career", "job", "work", "join", "position", "hiring", "openings", "vacanc", "opportunit", "recruit", "opening"]
        orig_domain = urlparse(url).netloc.replace("www.", "")
        for full_url, text in links:
            parsed_url = urlparse(full_url)
            curr_domain = parsed_url.netloc.replace("www.", "")
            if orig_domain in curr_domain or curr_domain == "" or any(d in curr_domain for d in KNOWN_JOB_DOMAINS):
                path = parsed_url.path.lower()
                text_lower = text.lower() if text else ""
                if any(kw in path or kw in text_lower for kw in career_keywords):
                    if full_url not in career_urls:
                        career_urls.append(full_url)

        if not career_urls:
            guesses = [
                "/careers", "/jobs", "/careers/", "/jobs/",
                "/join-us", "/work-with-us", "/join", "/work",
                "/about/careers", "/company/careers", "/about/jobs",
                "/opportunities", "/hiring", "/openings",
                "/vacancies", "/join-us/", "/careers/openings",
                "/recruitment", "/employment", "/open-positions",
                "/positions", "/apply", "/join-the-team",
                "/join-our-team", "/work-here", "/jobs/current",
            ]
            for guess in guesses:
                career_urls.append(urljoin(url, guess))
        else:
            career_urls.append(url)

        seen_links = set()
        for cu in career_urls[:8]:
            cr = _http_get(cu, timeout=6, try_playwright=True)
            if not cr:
                continue
            # If the careers page itself has a known board, recurse
            sub_b, sub_d = scan_html_for_boards(cr.text)
            if sub_b:
                sub_config = dict(config)
                sub_config['board_type'] = sub_b
                sub_config.update(sub_d)
                return fetch_jobs_for_company(comp_name, sub_config, regex)
            # Extract using all methods: JSON-LD, headers, anchor tags
            for job in _extract_jobs_from_html(cr.text, cu, regex, seen_links):
                jobs_list.append(job)
    return comp_name, jobs_list


def fetch_jobs(boards_db=None, keywords=None):
    """Fetch jobs for every company in the boards DB concurrently."""
    if keywords is not None:
        regex = re.compile(r'\b(' + '|'.join(re.escape(k) for k in keywords) + r')\b', re.I)
    else:
        regex = LEADERSHIP_REGEX
    if boards_db is None:
        boards_db = load_and_update_discovered_boards()
    results = {}
    print(f"Fetching jobs for {len(boards_db)} companies...")
    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = {executor.submit(fetch_jobs_for_company, name, cfg, regex): name for name, cfg in boards_db.items()}
        for future in as_completed(futures):
            try:
                comp_name, jobs = future.result()
            except Exception as e:
                comp_name = futures[future]
                jobs = []
                print(f"Error fetching {comp_name}: {e}")
            results[comp_name] = jobs
    return results


# ---------------------------------------------------------------------------
# See references/web-scraping-patterns.md (created 2026-09-01) for:
#   Playwright fallback, job-ID dedup patterns, garbage filter, validation commands.
# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------
_JOB_BLOCKLIST = re.compile(
    r"^(we |we'll |i |our |read |search |more details|see details|click here|"
    r"changing facts|testimonial|error 404|page not found|the page you requested|"
    r"back to homepage|try one of the links|head back to the homepage|"
    r"speaker 3rd|speaker 2nd|panelist 34|sponsor design|read full bio|view full bio|"
    r"ready to evaluate\?|talk to an engineer|request a quote|get a part recommendation|"
    r"toggle menu|technology platypus|it\'s encouraging|working at .+ has been the most |"
    r"we have redesigned|we\'re driven|we came together to really |"
    r"agentic ai shifts|after years at the intersection of|"
    r"be sure to|please contact|contact us|we hire for|we are looking|"
    r"fabless semiconductor startup|the best-in-class people|ready to evaluate)"
    ,
    re.IGNORECASE
)

def _is_real_job_title(title):
    """Filter out testimonials, bios, navigation text, and other non-job content."""
    if not title or len(title) < 4 or len(title) > 120:
        return False
    t = title.strip()
    if re.search(r'\b\d+[\d,]*\s+open jobs\b', t, re.I):
        return False
    if re.search(r'\bread bio\b', t, re.I):
        return False
    if re.search(r'\bformer\s+(?:vp|director|ceo|cto)\b', t, re.I):
        return False
    if re.search(r'\b(?:co-?founders?|founders?)\b', t, re.I):
        return False
    if any(q in t for q in ['“', '”', '"']) and len(t.split()) > 6:
        return False
    # Must contain at least one leadership keyword (already pre-filtered, but reaffirm)
    if not any(w in t.lower() for w in [
        "director", "vp", "vice president", "head", "chief",
        "architect", "lead", "principal", "fellow", "staff",
        "svp", "evp", "avp", "senior",
    ]):
        return False
    # Reject blocklist patterns
    if _JOB_BLOCKLIST.match(t):
        return False
    # Reject sentences ending with periods that are clearly prose
    if t.endswith('.') and len(t.split()) > 12:
        return False
    # Reject if it contains URL-like slashes without role keywords
    if t.count('/') > 3:
        return False
    return True


def _is_generic_page(link):
    from urllib.parse import urlparse
    p = urlparse(link or "").path.rstrip("/").lower()
    return p in ("", "/careers", "/jobs", "/career", "/job", "/openings", "/positions", "/work-with-us", "/join-us", "/opportunities")


def _norm_title(title):
    return re.sub(r'[^a-z0-9]', '', (title or "").lower())


def _extract_job_id(link):
    for pattern in [
        r"gh_jid=([0-9a-f]+)",
        r"/jobs/([0-9a-f-]{20,})",
        r"/([a-z0-9-]{20,})$",
        r"_r-([0-9]+)",
        r"[?&]id=([0-9a-f-]+)",
        r"/job/([0-9]+)",
    ]:
        m = re.search(pattern, link or "", re.I)
        if m:
            return m.group(1)
    return None


def _dedupe_jobs(jobs):
    """Deduplicate jobs per company, prioritizing specific direct postings over generic career pages."""
    jobs_by_id = {}
    title_to_specific = {}
    title_to_generic = {}

    for j in jobs:
        title = j.get("title", "")
        if not _is_real_job_title(title):
            continue
        link = j.get("link", "") or ""
        jid = _extract_job_id(link)
        nt = _norm_title(title)
        is_gen = _is_generic_page(link)

        if jid:
            if jid in jobs_by_id:
                continue
            jobs_by_id[jid] = j
            title_to_specific[nt] = j
        else:
            if is_gen:
                if nt in title_to_specific:
                    continue
                if nt not in title_to_generic:
                    title_to_generic[nt] = j
            else:
                if nt in title_to_specific:
                    continue
                title_to_specific[nt] = j

    for nt in list(title_to_generic.keys()):
        if nt in title_to_specific:
            del title_to_generic[nt]

    all_jobs = list(jobs_by_id.values())
    for nt, j in title_to_specific.items():
        if j not in all_jobs:
            all_jobs.append(j)
    for nt, j in title_to_generic.items():
        if j not in all_jobs:
            all_jobs.append(j)

    return all_jobs


def generate_report(job_data):
    """Build the Markdown report for the weekly email."""
    markdown = []
    markdown.append("# Semiconductor Leadership & Architect Job Analysis\n")
    markdown.append("Weekly report of executive, senior leadership, and high-level architecture opportunities at semiconductor startups.\n")
    cleaned_data = {comp: _dedupe_jobs(jobs) for comp, jobs in job_data.items()}
    total_jobs = sum(len(j) for j in cleaned_data.values())
    total_companies = sum(1 for j in cleaned_data.values() if j)
    markdown.append(f"**Summary:** {total_jobs} matching positions across {total_companies} companies.\n")
    markdown.append("## Summary of Matching Positions by Company\n")
    markdown.append("| Company | Key Executive & Senior Architect Openings | Location & Work Model |")
    markdown.append("| :--- | :--- | :--- |")
    for comp, jobs in sorted(cleaned_data.items()):
        if not jobs:
            continue
        job_lines = []
        loc_lines = []
        for j in jobs:
            title = (j['title'] or '').replace('|', '\\|')
            link = j['link'] or '#'
            job_lines.append(f"• [{title}]({link})")
            loc_lines.append(j.get('location', '') or 'N/A')
        jobs_str = "<br>".join(job_lines)
        locs_str = "<br>".join(sorted(set(loc_lines)))
        markdown.append(f"| **{comp}** | {jobs_str} | {locs_str} |")
    return "\n".join(markdown)


def generate_html_report(job_data):
    """Build a styled HTML email body."""
    html = []
    html.append("""<!DOCTYPE html>
<html>
<head>
<style>
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    color: #333333;
    line-height: 1.6;
    margin: 0;
    padding: 0;
    background-color: #f1f5f9;
  }
  .container {
    max-width: 700px;
    margin: 20px auto;
    background-color: #ffffff;
    border-radius: 8px;
    overflow: hidden;
    box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);
  }
  .header {
    background-color: #0f172a;
    color: #ffffff;
    padding: 30px 24px;
    text-align: center;
  }
  .header h1 {
    margin: 0;
    font-size: 24px;
    font-weight: 600;
    letter-spacing: -0.02em;
  }
  .header p {
    margin: 8px 0 0 0;
    font-size: 14px;
    color: #94a3b8;
  }
  .content {
    padding: 30px 24px;
  }
  .intro {
    font-size: 15px;
    margin-bottom: 24px;
    color: #475569;
  }
  .company-section {
    margin-bottom: 35px;
    border: 1px solid #e2e8f0;
    border-radius: 6px;
    overflow: hidden;
  }
  .company-header {
    background-color: #f8fafc;
    padding: 12px 16px;
    border-bottom: 1px solid #e2e8f0;
  }
  .company-title {
    font-size: 16px;
    font-weight: 700;
    color: #0f172a;
    margin: 0;
  }
  .job-row {
    padding: 16px;
    border-bottom: 1px solid #f1f5f9;
  }
  .job-row:last-child {
    border-bottom: none;
  }
  .job-title-container {
    margin-bottom: 8px;
  }
  .job-title {
    font-size: 15px;
    font-weight: 600;
    color: #2563eb;
    text-decoration: none;
  }
  .job-title:hover {
    text-decoration: underline;
  }
  .job-meta {
    font-size: 13px;
    color: #64748b;
  }
  .location-badge {
    background-color: #f1f5f9;
    color: #475569;
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 11px;
    font-weight: 500;
  }
  .footer {
    background-color: #f8fafc;
    padding: 20px 24px;
    text-align: center;
    font-size: 12px;
    color: #94a3b8;
    border-top: 1px solid #e2e8f0;
  }
</style>
</head>
<body>
  <div class="container">
    <div class="header">
      <h1>Semiconductor Leadership Job Update</h1>
      <p>Weekly compiled report for semiconductor startup roles</p>
    </div>
    <div class="content">
      <p class="intro">Here are the latest executive, senior engineering, and technical architecture positions that match your preferences.</p>
""")

    for comp, jobs in sorted(job_data.items()):
        if not jobs:
            continue
        safe_comp = (comp or '').replace('<', '&lt;').replace('>', '&gt;')
        html.append(f"""
      <div class="company-section">
        <div class="company-header">
          <h2 class="company-title">{safe_comp}</h2>
        </div>
        <div class="job-list">""")
        for j in jobs:
            title = (j['title'] or '').replace('<', '&lt;').replace('>', '&gt;')
            link = j['link'] or '#'
            loc = (j['location'] or 'N/A').replace('<', '&lt;').replace('>', '&gt;')
            html.append(f"""
          <div class="job-row">
            <div class="job-title-container">
              <a href="{link}" class="job-title" target="_blank">{title}</a>
            </div>
            <div class="job-meta">
              <span class="location-badge">{loc}</span>
            </div>
          </div>""")
        html.append("""
        </div>
      </div>""")

    html.append("""
    </div>
    <div class="footer">
      <p>This report was automatically compiled by your Semiconductor Jobs Crawler.</p>
    </div>
  </div>
</body>
</html>
""")
    return "\n".join(html)


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------
def send_email(html_body, subject="Weekly Semiconductor Leadership Jobs Update"):
    config = get_smtp_config()
    sender_email = config.get("SENDER_EMAIL")
    receiver_email = config.get("RECEIVER_EMAIL")
    smtp_server = config.get("SMTP_SERVER")
    smtp_port = int(config.get("SMTP_PORT", 587))
    smtp_user = config.get("SMTP_USER")
    smtp_pass = config.get("SMTP_PASSWORD")
    if not (sender_email and receiver_email and smtp_server and smtp_user and smtp_pass):
        print("SMTP email configuration incomplete in .env. Skipping email delivery.")
        return False
    print(f"Sending email update to {receiver_email}...")
    msg = MIMEMultipart('alternative')
    msg['From'] = sender_email
    msg['To'] = receiver_email
    msg['Subject'] = subject
    msg.attach(MIMEText(html_body, 'html'))
    try:
        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.sendmail(sender_email, receiver_email, msg.as_string())
        server.quit()
        print("Email sent successfully!")
        return True
    except Exception as e:
        print(f"Failed to send email: {e}")
        return False


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    update_repo()
    boards_db = load_and_update_discovered_boards()
    job_data = fetch_jobs(boards_db)
    total_jobs = sum(len(j) for j in job_data.values())
    if total_jobs == 0:
        print("WARNING: 0 matching jobs found across all companies. Aborting report update.")
        return
    report_md = generate_report(job_data)
    with open(REPORT_PATH, 'w', encoding='utf-8') as f:
        f.write(report_md)
    print(f"Updated report saved to: {REPORT_PATH}")
    html_body = generate_html_report(job_data)
    send_email(html_body)


if __name__ == '__main__':
    main()
