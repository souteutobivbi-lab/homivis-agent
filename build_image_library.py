"""
build_image_library.py
Fetches all professionals from Supabase, segments their images,
and writes a structured JSON library ready for Instagram posting with attribution.

Caption generation:
  1. Gemini Vision  — downloads 2-3 project photos, resizes, sends to gemini-3.5-flash
  2. Groq fallback  — text-only caption via Groq if Gemini is unavailable
  3. Template       — deterministic fallback, never fails

Output: data/image_library.json
Run:    python build_image_library.py

The script is RESUMABLE: if it was interrupted, re-run it and it will
skip professionals already in the output file and continue from where it left off.
"""

import hashlib
import json
import os
import random
import requests
from datetime import datetime, timezone

SUPABASE_URL  = "https://bymrrjapxffdheipdbrk.supabase.co"
SUPABASE_KEY  = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImJ5bXJyamFweGZmZGhlaXBkYnJrIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc3MTg4NDEyMCwiZXhwIjoyMDg3NDYwMTIwfQ.VkOTQvwmnL-ncPd8aScJOhcUJfgGkpVS-_KwpGIYd_0"
R2_PUBLIC_URL = "https://media.homivis.com"
OUTPUT_FILE   = "data/image_library.json"

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}

# ── Specialty keyword map ─────────────────────────────────────────────────────
SPECIALTY_KEYWORDS = {
    "landscape designer":    "garden design, planting schemes, outdoor living, hardscaping, softscaping, garden transformation",
    "landscape gardener":    "garden makeover, lawn care, planting, borders, hedges, garden renovation",
    "interior designer":     "interior styling, bespoke interiors, space planning, colour palette, luxury living",
    "bathroom fitter":       "bathroom renovation, wet room, tiling, sanitary ware, underfloor heating, bathroom remodel",
    "kitchen fitter":        "kitchen installation, bespoke cabinetry, worktops, kitchen remodel, fitted kitchen",
    "builder":               "construction, extensions, loft conversion, structural work, groundwork, renovation",
    "painter decorator":     "painting, decorating, wallpaper, colour consultation, feature walls, finish",
    "electrician":           "electrical installation, rewiring, consumer unit, smart home, lighting design",
    "plumber":               "plumbing, heating, boiler installation, underfloor heating, pipework",
    "plasterer":             "plastering, skimming, coving, rendering, smooth finish",
    "carpenter":             "bespoke joinery, fitted furniture, staircases, doors, woodwork",
    "roofer":                "roofing, flat roof, pitched roof, guttering, roof repair",
    "tiler":                 "tiling, floor tiles, wall tiles, mosaic, natural stone",
    "flooring specialist":   "hardwood flooring, engineered wood, luxury vinyl, carpet, floor installation",
    "architect":             "architectural design, planning permission, drawings, structural design",
    "surveyor":              "property survey, structural survey, valuation, building inspection",
    "damp specialist":       "damp proofing, waterproofing, rising damp, condensation, mould treatment",
    "conservatory installer":"conservatory, orangery, garden room, bi-fold doors, glazing",
    "window fitter":         "window installation, double glazing, uPVC, sash windows, bi-fold doors",
    "home stylist":          "home staging, property styling, interior accessories, soft furnishings",
}

def get_specialty_context(specialties):
    if not specialties:
        return "home improvement, renovation, property, UK homes, quality craftsmanship"
    for s in specialties:
        if s:
            key = s.lower().strip()
            for k, v in SPECIALTY_KEYWORDS.items():
                if k in key or key in k:
                    return v
    return ", ".join(str(s) for s in specialties if s)



# ── Caption generation ────────────────────────────────────────────────────────

# Each pool has enough variety that combining them with seeded randomness
# produces a unique caption per professional. The seed is derived from the
# business name so the same professional always gets the same caption.

INTRO_TEMPLATES = [
    "{name} is a {specialty} based in {location}, listed on Homivis.",
    "Meet {name} — a {specialty} serving {location}, found on Homivis.",
    "{name} has been delivering quality {specialty} work across {location}.",
    "Based in {location}, {name} is a {specialty} you'll find on Homivis.",
    "{name} brings hands-on {specialty} expertise to homeowners in {location}.",
    "If you're looking for a reliable {specialty} in {location}, {name} is on Homivis.",
    "{name} is a trusted {specialty} working across {location} and listed on Homivis.",
    "Homeowners in {location} turn to {name} for expert {specialty} work.",
    "{name} covers {location} as a specialist {specialty} listed on Homivis.",
    "For {specialty} projects in {location}, {name} is a name worth knowing.",
]

