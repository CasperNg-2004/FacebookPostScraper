"""
Facebook Public Page Post Scraper
----------------------------------
Scrapes posts from Facebook public pages using Playwright, expands truncated
"See more" text, and saves the results as JSON for downstream sentiment analysis.

IMPORTANT NOTES
- Facebook actively detects and blocks automated browsing. Expect selector
  breakage over time; you will likely need to update the CSS/text selectors
  below periodically.
- Most content (especially comments and full post text) requires a logged-in
  session, even on "public" pages. This script logs in once and reuses a
  saved storage_state (cookies + local storage) for future runs so you don't
  have to log in every time.
- Scraping Facebook is against their Terms of Service. Use responsibly, at a
  reasonable rate, and be mindful of privacy laws (GDPR/PDPA/etc.) if you
  capture commenter names or profile info.
- This targets Facebook's mobile web UI (m.facebook.com / mbasic fallback
  intentionally avoided) via the desktop UI in headful/headless Chromium,
  since it tends to be a bit more stable for scripted scrolling than mbasic.

Install:
    pip install playwright --break-system-packages
    playwright install chromium

Usage:
    python fb_scraper.py --pages "https://www.facebook.com/somepage" "https://www.facebook.com/anotherpage" \
        --limit 1000 --output posts.json
"""

import argparse
import asyncio
import json
import os
import random
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

STORAGE_STATE_PATH = "fb_session.json"
FB_EMAIL = os.environ.get("FB_EMAIL")
FB_PASSWORD = os.environ.get("FB_PASSWORD")

DEFAULT_PAGES = [
    "https://www.facebook.com/utarconfession2022.2023",
    "https://www.facebook.com/uc20212022",
]


# --------------------------------------------------------------------------
# Login / session handling
# --------------------------------------------------------------------------

async def session_is_valid(page):
    """Check whether the loaded storage_state still represents a logged-in session."""
    await page.goto("https://www.facebook.com/", wait_until="domcontentloaded")
    try:
        await page.wait_for_selector('[aria-label="Create a post"], div[role="feed"]', timeout=8_000)
        return True
    except PlaywrightTimeoutError:
        return False


async def ensure_logged_in(context, page):
    """Reuse a saved session if it's still valid; otherwise perform login and re-save it."""
    if Path(STORAGE_STATE_PATH).exists():
        if await session_is_valid(page):
            return
        print("Saved session appears expired/invalid -- logging in again.")

    if not FB_EMAIL or not FB_PASSWORD:
        print(
            "No saved session found and FB_EMAIL/FB_PASSWORD env vars are not set.\n"
            "Set them, or log in manually in the opened browser window within 90s.",
            file=sys.stderr,
        )

    await page.goto("https://www.facebook.com/login", wait_until="domcontentloaded")

    if FB_EMAIL and FB_PASSWORD:
        try:
            await page.fill('input[name="email"]', FB_EMAIL)
            await page.fill('input[name="pass"]', FB_PASSWORD)
            await page.click('button[name="login"]')
        except PlaywrightTimeoutError:
            pass

    # Give time for 2FA / manual login if needed (headful mode recommended).
    try:
        await page.wait_for_selector('[aria-label="Create a post"], div[role="feed"]', timeout=90_000)
    except PlaywrightTimeoutError:
        print("Could not confirm login automatically. Make sure you're logged in, then press Enter.")
        input()

    await context.storage_state(path=STORAGE_STATE_PATH)
    print(f"Session saved to {STORAGE_STATE_PATH}")


# --------------------------------------------------------------------------
# "See more" expansion
# --------------------------------------------------------------------------

async def expand_see_more(page):
    """Click every visible 'See more' expander so post text isn't truncated.
    (Any comment text this also expands gets stripped out later during
    extraction, so there's no need to scope this to post-only elements.)"""
    await page.evaluate(
        """
        () => {
            const buttons = Array.from(document.querySelectorAll(
                'div[role="button"], span'
            )).filter(el => /see more/i.test(el.innerText || '') && el.innerText.trim().length < 20);
            for (const btn of buttons) {
                try { btn.click(); } catch (e) {}
            }
        }
        """
    )
    await page.wait_for_timeout(200)


