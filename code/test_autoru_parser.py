"""Offline verification; synthetic fixtures never enter datasets/autoru."""
import argparse
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, UnidentifiedImageError

import autoru_parser as parser


URL = "https://auto.ru/cars/used/sale/test/model/1234567890-abcdef/"
PHOTO_A = "https://images.example.invalid/a.jpg"
PHOTO_B = "https://images.example.invalid/b.jpg"


def fixture():
    offer = {
        "id": "1234567890-abcdef",
        "vehicle_info": {"mark_info": {"name": "Test brand"}, "model_info": {"name": "Test model"}, "tech_param": {"displacement": 1598, "power": 123}},
        "documents": {"year": 2020}, "state": {"mileage": 123456, "state_not_beaten": False},
        "price_info": {"RUR": 750000}, "description": "Продавец указал: царапина. &quot;Текст&quot; & проверка.",
        "media": {"photos": [{"sizes": {"small": PHOTO_A + "?thumb", "orig": PHOTO_A}}, {"sizes": {"1200x900": PHOTO_B}}]},
    }
    unrelated = {"id": "9999999999-other", "vehicle_info": {"mark_info": {"name": "WRONG"}}}
    state = {"listing": {"offers": [unrelated, offer]}}
    return '<meta property="og:url" content="' + URL + '"><title>Test listing</title><script id="initial-state" type="application/json">' + json.dumps(state, ensure_ascii=False) + '</script><div class="CardInfoRow"><span class="CardInfoRow__cell">ПТС</span><span class="CardInfoRow__cell">Оригинал</span></div>'


def jpeg():
    stream = io.BytesIO()
    Image.new("RGB", (16, 12), "red").save(stream, "JPEG")
    return stream.getvalue()


def arguments(output, urls_file, **changes):
    values = dict(output=output, timeout=1, delay=0, cookies=None, limit=1, urls_file=urls_file, html_dir=None, start_url="https://auto.ru/cars/used/", start_page=1, max_pages=2, max_photos=0)
    values.update(changes)
    return argparse.Namespace(**values)


class ParsingTests(unittest.TestCase):
    def test_matches_listing_and_preserves_description(self):
        record = parser.parse_listing(fixture(), URL)
        self.assertEqual(record["vehicle"]["brand"], "Test brand")
        self.assertEqual(record["vehicle"]["year"], 2020)
        self.assertEqual(record["vehicle"]["mileage_km"], 123456)
        self.assertEqual(record["vehicle"]["engine"]["displacement_l"], 1.598)
        self.assertFalse(record["vehicle"]["state_not_beaten"])
        self.assertEqual(record["vehicle"]["pts"], "Оригинал")
        self.assertEqual(record["photo_urls"], [PHOTO_A, PHOTO_B])
        self.assertIn('"Текст" & проверка.', record["description"])
        self.assertIsNone(record["vehicle"]["owners"])

    def test_json_ld_without_offer_and_zero_price(self):
        car = {"@type": ["Car", "Product"], "url": URL, "name": "Vehicle", "brand": {"name": "Brand"}, "mileageFromOdometer": {"value": 1234}, "offers": {"price": 0, "priceCurrency": "RUB"}, "image": [{"contentUrl": PHOTO_A}]}
        text = '<script type="application/ld+json">' + json.dumps(car) + '</script>'
        record = parser.parse_listing(text, URL)
        self.assertEqual(record["price"]["amount"], 0)
        self.assertEqual(record["vehicle"]["mileage_km"], 1234)
        self.assertEqual(record["photo_urls"], [PHOTO_A])

    def test_recommended_vehicle_cannot_become_listing(self):
        car = {"@type": "Car", "url": URL.replace("1234567890", "9999999999"), "name": "Recommended", "image": PHOTO_A}
        with self.assertRaises(ValueError):
            parser.parse_listing('<script type="application/ld+json">' + json.dumps(car) + '</script>', URL)

    def test_discovers_relative_and_escaped_urls_without_duplicates(self):
        relative = URL.removeprefix("https://auto.ru")
        text = '<a href="' + relative + '?foo=bar">Car</a><script>' + URL.replace("/", "\\/") + '</script><a href="https://example.invalid/cars/used/sale/test/1234567890/">Wrong site</a>'
        self.assertEqual(parser.discover(text, "https://auto.ru/cars/used/"), [URL])

    def test_access_challenges_stop_and_widget_does_not(self):
        with self.assertRaises(parser.AccessBlocked):
            parser.assert_access("Войдите, чтобы продолжить", "https://auth.auto.ru/login/")
        with self.assertRaises(parser.AccessBlocked):
            parser.assert_access("<title>Подтвердите, что вы не робот</title>", URL)
        parser.assert_access(fixture() + '<script src="smartcaptcha.js"></script>', URL)


