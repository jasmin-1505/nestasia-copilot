# Operational Notes

## IMPORTANT — Two separate known issues, don't conflate them

### (a) Broken brand-store URL pattern — confirmed, unrelated to bot detection
`brand_categories.csv` rows that point at a Flipkart brand-store URL
(`.../<brand>~brand/pr`) return **HTTP 500** on that page itself — with both
the plain-`requests` and the Playwright discovery script, and regardless of
IP reputation. This is a dead/broken URL pattern, not a blocking measure.
**Fix**: use a `search?q=<brand>+cookware` URL for that brand instead
(e.g. `https://www.flipkart.com/search?q=nestasia+cookware`) — confirmed
working (HTTP 200, real product links returned) for every brand tested so
far. `brand_categories.csv` should only ever contain `search?q=` URLs for
this reason.

### (b) IP-reputation-based blocking — tested (Aug 2026), did not reproduce in a later session
Live-tested from a cloud sandbox environment: Flipkart returned HTTP 403 on
its own homepage, before even reaching a product or category page, using a
standard `requests` session with realistic browser headers and a
homepage-first warm-up. The working theory is IP-reputation-based blocking
of known cloud/datacenter IP ranges (AWS, GCP, Azure, and most VPS
providers), which tends to be more aggressive than blocking of residential
or office IPs.