# --------------------------------------------------------------------------
# Post extraction
# --------------------------------------------------------------------------

async def extract_posts_from_dom(page, page_name):
    """
    Pull post text out of the currently loaded feed DOM, WITHOUT the comment
    thread text. On these pages, each post's [role="article"] contains the
    post itself AND its full comment thread nested inside the SAME element
    (not a separate sibling/descendant article) -- so grabbing el.innerText
    directly includes every visible comment's text too.

    Fix: find the post's action row (Like / Comment / Share) -- comments
    always render AFTER this row in the DOM -- and prune the action row plus
    everything that comes after it, keeping only what's before it (header,
    hashtag, message text, reaction count). This sidesteps guessing at
    comment-container selectors entirely.
    """
    raw_result = await page.evaluate(
        """
        () => {
            const selectors = [
                'div[role="feed"] div[aria-posinset]',
                'div[role="main"] div[aria-posinset]',
                'div[role="feed"] div[role="article"]',
                'div[role="main"] div[role="article"]',
                'div[aria-posinset]',
                'div[role="article"]'
            ];
            let all = [];
            for (const sel of selectors) {
                all = Array.from(document.querySelectorAll(sel));
                if (all.length > 0) break;
            }

            // Only keep truly top-level articles (skip ones nested inside
            // another article, in layouts where that DOES happen) AND skip
            // anything Facebook itself has labeled as a comment. Individual
            // comments render as their own role="article" elements here,
            // each carrying aria-label="Comment by <name> <time> ago" --
            // that's a direct, reliable signal, far better than guessing at
            // DOM structure.
            const topLevel = all.filter(el => {
                const ownLabel = el.getAttribute('aria-label') || '';
                if (/comment by/i.test(ownLabel) || /reply by/i.test(ownLabel)) return false;

                // Skeletons have no text and will be skipped later, but we can
                // explicitly skip ones where the article itself is labeled as loading.
                // We MUST NOT use el.querySelector('[data-visualcompletion="loading-state"]')
                // because real posts often contain lazy-loaded images with this attribute!
                if (el.getAttribute('data-visualcompletion') === 'loading-state') return false;
                if (ownLabel.toLowerCase().includes('loading')) return false;

                // Ignore posts that contain an image or video (as requested by user).
                // We check for <video> tags, or <img> tags that are larger than typical
                // profile pictures (which are ~40x40). We also ignore emojis.
                const isMediaPost = Array.from(el.querySelectorAll('img, video')).some(media => {
                    if (media.tagName.toLowerCase() === 'video') return true;
                    
                    const src = media.getAttribute('src') || '';
                    if (src.includes('emoji')) return false;
                    
                    // Check rendered size in the DOM
                    const rect = media.getBoundingClientRect();
                    if (rect.width > 100 || rect.height > 100) return true;
                    
                    return false;
                });
                
                if (isMediaPost) return false;

                let p = el.parentElement;
                while (p) {
                    if (p.matches && (p.matches('div[aria-posinset]') || p.matches('div[role="article"]'))) return false;
                    p = p.parentElement;
                }
                return true;
            });

            // Remove `boundaryEl` and everything that comes after it in
            // document order (its later siblings, and its ancestors' later
            // siblings, all the way up to `root`).
            const pruneFromBoundaryOnward = (root, boundaryEl) => {
                let node = boundaryEl;
                while (node && node !== root) {
                    let sibling = node.nextSibling;
                    while (sibling) {
                        const toRemove = sibling;
                        sibling = sibling.nextSibling;
                        toRemove.remove();
                    }
                    node = node.parentNode;
                }
                if (boundaryEl && boundaryEl.parentNode) {
                    boundaryEl.remove();
                }
            };

            const getPostOnlyText = (el) => {
                // PRIMARY method: Facebook marks the post's own caption text
                // with a dedicated attribute, completely separate from the
                // header, action bar, and comments.
                const messageNode = el.querySelector(
                    'div[data-ad-preview="message"], div[data-ad-comet-preview="message"]'
                );
                if (messageNode) {
                    const t = (messageNode.innerText || '').trim();
                    if (t) return { text: t, method: 'message-attr' };
                }

                // FALLBACK: boundary pruning at the action row.
                const clone = el.cloneNode(true);

                // Find the first "Like", "Comment", or "Share" button. Since the post's
                // own action bar appears before any comments in the DOM, this safely
                // targets the boundary between the post and its comments.
                const clickable = Array.from(clone.querySelectorAll('div[role="button"], span, a'));
                const actionBtn = clickable.find(b => {
                    const t = (b.innerText || '').trim().toLowerCase();
                    return t === 'like' || t === 'comment' || t === 'share';
                });

                if (actionBtn) {
                    pruneFromBoundaryOnward(clone, actionBtn);
                    
                    // Attempt to specifically target the post text without the header
                    // (author name, timestamp) by finding the longest text container.
                    const autoDirs = Array.from(clone.querySelectorAll('div[dir="auto"]'));
                    if (autoDirs.length > 0) {
                        let longest = autoDirs.reduce((a, b) => 
                            (a.innerText || '').length > (b.innerText || '').length ? a : b
                        );
                        
                        // To avoid truncating multi-paragraph posts (where each paragraph is its own div[dir="auto"]),
                        // we grab the parent container of the longest paragraph.
                        let body = longest.parentElement;
                        // Sometimes we need to go up one more level if it's deeply nested
                        if (body && body.parentElement && body.parentElement !== clone) {
                            body = body.parentElement;
                        }
                        
                        const cleanText = (body.innerText || '').trim();
                        if (cleanText.length > 10) {
                            return { text: cleanText, method: 'action-row-text-dir-auto-parent' };
                        }
                    }

                    // Ultimate fallback: return everything that was left after pruning
                    return { text: (clone.innerText || '').trim(), method: 'action-row-text' };
                }

                // Fallback 3: no action-bar found at all (unexpected layout) --
                // remove obviously comment-related regions as a best effort.
                const killSelectors = ['form', '[aria-label*="omment" i]', '[aria-label*="eply" i]'];
                killSelectors.forEach(sel => {
                    clone.querySelectorAll(sel).forEach(node => node.remove());
                });
                return { text: (clone.innerText || '').trim(), method: 'aria-label-strip' };
            };

            // Compute once per element (avoid double work + lets us tally
            // which extraction method actually fired for each post).
            const extracted = topLevel.map(el => {
                const rawText = (el.innerText || '').trim();
                const { text, method } = getPostOnlyText(el);
                return { el, rawText, text, method };
            });

            const methodCounts = {};
            extracted.forEach(e => {
                methodCounts[e.method] = (methodCounts[e.method] || 0) + 1;
            });

            const debugSamples = extracted.map(e => ({
                rawLength: e.rawText.length,
                strippedLength: e.text.length,
                method: e.method,
            }));

            const posts = extracted.map(e => {
                const link = e.el.querySelector(
                    'a[href*="/posts/"], a[href*="/permalink/"], a[href*="story_fbid"]'
                );
                return {
                    text: e.text,
                    url: link ? link.getAttribute('href') : null,
                    timestampText: link ? (link.innerText || '').trim() : ''
                };
            });

            // Pick the article with the most raw text -- most likely to have
            // a substantial comment thread attached, useful for inspection.
            let biggestIdx = 0;
            let biggestLen = -1;
            debugSamples.forEach((d, i) => {
                if (d.rawLength > biggestLen) {
                    biggestLen = d.rawLength;
                    biggestIdx = i;
                }
            });

            return {
                posts,
                debug: {
                    rawArticleCount: all.length,
                    topLevelCount: topLevel.length,
                    lengths: debugSamples,
                    methodCounts: methodCounts,
                    firstArticleHTML: topLevel.length > 0 ? topLevel[0].outerHTML.slice(0, 5000) : null,
                    biggestArticleFullHTML: topLevel.length > 0 ? topLevel[biggestIdx].outerHTML : null
                }
            };
        }
        """
    )

    raw_posts = raw_result["posts"]
    debug_info = raw_result["debug"]
    print(
        f"  [debug] raw_articles={debug_info['rawArticleCount']} "
        f"top_level={debug_info['topLevelCount']} method_counts={debug_info.get('methodCounts')}"
    )
    print(f"  [debug] lengths={debug_info['lengths']}")

    # Dump the biggest REAL article's full HTML once, so we can inspect the
    # actual comment-thread markup. Skip loading skeletons (near-zero text) --
    # only lock this in once we see substantial content (real post + likely
    # comments), and keep retrying on subsequent scrolls until then.
    dump_path = Path(f"debug_full_article_{page_name}.html")
    biggest_len = max((l["rawLength"] for l in debug_info["lengths"]), default=0)
    if debug_info.get("biggestArticleFullHTML") and biggest_len >= 200:
        with open(dump_path, "w", encoding="utf-8") as f:
            f.write(debug_info["biggestArticleFullHTML"])
        print(f"  [debug] Saved full article HTML sample (rawLength={biggest_len}) -> {dump_path}")

    if debug_info["topLevelCount"] > 0 and all(l["strippedLength"] == 0 for l in debug_info["lengths"]):
        with open(f"debug_stripped_empty_{page_name}.html", "w", encoding="utf-8") as f:
            f.write(debug_info["firstArticleHTML"] or "")
        print(f"  [debug] Stripping produced empty text for all articles -- saved debug_stripped_empty_{page_name}.html for inspection.")

    results = []
    for item in raw_posts:
        text = (item.get("text") or "").strip()
        if not text:
            continue

        href = item.get("url")
        post_url = href.split("?")[0] if href else ""
        created_time = item.get("timestampText") or ""

        results.append(
            {
                "post_id": page_name,       # page identifier, per requested schema
                "text": text,
                "created_time": created_time,
                "source_page": post_url,     # direct link to this specific post
                "url": "",                   # reserved/unused, kept for schema compatibility
            }
        )

    return results


