import re

file_path = "/home/anonymous/.gemini/antigravity-cli/brain/4d871d0e-825e-4889-899b-740f21b06be3/.system_generated/steps/72/content.md"

with open(file_path, "r", encoding="utf-8") as f:
    content = f.read()

# Look for <a href="...">text</a> tags where text might be a job title or contain job terms
matches = re.findall(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', content, re.I)
print(f"Found {len(matches)} anchor tags.")
count = 0
for href, text in matches:
    clean_text = re.sub(r'<[^>]+>', '', text).strip()
    if len(clean_text) > 5 and any(k in clean_text.lower() for k in ["director", "vp", "architect", "cto", "fellow", "chief", "manager"]):
        print(f"LINK: {clean_text} -> {href}")
        count += 1
        if count > 20:
            break
