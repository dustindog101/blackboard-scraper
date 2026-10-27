"""Downloads of files that are only linked inside an item's body (assignment descriptions etc.)."""
import asyncio
import contextlib
import io
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.rest import DocumentRedirects, RestError
from scrapers import outline as ol
from scrapers.search import files_within, find_items_async, format_download_result, grab_item_async

DAV = "https://blackboard.umbc.edu/bbcswebdav/pid-23310389-dt-asiobject-rid-104041037_1/xid-104041037_1"
DESC = f'<p><a href="{DAV}">HW3.docx</a></p>'
COURSES = {"_1_1": "IS 410 Intro To Database Design (03.1520) FA2026"}
DOCX = b"PK\x03\x04docx-bytes"


def api_item(cid, title, handler, desc="", has_children=False, **extra):
    return {"id": cid, "title": title, "description": desc, "hasChildren": has_children,
            "contentHandler": {"id": handler, **extra}}


def outline_with(desc=DESC, ctype="assignment", course="_1_1"):
    return [
        {"content_id": "_8996078_1", "parent_id": None, "parent_path": [], "title": "Homework 3", "content_type": ctype,
         "has_children": False, "is_downloadable": True, "description": "", "external_url": None,
         "attachments": ol.extract_embedded_files(desc)},
        {"content_id": "_5_1", "parent_id": None, "parent_path": [], "title": "Syllabus.pdf", "content_type": "file",
         "has_children": False, "is_downloadable": True, "description": "", "external_url": None,
         "attachments": [{"file_name": "Syllabus.pdf", "download_url": "/x", "mime_type": "application/pdf"}]},
    ]


def fake_response(body, content_type="application/octet-stream", disposition=""):
    resp = MagicMock()
    resp.read.return_value = body
    resp.headers = {"Content-Type": content_type, "Content-Disposition": disposition}
    resp.__enter__.return_value = resp
    return resp


def run_grab(query, outlines, out_dir, courses=None, urlopen=None):
    async def outline(cid, **_k):
        return outlines[cid]
    courses = courses or COURSES
    with patch("scrapers.search.scrape_course_outline_async", side_effect=outline), \
            patch("scrapers.outline.get_cookie_header", return_value="a=b"), \
            patch("urllib.request.OpenerDirector.open", urlopen or (lambda *a, **k: fake_response(DOCX))), \
            contextlib.redirect_stderr(io.StringIO()):
        return asyncio.run(grab_item_async(query, courses, None, None, out_dir))


class TestExtraction(unittest.TestCase):
    def test_absolute_href(self):
        [att] = ol.extract_embedded_files(DESC)
        self.assertEqual((att["file_name"], att["download_url"], att["embedded"]), ("HW3.docx", DAV, True))
        self.assertEqual(att["id"], "23310389")

    def test_relative_and_single_quoted_href(self):
        [att] = ol.extract_embedded_files("<a class='x' href='/bbcswebdav/pid-7-dt-content-rid-9_1/courses/IS410/HW%201.docx'>Homework 1</a>")
        self.assertEqual(att["download_url"], "https://blackboard.umbc.edu/bbcswebdav/pid-7-dt-content-rid-9_1/courses/IS410/HW%201.docx")
        self.assertEqual(att["file_name"], "Homework 1.docx")  # link text gets the real extension

    def test_data_bbfile(self):
        raw = ('<div data-bbfile="{&quot;linkName&quot;:&quot;Notes.pdf&quot;,&quot;mimeType&quot;:&quot;application/pdf&quot;,'
               f'&quot;resourceUrl&quot;:&quot;{DAV}&quot;}}"></div>')
        [att] = ol.extract_embedded_files(raw)
        self.assertEqual((att["file_name"], att["mime_type"]), ("Notes.pdf", "application/pdf"))

    def test_dedupes_ignores_other_links_and_hosts(self):
        raw = DESC + DESC + '<a href="https://example.com/bbcswebdav/pid-1-x/f.pdf">x</a><a href="/webapps/other">y</a>'
        self.assertEqual([a["file_name"] for a in ol.extract_embedded_files(raw)], ["HW3.docx"])

    def test_crawl_adds_embedded_to_assignment(self):
        pages = {"contents": {"results": [api_item("_1_1", "Homework 3", "resource/x-bb-asmt-test-link", DESC)]}}
        with patch("scrapers.outline._api_get", side_effect=lambda url, *_a, **_k: pages["contents"]):
            [node] = ol._crawl_api_tree("_105737_1", "a=b")
        self.assertTrue(node["is_downloadable"])
        self.assertEqual(node["attachments"][0]["file_name"], "HW3.docx")
        self.assertEqual(node["content_type"], "assignment")

    def test_crawl_reads_handler_instructions_strips_signed_query_and_body(self):
        item = api_item("_1_1", "Homework 3", "resource/x-bb-asmt-test-link")
        item["contentHandler"]["instructions"] = f'<a href="{DAV}?Kq3c=token&amp;x=1">HW3.docx</a>'
        doc = api_item("_2_1", "Reading", "resource/x-bb-document")
        doc["body"] = "<a href='/bbcswebdav/pid-5-dt-content-rid-1_1/x/R.pdf'>R.pdf</a>"
        page = {"results": [item, doc]}
        with patch("scrapers.outline._api_get", side_effect=lambda *_a, **_k: page):
            nodes = ol._crawl_api_tree("_105737_1", "a=b")
        self.assertEqual(nodes[0]["attachments"][0]["download_url"], DAV)
        self.assertEqual(nodes[1]["attachments"][0]["file_name"], "R.pdf")

    def test_fetch_item_attachments_sees_relative_and_single_quote(self):
        doc = {"body": {"rawText": "<a href='/bbcswebdav/pid-9-x/y/Z.pdf'>Z.pdf</a>"}}
        with patch("scrapers.outline._api_get", side_effect=lambda url, *_a, **_k: doc if url.endswith("/contents/_1_1") else {"results": []}):
            atts = ol.fetch_item_attachments("c", "_1_1", "a=b")
        self.assertEqual([a["file_name"] for a in atts], ["Z.pdf"])