DETAIL_TEMPLATES = [
    "Their work covers {kw1}, {kw2} and {kw3} — every job delivered with care and precision that shows in the finished result.",
    "From {kw1} to {kw2}, {name} approaches every project with the same attention to detail and commitment to quality.",
    "Specialising in {kw1}, {kw2} and {kw3}, they bring a level of craft that makes a real difference to any home.",
    "Whether it's {kw1}, {kw2} or {kw3}, the standard of work {name} delivers is consistent from start to finish.",
    "Every project — from {kw1} through to {kw2} — is handled with the kind of expertise that only comes from genuine experience.",
    "{name} focuses on {kw1} and {kw2}, taking pride in a finish that homeowners notice and remember.",
    "With expertise across {kw1}, {kw2} and {kw3}, {name} handles projects of all sizes with the same high standard.",
    "The quality of {name}'s {kw1} and {kw2} work speaks for itself — precise, considered and built to last.",
    "From detailed {kw1} to full-scale {kw2}, {name} brings the same focus and skill to every commission.",
    "Their portfolio spans {kw1}, {kw2} and {kw3} — a range that reflects years of practical experience in the trade.",
]

CTA_TEMPLATES = [
    "Homivis makes it straightforward to find and hire skilled professionals like {name} across the UK.",
    "Find {name} and other verified {specialty}s on Homivis — the platform built for UK homeowners.",
    "Homivis connects homeowners with trusted professionals like {name} — no guesswork, just results.",
    "Browse {specialty}s near you on Homivis and connect with someone like {name} for your next project.",
    "On Homivis, finding a qualified {specialty} in {location} has never been simpler.",
    "Homivis lists verified professionals across the UK — {name} is just one example of the quality available.",
    "Start your next home project the right way — find professionals like {name} on Homivis.",
    "Whether it's {kw1} or {kw2}, Homivis is where UK homeowners find the right professional for the job.",
    "Get your project off the ground with the help of a trusted {specialty} like {name}, found on Homivis.",
    "Homivis takes the uncertainty out of hiring — {name} is one of hundreds of verified UK professionals on the platform.",
]

def generate_caption_for_professional(rec, project_photo_urls=None):
    """Generate a unique, keyword-rich caption from profile data. No API needed."""
    name        = rec.get("businessName") or "a Homivis professional"
    specialties = rec.get("specialties") or []
    specialty   = (specialties[0] or "home professional").strip()
    city        = rec.get("city", "")
    country     = rec.get("country", "")
    location    = city if city else (country if country else "the UK")
    kw_context  = get_specialty_context(specialties)
    kw_list     = [k.strip() for k in kw_context.split(",") if k.strip()]

    # Pad kw_list so we always have at least 3 keywords
    while len(kw_list) < 3:
        kw_list.append(specialty)

    # Deterministic seed from business name — same professional always gets same caption
    seed = int(hashlib.md5(name.encode()).hexdigest(), 16) % (2 ** 32)
    rng  = random.Random(seed)

    # Pick unique keywords
    kw_shuffled = kw_list[:]
    rng.shuffle(kw_shuffled)
    kw1, kw2, kw3 = kw_shuffled[0], kw_shuffled[1], kw_shuffled[2]

    ctx = dict(name=name, specialty=specialty, location=location,
               kw1=kw1, kw2=kw2, kw3=kw3)

    intro  = rng.choice(INTRO_TEMPLATES).format(**ctx)
    detail = rng.choice(DETAIL_TEMPLATES).format(**ctx)
    cta    = rng.choice(CTA_TEMPLATES).format(**ctx)
    credit = f"📸 All work credited to {name}. All rights reserved."

    return f"{intro}\n\n{detail}\n\n{cta}\n\n{credit}", "template"


# ── Hashtags ──────────────────────────────────────────────────────────────────

