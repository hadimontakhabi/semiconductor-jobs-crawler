import os
import re
import csv
import json
import requests
from html.parser import HTMLParser
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import urljoin, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

# Configuration
WORKSPACE_DIR = "/home/anonymous/Downloads/agy-working/semiconductor-jobs-crawler"
REPO_DIR = os.path.join(WORKSPACE_DIR, "awesome-semiconductor-startups")
REPORT_PATH = os.path.join(WORKSPACE_DIR, "semiconductor_leadership_jobs_analysis.md")

# Load environment variables if .env exists
smtp_config = {}
env_path = os.path.join(WORKSPACE_DIR, ".env")
if os.path.exists(env_path):
    with open(env_path, "r") as f:
        for line in f:
            if "=" in line and not line.startswith("#"):
                key, val = line.strip().split("=", 1)
                smtp_config[key.strip()] = val.strip()

# HTML Stripper for scraping pages
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
GREENHOUSE_REGEX = re.compile(r'(?:boards\.greenhouse\.io|job-boards\.greenhouse\.io|boards-api\.greenhouse\.io)/([^/"\'\s>]+)', re.I)
LEVER_REGEX = re.compile(r'jobs\.lever\.co/([^/"\'\s>]+)', re.I)
ASHBY_REGEX = re.compile(r'jobs\.ashbyhq\.com/([^/"\'\s>]+)', re.I)
WORKDAY_REGEX = re.compile(r'([^/"\'\s>]+\.myworkdayjobs\.com/[^/"\'\s>]+)', re.I)

class LinkParser(HTMLParser):
    def __init__(self, base_url):
        super().__init__()
        self.base_url = base_url
        self.links = []
        self.ignore_tags = {'script', 'style', 'svg', 'noscript', 'head', 'meta', 'link'}
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

KNOWN_JOB_DOMAINS = {
    'greenhouse.io', 'boards.greenhouse.io', 'job-boards.greenhouse.io',
    'lever.co', 'jobs.lever.co',
    'ashbyhq.com', 'jobs.ashbyhq.com',
    'myworkdayjobs.com',
    'breezy.hr',
    'bamboohr.com',
    'rippling.com',
    'workable.com',
    'smartrecruiters.com',
    'personio.com',
    'recruitee.com',
    'greetinghr.com',
    'jobvite.com',
    'applytojob.com',
    'notion.site',
    'hire.lever.co'
}

NON_JOB_PATH_KEYWORDS = [
    '/news/', '/news-', '/article/', '/articles/', '/blog/', '/press/', 
    '/press-release/', '/press-releases/', '/type/news/', '/interview/', 
    '/interviews/', '/about/', '/team/', '/leadership/', '/management/', 
    '/sharing/', '/share-offsite/', '/share/', '/events/', '/media/', 
    '/privacy/', '/terms/', '/contact/', '/investors/', '/products/',
    '/podcast/', '/insights/', '/announcements/', '/publication/', '/publications/',
    '/case-study/', '/case-studies/', '/whitepaper/', '/whitepapers/'
]

NON_JOB_TITLE_KEYWORDS = [
    'appointed', 'appoints', 'joins', 'joining', 'announces', 'announced',
    'wins', 'named', 'promoted', 'award', 'press release', 'interview',
    'biography', 'profile', 'board director', 'working group chair',
    'keynote', 'speaker', 'panelist', 'speaker series', 'read more',
    'learn more', 'privacy policy', 'terms of use'
]

def is_valid_job_link(link, title, company_url):
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
        if '/jobs/view/' not in path and '/jobs/' not in path:
            return False
            
    non_job_domains = [
        'businesswire.com', 'einpresswire.com', 'prnewswire.com', 'globenewswire.com',
        'facebook.com', 'twitter.com', 'x.com', 'youtube.com', 'instagram.com',
        'medium.com', 'vimeo.com', 'github.com', 'wikipedia.org'
    ]
    if any(domain in netloc for domain in non_job_domains):
        return False

    company_domain = urlparse(company_url).netloc.lower().replace('www.', '')
    base_comp_domain = '.'.join(company_domain.split('.')[-2:]) if '.' in company_domain else company_domain
    base_netloc = '.'.join(netloc.split('.')[-2:]) if '.' in netloc else netloc
    
    if base_comp_domain and base_comp_domain != base_netloc:
        is_job_board = any(netloc.endswith(jdomain) or jdomain in netloc for jdomain in KNOWN_JOB_DOMAINS)
        if not is_job_board:
            return False
            
    if any(kw in path for kw in NON_JOB_PATH_KEYWORDS):
        return False
        
    if title_lower.startswith('{') or '@context' in title_lower or 'schema.org' in title_lower or 'javascript:' in title_lower:
        return False
        
    if any(phrase in title_lower for phrase in NON_JOB_TITLE_KEYWORDS):
        return False

    if len(title_lower) < 3 or title_lower in ['careers', 'jobs', 'home', 'about', 'contact', 'details', 'apply', 'view', 'apply now']:
        return False
        
    return True