class TestSanitize(unittest.TestCase):
    def test_strips_paths_and_escapes(self):
        self.assertEqual(ol.safe_file_name("../../etc/passwd"), "passwd")
        self.assertEqual(ol.safe_file_name("..\\..\\evil.docx"), "evil.docx")
        self.assertEqual(ol.safe_file_name("a%2F..%2Fb.pdf"), "b.pdf")
        self.assertEqual(ol.safe_file_name("Tom &amp; Jerry%20Notes.pdf"), "Tom & Jerry Notes.pdf")
        self.assertEqual(ol.safe_file_name(".."), "downloaded_file")

    def test_download_never_leaves_destination_and_dedupes(self):
        atts = [{"file_name": "../../x.docx", "download_url": DAV}, {"file_name": "x.docx", "download_url": DAV}]
        with tempfile.TemporaryDirectory() as tmp, patch("urllib.request.OpenerDirector.open", lambda *a, **k: fake_response(DOCX)):
            dest = Path(tmp) / "IS_410"
            out = ol.download_attachments(atts, dest, cookie_header="a=b")
            self.assertEqual(sorted(p.name for p in dest.iterdir()), ["x (2).docx", "x.docx"])
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), ["IS_410"])
        self.assertEqual(len(out["downloaded"]), 2)


class TestMissingExtension(unittest.TestCase):
    def fetch(self, name, **response):
        with tempfile.TemporaryDirectory() as tmp, patch("urllib.request.OpenerDirector.open", lambda *a, **k: fake_response(DOCX, **response)):
            out = ol.download_attachments([{"file_name": name, "download_url": DAV}], Path(tmp), cookie_header="a=b")
            self.assertEqual(out["errors"], [])
            return out["downloaded"][0]["file_name"], sorted(p.name for p in Path(tmp).iterdir())

    def test_filename_from_content_disposition(self):
        for header in ('attachment; filename="HW3 final.docx"', "attachment; filename*=UTF-8''HW3%20final.docx"):
            self.assertEqual(self.fetch("xid-104041037_1", disposition=header), ("HW3 final.docx", ["HW3 final.docx"]))

    def test_disposition_filename_is_sanitized(self):
        name, _ = self.fetch("xid-1", disposition='attachment; filename="../../evil.docx"')
        self.assertEqual(name, "evil.docx")

    def test_extension_from_content_type_when_no_disposition(self):
        ctype = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        self.assertEqual(self.fetch("xid-1", content_type=ctype)[0], "xid-1.docx")
        self.assertEqual(self.fetch("xid-1", content_type="application/pdf; charset=binary")[0], "xid-1.pdf")

    def test_unknown_type_stays_extensionless(self):
        self.assertEqual(self.fetch("xid-1")[0], "xid-1")

    def test_html_without_extension_is_still_a_login_page(self):
        with self.assertRaises(RestError) as ctx, patch("urllib.request.OpenerDirector.open", lambda *a, **k: fake_response(b"<html>", "text/html")), \
                tempfile.TemporaryDirectory() as tmp:
            ol.download_course_file(DAV, Path(tmp) / "xid-1", "a=b")
        self.assertEqual(ctx.exception.kind, "auth")

    def test_dedupes_on_the_final_name(self):
        atts = [{"file_name": "a", "download_url": DAV}, {"file_name": "b", "download_url": DAV}]
        with tempfile.TemporaryDirectory() as tmp, patch("urllib.request.OpenerDirector.open", lambda *a, **k: fake_response(DOCX, disposition='attachment; filename="x.docx"')):
            out = ol.download_attachments(atts, Path(tmp), cookie_header="a=b")
            self.assertEqual([d["file_name"] for d in out["downloaded"]], ["x.docx", "x (2).docx"])
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), ["x (2).docx", "x.docx"])