class DatasetTests(unittest.TestCase):
    def test_hard_process_exit_resumes_page_queue_and_last_saved_photo(self):
        # os._exit skips exceptions, context managers and finally blocks. Test
        # the actual on-disk recovery, not just a graceful shutdown path.
        driver = '''
import os
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import autoru_parser as parser
from test_autoru_parser import URL, PHOTO_A, PHOTO_B, fixture, jpeg, arguments
other = URL.replace("1234567890", "1234567891")
mode = sys.argv[3]
class FakeClient:
    def __init__(self, *args):
        pass
    def get(self, url, image=False):
        if image:
            if mode == "second-photo" and url == PHOTO_B:
                os._exit(73)
            return jpeg(), "image/jpeg"
        if "?page=" in url:
            return '<a href="' + URL + '">A</a><a href="' + other + '">B</a>', url
        return fixture().replace("1234567890", "1234567891") if url == other else fixture(), url
parser.Client = FakeClient
if mode == "after-file":
    original = parser.save_photo
    def interrupted_save(*args):
        original(*args)
        os._exit(73)
    parser.save_photo = interrupted_save
if mode == "before-rename":
    original_replace = Path.replace
    def interrupted_replace(self, target):
        if self.suffix == ".part":
            os._exit(73)
        return original_replace(self, target)
    Path.replace = interrupted_replace
parser.run(arguments(Path(sys.argv[2]), None, limit=2, start_page=7, max_pages=1))
'''
        other = URL.replace("1234567890", "1234567891")
        for mode in ("second-photo", "after-file", "before-rename"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "out"
                child = subprocess.run([sys.executable, "-c", driver, str(Path(__file__).parent.resolve()), str(output), mode], capture_output=True, text=True, timeout=15)
                self.assertEqual(child.returncode, 73, child.stderr)
                checkpoint = json.loads((output / "checkpoint.json").read_text(encoding="utf-8"))
                self.assertEqual(checkpoint["next_page"], 8)
                self.assertEqual(checkpoint["pending_urls"], [URL, other])
                summary = json.loads((output / "run_summary.json").read_text(encoding="utf-8"))
                self.assertEqual(summary["status"], "running")  # finally was skipped
                if mode == "second-photo":
                    with (output / "listings.jsonl").open("ab") as stream:
                        stream.write(b'{"listing_id": "torn", "description": "\xd0')
                calls = []

                class FakeClient:
                    def __init__(self, *args):
                        pass

                    def get(self, url, image=False):
                        calls.append(url)
                        if image:
                            return jpeg(), "image/jpeg"
                        if url == other:
                            return fixture().replace("1234567890", "1234567891"), url
                        raise AssertionError(f"Already saved page requested again: {url}")

                args = arguments(output, None, limit=2, start_page=7, max_pages=1)
                with patch.object(parser, "Client", FakeClient), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(parser.run(args), 0)
                self.assertEqual(calls, [PHOTO_B, other, PHOTO_A, PHOTO_B])
                records = parser.read_records(output / "listings.jsonl")
                self.assertEqual(len(records), 2)
                self.assertTrue(all(record["download_complete"] for record in records.values()))
                mapping = [json.loads(line) for line in (output / "image_manifest.jsonl").read_text(encoding="utf-8").splitlines()]
                self.assertEqual(len(mapping), 4)
                self.assertTrue(all((output / row["image_path"]).is_file() for row in mapping))
                self.assertEqual(list((output / "images").rglob("*.part")), [])

    def test_truncated_last_jsonl_line_keeps_completed_records(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "records.jsonl"
            path.write_bytes(b'{"listing_id": "saved"}\n{"listing_id": "unfinished')
            self.assertEqual(parser.read_records(path), {"saved": {"listing_id": "saved"}})

    def test_corruption_in_middle_of_jsonl_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "records.jsonl"
            path.write_bytes(b'{"listing_id": "saved"}\n{broken}\n{"listing_id": "later"}\n')
            with self.assertRaises(ValueError):
                parser.read_records(path)

    def test_blocked_photo_resume_mapping_and_missing_file_repair(self):
        calls = []
        block = True

        class FakeClient:
            def __init__(self, *args):
                pass

            def get(self, url, image=False):
                calls.append(url)
                if image and url == PHOTO_B and block:
                    raise parser.AccessBlocked("Fixture challenge")
                return (jpeg(), "image/jpeg") if image else (fixture(), URL)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            urls = root / "urls.txt"
            urls.write_text(URL + "\n", encoding="utf-8")
            output = root / "out"
            args = arguments(output, urls)
            with patch.object(parser, "Client", FakeClient), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(parser.run(args), 2)
                summary = json.loads((output / "run_summary.json").read_text(encoding="utf-8"))
                self.assertEqual(summary["status"], "access_blocked")
                self.assertEqual(summary["total_photos"], 1)
                block = False
                calls.clear()
                self.assertEqual(parser.run(args), 0)
                self.assertEqual(calls, [PHOTO_B])
                mapping = [json.loads(line) for line in (output / "image_manifest.jsonl").read_text(encoding="utf-8").splitlines()]
                self.assertEqual(len(mapping), 2)
                self.assertTrue(all((output / row["image_path"]).is_file() for row in mapping))
                self.assertEqual(len(parser.read_records(output / "listings.jsonl")), 1)
                calls.clear()
                self.assertEqual(parser.run(args), 0)
                self.assertEqual(calls, [])
                (output / mapping[0]["image_path"]).unlink()
                self.assertEqual(parser.run(args), 0)
                self.assertEqual(calls, [PHOTO_A])

    def test_invalid_image_never_enters_mapping(self):
        class BadClient:
            def get(self, *args, **kwargs):
                return b"<html>Login page</html>", "image/jpeg"

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(UnidentifiedImageError):
                parser.save_photo(BadClient(), PHOTO_A, Path(directory), "1234567890-abcdef", 1)
            self.assertEqual(list(Path(directory).rglob("*.jpg")), [])

    def test_changed_photo_limit_downloads_remaining_photos(self):
        calls = []

        class FakeClient:
            def __init__(self, *args):
                pass

            def get(self, url, image=False):
                calls.append(url)
                return (jpeg(), "image/jpeg") if image else (fixture(), URL)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            urls = root / "urls.txt"
            urls.write_text(URL + "\n", encoding="utf-8")
            args = arguments(root / "out", urls, max_photos=1)
            with patch.object(parser, "Client", FakeClient), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(parser.run(args), 0)
                calls.clear()
                args.max_photos = 0
                self.assertEqual(parser.run(args), 0)
                self.assertEqual(calls, [PHOTO_B])


if __name__ == "__main__":
    unittest.main()
