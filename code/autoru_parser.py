"""Download public Auto.ru listings and photos. Python 3.10+, Pillow.

Only ordinary public HTTP requests are used. Login pages, CAPTCHAs and rate
limits stop the run; no private API, proxy rotation or challenge solving.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import io
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener

from PIL import Image, UnidentifiedImageError

ROOT = Path(__file__).resolve().parents[1]
LISTING_RE = re.compile(r"https?://(?:www\.)?auto\.ru/cars/(?:used|new)/sale/[^\s\"'<>?#]+", re.I)
ID_RE = re.compile(r"/(\d{6,}(?:-[a-zA-Z0-9]+)?)/?$")


class AccessBlocked(RuntimeError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def clean(value):
    return re.sub(r"\s+", " ", html.unescape(str(value))).strip() if value is not None else None


def number(value):
    if isinstance(value, dict):
        value = value.get("value")
    if value is None or isinstance(value, bool):
        return None
    match = re.search(r"-?\d+(?:[.,]\d+)?", str(value).replace("\u00a0", "").replace(" ", ""))
    if not match:
        return None
    result = float(match[0].replace(",", "."))
    return int(result) if result.is_integer() else result


def first(*values):
    return next((value for value in values if value is not None and value != "" and value != []), None)


def dig(obj, *path):
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def walk(obj):
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from walk(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from walk(value)


def canonical_url(value, base="https://auto.ru/"):
    value = html.unescape(str(value)).replace("\\/", "/")
    parsed = urlsplit(urljoin(base, value))
    if parsed.scheme not in ("http", "https") or parsed.hostname not in ("auto.ru", "www.auto.ru"):
        return None
    if not re.fullmatch(r"/cars/(?:used|new)/sale/[^?#]+", parsed.path):
        return None
    return urlunsplit(("https", "auto.ru", parsed.path.rstrip("/") + "/", "", ""))


def listing_id(url):
    match = ID_RE.search(urlsplit(url).path)
    if not match:
        raise ValueError(f"Cannot extract listing ID from {url}")
    return match[1]


class Page(HTMLParser):
    """Extract JSON scripts, metadata, links and public specification labels."""
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.scripts, self.meta, self.links = [], {}, []
        self.attributes, self.specs, self._spec = {}, [], None
        self.description_parts = []
        self._description_depth = 0
        self._depth = 0
        self._script = None
        self._title = False
        self.title = ""
        self.feed(text)
        self.close()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script":
            self._script = [attrs, []]
        if tag == "meta":
            key = attrs.get("property") or attrs.get("name")
            if key:
                self.meta[key] = attrs.get("content", "")
        if tag == "a" and attrs.get("href"):
            self.links.append(attrs["href"])
        if tag == "title":
            self._title = True
        if tag in ("div", "span", "li", "p"):
            self._depth += 1
            classes = attrs.get("class", "")
            if "CardInfoRow__cell" in classes:
                self._spec = [self._depth, []]
            if "CardDescriptionHTML" in classes and not self._description_depth:
                self._description_depth = self._depth
        if tag == "br" and self._description_depth:
            self.description_parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "script" and self._script:
            self.scripts.append((self._script[0], "".join(self._script[1])))
            self._script = None
        if tag == "title":
            self._title = False
        if tag in ("div", "span", "li", "p"):
            if self._spec and self._spec[0] == self._depth:
                self.specs.append(clean(" ".join(self._spec[1])))
                self._spec = None
            if self._description_depth == self._depth:
                self._description_depth = 0
            self._depth -= 1

    def handle_data(self, data):
        if self._script is not None:
            self._script[1].append(data)
        elif self._title:
            self.title += data
        if self._spec:
            self._spec[1].append(data)
        if self._description_depth:
            self.description_parts.append(data)

    def documents(self):
        decoder = json.JSONDecoder()
        for attrs, text in self.scripts:
            if not text.strip():
                continue
            if attrs.get("type") in ("application/json", "application/ld+json") or attrs.get("id") in ("initial-state", "__NEXT_DATA__"):
                try:
                    yield json.loads(text)
                except ValueError:
                    try:
                        yield json.loads(html.unescape(text))
                    except ValueError:
                        pass
            else:
                for match in re.finditer(r"(?:window\.)?__(?:initialState|INITIAL_STATE|PRELOADED_STATE)__\s*=\s*", text):
                    try:
                        yield decoder.raw_decode(text[match.end():].lstrip())[0]
                    except ValueError:
                        pass

    def specifications(self):
        # Auto.ru renders adjacent label/value cells in each CardInfoRow.
        return {self.specs[i]: self.specs[i + 1] for i in range(0, len(self.specs) - 1, 2)}


def discover(text, base):
    page = Page(text)
    values = list(page.links) + LISTING_RE.findall(html.unescape(text).replace("\\/", "/"))
    for doc in page.documents():
        for node in walk(doc):
            for key in ("url", "link", "sale_url"):
                if isinstance(node.get(key), str):
                    values.append(node[key])
    return list(dict.fromkeys(url for value in values if (url := canonical_url(value, base))))


def photo_urls(offer, car, page):
    photos = first(dig(offer, "media", "photos"), offer.get("photos"), []) or []
    urls = []
    for photo in photos:
        if isinstance(photo, str):
            urls.append(photo)
            continue
        if not isinstance(photo, dict):
            continue
        sizes = photo.get("sizes") or {}
        preferred = first(*(sizes.get(key) for key in ("orig", "original", "1200x900", "1200x900n", "large", "wizard"))) if isinstance(sizes, dict) else None
        if not preferred and isinstance(sizes, dict) and sizes:
            def area(key):
                parts = re.findall(r"\d+", key)
                return int(parts[0]) * int(parts[1]) if len(parts) >= 2 else 0
            preferred = sizes[max(sizes, key=area)]
        urls.append(first(preferred, photo.get("url"), photo.get("original")))
    if not urls:
        images = car.get("image") or car.get("photo") or []
        urls.extend(images if isinstance(images, list) else [images])
    if not urls and page.meta.get("og:image"):
        urls.append(page.meta["og:image"])
    result = []
    for value in urls:
        if isinstance(value, dict):
            value = first(value.get("url"), value.get("contentUrl"))
        if not isinstance(value, str):
            continue
        value = html.unescape(value).replace("\\/", "/")
        if value.startswith("//"):
            value = "https:" + value
        if urlsplit(value).scheme in ("http", "https") and value not in result:
            result.append(value)
    return result


def parse_listing(text, url):
    page = Page(text)
    nodes = [node for doc in page.documents() for node in walk(doc)]
    ident = listing_id(url)
    candidates = [n for n in nodes if "vehicle_info" in n or ("media" in n and "sale_info" in n)]
    matching = [n for n in candidates if str(n.get("id", "")).split("-")[0] == ident.split("-")[0]]
    offer = max(matching, key=lambda n: len(json.dumps(n)), default={})
    cars = [n for n in nodes if any(t in (n.get("@type") if isinstance(n.get("@type"), list) else [n.get("@type")]) for t in ("Car", "Vehicle", "Product"))]
    matching_cars = [n for n in cars if canonical_url(n.get("url", "")) == url or str(first(n.get("sku"), n.get("productID"), "")).split("-")[0] == ident.split("-")[0]]
    # Do not attribute a recommended vehicle's metadata to this listing.
    car = next(iter(matching_cars), cars[0] if len(cars) == 1 and not cars[0].get("url") else {})
    specs = page.specifications()
    info, state = offer.get("vehicle_info") or {}, offer.get("state") or {}
    tech = info.get("tech_param") or {}
    docs = offer.get("documents") or {}
    sale = offer.get("sale_info") or {}
    seller = offer.get("seller") or {}
    engine = car.get("vehicleEngine") or {}
    if not isinstance(engine, dict):
        engine = {"name": engine}
    pricing = car.get("offers") or {}
    if isinstance(pricing, list):
        pricing = next(iter(pricing), {})
    price_info = offer.get("price_info") or {}
    price = first(price_info.get("RUR"), price_info.get("price"), pricing.get("price"), page.meta.get("product:price:amount"))
    if isinstance(price, dict):
        price = first(price.get("price"), price.get("value"))
    brand = car.get("brand")
    if isinstance(brand, dict):
        brand = brand.get("name")
    description = first(offer.get("description"), car.get("description"), "".join(page.description_parts).strip())
    if description is not None and not isinstance(description, str):
        description = json.dumps(description, ensure_ascii=False)
    if description:
        description = html.unescape(re.sub(r"<[^>]+>", " ", description)).strip()
    title = clean(first(car.get("name"), page.meta.get("og:title"), page.title))
    images = photo_urls(offer, car, page)
    if not offer and not car:
        raise ValueError("No structured listing found; save the page and check its markup (no gallery images guessed).")
    location = first(seller.get("location"), sale.get("location"), offer.get("location"), pricing.get("availableAtOrFrom"))
    displacement = number(tech.get("displacement"))
    if displacement is not None and displacement > 20:
        displacement = displacement / 1000  # Auto.ru tech_param uses cubic centimetres.
    result = {
        "schema_version": 1, "source": "auto.ru", "listing_id": ident,
        "source_url": url, "scraped_at": now(), "title": title,
        "description": description,
        "price": {"amount": number(price), "currency": first(pricing.get("priceCurrency"), price_info.get("currency"), "RUB" if price_info.get("RUR") is not None else None)},
        "vehicle": {
            "brand": first(dig(info, "mark_info", "name"), brand),
            "model": first(dig(info, "model_info", "name"), car.get("model")),
            "generation": dig(info, "super_gen", "name"),
            "configuration": dig(info, "configuration", "name"),
            "year": number(first(docs.get("year"), state.get("year"), car.get("vehicleModelDate"), car.get("productionDate"), specs.get("Год выпуска"))),
            "mileage_km": number(first(state.get("mileage"), car.get("mileageFromOdometer"), specs.get("Пробег"))),
            "body_type": first(dig(info, "configuration", "body_type"), car.get("bodyType"), specs.get("Кузов")),
            "color": first(offer.get("color_hex"), car.get("color"), specs.get("Цвет")),
            "doors": number(first(dig(info, "configuration", "doors_count"), car.get("numberOfDoors"))),
            "engine": {
                "fuel_type": first(tech.get("engine_type"), car.get("fuelType"), engine.get("fuelType")),
                "displacement_l": displacement,
                "displacement_original": engine.get("engineDisplacement"),
                "power_hp": number(tech.get("power")),
                "power_original": engine.get("enginePower"),
            },
            "transmission": first(tech.get("transmission"), car.get("vehicleTransmission"), specs.get("Коробка")),
            "drive": first(tech.get("gear_type"), car.get("driveWheelConfiguration"), specs.get("Привод")),
            "steering_wheel": first(offer.get("steering_wheel"), specs.get("Руль")),
            "condition": first(state.get("condition"), car.get("itemCondition"), specs.get("Состояние")),
            "owners": first(docs.get("owners_number"), specs.get("Владельцы")),
            "pts": first(docs.get("pts"), specs.get("ПТС")),
            "vin": first(docs.get("vin"), car.get("vehicleIdentificationNumber")),
            "state_not_beaten": state.get("state_not_beaten"),
        },
        "location": location,
        "seller": {"name": first(seller.get("name"), seller.get("company_name")), "type": first(seller.get("type"), seller.get("seller_type"))},
        "published_at": first(dig(offer, "additional_info", "creation_date"), offer.get("creation_date"), car.get("datePosted")),
        "features": first(offer.get("equipment"), offer.get("extras"), car.get("vehicleConfiguration")),
        "attributes": specs, "photo_urls": images, "photos": [], "download_complete": False,
        "raw_data": {"offer": offer, "json_ld": car},
    }
    return result


def assert_access(text, url):
    if urlsplit(url).hostname in ("auth.auto.ru", "passport.yandex.ru"):
        raise AccessBlocked("Auto.ru requires sign-in; provide an authorized Netscape cookie file or public saved HTML.")
    lower = text.lower()
    if any(token in lower for token in ("showcaptcha", "smartcaptcha", "подтвердите, что вы не робот", "войдите, чтобы продолжить")):
        # A normal page can preload a captcha widget without displaying a challenge.
        page = Page(text)
        if not discover(text, url) and not any("vehicle_info" in node or node.get("@type") in ("Car", "Vehicle") for doc in page.documents() for node in walk(doc)):
            raise AccessBlocked("Auto.ru returned a login page or CAPTCHA; collection stopped.")


class Client:
    def __init__(self, timeout, delay, cookies=None):
        handlers = []
        if cookies:
            jar = MozillaCookieJar(str(cookies))
            jar.load(ignore_discard=True, ignore_expires=False)
            handlers.append(HTTPCookieProcessor(jar))
        self.opener = build_opener(*handlers)
        self.timeout, self.delay, self.last_page = timeout, delay, 0.0

    def get(self, url, *, image=False):
        if not image:
            time.sleep(max(0, self.delay - (time.monotonic() - self.last_page)))
            self.last_page = time.monotonic()
        headers = {"User-Agent": "CarDamageDataset/1.0 (public listing research)", "Accept-Language": "ru-RU,ru;q=0.9"}
        req = Request(url, headers=headers)
        try:
            with self.opener.open(req, timeout=self.timeout) as response:
                content_type = response.headers.get("Content-Type", "")
                max_bytes = 40 * 1024 * 1024 if image else 25 * 1024 * 1024
                data = response.read(max_bytes + 1)
                if len(data) > max_bytes:
                    raise ValueError("Response exceeds download size limit")
                final_url = response.url
                if image:
                    if not content_type.startswith("image/"):
                        raise ValueError(f"Expected an image, got {content_type}")
                    return data, content_type
                text = data.decode(response.headers.get_content_charset() or "utf-8", errors="replace")
                assert_access(text, final_url)
                return text, final_url
        except HTTPError as exc:
            if exc.code in (401, 403, 429):
                raise AccessBlocked(f"HTTP {exc.code} from {urlsplit(url).hostname}; no retries or challenge bypass.") from exc
            raise


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(path)


def append_json(path, value):
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def read_records(path):
    records = {}
    if path.exists():
        with path.open("rb") as stream:
            for line in stream:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    # A killed process may leave a partial final JSONL record.
                    # Corruption elsewhere must still be reported, not discarded.
                    if not line.endswith(b"\n") and stream.read(1) == b"":
                        break
                    raise
                records[record["listing_id"]] = record
    return records


def save_record(output, record):
    atomic_json(output / "checkpoints" / "listings" / f"{record['listing_id']}.json", record)


def load_records(output):
    records = read_records(output / "listings.jsonl")
    # These atomic snapshots are newer than the last JSONL export after a crash.
    for path in sorted((output / "checkpoints" / "listings").glob("*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        records[record["listing_id"]] = record
    return records


def saved_photo_exists(output, photo):
    path = output / photo["path"]
    return path.is_file() and path.stat().st_size == photo["bytes"]


def save_photo(client, url, output, ident, index):
    checkpoint = output / "checkpoints" / "photos" / ident / f"{index:03d}.json"
    if checkpoint.exists():
        photo = json.loads(checkpoint.read_text(encoding="utf-8"))
        if photo["url"] == url:
            target = output / photo["path"]
            # Recover an image even if the process died before updating its
            # listing snapshot, or immediately before the final file rename.
            for candidate in (target, target.with_suffix(target.suffix + ".part")):
                if candidate.is_file():
                    data = candidate.read_bytes()
                    if hashlib.sha256(data).hexdigest() == photo["sha256"]:
                        if candidate != target:
                            candidate.replace(target)
                        return photo
    data, _ = client.get(url, image=True)
    with Image.open(io.BytesIO(data)) as image:
        width, height, fmt = image.width, image.height, image.format
        image.verify()
    suffix = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "AVIF": ".avif"}.get(fmt)
    if not suffix:
        raise ValueError(f"Unsupported image format: {fmt}")
    relative = Path("images") / ident / f"{index:03d}{suffix}"
    target = output / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(target.suffix + ".part")
    with temp.open("wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    photo = {"path": relative.as_posix(), "url": url, "sha256": hashlib.sha256(data).hexdigest(), "width": width, "height": height, "bytes": len(data), "format": fmt}
    # Save the identity/hash before exposing the finished image. Both windows
    # around this rename can then be recovered without another HTTP download.
    atomic_json(checkpoint, photo)
    temp.replace(target)
    return photo


def export(records, output):
    temp = output / "listings.jsonl.tmp"
    with temp.open("w", encoding="utf-8") as stream:
        for record in records.values():
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(output / "listings.jsonl")
    temp = output / "image_manifest.jsonl.tmp"
    with temp.open("w", encoding="utf-8") as stream:
        for record in records.values():
            for photo in record["photos"]:
                row = {"image_path": photo["path"], "listing_id": record["listing_id"], "source_url": record["source_url"], "description": record["description"], "vehicle": record["vehicle"], "price": record["price"], "photo": photo}
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(output / "image_manifest.jsonl")


def run(args):
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    (output / "images").mkdir(exist_ok=True)
    records = load_records(output)
    for record in records.values():
        record["photos"] = [photo for photo in record["photos"] if saved_photo_exists(output, photo)]
        expected = record["photo_urls"][:args.max_photos] if args.max_photos else record["photo_urls"]
        present = {photo["url"] for photo in record["photos"]}
        record["download_complete"] = bool(expected) and all(url in present for url in expected)
    client = Client(args.timeout, args.delay, args.cookies)
    summary = {"started_at": now(), "target_listings": args.limit, "start_url": args.start_url, "status": "running", "errors_this_run": 0}
    atomic_json(output / "run_summary.json", summary)
    input_urls = []
    if args.urls_file:
        for line in args.urls_file.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.lstrip().startswith("#"):
                url = canonical_url(line.strip())
                if not url:
                    raise ValueError(f"Invalid Auto.ru listing URL: {line}")
                input_urls.append(url)
    local = {}
    if args.html_dir:
        for path in sorted(args.html_dir.glob("*.html")):
            text = path.read_text(encoding="utf-8")
            page = Page(text)
            url = canonical_url(page.meta.get("og:url", ""))
            if url:
                local[url] = text
                input_urls.append(url)
            else:
                input_urls.extend(discover(text, args.start_url))
    source = {
        "start_url": args.start_url, "start_page": args.start_page,
        "urls_file": str(args.urls_file.resolve()) if args.urls_file else None,
        "html_dir": str(args.html_dir.resolve()) if args.html_dir else None,
    }
    checkpoint_path = output / "checkpoint.json"
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8")) if checkpoint_path.exists() else {}
    resumed = checkpoint.get("source") == source
    if not resumed:
        checkpoint = {}
    page_number = checkpoint.get("next_page", args.start_page)
    seen = set(checkpoint.get("seen_urls", []))
    unfinished = [record["source_url"] for record in records.values() if not record["download_complete"]]
    queued = list(dict.fromkeys(checkpoint.get("pending_urls", []) + checkpoint.get("retry_urls", []) + unfinished + [url for url in input_urls if url not in seen]))
    seen.update(queued)
    retry_urls = []
    exhausted = False

    def persist_crawl():
        atomic_json(checkpoint_path, {
            "schema_version": 1, "updated_at": now(), "source": source,
            "next_page": page_number, "pending_urls": queued,
            "retry_urls": retry_urls, "seen_urls": sorted(seen),
        })

    def usable():
        return sum(bool(record["photos"]) and record.get("download_complete", False) for record in records.values())

    def log_error(url, exc):
        summary["errors_this_run"] += 1
        append_json(output / "errors.jsonl", {"at": now(), "url": url, "error_type": type(exc).__name__, "message": str(exc)})

    persist_crawl()
    # Rebuild public mappings from durable snapshots, including a listing whose
    # last photo was saved without reaching the previous run's finally block.
    export(records, output)
    summary["resumed"] = resumed
    if resumed:
        print(f"Resuming: next search page {page_number}, {len(queued)} pending listings", flush=True)
    try:
        while usable() < args.limit:
            if not queued:
                if args.urls_file or args.html_dir or page_number >= args.start_page + args.max_pages:
                    exhausted = True
                    break
                parsed = urlsplit(args.start_url)
                query = dict(parse_qsl(parsed.query, keep_blank_values=True))
                query["page"] = str(page_number)
                url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))
                print(f"Listing page {page_number}: {url}", flush=True)
                text, _ = client.get(url)
                queued = [link for link in discover(text, args.start_url) if link not in seen]
                if not queued:
                    raise ValueError("No new listing links found; site markup may have changed or the search is exhausted.")
                seen.update(queued)
                page_number += 1
                persist_crawl()
            # Keep the current URL in the durable queue until it is processed.
            url = queued[0]
            ident = listing_id(url)
            previous = records.get(ident)
            if previous and previous.get("download_complete") and previous["photos"]:
                queued.pop(0)
                persist_crawl()
                continue
            record, saved = None, {}
            try:
                if previous:
                    record = previous
                else:
                    text = local.get(url)
                    if text is None:
                        text, _ = client.get(url)
                    assert_access(text, url)
                    record = parse_listing(text, url)
                urls = record["photo_urls"][:args.max_photos] if args.max_photos else record["photo_urls"]
                saved = {photo["url"]: photo for photo in record["photos"] if saved_photo_exists(output, photo)}
                records[ident] = record
                save_record(output, record)
                for index, image_url in enumerate(urls, 1):
                    if image_url not in saved:
                        try:
                            saved[image_url] = save_photo(client, image_url, output, ident, index)
                            record["photos"] = list(saved.values())
                            save_record(output, record)
                        except AccessBlocked:
                            raise
                        except (OSError, ValueError, URLError, UnidentifiedImageError) as exc:
                            log_error(image_url, exc)
                record["photos"] = [saved[image_url] for image_url in record["photo_urls"] if image_url in saved]
                record["download_complete"] = bool(urls) and all(image_url in saved for image_url in urls)
                records[ident] = record
                save_record(output, record)
                append_json(output / "listings.jsonl", record)
                if not record["download_complete"]:
                    retry_urls.append(url)
                print(f"{usable()}/{args.limit}: {ident}, {len(record['photos'])}/{len(urls)} photos", flush=True)
            except AccessBlocked:
                # Preserve photos downloaded before a challenge interrupted this listing.
                if record is not None:
                    record["photos"] = list(saved.values())
                    record["download_complete"] = False
                    records[ident] = record
                    save_record(output, record)
                    append_json(output / "listings.jsonl", record)
                raise
            except (OSError, ValueError, URLError) as exc:
                log_error(url, exc)
                retry_urls.append(url)
            queued.pop(0)
            persist_crawl()
        summary["status"] = "completed" if usable() >= args.limit else "partial"
        if exhausted:
            summary["reason"] = "Input URLs or page budget exhausted before reaching the target."
    except AccessBlocked as exc:
        summary.update(status="access_blocked", reason=str(exc))
        log_error(url if "url" in locals() else args.start_url, exc)
    except (OSError, ValueError, URLError) as exc:
        summary.update(status="failed", reason=str(exc))
        log_error(url if "url" in locals() else args.start_url, exc)
    except KeyboardInterrupt:
        summary.update(status="interrupted", reason="Stopped by the operator; rerun the same command to resume.")
    finally:
        persist_crawl()
        export(records, output)
        summary.update(finished_at=now(), listings_with_photos=sum(bool(r["photos"]) for r in records.values()), complete_listings=usable(), total_records=len(records), total_photos=sum(len(r["photos"]) for r in records.values()), next_page=page_number)
        atomic_json(output / "run_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=True, indent=2), flush=True)
    return 0 if summary["status"] == "completed" else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=1000, help="Target number of complete listings with photos, including earlier runs")
    parser.add_argument("--output", type=Path, default=ROOT / "datasets" / "autoru")
    parser.add_argument("--start-url", default="https://auto.ru/cars/used/")
    parser.add_argument("--start-page", type=int, default=1)
    parser.add_argument("--max-pages", type=int, default=100)
    parser.add_argument("--max-photos", type=int, default=0, help="0 = all available listing photos")
    parser.add_argument("--timeout", type=float, default=25)
    parser.add_argument("--delay", type=float, default=2, help="Minimum seconds between HTML requests")
    parser.add_argument("--cookies", type=Path, help="Optional existing authorized session, Netscape cookies.txt; never copied into dataset")
    parser.add_argument("--urls-file", type=Path, help="UTF-8 file with one public Auto.ru listing URL per line")
    parser.add_argument("--html-dir", type=Path, help="Parse locally saved public HTML files; photo downloads still require network")
    args = parser.parse_args()
    if args.limit < 1 or args.max_pages < 1 or args.start_page < 1 or args.max_photos < 0 or args.delay < 0 or args.timeout <= 0:
        parser.error("Invalid negative or zero limit, page, delay or timeout")
    start = urlsplit(args.start_url)
    if start.scheme != "https" or start.hostname not in ("auto.ru", "www.auto.ru"):
        parser.error("--start-url must be an https://auto.ru/ search URL")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