def build_hashtags(rec):
    specialties = rec.get("specialties") or []
    specialty   = (specialties[0] or "").lower().strip()
    city        = (rec.get("city") or "").lower().replace(" ", "")

    base = ["#homivis", "#ukhomes", "#homeimprovement", "#renovation",
            "#britishhomes", "#homeinspo", "#propertyrenovation"]

    trade_tags = {
        "landscape":  ["#gardendesign", "#landscaping", "#gardentransformation", "#outdoorliving", "#gardeninspo"],
        "interior":   ["#interiordesign", "#interiorstyle", "#homedecor", "#interiorinspo", "#luxuryliving"],
        "bathroom":   ["#bathroomdesign", "#bathroomrenovation", "#bathroominspo", "#wetroom", "#bathroomdecor"],
        "kitchen":    ["#kitchendesign", "#kitchenstyle", "#kitchenrenovation", "#kitcheninspo", "#bespokekitchen"],
        "builder":    ["#construction", "#buildingwork", "#extension", "#loftconversion", "#selfbuild"],
        "painter":    ["#paintinganddecorating", "#decorator", "#interiorpainting", "#featurewall", "#homerefresh"],
        "electrician":["#electricalwork", "#smartHome", "#lightingdesign", "#rewiring", "#electrician"],
        "plumber":    ["#plumbing", "#heating", "#boilerinstallation", "#underfloorheating", "#plumber"],
        "carpenter":  ["#joinery", "#bespokefurniture", "#woodwork", "#carpentry", "#fittedwardrobe"],
        "flooring":   ["#flooringdesign", "#hardwoodflooring", "#floorinstallation", "#luxuryvinyl", "#floorgoals"],
        "tiler":      ["#tiling", "#tiledecor", "#bathroomtiles", "#floortiles", "#tilework"],
        "roofer":     ["#roofing", "#roofreplacement", "#flatroof", "#roofer", "#roofrepair"],
    }

    added = set(base)
    for key, tags in trade_tags.items():
        if key in specialty:
            for t in tags:
                if t not in added:
                    base.append(t)
                    added.add(t)
            break

    if specialty:
        tag = "#" + specialty.replace(" ", "").replace("-", "")
        if tag not in added:
            base.append(tag)
            added.add(tag)

    if city:
        tag = f"#{city}homes"
        if tag not in added:
            base.append(tag)

    return " ".join(base[:20])


# ── Supabase fetching ─────────────────────────────────────────────────────────

def discover_columns():
    url = f"{SUPABASE_URL}/rest/v1/UnclaimedProfessional?select=*&limit=1"
    r = requests.get(url, headers=HEADERS, timeout=30)
    if r.status_code != 200:
        print(f"  Error discovering columns: {r.status_code} {r.text[:300]}")
        return []
    rows = r.json()
    return list(rows[0].keys()) if rows else []


def fetch_all_professionals():
    all_cols = discover_columns()
    print(f"  Available columns: {all_cols}")

    wanted = {
        "id":            ["id"],
        "businessName":  ["businessName", "business_name", "name"],
        "specialties":   ["services", "specialties", "specialty", "niche"],
        "city":          ["city"],
        "state":         ["state"],
        "country":       ["country"],
        "website":       ["website", "websiteUrl", "website_url"],
        "logoUrl":       ["logoUrl", "logo_url", "logo"],
        "bannerUrl":     ["bannerUrl", "banner_url", "banner"],
        "projectPhotos": ["projectPhotos", "project_photos", "photos"],
    }

    select_cols = []
    col_map = {}
    for canon, variants in wanted.items():
        for v in variants:
            if v in all_cols:
                select_cols.append(v)
                col_map[canon] = v
                break

    if not select_cols:
        select_cols = ["*"]
    else:
        print(f"  Fetching columns: {select_cols}")

    all_records = []
    offset = 0
    limit  = 1000

    while True:
        url = (
            f"{SUPABASE_URL}/rest/v1/UnclaimedProfessional"
            f"?select={','.join(select_cols)}&limit={limit}&offset={offset}"
        )
        r = requests.get(url, headers=HEADERS, timeout=30)
        if r.status_code != 200:
            print(f"  Fetch error: {r.status_code} {r.text[:300]}")
            break
        batch = r.json()
        if not batch:
            break
        for row in batch:
            n = {}
            for canon, actual in col_map.items():
                n[canon] = row.get(actual)
            n.update(row)
            all_records.append(n)
        print(f"  Fetched {len(all_records)} professionals...")
        if len(batch) < limit:
            break
        offset += limit

    return all_records


# ── Helpers ───────────────────────────────────────────────────────────────────

def is_valid_image(url):
    return bool(url and isinstance(url, str) and url.startswith("http"))

def image_host(url):
    if R2_PUBLIC_URL in url:
        return "r2"
    if "hzcdn.com" in url or "houzz.com" in url:
        return "houzz"
    return "other"

def build_attribution(rec):
    name        = rec.get("businessName") or "Homivis Professional"
    city        = rec.get("city", "")
    country     = rec.get("country", "")
    specialties = rec.get("specialties") or []
    specialty   = specialties[0] if specialties else "Home Professional"
    location    = ", ".join(p for p in [city, country] if p)
    credit = f"📌 {name}"
    if specialty:
        credit += f" · {specialty}"
    if location:
        credit += f" · {location}"
    return credit


# ── Load existing output (for resume) ────────────────────────────────────────

def load_existing_output():
    """Return (list of existing professionals, set of their IDs)."""
    if not os.path.exists(OUTPUT_FILE):
        return [], set()
    try:
        with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        existing = data.get("professionals", [])
        done_ids = {str(p["professional_id"]) for p in existing}
        print(f"  Resuming: {len(existing)} professionals already in output file.")
        return existing, done_ids
    except Exception as e:
        print(f"  Could not load existing output ({e}), starting fresh.")
        return [], set()


