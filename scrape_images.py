import os
import re
import time
import hashlib
import threading
import requests
from tqdm import tqdm
from concurrent.futures import ThreadPoolExecutor, as_completed

# ---------------- CONFIG ----------------
SCRAPE_URL = "https://piixes.com/"
IMAGE_URL = "https://piixes.com/api/icon/512/{}.png"
SCRAPE_BODY = "[18]"
SLUG_REGEX = re.compile(r"api/icon/128/([a-z0-9\-]+)\.png")

MAX_WORKERS = 8
TIMEOUT = 20
MAX_RETRIES = 4
BACKOFF_BASE = 1.5
CHECKPOINT_INTERVAL = 5  # Save every N new slugs

# Optional: Hard-code cookie to skip manual input
# Leave as empty string to be prompted each run
HARDCODED_COOKIE = ""
# ----------------------------------------

session = requests.Session()
lock = threading.Lock()

def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def atomic_write(path, content):
    """Write file atomically using temp + rename."""
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        f.write(content)
    os.replace(tmp_path, path)

def load_checkpoint(path):
    """Load slugs from checkpoint file."""
    if not os.path.exists(path):
        return set()
    with open(path, "r") as f:
        return set(line.strip() for line in f if line.strip())

def save_checkpoint(path, slugs):
    """Save slugs to checkpoint file atomically."""
    atomic_write(path, "\n".join(sorted(slugs)) + "\n")

def scrape_slugs(headers, checkpoint_path):
    slugs = load_checkpoint(checkpoint_path)
    initial_count = len(slugs)
    
    if initial_count > 0:
        print(f"[scrape] Loaded {initial_count} slugs from checkpoint")
    
    rounds = 0
    new_since_save = 0

    while True:
        rounds += 1
        r = session.post(
            SCRAPE_URL,
            headers=headers,
            data=SCRAPE_BODY,
            timeout=TIMEOUT
        )

        found = 0
        for slug in SLUG_REGEX.findall(r.text):
            if slug not in slugs:
                slugs.add(slug)
                found += 1
                new_since_save += 1

        print(f"[scrape] round {rounds}: +{found}, total {len(slugs)}")

        # Checkpoint every N new slugs
        if new_since_save >= CHECKPOINT_INTERVAL:
            with lock:
                save_checkpoint(checkpoint_path, slugs)
                print(f"[scrape] ✓ Checkpoint saved ({len(slugs)} slugs)")
            new_since_save = 0

        if found == 0:
            break

        time.sleep(0.6)

    # Final save
    with lock:
        save_checkpoint(checkpoint_path, slugs)
        print(f"[scrape] ✓ Final checkpoint saved ({len(slugs)} slugs)")

    return list(slugs)

def download(slug, out_dir, seen_hashes, completed_path):
    # Create nested directory structure
    img_dir = os.path.join(out_dir, "piixes.com", "api", "icon", "512")
    os.makedirs(img_dir, exist_ok=True)
    
    path = os.path.join(img_dir, f"{slug}.png")
    
    # Check if already downloaded in this session or previous
    if os.path.exists(path):
        return "skip"

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = session.get(
                IMAGE_URL.format(slug),
                timeout=TIMEOUT
            )

            if r.status_code != 200:
                raise Exception("non-200")

            digest = sha256(r.content)

            with lock:
                if digest in seen_hashes:
                    return "dedup"
                seen_hashes.add(digest)

            with open(path, "wb") as f:
                f.write(r.content)

            # Track successful download
            with lock:
                with open(completed_path, "a") as cf:
                    cf.write(f"{slug}\n")

            return "ok"

        except Exception:
            if attempt == MAX_RETRIES:
                return "fail"
            time.sleep(BACKOFF_BASE ** attempt)

def main():
    out_dir = input("Output folder path: ").strip()
    os.makedirs(out_dir, exist_ok=True)

    checkpoint_path = os.path.join(out_dir, "slugs_checkpoint.txt")
    completed_path = os.path.join(out_dir, "completed_downloads.txt")
    failed_path = os.path.join(out_dir, "failed_downloads.txt")

    # Use hard-coded cookie or prompt user
    if HARDCODED_COOKIE:
        cookie = HARDCODED_COOKIE
        print("\n[+] Using hard-coded cookie from config")
    else:
        print("\n" + "="*70)
        print("HOW TO GET YOUR COOKIE:")
        print("="*70)
        print("1. Open https://piixes.com in your browser")
        print("2. Press F12 to open Developer Tools")
        print("3. Go to the 'Network' tab")
        print("4. Refresh the page (F5)")
        print("5. Click on any request to piixes.com")
        print("6. Find 'Cookie:' in Request Headers section")
        print("7. Copy the entire cookie value (everything after 'Cookie: ')")
        print("="*70)
        print()
        cookie = input("Paste Cookie header value: ").strip()

    headers = {
        "accept": "text/x-component",
        "content-type": "text/plain;charset=UTF-8",
        "origin": "https://piixes.com",
        "referer": "https://piixes.com/",
        "user-agent": "Mozilla/5.0",
        "cookie": cookie
    }

    print("\n[+] Scraping slugs…")
    slugs = scrape_slugs(headers, checkpoint_path)
    print(f"[+] Total slugs discovered: {len(slugs)}\n")

    # Load already completed downloads
    completed = load_checkpoint(completed_path)
    if completed:
        print(f"[+] Found {len(completed)} previously completed downloads\n")
    
    # Filter out already completed
    slugs_to_download = [s for s in slugs if s not in completed]
    
    if not slugs_to_download:
        print("[+] All downloads already completed!")
        return

    print(f"[+] Downloading {len(slugs_to_download)} PNGs…")

    seen_hashes = set()
    results = {"ok": 0, "skip": 0, "dedup": 0, "fail": 0}
    failed_slugs = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(download, slug, out_dir, seen_hashes, completed_path): slug
            for slug in slugs_to_download
        }

        for future in tqdm(as_completed(futures), total=len(futures)):
            slug = futures[future]
            status = future.result()
            
            with lock:
                results[status] += 1
                if status == "fail":
                    failed_slugs.append(slug)

    # Save failed downloads for manual retry
    if failed_slugs:
        atomic_write(failed_path, "\n".join(failed_slugs) + "\n")
        print(f"\n[!] {len(failed_slugs)} failed downloads saved to {failed_path}")

    print("\nDone.")
    for k, v in results.items():
        print(f"{k:>6}: {v}")

if __name__ == "__main__":
    main()