**Update**: in a later audit session, plain `requests` calls to the Flipkart
homepage and to `search?q=` URLs both returned HTTP 200 from that session's
network — the 403 did not reproduce. This doesn't mean the original finding
was wrong (blocking behavior is plausibly IP-pool- and time-dependent, and
Flipkart's policies can change), but it does mean **the "will NOT work on a
typical cloud VM" claim should be read as environment-dependent, not a
guarantee either way.** Treat blocking as something to check for on the
specific network you're actually running from, not as a settled fact.

**What this means practically:**
- Running this script from a cloud VM might get blocked, or might not —
  this has been observed to vary between sessions/environments.
- Running it from your own laptop/desktop on a home or office internet
  connection is generally the safer bet, since residential IPs tend to
  carry better reputation, but it is not a guarantee either.
- If you want to run this on a schedule from a server rather than your own
  machine and hit blocking, you'd typically need a residential/mobile proxy
  service (e.g. providers used for legitimate market-research scraping)
  routed through the requests session. This adds cost and complexity, and is
  worth validating is within the proposal's "no paid data providers"
  constraint before adopting it — as written, the proposal's data-sources
  section commits to public, low-volume, ToS-respecting collection without
  paid tooling, so a paid proxy service would need a scope conversation
  first.
- **Recommended step**: before relying on a schedule, run
  `discover_products_playwright.py` and `extract_reviews.py` once from
  whichever machine will actually run them, and confirm you're not hitting
  403s from that specific network. If you are, you have two realistic
  options: (a) schedule it locally via your own machine staying on/cron, or
  (b) accept a manual run cadence (open laptop, run script, done) rather
  than full automation.

### (c) Amazon.in bot-check interstitial — confirmed (Sep 2026), single product, single session
Tested `fetch_amazon_reviews()` against one real Nestasia product page
(`amazon.in/dp/B0B4B8CL2F`, found via live search, not constructed). Result:
**HTTP 200** (not a hard block like Flipkart's 403), but the response body
(3.8KB) was Amazon's automated-access interstitial — "Click the button
below to continue shopping," a form posting to
`/errors_page/validateCaptcha` — not the real product page. No
`#productTitle`, no review markup, `div[data-hook='review']` count: 0.

This is a **different failure mode from both Flipkart issues above**: not a
dead URL (the URL is real and correct), and not a hard status-code block
(status was 200). It's a soft bot-check challenge page served in place of
real content.

The existing `fetch_amazon_reviews()` block-detection logic
(`resp.status_code != 200 or "captcha" in resp.text.lower()`) **did
correctly catch this** — the substring `"captcha"` matches because the
challenge form's action URL contains `validateCaptcha`, so the function
logged a warning and returned an empty list rather than silently returning
0 reviews or crashing on missing selectors. Worth flagging: this is a
coincidental match (Amazon's URL happens to contain "captcha"), not a
deliberate detection of the interstitial's actual content — if Amazon
changes that URL/wording, this check could stop catching it silently. No
selector mismatch was diagnosed or ruled out beyond this, since the real
review markup was never reached to test against.

**One test, one product, one session — not yet established as Amazon's
universal behavior.** Could vary by IP, by product, by time of day, the same
way Flipkart's blocking did. Per the no-aggressive-retry rule, this was not
re-tested against other products or with a delay/retry in this session.

### (d) nestasia.in burst-based bot protection — confirmed (Sep 2026), third domain with this pattern
During manual reconnaissance for `own_site_collector.py` (inspecting real
collection-page DOM structure before writing selectors), 8 different
nestasia.in collection URLs were requested back-to-back with **no delay**
between them, outside of the script itself. The first request succeeded
(HTTP 200); every request after it returned **HTTP 403**, including a
retry of the same URL that had just worked. This is nestasia.in's own site
(not a competitor marketplace) — **the third domain in this project
(after Flipkart and Amazon) confirmed to have some form of bot/rate
protection**, so "it's our own site" is not a reason to assume it's safe
from this pattern.

**Important distinction from the finding above**: this was triggered by
unpaced manual testing, not by `own_site_collector.py` itself. When the
script was later run for real against Cookware with its normal
`DELAY_BETWEEN_PAGES_SECONDS = 9` pacing, it completed both pages cleanly
with no 403 — the existing delay appears sufficient against this specific
protection. **Flagging this explicitly so a future editor doesn't look at
that clean run, assume nestasia.in is unprotected, and shorten or remove
the delay** — the 403 is real and reproducible under burst conditions, the
paced script has just stayed under whatever threshold triggers it, in the
one test run so far.

### (e) own_site_collector.py card selector was picking up a sitewide search-drawer widget — found and fixed (Sep 2026)
While validating mismatch detection against Trivets, the first 10 of 19
"SKUs found" turned out to be the exact same 10 Cookware products from the
earlier test run, not trivets. Cause: nestasia.in's predictive-search
drawer (`#SearchDrawerDefault` → `.recommended-products`) is present in the
DOM on every page, sitewide, and reuses the same `[data-product-card]`
markup as the real collection grid for its fixed "recommended products"
list. The original selector queried `[data-product-card]` unscoped, so it
silently picked up both. This means **the earlier Cookware run's reported
count of 22 SKUs was also contaminated** by these same 10 items and should
not be trusted. Fixed by scoping to `#ProductGridContainer
[data-product-card]` (positive scoping into the real grid container,
rather than blocklisting the search drawer specifically, since there could
be other sitewide widgets like it not yet discovered). Re-verified against
Trivets after the fix: 9 real trivet SKUs, matching what a manual count of
the collection page shows.

### (f) Stock-display mismatch bug -- original manual findings vs. this session's re-test (Sep 2026)
**Original manual research (predates this testing session, referenced in
the founder deck's opening slide, the technical report's top risk item, and
the work checklist's "escalate immediately" task):**
- Trivets: 8 SKUs, 8-for-8 mismatched (tag said "In Stock", button showed
  "Sold Out" on all 8).
- Kitchen Racks: 1 SKU, 100% mismatched.
- Bakeware: 2 SKUs -- "Set Of 2 Ceramic Dishes For Baking And Serving" and
  "Glass Baking Dish with Heat Protectant Silicone Grips" (originally
  noted with a trailing "Set of 2") -- both 100% mismatched.

**Re-test results this session**, using the corrected `#ProductGridContainer`
scoping from (e) above:

| Subcategory | SKUs found | Complete or partial? | Same population as manual research? | Mismatches found |
|---|---|---|---|---|
| Cookware | 12 | Unverified -- no independent total was checked; see (g) | n/a -- not a flagged case | 0 |
| Trivets | 9 | Unverified -- no independent total was checked; see (g) | Ambiguous -- count differs from the prior 8, not resolved | 0 |
| Kitchen Racks | 1 | Complete (trivially, single SKU) | Confirmed same SKU -- count matches exactly | 0 |
| Bakeware | 3 | Complete -- both originally-flagged SKU names matched directly, see below | Confirmed -- both originally-flagged SKU names ("Set Of 2 Ceramic Dishes For Baking And Serving", "Glass Baking Dish with Heat Protectant Silicone Grips") matched directly against this run's result and are still present under the same names; the 3rd SKU ("Fluted Glass Casserole Dish With Lid 1600ml") is new, explaining the 2-vs-3 count | 0 |
| Container (jars-canisters) | 12 | **Confirmed PARTIAL/undercount** -- see (g). Real total was previously estimated at ~99 SKUs | Unverified | 0 (within the partial 12 only) |
| Container (fridge-storage-containers) | 12 | **Confirmed PARTIAL/undercount** -- see (g). Real total was previously estimated at ~77 SKUs | Unverified | 0 (within the partial 12 only) |
| Lunch Boxes+Bags (lunch-boxes) | 12 | **Confirmed PARTIAL/undercount** -- see (g). Real total per the site's own counter: 39 | Ambiguous -- prior manual baseline was 18, live site now shows 39 | 0 (within the partial 12 only) |
| Lunch Boxes+Bags (lunch-bags) | 12 | **Confirmed PARTIAL/undercount** -- see (g). Real total per the site's own counter: 50 | Ambiguous -- prior manual baseline sampled 59, live site now shows 50 | 0 (within the partial 12 only) |

**0 mismatches found in every sample collected this session, across 49 SKUs
total.** But this total is misleading if read as "49 SKUs checked" -- see
(g) below. Only Kitchen Racks and Bakeware are confirmed-complete,
genuine same-product re-tests. Cookware and Trivets were never
independently checked against the site's own product-count total, so their
completeness is unverified (they may also be page-1-only undercounts that
happened not to matter because no external baseline flagged a larger
number). **Container and both halves of Lunch Boxes+Bags are now confirmed
to be partial, page-1-only samples** -- their true populations (found via
each collection's own on-page product counter) are far larger than what
this session collected: Container ~99+~77 SKUs vs. 12+12 collected;
Lunch Boxes+Bags 39+50 SKUs vs. 12+12 collected. The "0 mismatches" result
for these four is only a claim about the ~28% of the combined
Container/Lunch Boxes+Bags catalogue that was actually reachable this
session, not the full population.

**Action for the founder**: raise this as a confirmed observation with an
open question, not an asserted fixed fact and not an unresolved alarm:
*"This appears resolved since our original research -- can you confirm
whether your team fixed it, so we know the escalation path works?"*
**But qualify this explicitly**: the confirmation is solid for the two
smallest cases (Kitchen Racks, Bakeware) where genuine same-product
re-tests were possible; it is NOT yet solid for the two largest cases
(Container, Lunch Boxes+Bags), where the tooling could only reach roughly
a quarter of each catalogue before hitting site protection (see (g)). Don't
present the founder-facing line above as covering all five subcategories
equally -- it covers the small ones with real confidence and the large
ones with a coin-flip's worth of coverage.

**Still true after a same-day re-attempt.** A later re-run this same day,
using the now-fixed block-detection logic and double the normal pacing,
was blocked again before collecting any new data (see the "Update" in (g)
below). The Container / Lunch Boxes+Bags numbers above are unchanged from
earlier in the day -- this is not a new, worse finding, just confirmation
that the gap hasn't closed yet.

### (g) own_site_collector.py silently truncates any collection larger than one page -- confirmed (Sep 2026), not yet fixed
Discovered while collecting Lunch Boxes+Bags. `collect_subcategory()`
stops paginating as soon as a page returns 0 NEW product URLs, but it
never checks whether that page actually loaded -- if the page-2+ request
gets served nestasia.in's Cloudflare challenge page (title literally
"Just a moment...", HTTP 403) instead of the real collection page,
`#ProductGridContainer [data-product-card]` naturally finds 0 cards, and
the script reads that as "no more products, stop" instead of "this fetch
was blocked." **This is the same class of bug as Amazon's original
"captcha" substring problem** (see `_detect_amazon_block` and finding (c))
-- a blocked response silently looks identical to a genuinely exhausted
result, just manifesting here as an under-count instead of a false "0
reviews."

**Confirmed directly**: for `lunch-bags`, page 1 succeeded (12 cards) and
page 2 returned HTTP 403 / "Just a moment..." -- but the site's own
product-count indicator ("50 products") proves page 2 has 12 more real,
different products, not zero. A single retry with a 30s backoff recovered
page 2 successfully once. On a second attempt minutes later (collecting
`lunch-boxes` then `lunch-bags` back to back), the block escalated further
-- even page 1 of `lunch-bags`, which had worked cleanly earlier in the
same session, failed after a 30s retry. Per this project's no-aggressive-
retry rule, no further retries were attempted; collection was stopped.

**Practical impact**: every subcategory with more than ~12 products silently
returns only its first page as if that were the whole collection --
confirmed for `jars-canisters` (~99 SKUs really, 12 collected),
`fridge-storage-containers` (~77 SKUs really, 12 collected), `lunch-boxes`
(39 per the site's own counter, 12 collected), and `lunch-bags` (50 per the
site's own counter, 12 collected). Small collections (Kitchen Racks,

**Update -- fix deployed, then re-tested against the live block (Sep 2026,
later same day):** `_detect_nestasia_block()` and the `collection_complete`
flag described below were implemented and unit-tested against the captured
interstitial (no live requests). A live re-attempt at `jars-canisters` was
then made with DOUBLE the normal pacing (18s instead of 9s) to see whether
the block had cleared: attempt 1 got page 1 cleanly (12 real SKUs) then hit
the block on page 2, exactly as designed -- logged loudly, marked
`collection_complete=False`, did not silently under-report. Per the
"one retry, then stop" rule, a single retry was attempted after a 40s
backoff; **it failed even faster than attempt 1**, blocked already on page
1 -- a collection that had loaded fine moments earlier. The run was
stopped immediately per instructions, before touching
`fridge-storage-containers`, `lunch-boxes`, or `lunch-bags` at all.

**Conclusion: the block has not cleared, and doubling the pacing did not
help.** This looks like a longer-duration cooldown (hours, not seconds) is
needed, consistent with how the Flipkart block behaved earlier in this
project. Container and Lunch Boxes+Bags remain exactly as they were:
partial, page-1-only data, `collection_complete=False`. (Small honest
footnote: attempt 1's 12 real `jars-canisters` rows were not persisted to
`nestasia_catalogue.csv` -- a bookkeeping oversight in the ad hoc test
harness discarded them before they were written, not a network issue. No
extra live request was made to recover them, since that would have used up
the one permitted retry for a data-handling mistake rather than a genuine
site block.)

Small collections (Kitchen Racks,
Bakeware, Trivets, and apparently Cookware) were unaffected only because
their true totals happen to fit on one page -- not because the script
handled pagination correctly for them, it just never needed to prove it.

**Not yet fixed (as of the original entry above).** Before scaling to a full
5-subcategory run, or before trusting any "0 mismatches" result on a
collection whose true size wasn't independently checked,
`collect_subcategory()` needs the same kind of deliberate block-detection
`_detect_amazon_block()` got: check the response status and/or page title
for the Cloudflare challenge, distinguish "blocked, retry or stop with a
warning" from "genuinely no more products," and back off substantially
(30s+, informed by what worked above) rather than the standard 9s inter-page
delay once a block is detected. Given the block appeared to escalate with
cumulative session request volume today, a full run may also need much
longer pacing between subcategories than currently used, or splitting the
run across a longer wall-clock window rather than one continuous session.

**Update -- retry attempted a day later (Sep 2026), block re-triggered
almost immediately; a second real bug found and fixed before this retry, not
after.** The working theory going into this retry was a fixed cooldown
window (not a per-request pacing issue), since a doubled 18s delay hadn't
helped the day before. Per instructions, this retry started with a single,
minimal recon request -- one `page.goto()` of `jars-canisters`, nothing
else -- before attempting anything larger.

That single recon request came back completely clean: HTTP 200, the correct
page title (not "Just a moment..."), 12 real product cards. On that basis,
the retry proceeded to the full 4-subcategory run at the standard (not
doubled) 9s pacing, per instructions.

**Before that full run, `collect_subcategory()` was also fixed to apply
the exact page-cap-awareness principle `competitor_collector.py`'s
Wonderchef build required (see (h) below) -- built in this time, not found
after the fact.** The old loop only set `collection_complete=False` on a
detected block; if it instead exited by exhausting
`MAX_PAGES_PER_SUBCATEGORY` while the last page still returned new SKUs
(not the 0-new signal for a genuine end), it would have silently reported
`collection_complete=True` on a page-cap truncation -- the identical failure
shape as finding (h)'s Wonderchef bug, just not yet exercised here because
every subcategory tested cleanly so far had happened to fit within the old
cap of 6 pages. Fixed proactively, and `MAX_PAGES_PER_SUBCATEGORY` raised
from 6 to 12, since the known prior baselines for these exact subcategories
(~99, ~77, 39, 50 SKUs) need up to ~9 pages at ~12 SKUs/page -- at the old
cap, every one of them would have hit the page-cap-truncation branch
regardless of whether the block had cleared, making genuine completion
structurally impossible even on a clean run.

**Then the full run immediately re-hit the block, in a form worse than
previously documented.** `jars-canisters` page 1 succeeded cleanly (12 SKUs,
matching the recon check moments earlier) but page 2 returned HTTP 403 --
the same page-1-clean-page-2-blocked pattern as the day before. What's new
this time: the block did not stay scoped to `jars-canisters`. The very next
subcategory attempted, `fridge-storage-containers` -- a fresh page-1 request
to a URL that had NOT been touched at all yet this session -- was blocked
immediately too, and so were `lunch-boxes` and `lunch-bags` after it, each
on their first request. **This means the block is not purely per-collection-
URL or purely pagination-depth-triggered; once tripped, it appears to cover
the whole site for the remainder of the session**, which is a stronger and
more restrictive form of the block than either prior entry in this finding
established. Per instructions, no further requests were attempted once this
pattern was clear -- the run was not retried, and Prestige/Borosil-equivalent
concerns don't apply here since this is nestasia.in itself, not a
competitor.

**Result persisted honestly, not discarded**: 12 real `jars-canisters` SKUs
(page 1 only) were appended to `nestasia_catalogue.csv` with
`collection_complete=False` -- correctly marked partial, not a lucky small
subcategory that happened to fit on one page the way Kitchen Racks or
Bakeware did. `fridge-storage-containers`, `lunch-boxes`, and `lunch-bags`
contributed zero rows this run (blocked before any page succeeded), so
nothing was appended for them -- their prior partial 12-row baselines from
the day before remain the latest data on file for those three.

**Conclusion: the block has still not durably cleared, roughly a day later.**
A single isolated page load can succeed (both this session's recon check and
the immediately-following real page 1 did), which means a quick manual spot
check is not a reliable signal that pagination-depth collection will work --
the block appears to trigger specifically once a session makes more than a
couple of requests, and once tripped, it now looks broader (whole-session,
not per-URL) than previously documented. The original "fixed cooldown
window, not a pacing issue" theory is still not disproven (this attempt
didn't wait long enough to test a multi-day gap), but "roughly 24 hours" is
now also confirmed NOT long enough on its own. **Lunch Boxes+Bags --
the subcategory pair with the most statistically meaningful prior baseline
(18 and up to 59 SKUs in earlier manual research; 39 and 50 per the site's
own on-page counters). Still does not have a complete, current data point on
whether nestasia.in's stock-display bug is resolved.** This remains the
single most important open question this project's tooling has not yet been
able to answer, purely due to site-side blocking, not a script defect (the
script itself is now believed correct on both fronts that mattered here:
block detection and page-cap-awareness). Before the next attempt, consider
waiting longer than one calendar day, and treat any future single clean
recon request as necessary but explicitly NOT sufficient evidence that a
multi-page run will succeed.

### (h) competitor_collector.py -- first live test (Wonderchef, Sep 2026), and a page-cap under-count bug found and fixed before it ever shipped
`competitor_collector.py` / `competitor_sites.csv` are new, built to the same
conventions as `own_site_collector.py` (config-driven CSV input, block
detection from the first line rather than patched on later, `collection_complete`
flag, `source_type="live_storefront"`, `collected_at`). Tested against exactly
one competitor brand, Wonderchef, per instructions -- Home Centre, Milton,
Prestige, and Borosil are **not yet added** to `competitor_sites.csv` and
should not be until each has had its own live DOM reconnaissance (see below).

**Why Wonderchef first**: a quick reconnaissance pass (HTTP status + title +
rough product-link count) across candidate URLs for all five brands found
Wonderchef the only one that was immediately usable: `homecentre.in` returned
a generic homepage with no real product links from the URL guessed,
`milton.in` and `ttkprestige.com` both 404'd on the guessed category URL, and
`borosil.com/kitchenware` 404'd (redirected to an unrelated corporate page).
Wonderchef returned HTTP 200, a correct page title, and a real product grid on
the first try. **This is not evidence the other four are broken or
protected** -- it's evidence the guessed URLs were wrong; each needs its real
category URL found by hand before any selector work is worth doing.

**Platform note**: Wonderchef runs Shopify on the "t4s" theme -- coincidentally
the same platform family as nestasia.in itself, but a different theme skin, so
none of `own_site_collector.py`'s selectors transfer directly.
`competitor_collector.py` dispatches extraction by a `platform` column
specifically so each brand's real theme can get its own extractor without
forcing a shared (and likely wrong) selector set across brands that turn out
to run on entirely different platforms (Home Centre in particular looks like
it may be a different commerce platform entirely, based on its URL pattern
`/c/HC_HOME_KITCHENDINING` -- not yet confirmed).

**Card-scoping repeat of finding (e)**: exactly like nestasia.in's search-drawer
widget, Wonderchef's collection pages carry "recently viewed" / recommendation
carousels that reuse the identical `.t4s-product[data-product-options]` card
markup as the real grid -- an unscoped query on the Cookware page picked up 40
carousel cards alongside the 30 real ones. Fixed the same way as (e): positive-scope
into `.t4s-main-area` (confirmed unique, contains only real grid cards), not a
carousel blocklist.

**New bug found and fixed before any real run**: the first test run reported
Cookware as 169 SKUs with `collection_complete=True`. That was wrong --
`MAX_PAGES_PER_CATEGORY=6` was hit while page 6 still returned 23 *new* SKUs
(not the 0-new signal that means a genuine end), and a manual check of pages 7
and 8 (outside the capped run) found a full 30 cards on each. So the real
Cookware catalogue is meaningfully larger than 169, and the original logic
would have silently mislabeled a page-cap truncation as a complete collection
-- the same failure shape as finding (g)'s block-detection gap, just triggered
by the page cap instead of a bot challenge. Fixed: `collect_category()` now
also sets `collection_complete=False` when the loop exits by hitting the page
cap while the last page still had new SKUs, not just on a detected block.
Re-run after the fix correctly reported Cookware as
`collection_complete=False` (169 SKUs, partial) and Bakeware as
`collection_complete=True` (34 SKUs, genuine 0-new stop on page 3) --
Bakeware's true population fits within the page cap, Cookware's does not.

**Result of this test**: 203 total SKUs collected across the two Wonderchef
categories tested (Cookware partial at 169, Bakeware complete at 34), zero
403s / interstitials encountered across 9 page loads with the standard 9s
pacing, and **zero stock-state mismatches** found (`stock_mismatch` was
`False` for every row) -- but flagged at the time as an UNPROVEN result, not
a clean one: `stock_status_tag` was "In Stock" for all 203 rows, meaning the
mismatch check had never actually been exercised against a real
out-of-stock item. This exact caveat turned out to matter -- see the
re-check below.

**Before adding the other 4 brands**: each needs the same live reconnaissance
Wonderchef got here -- correct category URL, confirmed platform, confirmed
card selector via manual DOM inspection (not guessed), and a real paginated
test run -- before any row is added to `competitor_sites.csv`. Per instructions,
this has deliberately not been done yet.

**Re-check (Sep 2026, after Home Centre (i) and Milton (l) both confirmed
"hide out-of-stock by default" patterns on their own sites)**: revisited
specifically because Wonderchef's "0 mismatches" had the same shape as those
two brands' initial blind spots -- an untested case wearing a negative's
clothes. Checked, in order, exactly as instructed:

1. **Does Wonderchef have a default in-stock-only filter, applied even on a
   bare URL visit?** No. Confirmed several ways: the URL never changes after
   a bare page load (no client-side redirect, unlike Milton's silent
   `?filter.v.availability=1`); explicit `?filter.v.availability=0`,
   `?filter.v.availability=1`, and `?available=0` query params all returned
   the identical 30 cards (the theme doesn't even process that parameter);
   no `<input type="checkbox">`/`<input type="radio">` facet matching
   avail/stock exists in the DOM; and opening the theme's own "Filter"
   drawer (`.t4s-btn-filter`) and reading all 291 unique facet/label strings
   inside it found zero availability-related options at all (only category,
   price, and similar facets). **Wonderchef's theme simply does not offer a
   stock-availability filter, on the frontend or via URL param** -- there
   was never a hidden default excluding OOS SKUs from view, unlike Home
   Centre and Milton.
2. **How many genuinely sold-out products exist?** Since there's no filter
   to isolate them, the only way to find out was a full, uncapped walk of
   both categories to their true genuine end (not the earlier
   `MAX_PAGES_PER_CATEGORY`-limited runs), checking every card's own
   declared `available` field. Real totals: **Cookware has 264 unique SKUs
   (not 169 -- confirming the original partial run really was an
   undercount, as (h) already flagged), of which 70 are `available:
   false`. Bakeware has 34 SKUs (matching the earlier complete count), of
   which 4 are `available: false`.** So the original 203-row sample's "0
   mismatches" was checking a set of SKUs that, by the luck of pagination
   order, happened to contain zero of the 74 real sold-out products in the
   full catalogue -- exactly the blind-spot shape suspected, now confirmed
   as fact rather than a hypothetical.
3. **For all 74 genuinely sold-out SKUs, checked both signals independently
   via `getComputedStyle` -- not a text regex, not a collapsed metric,
   applying the exact fix Milton's build required (see (l)):** add-to-cart
   button visibility and its own text, checked separately from any sold-out
   badge. Result: **all 74 render the add-to-cart button as visible, with
   its text correctly reading "Sold out"** -- not "+ Add", not blank. Zero
   of the 74 showed an active/misleading add-to-cart control. Spot-checked 3
   of the 74 (`natura-ceramic-fry-pan-20cm-blue-grey`,
   `wonderchef-taurus-hard-anodized-pressure-cooker-5-litre`,
   `waterstone-silicon-turner`) directly against their real product pages:
   all 3 show "OUT OF STOCK", `disabled: true` -- fully consistent with both
   the declared tag and the grid card's button text.

**Conclusion: this is now a genuine, confirmed clean result, not an
unproven one.** Wonderchef has no default availability filter to create a
blind spot in the first place, the mismatch check WAS properly exercised
against all 74 real out-of-stock SKUs across both categories (not a lucky
slice), and every single one correctly showed "Sold out" with no
contradicting signal. Unlike Home Centre (structurally can't produce this
signal at all) and unlike Milton before its own re-check (confirmed 6 real
mismatches, see (l)), **Wonderchef appears to genuinely not have
nestasia.in's stock-display bug** -- not because the test never had a
chance to fail, but because it was given a full chance to and didn't.

### (i) competitor_collector.py -- Home Centre added (Sep 2026): different platform, a stronger completeness check, but a structural gap in stock-mismatch detection
Second competitor brand tested, per instructions specifically because it's
the sharpest identified competitive threat (same aesthetic-led positioning
as Nestasia, far greater physical + SKU scale) -- not just next-on-a-list.
Same one-brand-at-a-time, reconnaissance-first discipline as Wonderchef (h).

**Real category URLs found by live navigation** (not guessed -- the earlier
guess `homecentre.in/.../HC_HOME_KITCHENDINING` in finding (h) was wrong):
- Cookware: `https://www.homecentre.in/in/en/c/kitchen-cooking`
- Bakeware: `https://www.homecentre.in/in/en/c/kitchen-cooking-bakeware`

**Platform, confirmed by live DOM inspection, not assumed**: Next.js +
Material-UI frontend, backed by an Unbxd search/merchandising API -- a
completely different stack from Wonderchef's Shopify theme, despite both
being "modern JS storefronts." New platform key `unbxd_nextjs` added to
`competitor_collector.py`'s `PLATFORM_COLLECTORS` dispatch specifically so
Wonderchef's Shopify selectors were never assumed to transfer.

**Pagination mechanics differ fundamentally from both prior sites**: page 1's
full product listing is embedded as structured JSON in
`window.__NEXT_DATA__` (no DOM scraping needed at all for page 1), but
clicking "NEXT PAGE" is a client-side action that does NOT update that
object -- it fires a fresh request to `search.unbxd.io/.../category?...&page=N`
that returns the identical JSON shape. The collector clicks "NEXT PAGE" and
intercepts that network response (`page.expect_response`) rather than
re-reading the DOM or guessing a URL query param (`?page=N`, `?start=N`, and
`?pn=N` were all tried directly via `page.goto` and every one of them
silently returned page 1's content again -- confirmed dead ends, not
assumptions).

**A stronger `collection_complete` check than either prior site, built in
from the start this time**: the Unbxd API response carries its own
authoritative `numberOfProducts` field (259 for Cookware, 13 for Bakeware),
so completeness here is a direct equality check (`collected == declared
total`) instead of the "stop on 0-new-page" or "stop at a page cap" heuristics
own_site_collector.py and Wonderchef were limited to. Per the explicit
instruction to build yesterday's page-cap lesson in up front rather than
finding it again after the fact, `_collect_unbxd_nextjs()` treats ALL of the
following as `collection_complete=False`: a detected block, a page-cap
exhaustion before reaching the declared total, a 0-new page that still falls
short of the declared total (a real discrepancy, not genuine exhaustion), and
a "NEXT PAGE" button that goes disabled/missing before the declared total is
reached. It is only `True` when the running count actually equals the
declared total. Confirmed working both ways in the same test run: Cookware
(259 declared, 6 pages, last page 19, button correctly disabled after)
-> `True`; Bakeware (13 declared, fits entirely on page 1, no click needed at
all) -> `True` trivially. No block or page-cap scenario was hit live in this
session to exercise the `False` branches end-to-end, so those paths are
implemented per the same principle as the other two but not yet
observed against a real block on this specific site.

**Result of this test**: 272 SKUs collected across Cookware (259) and
Bakeware (13), zero blocks or interstitials across 7 page loads at the
standard 9s pacing (well under the "stop after 2 blocks" threshold this
session was scoped to).

**Taxonomy overlap -- new finding, not a bug**: all 13 Bakeware SKUs turned
out to already be a subset of the 259 Cookware SKUs (`kitchen-cooking-bakeware`
is a child of `kitchen-cooking` in Home Centre's own category tree, confirmed
by comparing `product_url` sets: `bakeware_urls - cookware_urls` is empty).
This is unlike Wonderchef, where Cookware and Bakeware were disjoint sibling
collections. **Don't naively sum per-category SKU counts for Home Centre when
computing a brand total** -- 259 + 13 double-counts 13 real products. Both
rows are still kept in `competitor_sites.csv` because each is a legitimate,
independently-verified category page that returned exactly what it declared;
the overlap is a fact about Home Centre's taxonomy, not a collection error.

**Stock-mismatch detection: a genuinely new, structural finding, not just
"0 mismatches" again**. Unlike nestasia.in and Wonderchef, Home Centre's
category-browse Unbxd API call carries `filter=inStock:"1"` in its own query
(visible directly in the live network request) -- out-of-stock SKUs are
excluded from the listing itself, not merely shown with a disabled button.
Compounding this, the grid card has no add-to-cart control at all (only a
wishlist heart icon; adding to cart happens on the product detail page, not
the listing). So there is no on-page "declared tag vs. rendered button" pair
to compare here at all -- both signals nestasia.in's and Wonderchef's
mismatch check relies on simply don't exist on this surface. `stock_mismatch`
is recorded as the string `"N/A (category browse API filters to in-stock
only -- see NOTES.md finding (i))"` rather than `True`/`False`, and
`stock_status_tag` is "In Stock" for all 272 rows by construction of the
platform's own query, not because 272 independent checks happened to agree.
**This means "0 mismatches on Home Centre" would be a meaningless claim if
stated without this caveat** -- it is answering a different, weaker question
than nestasia.in's confirmed bug did. Whether Home Centre's *product detail
page* (not the listing) has its own version of a declared-vs-rendered stock
mismatch is a genuinely open question this test cannot answer, since it never
visited a PDP.

**Before Milton, Prestige, or Borosil**: per instructions, this session may
proceed to reconnaissance (URL + platform + selectors only, not a full build)
on Milton next, one at a time, since Home Centre worked cleanly with zero
blocks. Prestige and Borosil remain untouched.

### (j) Milton -- reconnaissance only, not built yet (Sep 2026)
Per instructions, only reconnaissance was done here (real URLs, platform,
rough card structure) -- no selectors were wired into `competitor_collector.py`
and no row was added to `competitor_sites.csv`. `milton.in` resolves cleanly
(HTTP 200; yesterday's `milton.in/collections/kitchenware` 404 in finding (h)
was a wrong URL guess, not a broken/blocked site).

**Real category URLs found by live navigation**:
- Cookware: `https://www.milton.in/collections/cookware`
- Closest Bakeware equivalent: `https://www.milton.in/collections/glass-bakeware-casseroles`

**Platform**: Shopify (confirmed: `window.Shopify` present), but explicitly
**NOT** the `t4s` theme Wonderchef runs (`window.Shopify.theme.name` reports a
custom Milton-specific theme build, and neither `.t4s-product[data-product-options]`
nor `.t4s-main-area` nor nestasia.in's `[data-product-card]` matched anything
on the page -- 0 hits for all three). **Do not assume "it's Shopify" means
Wonderchef's selectors apply** -- that would have been exactly the kind of
guess this project's reconnaissance-first discipline exists to avoid. Milton's
card markup uses human-readable BEM-style classes instead
(`.product-card`, `.product-card__wrapper`, `.product-card__image-wrapper`) --
a third distinct Shopify theme layout in this project, after nestasia.in's
and Wonderchef's.

**Pagination mechanism -- resolved in a follow-up recon session (Sep 2026),
with evidence, not a guess**. No plain `<a href="...page=2">` link exists on
this theme (confirmed the first time), and a bare `?page=2` full-page
navigation is a dead end too -- it silently returns page 1's content again
(confirmed directly: identical first-3 product hrefs and identical
"next action" value whether `?page=2` was in the URL or not). The real
mechanism is a "Show more" button (`type="infinite"`,
`is="load-more-button"`) whose `action` attribute is a Shopify **Section
Rendering API** URL:
`https://www.milton.in/collections/cookware?filter.v.availability=1&page=N&section_id=template--21887207571556__collection_product_grid_4BPCL6`.
This is a real, documented Shopify platform feature (fetches just one
theme "section" as an HTML fragment, rather than a full page reload) -- not
a fragile reverse-engineered endpoint. Confirmed working end-to-end:
1. `section_id` is read directly from page 1's DOM (`[id^="shopify-section-"][id*="collection_product_grid"]`,
   stripped of its `shopify-section-` prefix) -- no need to click anything to
   discover it.
2. Every subsequent page is then just a direct `page.request.get(url)` call
   with `page=N` incremented and the same `section_id` appended -- no button
   click, no click-retry flakiness, no network-interception timing needed at
   all (simpler than Home Centre's click-and-intercept requirement).
3. **Two independent, agreeing end-of-pagination signals**, both confirmed
   live for Cookware: the fragment's own "load more" button disappears
   exactly on the last real page (page 5, 16 products), AND the very next
   fetch (page 6) independently comes back with 0 products. Real Cookware
   total observed this way: 94 unique products across pages 1-5 (23 + 17 new
   + 19 + 19 + 16, deduping a couple of boundary repeats between page 1 and
   page 2). Zero blocking across this full walk.
4. One card-extraction wrinkle to handle when this gets built: a raw
   `.product-card` count over-counts vs. unique product URLs (multiple
   anchors per card -- image link, title link, per-variant swatch links) --
   dedup by the href with its `?variant=...` query string stripped, the same
   discipline Wonderchef's handle-based canonical URL served (see (h)).

**Also confirmed**: the button's `action` URL already carries
`filter.v.availability=1` on page 1 by default, with no filter interaction
from this session -- meaning **Milton's default collection browse view also
excludes out-of-stock SKUs**, the same structural pattern as Home Centre (i),
though unresolved here: unlike Home Centre, Milton's grid cards DO render a
real "Add" / "View details" control (seen in the earlier nav scan), so
whether a genuine declared-tag-vs-button mismatch check is possible on this
platform is still an open question for the actual build, not answered by
this recon.

**Conclusion: this mechanism is a real, third distinct pattern in this
project (URL-param pagination for Wonderchef, click-and-intercept for Home
Centre, section-rendering-API direct-fetch for Milton), but it turned out to
be reliable and NOT more complex to automate than the other two** -- no
scroll simulation needed, no XHR guessing, no observed blocking. Milton is
now a reasonable candidate to actually build next session. Per instructions,
this session stopped at reconnaissance -- no `milton` platform collector was
written, and no row was added to `competitor_sites.csv`.

### (l) Milton built and confirmed (Sep 2026) -- the stock-display bug is NOT nestasia.in-specific
Full build, following directly from (j)'s recon. `shopify_hyper_sections`
platform added to `competitor_collector.py`; Milton/Cookware row added to
`competitor_sites.csv`. **This is the first competitor test where a genuine
declared-tag-vs-rendered-button mismatch check actually worked end-to-end --
and it found real mismatches, verified against product-page ground truth.**

**Reproduced recon's number, but with a twist worth recording.** The prior
recon session reported 94 unique products via href-based dedup during a
single `filter.v.availability=1` walk. Re-verifying cleanly before building
(after finding and fixing a `Set`-serialization bug in the recon's own
counting script -- Playwright's `evaluate()` cannot return a JS `Set` object
over the wire, it silently comes back as `{}`, so an unspread `new
Set(...)` return value looked like an empty result in Python without
raising an error) showed the true `filter=1` total is 88, not 94 -- the
recon's own manual counting had a small error, not the site. The standing
collector's actual build run reproduced **96 total unique SKUs** exactly,
matching a from-scratch recount done immediately before the build. Flagging
and resolving this discrepancy BEFORE trusting the build, rather than
after, is exactly the discipline this session was asked to apply.

**A structural discovery changed the whole design, mid-recon-to-build**:
Milton's collection page also redirects itself (client-side, confirmed NOT a
server redirect) to `?filter.v.availability=1` by default -- the same
"hide out-of-stock by default" pattern as Home Centre (i). Collecting only
that default view would have made stock-mismatch detection just as vacuous
here as it is on Home Centre -- structurally unable to see the very SKUs
where a mismatch could exist. Unlike Home Centre, though, Milton's theme
also serves a working `filter.v.availability=0` (out-of-stock-only) view, so
`_collect_shopify_hyper_sections()` walks BOTH filter values and merges them
-- 88 unique via filter=1, 27 unique via filter=0, 19 overlapping (products
with some variants in stock and some not, tagged "Mixed availability" and
excluded from a True/False mismatch verdict rather than forced into one).

**Two real bugs caught and fixed in this collector before trusting its
output -- both by verifying against live ground truth, not by inspection
alone:**
1. First draft's mismatch check flagged 14 false positives: cards for
   multi-variant products render a "View details" link instead of a
   one-click "Add" (Shopify can't add-to-cart without a variant selection),
   which the first draft misread as an implicit "not in stock" signal.
   Verified directly against one flagged product's real product page
   (`/products/hexatech-4-pcs-set-procook-by-milton`): "Add To Cart",
   enabled -- genuinely in stock, no bug. Fixed by treating "no add-to-cart
   control found" as `Unknown`, not a negative signal.
2. Second draft collapsed two independent signals into one: a card's whole
   `innerText` was regex-matched for "sold out", which is the exact
   fragile-coincidental-signal mistake `_detect_amazon_block`'s own comment
   warns against (hidden quick-view-modal text could match without a
   shopper ever seeing it). Investigating this properly, rather than
   assuming the match was safe, uncovered the real bug: the theme has an
   actual `.f-badge--soldout` badge, confirmed **visible** via
   `getComputedStyle` (`display: flex`, `visibility: visible`) -- AND, on
   the same card, the `.product-card-add-btn` quick-add link is ALSO visible
   and active (`display: inline`, not disabled) at the same time. Collapsing
   these into one signal was hiding the actual finding. Fixed by capturing
   both signals independently (`actual_button_state` can now read
   `"Add + Sold Out badge visible"`) and only counting a mismatch when they
   genuinely contradict, per the same tag-vs-button principle
   `own_site_collector.py` established for nestasia.in.

**Result, verified against ground truth, not just internal consistency**:
96 SKUs collected, `collection_complete=True` for both filter passes (each
independently confirmed by the required double end-signal -- "load more"
button gone AND the next page independently empty -- exactly as instructed,
no shortcuts taken even though it cost an extra request per filter pass).
**6 confirmed stock-mismatches**: `triply-stainless-steel-pressure-cooker-
outer-lid`, `flat-dosa-tawa-blackpearl`, `stainless-steel-pressure-cooker-
outer-lid`, `tawa-hard-anodized`, `dosa-tawa-granito-induction`, `tawa-with-
induction-hard-anodised` -- every one of these shows a visible "Sold out"
badge AND a visible, active "Add" quick-add control on the same collection-
page card. Spot-checked 3 of the 6 directly against their real product
pages: all 3 confirmed "Sold Out" / disabled on the PDP, matching the badge
and contradicting the still-active grid card control.

**This is the answer the whole investigation was missing.** Wonderchef's
sample never had an out-of-stock SKU to test against (0 mismatches was an
unproven result, not a clean one -- see (h)). Home Centre's architecture
structurally can't produce this signal at all (see (i)). Milton is the
first site where the check could actually run to completion against real
out-of-stock inventory, and it found the same class of bug nestasia.in has:
**a product correctly flagged as sold out in one part of the UI, while
another part of the same page still offers to add it to cart.** This is now
evidence that the stock-display bug is a pattern that can recur across
different brands and different underlying platforms (nestasia.in and Milton
run different Shopify themes entirely), not something specific to
nestasia.in's own theme or implementation.

Milton's Bakeware-equivalent category was NOT added this session, per
instructions (Cookware only, confirm first). Prestige and Borosil remain
untouched.

### (m) Prestige built and confirmed (Sep 2026) -- fourth platform, no page-cap bug, and a genuine complete clean result
Fourth brand, following the same reconnaissance-first discipline as the
prior three. `magento_luma` platform added to `competitor_collector.py`;
Prestige/Cookware row added to `competitor_sites.csv`.

**Real category URL found by live navigation, not guessed**: the main
`ttkprestige.com` site's own "Cookware" nav link points to a completely
separate subdomain, `https://shop.ttkprestige.com/cookware.html` -- the
`.html`-suffixed URL was the first hint this wouldn't be a fourth Shopify
variant. Confirmed live: `window.checkout` and a `BASE_URL` global (Magento
frontend markers), `mage-cache-storage` in `localStorage`, and
`.products-grid > .product-items > .product-item` markup -- Magento, running
the Amasty "Shop By" layered-navigation extension. A genuine fourth distinct
platform, as expected going in (three brands, three platforms already) --
none of Wonderchef's, Home Centre's, or Milton's selectors, pagination
mechanics, or assumptions carried over. Card container confirmed unique and
uncontaminated before writing any selector: `#amasty-shopby-product-list
.product-item` count matched the unscoped `.product-item` count exactly
(16 == 16) -- unlike nestasia.in and Wonderchef, no sitewide widget reuses
this markup elsewhere on the page, so no positive-scoping fix was needed
beyond confirming that first.

**All three required-in-from-the-start lessons applied, not rediscovered:**

1. **Page-cap vs. genuine-end.** Pagination here is the simplest of the four
   platforms -- plain `?p=N` via direct `page.goto()`, confirmed by
   observing genuinely different product links between `?p=1` and `?p=2`
   (unlike Milton's broken `?page=N`, which silently re-served page 1). But
   simplicity didn't excuse skipping the check: `collect_magento_luma()`
   requires THREE signals to agree before `collection_complete=True` --
   the toolbar's own declared total ("Items X-Y of Z", read once on page 1),
   the "next" pagination link disappearing, AND the following page
   independently coming back empty. This is one more independent signal
   than Milton's two-signal minimum, since Magento happens to expose an
   authoritative declared total the other page-cap-bug platforms didn't.
   `MAX_PAGES_PER_CATEGORY` was also raised from 6 to 25 up front (Prestige
   Cookware alone declares 321 products at 16/page, needing ~21 pages) --
   the same lesson as own_site_collector.py's cap increase: a cap smaller
   than a real known catalogue size makes genuine completion structurally
   impossible regardless of whether the completeness logic is correct.
2. **Default availability filter, checked explicitly, not assumed.**
   Checked the same rigorous way Wonderchef's absence was confirmed, not
   inferred from "this isn't Home Centre or Milton's platform": the bare
   category URL never redirects to a filtered variant (no client-side
   history-API rewrite, unlike Home Centre and Milton), explicit
   query-param guesses had no effect on returned results, no availability
   checkbox/radio exists in the DOM, and reading all facet labels in the
   Amasty filter sidebar (Price, Base, Capacity, Country Of Origin, Includes
   Lid, Material, Power, Product Sub Type, Product Type, Size, Type, Store
   Code) found no availability/stock option at all. **No default
   availability filter exists on Prestige.** Unlike Home Centre and Milton,
   nothing needed to be walked twice to see out-of-stock SKUs -- they're
   already in the default listing.
3. **Two independent signals via computed visibility, ground-truth
   verified.** Scanning the full 321-product catalogue found that Magento's
   Luma catalog-list template only renders a `.stock` block on the GRID
   card when a product is NOT saleable -- confirmed empirically (zero
   `.stock` elements across the first 11 pages, all spot-checked as "In
   stock" on their own product pages) rather than assumed from a Magento
   default. A separate false-positive was checked for and ruled out FIRST,
   before it could contaminate results the way it initially did for Milton:
   many in-stock *configurable* products (need a size/variant picked before
   "Add to Cart" can render) also show no add-to-cart link on the grid --
   3 such cards were spot-checked against their real product pages before
   writing the mismatch logic, all confirmed "In stock" with a working Add
   to Cart. `_magento_is_mismatch()` treats "no add-to-cart control, no
   stock badge either" as `Unknown`, never as a negative signal.

**Result**: **321 SKUs collected, exactly matching the declared total.
`collection_complete=True`, with all three required signals agreeing**
(declared total reached at page 21, next-link gone after page 21, page 22
independently confirmed empty). 133 products are genuinely Out of Stock
(discovered via a full-catalogue scan, not a partial sample -- Magento's
default sort here puts OOS items toward the end, pages 12-21 were
increasingly dominated by them), 188 are In Stock. **Zero real
stock-mismatches found** (`stock_mismatch=True` for 0 of 321 rows) -- 252
rows cleanly agree (both signals present and consistent), and 69 rows are
honestly recorded as `Unknown` (configurable products with no on-grid
add-to-cart signal either way), never miscounted into the clean bucket.

**Ground-truth verified, not just internally consistent**: spot-checked 5
products directly against their real product pages -- 3 randomly-sampled
declared-Out-of-Stock products (all confirmed "Out of stock" on the PDP,
each showing a "Notify Me" button instead of "Add to Cart", which is the
site's own correct handling, not a bug) and 2 randomly-sampled declared-
In-Stock-with-active-Add-button products (both confirmed "In stock" /
"ADD TO CART" on the PDP). All 5 agreed exactly with the collected data.

**This is Prestige's genuine, fully-tested result: like Wonderchef, and
unlike Milton, Prestige does not appear to have nestasia.in's stock-display
bug** -- not because the test never had a chance to fail (all 133 real
out-of-stock SKUs were checked, not a lucky slice, and there was no default
filter hiding them), but because it was given a full chance to and didn't.
Running tally after four brands: nestasia.in (confirmed bug), Home Centre
(architecture can't produce the signal), Milton (confirmed bug, 6 real
mismatches), Wonderchef (properly tested, clean), Prestige (properly tested,
clean). The bug recurs but is not universal.

Per instructions, Prestige's Bakeware-equivalent category was not added
this session, and Borosil, Milton's Bakeware category, and nestasia.in were
not touched.

### (n) Borosil built and confirmed (Sep 2026) -- the fifth and last originally-scoped competitor, a fifth distinct platform, and a second confirmed occurrence of the bug
Last parked competitor, finished with the same reconnaissance-first
discipline as the other four. `shopify_borosil_revamp` platform added to
`competitor_collector.py`; Borosil/Cookware row added to
`competitor_sites.csv`. Borosil already had a `competitor_brand` row (id=5,
created during the earlier paid-ad-creative load) -- reused it, did not
create a duplicate (see the production-load section below).

**Real category URL confirmed live**, not assumed from the project's
original research phase: `myborosil.com` still resolves (HTTP 200), and its
own nav has a "Cookware" -> "View All" link at
`https://myborosil.com/collections/cookware`.

**Platform, confirmed by live inspection**: Shopify again, but a fifth
distinct custom theme (`window.Shopify.theme.name ==
"borosil-revamp/go-live-optimized-030625"`), sharing no markup with
Wonderchef's t4s theme, Milton's Hyper theme, or nestasia.in's own theme.
**Two separate unscoped-selector traps found and avoided before writing any
real selector** -- the same class of mistake this project has hit on
nestasia.in (search drawer) and Wonderchef (carousel), found here twice in
one reconnaissance pass:
1. The first `a[href*='/products/']` on the page was the site's own header
   mega-menu link, not a product card.
2. The obvious `[data-product-id]` attribute is not one-per-card -- it's on
   hidden per-variant swatch `<input>` elements, returning 242 hits on a
   64-product page. The real, positively-scoped, confirmed-unique card
   element is the theme's own custom web component,
   `.product-grid borosil-product-card`.

**A genuine find made this build cleaner than most**: each card embeds the
FULL Shopify product JSON inline (`<script type="application/json"
class="variant-data">`) -- `available`, `price`, `compare_at_price`, and a
`variants` array, all directly parseable, no scattered data-* attributes to
reassemble the way nestasia.in's or Milton's cards required.

**Pagination**: plain `?page=N`, confirmed genuinely different content
between pages. `collection_complete` required the SAME two-independent-
signal agreement Milton's and Prestige's collectors use (next-page link
gone AND the following page independently empty) -- built into
`_collect_shopify_borosil_revamp()` from the start, per instructions, not
discovered as a bug afterward the way it was on Wonderchef (h) and
nestasia.in (g). Confirmed live: next-link gone after page 3, page 4
independently empty -- both signals agreed.

**Default availability filter, checked explicitly**: the collection page
has a genuine "Exclude Out Of Stock" checkbox -- the same kind of tell
Milton's "X of Y" count string was -- but `input.checked === false` on a
fresh load, and behaviorally 6 of the 68 raw cards on the default
(unfiltered) listing are declared unavailable, which a default filter would
have hidden. No dual-pass walk needed, unlike Home Centre (i) and Milton (l).

**Result**: **63 unique SKUs** (68 raw card elements before handle-based
dedup -- at least one product, `borosil-vajra-ceramic-flat-tawa-26-cm`,
rendered under two different card ids on the same page, confirmed the same
handle both times, correctly collapsed to one row).
`collection_complete=True`, both required signals agreeing.

**3 real, ground-truth-verified stock-mismatches found** -- the second
confirmed occurrence of nestasia.in's bug pattern on a competitor site,
after Milton (l): `borosil-vajra-ceramic-flat-tawa-26-cm`,
`borosil-vajra-ceramic-fry-pan-20-cm-900-ml`, and
`borosil-presto-ss-outer-lid-pressure-cooker-3-l`. All three are
**single-variant** products (ruled out the "some other variant is in
stock" explanation directly from the embedded JSON: `variants.length === 1`
and that one variant's own `available` is also `false` -- no ambiguity).
All three were spot-checked against their real product pages, in fresh,
isolated browser contexts specifically to rule out stale cart/session state
as an alternative explanation for an "Added, go to bag"-labeled button --
each one shows a non-disabled add-to-cart button AND a visible "Get
Notified when this product comes In [stock]" restock-alert widget on the
same real page. A restock-alert widget only makes sense for a genuinely
out-of-stock product, and it coexists with a control that gives no
indication anything is wrong -- the same "correct signal exists somewhere
on the page, but the primary action control doesn't reflect it" shape as
Milton's confirmed bug.

**Production load**: `db/load_borosil_skus.py` loaded these 63 rows into
production only, explicitly reusing the existing `competitor_brand` row
(id=5, `name='Borosil'`) rather than creating a second one -- looked up by
name, and the script aborts rather than silently creating a row if none is
found. Independently re-verified afterward (fresh connection): exactly one
`competitor_brand` row named Borosil exists, its 2 `paid_ad_creative` rows
from the earlier ad-creative load and its 63 new `sku` rows are both
correctly linked to that same id, and total `sku` count is 1017 (954 + 63,
matching exactly). The row's `platform` (previously `NULL`) and `notes`
(previously "no full catalogue collector exists for this brand yet") were
updated afterward to reflect that a collector now exists -- leaving that
stale note in place would have been misleading given the same table now
has real per-SKU data alongside the older ad-creative rows for the same
brand.

**Running tally after all five originally-scoped brands**: nestasia.in
(confirmed bug), Home Centre (architecture can't produce the signal),
Milton (confirmed bug, 6 real mismatches), Wonderchef (properly tested,
clean), Prestige (properly tested, clean), Borosil (confirmed bug, 3 real
mismatches). **The bug has now been confirmed on two of the five
competitors actually tested for it (Home Centre's architecture makes it
untestable there), plus nestasia.in itself -- it is a recurring pattern
across unrelated brands and platforms, not a one-off.**

Borosil's Bakeware-equivalent category was not added this session (Cookware
only, per the one-category-first discipline every brand has followed).

## On "real-time" vs incremental refresh
This script is deliberately NOT real-time. Every run:
1. Reads `checkpoint.json` for the last review date seen per product.
2. Fetches the current review list from the platform.
3. Keeps only reviews newer than the checkpoint.
4. Appends those to `reviews_dataset.csv` and updates the checkpoint.

Running it more often than daily adds no analytical value (reviews arrive
slowly) and raises the chance of being rate-limited or blocked. Weekly is
closer to what the proposal itself scopes for Phase 2 ("weekly refresh").
Pick a cadence and schedule it (see README.md) -- don't run it manually
every time you want fresh data; let cron/Task Scheduler do it, from
whichever machine actually gets through (see note above).

## Amazon fallback plan
Amazon.in blocks scripted requests more aggressively than Flipkart — see
"(c) Amazon.in bot-check interstitial" above for a concrete, one-product
test confirming this in practice (not just an assumption). If
`fetch_amazon_reviews()` logs repeated block warnings for a product:
- Don't retry in a tight loop -- that's the fastest way to get an IP banned.
- Fall back to the proposal's own suggested mitigation: manual collection
  for the highest-priority SKUs. A simple spreadsheet with columns
  matching `reviews_dataset.csv` (brand, product_name, rating, review_date,
  review_text, reviewer_name, verified_purchase, source_url) can be merged
  in later with `pandas.concat`.
- Amazon's official Product Advertising API (PA-API) gives structured
  product data (price, rank, images) but does NOT expose review text --
  it's not a substitute for review collection, only a complement.

## Copyright / data handling
Review text is user-generated content and technically copyrighted by the
reviewer, even though platforms host it publicly. For the knowledge base:
- Store full review text internally (source data), but when the copilot
  SURFACES a review-derived insight in a recommendation, it should
  paraphrase / summarize sentiment rather than quote review text verbatim
  at length, and always cite the source URL.
- This mirrors how the proposal's own "Groundedness" and "Citation
  coverage" metrics are meant to work -- claims traceable to a source,
  not verbatim reproduction of the source.

## If Flipkart's page structure changes
Flipkart's CSS class names (e.g. `_27M-vq`, `_3LWZlK`) are auto-generated
and do change periodically. If `fetch_flipkart_reviews()` starts returning
0 results across all products:
1. Open a product review page in a browser.
2. Right-click a review block -> Inspect.
3. Find the new class names for the review container, rating, text, and
   reviewer name.
4. Update the `soup.select(...)` calls in `extract_reviews.py` accordingly.

## Legal / ToS reminder
Per the proposal: public pages only, no login-gated content, respect
robots.txt and rate limits, no paid data providers. This script is built
to that spec -- don't override `REQUEST_DELAY_SECONDS` down to make it
faster; that's the boundary the proposal itself commits to.