def scan_html_for_boards(html):
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
            site = parts[-1] if len(parts) > 1 else tenant
            if len(parts) > 2 and parts[1] in ["en-US", "en-GB", "zh-CN"]:
                site = parts[-1]
            return "workday", {"host": host, "tenant": tenant, "site": site}
        
    return None, None

def scrape_homepage_and_careers(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
    }
    if not url.startswith("http"):
        url = f"https://{url}"
        
    try:
        r = requests.get(url, timeout=6, headers=headers, allow_redirects=True)
        if r.status_code != 200 and url.startswith("https://"):
            url = url.replace("https://", "http://")
            r = requests.get(url, timeout=6, headers=headers, allow_redirects=True)
            
        if r.status_code == 200:
            html = r.text
            
            b_type, details = scan_html_for_boards(html)
            if b_type:
                return b_type, details
                
            links = extract_links(html, url)
            career_urls = []
            career_keywords = ["career", "job", "work", "join", "position", "hiring", "openings"]
            
            orig_domain = urlparse(url).netloc.replace("www.", "")
            
            for full_url, text in links:
                parsed_url = urlparse(full_url)
                curr_domain = parsed_url.netloc.replace("www.", "")
                
                if orig_domain in curr_domain or curr_domain == "":
                    path = parsed_url.path.lower()
                    text_lower = text.lower() if text else ""
                    if any(kw in path or kw in text_lower for kw in career_keywords):
                        if full_url not in career_urls:
                            career_urls.append(full_url)
                            
            for cu in career_urls[:3]:
                try:
                    cr = requests.get(cu, timeout=5, headers=headers, allow_redirects=True)
                    if cr.status_code == 200:
                        b_type, details = scan_html_for_boards(cr.text)
                        if b_type:
                            return b_type, details
                except:
                    pass
    except Exception:
        pass
    return None, None

def guess_from_apis(name, domain_name):
    candidates = []
    clean_name = re.sub(r'[^a-zA-Z0-9]', '', name).lower()
    candidates.append(clean_name)
    candidates.append(domain_name.lower())
    for suffix in ["semi", "semiconductor", "labs", "systems", "tech", "technology"]:
        if clean_name.endswith(suffix):
            candidates.append(clean_name[:-len(suffix)])
            
    candidates = list(dict.fromkeys([c for c in candidates if c]))
    headers = {"User-Agent": "Mozilla/5.0"}
    
    for candidate in candidates:
        try:
            gh_url = f"https://boards-api.greenhouse.io/v1/boards/{candidate}/jobs"
            r = requests.get(gh_url, timeout=3, headers=headers)
            if r.status_code == 200:
                data = r.json()
                if "jobs" in data:
                    return "greenhouse", {"handle": candidate}
        except:
            pass
            
        try:
            lever_url = f"https://api.lever.co/v0/postings/{candidate}?limit=1"
            r = requests.get(lever_url, timeout=3, headers=headers)
            if r.status_code == 200:
                return "lever", {"handle": candidate}
        except:
            pass
            
    return None, None

def discover_board(row):
    name = row['Company']
    website = row['Website']
    domain_name = website.split('.')[0] if '.' in website else website
    
    b_type, details = scrape_homepage_and_careers(website)
    if b_type:
        return name, website, b_type, details, "scraped"
        
    b_type, details = guess_from_apis(name, domain_name)
    if b_type:
        return name, website, b_type, details, "guessed"
        
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