# --------------------------------------------------------------------------
# Main scroll + collect loop
# --------------------------------------------------------------------------

async def scrape_page(page, page_url, post_limit):
    page_name = page_url.rstrip("/").split("/")[-1]
    print(f"\n=== Scraping {page_name} (target: {post_limit} posts) ===")

    await page.goto(page_url, wait_until="domcontentloaded")
    
    # Wait for the page to render real posts (not just skeletons)
    try:
        await page.wait_for_function(
            "() => {"
            "  const posts = document.querySelectorAll('div[aria-posinset], div[role=\"article\"]');"
            "  if (posts.length === 0) return false;"
            "  for (const p of posts) {"
            "    if (!p.querySelector('[data-visualcompletion=\"loading-state\"]')) return true;"
            "  }"
            "  return false;"
            "}",
            timeout=15000
        )
    except PlaywrightTimeoutError:
        print("  [debug] Timeout waiting for real articles to load. Will proceed anyway.")
        
    await page.wait_for_timeout(5000)

    # --- Diagnostics: if nothing matches at all, dump evidence instead of
    # silently looping 0/0/0 six times and giving up. ---
    feed_count = await page.locator('div[role="feed"]').count()
    main_count = await page.locator('div[role="main"]').count()
    article_count = await page.locator('div[role="article"]').count()
    login_wall = await page.locator('text=Log in to continue').count() + await page.locator('text=You must log in').count()

    print(f"  [debug] role=feed: {feed_count}, role=main: {main_count}, role=article: {article_count}, login_wall_hits: {login_wall}")

    if article_count == 0:
        debug_png = f"debug_{page_name}.png"
        debug_html = f"debug_{page_name}.html"
        await page.screenshot(path=debug_png, full_page=True)
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(await page.content())
        print(f"  [debug] No articles found at all. Saved {debug_png} and {debug_html} for inspection.")
    
    # Temporarily dump full page HTML to investigate DOM structure
    with open(f"full_page_{page_name}.html", "w", encoding="utf-8") as f:
        f.write(await page.content())

    seen_ids = set()
    seen_texts = set()
    collected = []

    stagnant_scrolls = 0
    max_stagnant_scrolls = 15  # Increased from 6 to give it more time to load

    while len(collected) < post_limit and stagnant_scrolls < max_stagnant_scrolls:
        await expand_see_more(page)
        new_posts = await extract_posts_from_dom(page, page_name)

        added = 0
        for post in new_posts:
            # deduplicate by URL or text, NOT by post_id (which is just the page name)
            dedup_key = post["source_page"] or post["text"][:120]
            if dedup_key in seen_ids or dedup_key in seen_texts:
                continue
            
            if post["source_page"]:
                seen_ids.add(post["source_page"])
            else:
                seen_texts.add(dedup_key)
                
            collected.append(post)
            added += 1

        if added == 0:
            stagnant_scrolls += 1
        else:
            stagnant_scrolls = 0

        print(f"  collected so far: {len(collected)} (+{added})")

        # Human-like scroll with jitter to reduce detection / rate limiting.
        # Increased scroll amount slightly to ensure we hit the lazy-load trigger
        await page.mouse.wheel(0, random.randint(2500, 3500))
        await page.wait_for_timeout(random.randint(2000, 3500))

    return collected[:post_limit]


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def get_versioned_output_path(base_path: str) -> Path:
    """Given e.g. 'posts.json', return the next unused 'posts(1).json',
    'posts(2).json', etc. so every run gets its own file instead of
    overwriting the previous one."""
    p = Path(base_path)
    stem = p.stem
    suffix = p.suffix or ".json"
    parent = p.parent if str(p.parent) != "." else Path(".")

    n = 1
    while True:
        candidate = parent / f"{stem}({n}){suffix}"
        if not candidate.exists():
            return candidate
        n += 1


