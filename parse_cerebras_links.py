#!/usr/bin/env python3
"""Parse Cerebras links and job board references from files."""

import os
import re
import sys

# Add workspace to path for imports
sys.path.insert(0, "/home/anonymous/code/semiconductor-jobs-crawler")

from urllib.parse import urlparse, urljoin

def parse_file(filepath):
    """Parse a file and return job board links found."""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        print(f"Error reading file: {e}")
        return []

    # Look for <a href="...">text</a> tags with job-related terms
    matches = re.findall(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', content, re.I)
    results = []
    for href, text in matches:
        clean_text = re.sub(r'<[^>]+>', '', text).strip()
        if len(clean_text) > 3:
            # Check for job board patterns
            netloc = urlparse(href).netloc.lower() if href.startswith("http") else ""
            is_board = any(domain in netloc for domain in [
                "greenhouse", "lever", "ashby", "workday", "myworkdayjobs"
            ])
            if is_board or any(k in clean_text.lower() for k in [
                "director", "vp", "chief", "architect", "fellow", "cto", "manager", "lead"
            ]):
                results.append((clean_text, href, is_board))
    return results


def find_boards_in_workspace():
    """Find all board references in workspace files."""
    workspace_dir = "/home/anonymous/code/semiconductor-jobs-crawler"
    files_to_scan = [
        "all_discovered_boards.json",
        "startups.csv",
        "README.md"
    ]
    all_boards = set()
    for fname in files_to_scan:
        path = os.path.join(workspace_dir, fname)
        if os.path.exists(path):
            if fname.endswith(".json"):
                import json
                with open(path, "r") as f:
                    data = json.load(f)
                    for comp, info in data.items():
                        bt = info.get("board_type", "")
                        if bt:
                            all_boards.add((comp, bt, info.get("website", "")))
            else:
                results = parse_file(path)
                for text, href, is_board in results:
                    if is_board:
                        # Extract domain as company identifier
                        domain = urlparse(href).netloc.split('.')[0] if href.startswith("http") else href
                        all_boards.add((domain, "detected", href))

    return sorted(all_boards)


if __name__ == "__main__":
    boards = find_boards_in_workspace()
    print(f"Found {len(boards)} board references in workspace.")
    for comp, bt, info in boards[:20]:
        print(f"  {comp} -> {bt} ({info})")
    if len(boards) > 340:
        print(f"NOTE: Found more than 340 references ({len(boards)})")