def load_and_update_discovered_boards():
    JSON_PATH = os.path.join(WORKSPACE_DIR, "all_discovered_boards.json")
    boards_db = {}
    if os.path.exists(JSON_PATH):
        try:
            with open(JSON_PATH, 'r') as f:
                boards_db = json.load(f)
        except Exception as e:
            print(f"Error loading {JSON_PATH}: {e}")
            
    csv_path = os.path.join(REPO_DIR, "startups.csv")
    if not os.path.exists(csv_path):
        print(f"Startups CSV not found at {csv_path}!")
        return boards_db
        
    with open(csv_path, 'r') as f:
        all_companies = list(csv.DictReader(f))
        
    # Reconstruct historical mapping from git log of startups.csv to find alumni websites/metadata
    hist_map = {}
    try:
        import subprocess
        out = subprocess.check_output(
            ['git', '-C', REPO_DIR, 'log', '-p', '-U0', '--', 'startups.csv'],
            stderr=subprocess.DEVNULL,
            text=True
        )
        for l in out.splitlines():
            if (l.startswith('+') or l.startswith('-')) and not (l.startswith('+++') or l.startswith('---')):
                parts = csv.reader([l[1:]])
                try:
                    r = next(parts)
                    if len(r) >= 4:
                        hist_map[r[0].strip()] = {
                            'Website': r[1].strip(),
                            'Technology': r[2].strip(),
                            'Country': r[3].strip()
                        }
                except:
                    pass
    except Exception as e:
        print(f"Error loading historical metadata: {e}")

    # Helper function for normalized string matching
    def norm_name(n):
        n = n.lower()
        n = "".join(c for c in n if c.isalnum())
        for suffix in ["semiconductor", "semi", "labs", "systems", "technologies", "technology", "inc", "ltd", "corp", "corporation", "co", "group"]:
            if n.endswith(suffix):
                n = n[:-len(suffix)]
        return n.strip()

    # Pre-calculate normalized mapping for fuzzy matching
    norm_hist = {norm_name(k): v for k, v in hist_map.items()}

    # Load active alumni
    alumni_path = os.path.join(REPO_DIR, "alumni.csv")
    if os.path.exists(alumni_path):
        with open(alumni_path, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                comp = row['Company'].strip()
                exit_type = row['Exit'].strip()
                if exit_type.lower() != 'shutdown':
                    norm_comp = norm_name(comp)
                    info = None
                    # Try exact match first, then fuzzy match
                    if comp in hist_map:
                        info = hist_map[comp]
                    else:
                        for nh_k, val in norm_hist.items():
                            if norm_comp == nh_k or norm_comp in nh_k or nh_k in norm_comp:
                                info = val
                                break
                    if info:
                        all_companies.append({
                            'Company': comp,
                            'Website': info['Website'].strip(),
                            'Technology': info['Technology'].strip(),
                            'Country': info['Country'].strip()
                        })
        
    to_discover = [row for row in all_companies if row['Company'] not in boards_db]
    if to_discover:
        print(f"Discovered {len(boards_db)} boards. Finding handles for {len(to_discover)} new startups...")
        discovered_count = 0
        with ThreadPoolExecutor(max_workers=15) as executor:
            futures = {executor.submit(discover_board, row): row for row in to_discover}
            for future in as_completed(futures):
                name, website, b_type, details, method = future.result()
                if b_type:
                    entry = {"board_type": b_type}
                    entry.update(details)
                else:
                    entry = {"board_type": "generic_careers", "website": website}
                    
                for row in all_companies:
                    if row['Company'] == name:
                        entry['tech'] = row.get('Technology', '')
                        entry['country'] = row.get('Country', '')
                        entry['website'] = row.get('Website', '')
                        break
                boards_db[name] = entry
                discovered_count += 1
        if discovered_count > 0:
            print(f"Discovered {discovered_count} new job boards! Saving database...")
            with open(JSON_PATH, 'w') as f:
                json.dump(boards_db, f, indent=2)
    return boards_db

# 1. Update repository
def update_repo():
    print("Updating awesome-semiconductor-startups repo...")
    if os.path.exists(REPO_DIR):
        os.system(f"git -C {REPO_DIR} pull")
    else:
        os.system(f"git clone https://github.com/aolofsson/awesome-semiconductor-startups.git {REPO_DIR}")

# 2. Extract matching startups
def get_target_startups():
    target_techs = {'AI', 'RISC-V', 'HPC'}
    companies = []
    csv_path = os.path.join(REPO_DIR, "startups.csv")
    if not os.path.exists(csv_path):
        print("CSV file not found!")
        return []
    with open(csv_path, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            tech = row['Technology'].strip()
            if tech in target_techs:
                companies.append(row)
    return companies

def fetch_jobs_for_company(comp_name, config, regex):
    b_type = config.get('board_type')
    jobs_list = []
    headers = {"User-Agent": "Mozilla/5.0"}
    
    if b_type == 'greenhouse':
        handle = config.get('handle')
        try:
            r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{handle}/jobs", timeout=10, headers=headers)
            if r.status_code == 200:
                for job in r.json().get('jobs', []):
                    title = job.get('title', '')
                    if regex.search(title):
                        jobs_list.append({
                            'title': title,
                            'location': job.get('location', {}).get('name', 'N/A'),
                            'link': job.get('absolute_url', '')
                        })
        except Exception as e:
            print(f"Greenhouse API error for {comp_name}: {e}")
            
    elif b_type == 'lever':
        handle = config.get('handle')
        try:
            r = requests.get(f"https://api.lever.co/v0/postings/{handle}", timeout=10, headers=headers)
            if r.status_code == 200:
                for job in r.json():
                    title = job.get('title', '')
                    if regex.search(title):
                        jobs_list.append({
                            'title': title,
                            'location': job.get('categories', {}).get('location', 'N/A'),
                            'link': job.get('hostedUrl', '')
                        })
        except Exception as e:
            print(f"Lever API error for {comp_name}: {e}")
            
    elif b_type == 'ashby':
        handle = config.get('handle')
        try:
            r = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{handle}", timeout=10, headers=headers)
            if r.status_code == 200:
                for job in r.json().get('jobs', []):
                    title = job.get('title', '')
                    if regex.search(title):
                        jobs_list.append({
                            'title': title,
                            'location': job.get('location', 'N/A'),
                            'link': job.get('jobUrl', '')
                        })
        except Exception as e:
            print(f"Ashby API error for {comp_name}: {e}")
            
    elif b_type == 'workday':
        host = config.get('host')
        tenant = config.get('tenant')
        site = config.get('site')
        try:
            url = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
            offset = 0
            limit = 20
            total = 1
            wd_headers = {'Content-Type': 'application/json', 'User-Agent': 'Mozilla/5.0'}
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
                            'link': f"https://{host}/en-US/{site}{ext_path}"
                        })
                offset += limit
        except Exception as e:
            print(f"Workday API error for {comp_name}: {e}")
            
    elif b_type == 'generic_careers':
        website = config.get('website')
        url = f"https://{website}" if not website.startswith("http") else website
        try:
            r = requests.get(url, timeout=5, headers=headers, allow_redirects=True)
            if r.status_code != 200 and url.startswith("https://"):
                url = url.replace("https://", "http://")
                r = requests.get(url, timeout=5, headers=headers, allow_redirects=True)
                
            if r.status_code == 200:
                html = r.text
                links = extract_links(html, url)
                
                career_urls = []
                career_keywords = ["career", "job", "work", "join", "position", "hiring", "openings"]
                orig_domain = urlparse(url).netloc.replace("www.", "")
                
                for full_url, text in links:
                    parsed_url = urlparse(full_url)
                    curr_domain = parsed_url.netloc.replace("www.", "")
                    if orig_domain in curr_domain or curr_domain == "":
                        path = parsed_url.path.lower()
                        text_lower = text.lower() if text else ""
                        if any(kw in path or kw in text_lower for kw in career_keywords):
                            if full_url not in career_urls:
                                career_urls.append(full_url)
                                
                if not career_urls:
                    career_urls = [urljoin(url, "/careers"), urljoin(url, "/jobs"), url]
                else:
                    career_urls.append(url)
                    
                seen_links = set()
                for cu in career_urls[:3]:
                    try:
                        cr = requests.get(cu, timeout=5, headers=headers, allow_redirects=True)
                        if cr.status_code == 200:
                            clinks = extract_links(cr.text, cu)
                            for link, text in clinks:
                                norm_link = link.split('#')[0].split('?')[0]
                                if norm_link in seen_links:
                                    continue
                                
                                url_path = urlparse(link).path.lower()
                                text_match = regex.search(text) if text else None
                                url_match = regex.search(url_path)
                                
                                if text_match or url_match:
                                    clean_title = text.strip() if (text_match and len(text.strip()) > 5 and text.strip().lower() not in ["details", "apply", "view", "apply now"]) else clean_title_from_url(link)
                                    if clean_title and is_valid_job_link(link, clean_title, url):
                                        jobs_list.append({
                                            'title': clean_title,
                                            'location': 'Careers Page',
                                            'link': link
                                        })
                                        seen_links.add(norm_link)
                    except Exception:
                        pass
        except Exception:
            pass
            
    return comp_name, jobs_list