class TestDownloadErrors(unittest.TestCase):
    def download(self, effect, name="a.docx"):
        with tempfile.TemporaryDirectory() as tmp, patch("urllib.request.OpenerDirector.open", side_effect=effect):
            dest = Path(tmp) / "d" / name
            try:
                ol.download_course_file(DAV, dest, "a=b")
            finally:
                self.assertFalse(dest.parent.exists())

    def test_html_login_page_is_session_expired(self):
        with self.assertRaises(RestError) as ctx:
            self.download(lambda *a, **k: fake_response(b"<!DOCTYPE html><html>login</html>", "text/html;charset=UTF-8"))
        self.assertEqual(ctx.exception.kind, "auth")
        self.assertIn("bb login", str(ctx.exception))

    def test_http_codes_are_distinct(self):
        for code, kind in ((401, "auth"), (403, "forbidden"), (404, "not_found"), (500, "unknown")):
            err = urllib.error.HTTPError(DAV, code, "x", {}, None)
            with self.subTest(code), self.assertRaises(RestError) as ctx:
                self.download(err)
            self.assertEqual(ctx.exception.kind, kind)

    def test_session_cookie_never_follows_a_redirect_off_blackboard(self):
        request = urllib.request.Request(DAV, headers={"Cookie": "a=b"})
        handler = DocumentRedirects(True)
        for url in ("https://abc.content.blackboardcdn.com/f", "https://alt-xyz.blackboard.com/f"):
            redirected = handler.redirect_request(request, None, 302, "Found", {}, url)
            self.assertFalse(redirected.has_header("Cookie"), url)
        with self.assertRaises(urllib.error.HTTPError):
            handler.redirect_request(request, None, 302, "Found", {}, "https://evil.example/f")
        with self.assertRaises(urllib.error.HTTPError):
            handler.redirect_request(request, None, 302, "Found", {}, "http://blackboard.umbc.edu/f")

    def test_download_uses_the_redirect_guard_and_refusal_is_forbidden(self):
        refused = urllib.error.HTTPError(DAV, 403, "Document redirect outside its provider", {}, None)
        with patch("scrapers.outline.urllib.request.build_opener") as build:
            build.return_value.open.side_effect = refused
            with tempfile.TemporaryDirectory() as tmp, self.assertRaises(RestError) as ctx:
                ol.download_course_file(DAV, Path(tmp) / "a.docx", "a=b")
        self.assertEqual(ctx.exception.kind, "forbidden")
        self.assertIsInstance(build.call_args.args[0], DocumentRedirects)
        self.assertTrue(build.call_args.args[0].blackboard)

    def test_html_file_is_allowed_when_html_expected(self):
        with tempfile.TemporaryDirectory() as tmp, patch("urllib.request.OpenerDirector.open", lambda *a, **k: fake_response(b"<html>", "text/html")):
            ol.download_course_file(DAV, Path(tmp) / "page.html", "a=b")
            self.assertTrue((Path(tmp) / "page.html").exists())

    def test_grab_reports_expired_session_not_empty(self):
        def html_page(*_a, **_k):
            return fake_response(b"<html>login</html>", "text/html")
        with tempfile.TemporaryDirectory() as tmp:
            result = run_grab("HW3.docx", {"_1_1": outline_with()}, Path(tmp) / "dl", urlopen=html_page)
            self.assertFalse((Path(tmp) / "dl").exists())
        self.assertEqual(result["status"], "error")
        self.assertIn("bb login", format_download_result(result))


