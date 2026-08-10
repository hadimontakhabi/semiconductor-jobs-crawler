import os
import csv
import json
import re
import requests
from urllib.parse import urljoin, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser

WORKSPACE_DIR = "/home/anonymous/Downloads/gemini-working"
CSV_PATH = os.path.join(WORKSPACE_DIR, "awesome-semiconductor-startups", "startups.csv")
JSON_PATH = os.path.join(WORKSPACE_DIR, "all_discovered_boards.json")

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
        # Greenhouse
        try:
            gh_url = f"https://boards-api.greenhouse.io/v1/boards/{candidate}/jobs"
            r = requests.get(gh_url, timeout=3, headers=headers)
            if r.status_code == 200:
                data = r.json()
                if "jobs" in data:
                    return "greenhouse", {"handle": candidate}
        except:
            pass
            
        # Lever
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

def main():
    boards_db = {}
    if os.path.exists(JSON_PATH):
        try:
            with open(JSON_PATH, 'r') as f:
                boards_db = json.load(f)
        except Exception as e:
            print(f"Error loading {JSON_PATH}: {e}")
            
    seed_greenhouse = {
        'Anello': 'anellophotonics',
        'Aspinity': 'aspinity',
        'Baya Systems': 'bayasystems',
        'Beam': 'beam',
        'Cerebras Systems': 'cerebrassystems',
        'EnCharge AI': 'enchargeai',
        'Eridan': 'eridan',
        'Ethernovia': 'ethernovia',
        'Fractile': 'fractile',
        'HaiLa': 'haila',
        'Kittycad': 'zoo',
        'Lemurian Labs': 'lemurianlabs',
        'Lightmatter': 'lightmatter',
        'Matx': 'matx',
        'NcodiN': 'ncodin',
        'PQShield': 'pqshield',
        'PsiQuantum': 'psiquantum',
        'SambaNova Systems': 'sambanovasystems',
        'Tenstorrent': 'tenstorrent'
    }
    seed_lever = {
        'Alif Semiconductor': 'alifsemi',
        'Astrus': 'astrus',
        'Eliyan': 'eliyan',
        'Extropic': 'extropic',
        'Kepler Computing': 'kepler',
        'Lumotive': 'lumotive',
        'OmniDesign': 'omnidesigntech',
        'Plaid Semiconductor': 'plaid',
        'VoltAI': 'voltai',
        'zeroRISC': 'zerorisc'
    }
    seed_custom = {
        'd-Matrix': {'board_type': 'ashby', 'handle': 'd-matrix'},
        'SiFive': {'board_type': 'workday', 'host': 'sifive.wd1.myworkdayjobs.com', 'tenant': 'sifive', 'site': 'sifivecareers'}
    }
    
    for comp, handle in seed_greenhouse.items():
        if comp not in boards_db:
            boards_db[comp] = {"board_type": "greenhouse", "handle": handle}
            
    for comp, handle in seed_lever.items():
        if comp not in boards_db:
            boards_db[comp] = {"board_type": "lever", "handle": handle}
            
    for comp, data in seed_custom.items():
        if comp not in boards_db:
            boards_db[comp] = data
            
    with open(CSV_PATH, 'r') as f:
        startups = list(csv.DictReader(f))
        
    to_discover = [row for row in startups if row['Company'] not in boards_db]
    print(f"Total startups in CSV: {len(startups)}")
    print(f"Already in database: {len(boards_db)}")
    print(f"Need to discover: {len(to_discover)}")
    
    if to_discover:
        discovered_count = 0
        print("Running discovery concurrently...")
        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = {executor.submit(discover_board, row): row for row in to_discover}
            for future in as_completed(futures):
                name, website, b_type, details, method = future.result()
                if b_type:
                    print(f"Found: {name} -> {b_type} ({details}) via {method}")
                    entry = {"board_type": b_type}
                    entry.update(details)
                    for row in startups:
                        if row['Company'] == name:
                            entry['tech'] = row.get('Technology', '')
                            entry['country'] = row.get('Country', '')
                            entry['website'] = row.get('Website', '')
                            break
                    boards_db[name] = entry
                    discovered_count += 1
                else:
                    pass
        print(f"Successfully discovered {discovered_count} new job boards!")
        
        with open(JSON_PATH, 'w') as f:
            json.dump(boards_db, f, indent=2)
        print(f"Saved database to {JSON_PATH}")
    else:
        print("All startups are already mapped in the database!")

if __name__ == "__main__":
    main()