# 3. Job fetches
def fetch_jobs():
    keywords = ["director", "vp", "vice president", "chief", "architect", "fellow", "cto", "head of", "lead"]
    regex = re.compile(r'\b(' + '|'.join(keywords) + r')\b', re.I)
    
    boards_db = load_and_update_discovered_boards()
    results = {}
    
    print(f"Fetching jobs concurrently for {len(boards_db)} companies...")
    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = {executor.submit(fetch_jobs_for_company, name, cfg, regex): name for name, cfg in boards_db.items()}
        for future in as_completed(futures):
            comp_name, jobs = future.result()
            results[comp_name] = jobs
            
    return results

# 4. Generate report markdown
def generate_report(job_data):
    markdown = []
    markdown.append("# Semiconductor Leadership & Architect Job Analysis")
    markdown.append("\nThis report lists the current executive, senior leadership, and high-level architecture opportunities at top semiconductor startups across all technologies in the repository.\n")
    markdown.append("## 📊 Summary of Matching Positions by Company\n")
    markdown.append("| Company | Key Executive & Senior Architect Openings | Location & Work Model |")
    markdown.append("| :--- | :--- | :--- |")
    
    for comp, jobs in job_data.items():
        if not jobs:
            continue
        job_lines = []
        loc_lines = []
        for j in jobs:
            job_lines.append(f"• [{j['title']}]({j['link']})")
            loc_lines.append(j['location'])
            
        jobs_str = "<br>".join(job_lines)
        locs_str = "<br>".join(list(set(loc_lines)))
        markdown.append(f"| **{comp}** | {jobs_str} | {locs_str} |")
        
    return "\n".join(markdown)