class TestGrabEmbedded(unittest.TestCase):
    def grab(self, query, **kw):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_grab(query, kw.pop("outlines", {"_1_1": outline_with()}), Path(tmp), **kw)
            saved = sorted(str(Path(f["saved_to"]).relative_to(tmp)) for f in result.get("downloaded_files", [])
                           if Path(f["saved_to"]).is_absolute())
        return result, saved

    def test_by_file_name_case_insensitive(self):
        result, saved = self.grab("hw3.DOCX")
        self.assertEqual(result["status"], "success")
        self.assertEqual(saved, ["IS_410/HW3.docx"])
        self.assertEqual(result["downloaded_files"][0]["size_bytes"], len(DOCX))
        self.assertEqual(result["embedded_in"]["title"], "Homework 3")
        self.assertEqual(result["content_type"], "file")

    def test_by_pid_both_forms(self):
        for q in ("23310389", "_23310389_1"):
            with self.subTest(q):
                result, saved = self.grab(q)
                self.assertEqual((result["status"], saved), ("success", ["IS_410/HW3.docx"]))

    def test_assignment_by_title_downloads_its_files(self):
        result, saved = self.grab("Homework 3")
        self.assertEqual((result["status"], result["content_id"], saved), ("success", "_8996078_1", ["IS_410/HW3.docx"]))
        self.assertEqual(set(result), {"status", "course_id", "course_name", "content_id", "title", "content_type", "downloaded_files"})

    def test_assignment_without_files_is_not_a_file(self):
        result, _ = self.grab("Homework 3", outlines={"_1_1": outline_with(desc="<p>no files</p>")})
        self.assertEqual(result["status"], "not_a_file")

    def test_same_name_in_two_courses_is_ambiguous(self):
        courses = {"_1_1": COURSES["_1_1"], "_2_1": "IS 420 Other (01.0001) FA2026"}
        result, saved = self.grab("HW3.docx", outlines={"_1_1": outline_with(), "_2_1": outline_with()}, courses=courses)
        self.assertEqual(result["status"], "multiple_matches")
        self.assertEqual(saved, [])

    def test_same_file_in_two_items_of_one_course_is_one_match(self):
        outline = outline_with() + [dict(outline_with()[0], content_id="_9_1", title="Homework 3 copy")]
        result, saved = self.grab("HW3.docx", outlines={"_1_1": outline})
        self.assertEqual((result["status"], saved), ("success", ["IS_410/HW3.docx"]))

    def test_regular_file_unaffected(self):
        with patch("scrapers.search.download_content_item_files",
                   return_value={"downloaded": [{"file_name": "Syllabus.pdf", "saved_to": "x", "size_bytes": 1}], "errors": []}) as dl:
            result, _ = self.grab("Syllabus.pdf")
        self.assertEqual(result["status"], "success")
        self.assertEqual(dl.call_args.kwargs["content_id"], "_5_1")

    def test_folder_lists_embedded_files_of_descendants(self):
        outline = [{"content_id": "f", "parent_id": None, "parent_path": [], "title": "Week 1", "content_type": "folder",
                    "has_children": True, "is_downloadable": False, "attachments": []}]
        outline += [dict(o, parent_id="f", parent_path=["Week 1"]) for o in outline_with()]
        found = files_within(outline, "f")
        self.assertEqual([f["title"] for f in found], ["HW3.docx", "Syllabus.pdf"])
        self.assertEqual(found[0]["embedded_in"], "Homework 3")


class TestSearchEmbedded(unittest.TestCase):
    def test_search_finds_attachment_name_and_marks_file(self):
        async def outline(*_a, **_k):
            return outline_with()
        with patch("scrapers.search.scrape_course_outline_async", side_effect=outline), \
                patch("scrapers.search._safe_discussions", return_value=None), \
                contextlib.redirect_stderr(io.StringIO()):
            matches = asyncio.run(find_items_async("HW3.docx", COURSES))
        [m] = matches
        self.assertEqual((m["title"], m["content_type"], m["is_downloadable"]), ("HW3.docx", "file", True))
        self.assertEqual(m["embedded_in"]["title"], "Homework 3")
        self.assertEqual(m["parent_path"], ["Homework 3"])