async def main(pages, post_limit, output_path, headless):
    output_path = get_versioned_output_path(output_path)
    print(f"Output will be saved to: {output_path}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)

        context_kwargs = {}
        session_path = Path(STORAGE_STATE_PATH)
        if session_path.exists():
            try:
                raw = session_path.read_text(encoding="utf-8").strip()
                if raw:
                    json.loads(raw)  # validate it's real JSON before handing to Playwright
                    context_kwargs["storage_state"] = STORAGE_STATE_PATH
                else:
                    print(f"{STORAGE_STATE_PATH} is empty -- ignoring it and logging in fresh.")
            except json.JSONDecodeError:
                print(f"{STORAGE_STATE_PATH} is not valid JSON -- ignoring it and logging in fresh.")

        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            ),
            **context_kwargs,
        )
        page = await context.new_page()

        await ensure_logged_in(context, page)

        all_results = []
        for page_url in pages:
            posts = await scrape_page(page, page_url, post_limit)
            all_results.extend(posts)

            # Save incrementally after each page in case of a crash later.
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(all_results, f, ensure_ascii=False, indent=2)

        await browser.close()

    total = len(all_results)
    print(f"\nDone. Saved {total} posts across {len(pages)} page(s) to {output_path}")


def parse_args():
    parser = argparse.ArgumentParser(description="Scrape Facebook public page posts.")
    parser.add_argument(
        "--pages",
        nargs="+",
        default=DEFAULT_PAGES,
        help="Facebook page URLs to scrape (defaults to the two fixed UTAR confession pages).",
    )
    parser.add_argument("--limit", type=int, default=1000, help="Target number of posts PER page.")
    parser.add_argument("--output", default="posts.json", help="Output JSON file path.")
    parser.add_argument("--headful", action="store_true", help="Run browser with a visible window (recommended for login).")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(main(args.pages, args.limit, args.output, headless=not args.headful))