# 4b. Generate styled HTML for email
def generate_html_report(job_data):
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

    for comp, jobs in job_data.items():
        if not jobs:
            continue
        html.append(f"""
      <div class="company-section">
        <div class="company-header">
          <h2 class="company-title">{comp}</h2>
        </div>
        <div class="job-list">""")
        for j in jobs:
            html.append(f"""
          <div class="job-row">
            <div class="job-title-container">
              <a href="{j['link']}" class="job-title" target="_blank">{j['title']}</a>
            </div>
            <div class="job-meta">
              <span class="location-badge">{j['location']}</span>
            </div>
          </div>""")
        html.append("""
        </div>
      </div>""")

    html.append("""
    </div>
    <div class="footer">
      <p>This report was automatically compiled by your Antigravity Assistant.<br>To configure SMTP settings or modify target technologies, edit the script in your workspace.</p>
    </div>
  </div>
</body>
</html>
""")
    return "\n".join(html)

# 5. Email sender
def send_email(html_body):
    sender_email = smtp_config.get("SENDER_EMAIL")
    receiver_email = smtp_config.get("RECEIVER_EMAIL")
    smtp_server = smtp_config.get("SMTP_SERVER")
    smtp_port = int(smtp_config.get("SMTP_PORT", 587))
    smtp_user = smtp_config.get("SMTP_USER")
    smtp_pass = smtp_config.get("SMTP_PASSWORD")
    
    if not (sender_email and receiver_email and smtp_server and smtp_user and smtp_pass):
        print("SMTP email configuration incomplete in .env. Skipping email delivery.")
        return False
        
    print(f"Sending email update to {receiver_email}...")
    
    msg = MIMEMultipart()
    msg['From'] = sender_email
    msg['To'] = receiver_email
    msg['Subject'] = "Weekly Semiconductor Leadership Jobs Update"
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

def main():
    update_repo()
    job_data = fetch_jobs()
    total_jobs = sum(len(jobs) for jobs in job_data.values())
    if total_jobs == 0:
        print("WARNING: 0 matching jobs found across all companies. This usually indicates network failure or offline connectivity. Aborting report update and email delivery.")
        return
        
    report_md = generate_report(job_data)
    
    # Save file
    with open(REPORT_PATH, 'w', encoding='utf-8') as f:
        f.write(report_md)
    print(f"Updated report saved to: {REPORT_PATH}")
    
    # Email if configured
    html_body = generate_html_report(job_data)
    send_email(html_body)

if __name__ == '__main__':
    main()