def save_output(professionals):
    """Write current professionals list to the output file."""
    os.makedirs("data", exist_ok=True)
    total_images = sum(p["total_images"] for p in professionals)
    output = {
        "generated_at":        datetime.now(timezone.utc).isoformat(),
        "total_professionals": len(professionals),
        "total_images":        total_images,
        "professionals":       professionals,
    }
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)


# ── Main processing ───────────────────────────────────────────────────────────

def segment_images(records, existing_professionals, done_ids):
    professionals = list(existing_professionals)  # start with what we have
    no_images     = 0
    skipped       = 0
    total         = len(records)

    # Stats for caption sources
    source_counts = {"gemini": 0, "groq": 0, "template": 0}

    for idx, rec in enumerate(records):
        prof_id   = str(rec.get("id") or "")
        name      = rec.get("businessName") or "Unknown Professional"
        city      = rec.get("city", "")
        country   = rec.get("country", "")
        website   = rec.get("website", "")
        credit    = build_attribution(rec)

        if country not in ("GB", "UK"):
            continue

        # Skip if already processed (resume support)
        if prof_id in done_ids:
            skipped += 1
            continue

        # Collect valid images — project photos first for vision, banner last
        project_photos = []
        photos_raw = rec.get("projectPhotos") or []
        for photo in photos_raw:
            if is_valid_image(photo):
                project_photos.append(photo)

        raw_images = []
        for i, url in enumerate(project_photos):
            raw_images.append({"url": url, "image_type": f"project_photo_{i+1}"})

        banner = rec.get("bannerUrl")
        if is_valid_image(banner):
            raw_images.append({"url": banner, "image_type": "banner"})

        if not raw_images:
            no_images += 1
            continue

        print(f"  [{idx+1}/{total}] {name} ({len(raw_images)} images)…")
        caption, source = generate_caption_for_professional(rec, project_photos)
        source_counts[source] = source_counts.get(source, 0) + 1
        hashtags = build_hashtags(rec)

        images = [
            {
                "image_url":  img["url"],
                "image_type": img["image_type"],
                "host":       image_host(img["url"]),
                "posted":     False,
            }
            for img in raw_images
        ]

        professionals.append({
            "professional_id":   prof_id,
            "professional_name": name,
            "specialty":         (rec.get("specialties") or [None])[0],
            "city":              city,
            "country":           country,
            "website":           website,
            "attribution":       credit,
            "instagram_caption": caption,
            "caption_source":    source,
            "hashtags":          hashtags,
            "posted":            False,
            "total_images":      len(images),
            "images":            images,
        })

        # Save after every professional so progress is never lost
        save_output(professionals)

    if skipped:
        print(f"\n  Skipped {skipped} already-processed professionals.")

    return professionals, no_images, source_counts


def main():
    print("Building Homivis image library...\n")

    print("✅ Using profile-based template captions — no API needed, runs instantly.\n")

    print("Fetching professionals from Supabase...")
    records = fetch_all_professionals()
    print(f"Total professionals: {len(records)}\n")

    print("Loading existing output for resume...")
    existing_professionals, done_ids = load_existing_output()
    print()

    print("Processing professionals...")
    professionals, no_images, source_counts = segment_images(records, existing_professionals, done_ids)
    total_images = sum(p["total_images"] for p in professionals)

    print(f"\n{'='*50}")
    print(f"Professionals with images: {len(professionals)}")
    print(f"Professionals with no images: {no_images}")
    print(f"Total images: {total_images}")
    print(f"\nCaption sources this run:")
    for src, count in source_counts.items():
        if count:
            print(f"  {src}: {count}")

    all_imgs  = [img for p in professionals for img in p["images"]]
    banners   = sum(1 for img in all_imgs if img["image_type"] == "banner")
    photos    = sum(1 for img in all_imgs if img["image_type"].startswith("project_photo"))
    on_r2     = sum(1 for img in all_imgs if img.get("host") == "r2")
    on_houzz  = sum(1 for img in all_imgs if img.get("host") == "houzz")

    print(f"\nImage breakdown:")
    print(f"  Project photos: {photos}")
    print(f"  Banners:        {banners}")
    print(f"  On R2 CDN:      {on_r2}  ✅ ready to post")
    print(f"  On Houzz CDN:   {on_houzz}  ⚠️  need migration")

    # Final save (should already be saved incrementally, but do it once more)
    save_output(professionals)
    print(f"\n✅ Saved to {OUTPUT_FILE}")

    if professionals:
        sample = professionals[0]
        print(f"\nSample (first professional):")
        print(f"  Name:   {sample['professional_name']}")
        print(f"  Source: {sample.get('caption_source', 'unknown')}")
        print(f"  Caption preview:")
        for line in sample["instagram_caption"].split("\n")[:4]:
            print(f"    {line}")


if __name__ == "__main__":
    main()
