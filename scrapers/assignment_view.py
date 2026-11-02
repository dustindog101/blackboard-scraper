"""Focused, read-only assignment views shared by text, JSON and Markdown output."""

import copy
import io
import re
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from xml.etree import ElementTree

from core.config import BLACKBOARD_BASE
from core.rest import DocumentRedirects as _DocumentRedirects
from scrapers.quiz import _api_get, _clean_html_text, _format_local_datetime, _get_cookie_header

MAX_DOCUMENT_BYTES = 8 * 1024 * 1024
DEFAULT_TEXT_LIMIT = 12000
FULL_TEXT_LIMIT = 60000


def _read_document(resource, cookie, limit):
    """Read explicit instruction/rubric documents; never launch learning tools."""
    url = resource["url"]
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname
    name = resource.get("title", "")
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "*/*"}
    is_bb = host == urllib.parse.urlsplit(BLACKBOARD_BASE).hostname
    match = re.fullmatch(r"/document/d/([A-Za-z0-9_-]+)(?:/.*)?", parts.path)
    if parts.scheme != "https":
        return {"status": "linked", "message": "Open the linked document to read it."}
    if host == "docs.google.com" and match:
        url = f"https://docs.google.com/document/d/{match.group(1)}/export?format=txt"
        kind = "text"
    elif is_bb and parts.path.startswith(("/bbcswebdav/", "/learn/api/public/")):
        kind = "pdf" if name.lower().endswith(".pdf") else "docx" if name.lower().endswith(".docx") else None
        if not kind:
            return {"status": "linked", "message": "Open the linked document to read it."}
        headers["Cookie"] = cookie
    else:
        return {"status": "linked", "message": "Open the linked document to read it."}
    try:
        opener = urllib.request.build_opener(_DocumentRedirects(is_bb))
        with opener.open(urllib.request.Request(url, headers=headers), timeout=8) as response:
            body = response.read(MAX_DOCUMENT_BYTES + 1)
            content_type = response.headers.get("Content-Type", "")
        if len(body) > MAX_DOCUMENT_BYTES:
            return {"status": "linked", "message": "Document exceeds the automatic reading limit."}
        if kind == "text":
            if "text/plain" not in content_type:
                return {"status": "unavailable", "message": "Document requires access through its provider."}
            text = body.decode("utf-8-sig")
        elif kind == "pdf":
            from pypdf import PdfReader
            text = "\n\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(body)).pages)
        else:
            with zipfile.ZipFile(io.BytesIO(body)) as archive:
                info = archive.getinfo("word/document.xml")
                if info.file_size > MAX_DOCUMENT_BYTES:
                    return {"status": "linked", "message": "Document exceeds the automatic reading limit."}
                root = ElementTree.fromstring(archive.read(info))
            ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
            text = "\n".join("".join(p.itertext()) for p in root.findall(".//w:p", ns))
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(r"\n[^\S\n]*\n(?:[^\S\n]*\n)+", "\n\n", text).strip()
        if not text:
            return {"status": "unavailable", "message": "No readable text was returned; open the document."}
        return {"status": "available", "text": text[:limit], "truncated": len(text) > limit}
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, ElementTree.ParseError, ImportError):
        return {"status": "unavailable", "message": "Could not read the document; its link is still available."}
    except Exception:
        # PDF parsers can raise provider-specific exceptions on malformed/encrypted files.
        return {"status": "unavailable", "message": "Could not read the document; its link is still available."}


def _related_resources(course_id, content, cookie):
    """Only explicit prompt/rubric links in a folder matching this assignment."""
    parent = content.get("parentId")
    if not parent:
        return []
    folder = _api_get(f"/learn/api/v1/courses/{course_id}/contents/{parent}", cookie) or {}
    generic = {"assignment", "assignments", "essay", "final", "submit", "submission", "journal", "folder"}
    def words(text):
        return {w for w in re.findall(r"[a-z]+", (text or "").lower()) if len(w) > 3 and w not in generic}
    if not (words(content.get("title", "")) & words(folder.get("title", ""))):
        return []
    path = f"/learn/api/public/v1/courses/{course_id}/contents/{parent}/children"
    siblings = []
    visited = set()
    while path and path not in visited:
        visited.add(path)
        response = _api_get(path, cookie) or {}
        siblings.extend(_records(response.get("results")))
        next_page = (response.get("paging") or {}).get("nextPage")
        if next_page and urllib.parse.urlsplit(next_page).hostname not in (None, urllib.parse.urlsplit(BLACKBOARD_BASE).hostname):
            break
        path = next_page
    resources = []
    for item in siblings:
        if ((item.get("availability") or {}).get("available") or "Yes").lower() == "no":
            continue
        title = item.get("title") or ""
        kind = "rubric" if re.search(r"\brubric\b", title, re.I) else (
            "instructions" if re.search(r"\b(prompt|instructions)\b", title, re.I) else None
        )
        handler = item.get("contentHandler") or {}
        url = handler.get("url")
        if not url and "file" in handler.get("id", "").lower():
            title = handler.get("file", {}).get("fileName") or title
            url = f"{BLACKBOARD_BASE}/learn/api/public/v1/courses/{course_id}/contents/{item['id']}/attachments/default/download"
        if kind and url:
            resources.append({"kind": kind, "title": title, "url": url})
    return resources


def _records(value):
    """Blackboard optional collections may be missing, null, or malformed."""
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _rubric_summary(column):
    if not column or column.get("_http_status"):
        return {"status": "unknown", "message": "Could not check rubric availability."}, []
    associations = _records(column.get("rubricAssociations"))
    visible = [a for a in associations if a.get("displayBefore") is True]
    if not visible:
        return {"status": "hidden" if associations else "none"}, []
    definitions = []
    incomplete = False
    for association in visible:
        raw = association.get("rubricDefinition")
        if not isinstance(raw, dict):
            incomplete = True
            continue
        headers = _records(raw.get("columnHeaders"))
        levels = {c.get("id"): c.get("header") or "Level" for c in headers}
        criteria = []
        for row in sorted(_records(raw.get("rows")), key=lambda x: x.get("position") or 0):
            criterion = {"name": row.get("header") or "Criterion", "levels": []}
            if isinstance(row.get("percentage"), (int, float)):
                criterion["weight_percent"] = row["percentage"]
            cells = _records(row.get("cells"))
            if not cells:
                incomplete = True
            for cell in sorted(cells, key=lambda x: x.get("position") or 0):
                level = {"name": levels.get(cell.get("rubricColumnId"), "Level"),
                         "description": _clean_html_text(cell.get("description") or "")}
                for source, target in [("percentageMin", "min_percent"), ("percentageMax", "max_percent"),
                                       ("points", "points"), ("pointsMin", "min_points"), ("pointsMax", "max_points")]:
                    if isinstance(cell.get(source), (int, float)):
                        level[target] = cell[source]
                criterion["levels"].append(level)
            criteria.append(criterion)
        if criteria:
            definitions.append({"title": raw.get("title") or "Rubric", "criteria": criteria})
        else:
            incomplete = True
    summary = {"status": "available" if definitions else "unknown", "source": "blackboard", "display_before": True}
    if definitions:
        summary["criteria_count"] = sum(len(d["criteria"]) for d in definitions)
    if incomplete:
        summary["message"] = "A rubric is visible before starting, but some scoring details could not be retrieved."
    return summary, definitions


def prepare_assignment_view(data, mode="default"):
    """Enrich the resolved item and project precisely the requested view."""
    if data.get("error") or not data:
        return data
    result = copy.deepcopy(data)
    result["view"] = mode
    cookie = _get_cookie_header()
    cid, column_id = result.get("course_id"), result.get("column_id")
    column = _api_get(f"/learn/api/v1/courses/{cid}/gradebook/columns/{column_id}?expand=associatedRubrics,gradebookCategory", cookie) if cookie and cid and column_id else None
    rubric, definitions = _rubric_summary(column)
    result["rubric"] = rubric
    if column:
        result["due_date"] = column.get("dueDate") or result.get("due_date", "")
        if column.get("possible") is not None:
            try:
                points = float(column["possible"])
                result["max_points"] = f"{points:g} {'point' if points == 1 else 'points'}"
            except (ValueError, TypeError):
                pass
    content = {}
    resources = []
    item_id = result.get("assessment_id")
    is_external = result.get("item_type") == "External tool" or "blti" in ((column or {}).get("scoreProviderHandle") or "")
    if cookie and cid and item_id and (rubric["status"] == "none" or is_external):
        content = _api_get(f"/learn/api/v1/courses/{cid}/contents/{item_id}", cookie) or {}
    if is_external:
        tool = content.get("contentDetail", {}).get("resource/x-bb-blti-link", {})
        provider = tool.get("domainConfig", {}).get("name") or "External tool"
        result["item_type"] = "External tool"
        result["rubric"] = {"status": "external", "source": provider}
        if result.get("status") == "NOT_ATTEMPTED":
            result["status"] = "UNKNOWN"
        result["external_tool"] = {"name": provider, "message": f"Questions, submissions and provider rubrics must be viewed in {provider}."}
        result["instructions"] = _clean_html_text(content.get("description", ""))
        for field in ("attempts_allowed", "time_limit", "is_timed_test", "question_count"):
            result.pop(field, None)
    elif not item_id:
        result["item_type"] = "Gradebook entry"
        if result.get("status") == "NOT_ATTEMPTED":
            result["status"] = "UNKNOWN"
        result["notice"] = "This gradebook entry has no linked assignment content."
        for field in ("attempts_allowed", "time_limit", "is_timed_test", "question_count"):
            result.pop(field, None)
    elif content and result.get("item_type") == "Assignment":
        resources = _related_resources(cid, content, cookie)
    resources += [{"kind": "rubric", "title": r["text"], "url": r["url"]}
                  for r in (result.get("links") or []) if re.search(r"\brubric\b", r["text"], re.I)]
    rubric_links = [r for r in resources if r["kind"] == "rubric"]
    if rubric["status"] == "none" and rubric_links:
        result["rubric"] = {"status": "linked", "source": "course_resource", "resources": rubric_links}
    limit = DEFAULT_TEXT_LIMIT if mode == "default" else FULL_TEXT_LIMIT
    if mode != "rubric":
        instruction_links = [r for r in resources if r["kind"] == "instructions"] if not result.get("instructions") else []
        # Only explicit instructional documents, not videos, articles, or every course reading.
        inline_remainder = result.get("instructions", "")
        for link in (result.get("links") or []):
            inline_remainder = inline_remainder.replace(f"{link['text']} ({link['url']})", "")
        linked_only = bool(result.get("links")) and not inline_remainder.strip()
        if result.get("instructions") and (linked_only or re.search(r"\b(download|attach\w*|instructions|homework)\b", result["instructions"], re.I)):
            instruction_links += [{"kind": "instructions", "title": r["text"], "url": r["url"]}
                                  for r in (result.get("links") or []) if r["text"].lower().endswith((".pdf", ".docx")) and not re.search(r"\brubric\b", r["text"], re.I)]
        sources = []
        instruction_links = list({r["url"]: r for r in instruction_links}.values())
        selected = instruction_links[:2] if mode == "default" else instruction_links
        for resource in selected:
            document = _read_document(resource, cookie, limit)
            sources.append({**resource, **{k: v for k, v in document.items() if k != "text"}})
            if document.get("text"):
                existing = result.get("instructions", "").replace(f" ({resource['url']})", "")
                result["instructions"] = (existing + "\n\n" + document["text"]).strip()
                result["links"] = [link for link in (result.get("links") or []) if link["url"] != resource["url"]]
        if len(selected) < len(instruction_links):
            sources += [{**r, "status": "linked", "message": "Additional instruction document; use --instructions or --full to read it."}
                        for r in instruction_links[len(selected):]]
        if sources:
            result["instruction_sources"] = sources
        instructions = result.get("instructions", "")
        if len(instructions) > limit:
            result["instructions"] = instructions[:limit]
            result["instructions_truncated"] = True
        elif any(s.get("truncated") for s in sources):
            result["instructions_truncated"] = True
    if mode in ("rubric", "full"):
        if definitions:
            result["rubric"]["definitions"] = definitions
        elif rubric_links and result["rubric"]["status"] == "linked":
            documents = []
            for resource in {r["url"]: r for r in rubric_links}.values():
                documents.append({**resource, **_read_document(resource, cookie, FULL_TEXT_LIMIT)})
            result["rubric"]["documents"] = documents
            if any(d.get("status") == "available" for d in documents):
                result["rubric"]["status"] = "available"
    if mode in ("default", "instructions") and result["rubric"]["status"] in ("available", "linked"):
        result["rubric"]["hint"] = "Use --rubric for the rubric or --full for all information."
    if mode == "rubric":
        result = {k: result[k] for k in ("title", "course_name", "course_id", "assessment_id", "column_id", "view", "rubric", "external_tool") if k in result}
    elif mode == "instructions":
        result = {k: result[k] for k in ("title", "course_name", "course_id", "assessment_id", "view", "instructions", "links", "instruction_sources", "instructions_truncated", "rubric", "external_tool", "notice") if k in result}
    elif mode == "default":
        result["work_available"] = bool(result.get("attempt_id"))
        if result["work_available"]:
            result["work_hint"] = "Use --full to inspect the existing submission/attempt."
        if result.get("item_type") == "Discussion Board":
            result["work_hint"] = "Use --full to inspect your submitted discussion posts."
        for field in ("questions", "submissions", "submission", "submission_count", "attempt_id"):
            result.pop(field, None)
    return {k: v for k, v in result.items() if v not in ("", None, [])}


def _rubric_lines(rubric, include_details=False):
    status = rubric.get("status", "unknown")
    if not include_details:
        if status == "available":
            count = rubric.get("criteria_count")
            summary = f" ({count} {'criterion' if count == 1 else 'criteria'})" if count else ""
            return [f"Rubric: available{summary} — use --rubric or --full."]
        if status == "linked":
            return ["Rubric: linked course document — use --rubric or --full."]
        if status == "unknown":
            return [rubric.get("message") or "Rubric: availability could not be checked."]
        if status == "hidden":
            return ["Rubric: not available before starting."]
        return []
    if status == "external":
        return [f"Rubric must be viewed in {rubric.get('source', 'the external tool')}; provider visibility was not checked."]
    if status == "none":
        return ["No rubric is attached or identified in this assignment's folder."]
    if status == "hidden":
        return ["The rubric is not available before starting."]
    if status == "unknown":
        return [rubric.get("message") or "Could not check rubric availability. Try again when the session is active."]
    lines = [rubric["message"]] if rubric.get("message") else []
    for definition in rubric.get("definitions", []):
        lines += ["", f"Rubric: {definition['title']}"]
        for criterion in definition["criteria"]:
            weight = criterion.get("weight_percent")
            lines.append(f"\n{criterion['name']}{f' — {weight:g}%' if weight is not None else ''}")
            for level in criterion["levels"]:
                bounds = f" ({level['min_percent']:g}–{level['max_percent']:g}%)" if "min_percent" in level and "max_percent" in level else ""
                if not bounds and "min_points" in level and "max_points" in level:
                    bounds = f" ({level['min_points']:g}–{level['max_points']:g} points)"
                elif not bounds and "points" in level:
                    bounds = f" ({level['points']:g} points)"
                lines.append(f"  {level['name']}{bounds}: {level['description']}")
    for document in rubric.get("documents", []):
        lines += ["", f"Rubric document: {document['title']}", f"Source: {document['url']}", document.get("text") or document.get("message", "Open the linked document.")]
        if document.get("truncated"):
            lines.append("Document text truncated; open its source for the complete rubric.")
    if not lines:
        lines += [f"Rubric document: {r['title']} — {r['url']}" for r in rubric.get("resources", [])]
    return lines


def format_assignment_view(data):
    if data.get("error") or not data:
        from scrapers.quiz import format_assessment_attempt_cli
        return format_assessment_attempt_cli(data)
    mode = data.get("view", "default")
    lines = [f"{data.get('title', 'Assignment')}" + (f" [{data['item_type']}]" if data.get("item_type") else ""), f"Course: {data.get('course_name', data.get('course_id', 'Course'))}"]
    if mode in ("default", "full"):
        details = []
        if data.get("due_date"):
            due = _format_local_datetime(data["due_date"].replace(" UTC", "+00:00"))
            details.append(f"Due: {due}")
        if data.get("max_points"):
            details.append(data["max_points"])
        if data.get("status"):
            details.append(f"Status: {data['status']}")
        if details:
            lines.append(" · ".join(details))
        if data.get("is_timed_test"):
            lines.append(f"Time limit: {data.get('time_limit', 'Timed assessment')}")
        if data.get("attempts_allowed") and data["attempts_allowed"] != "Unlimited":
            lines.append(f"Attempts allowed: {data['attempts_allowed']}")
        if data.get("question_count"):
            lines.append(f"Questions: {data['question_count']}")
    if mode != "rubric":
        lines += _rubric_lines(data.get("rubric", {})) if mode != "full" else []
        if data.get("work_available"):
            lines.append("Existing submission/attempt available — use --full to inspect it.")
        elif data.get("question_count") and mode == "default":
            lines.append("Question details are not loaded; --full reads existing attempts without starting one.")
        elif data.get("work_hint"):
            lines.append(data["work_hint"])
        if data.get("notice"):
            lines.append(data["notice"])
        if data.get("external_tool"):
            lines.append(data["external_tool"]["message"])
        if data.get("instructions"):
            lines += ["", "Instructions:", data["instructions"]]
        elif data.get("instruction_sources"):
            lines += ["", "Instructions are linked below; document text could not be read."]
        elif not data.get("external_tool") and not data.get("notice"):
            lines += ["", "No general instructions are provided on this item."]
        for source in data.get("instruction_sources", []):
            lines.append(f"Instruction document: {source['title']} — {source['url']}")
            if source.get("message"):
                lines.append(source["message"])
        source_urls = {source["url"] for source in data.get("instruction_sources", [])}
        extra_links = [link for link in data.get("links", [])
                       if link["url"] not in source_urls and link["url"] not in data.get("instructions", "")]
        if extra_links:
            lines += ["", "Links:"] + [f"{link['text']} — {link['url']}" for link in extra_links]
        if data.get("instructions_truncated"):
            lines.append("Instructions truncated; use --instructions or open the source for more." if mode == "default" else "Instructions truncated; open the source for the complete text.")
    if mode in ("rubric", "full"):
        lines += _rubric_lines(data.get("rubric", {}), include_details=True)
    if mode == "full":
        from scrapers.quiz import format_assessment_attempt_cli
        work = {k: v for k, v in data.items() if k not in ("instructions", "links")}
        # Reuse the existing question/post renderer without duplicating its summary.
        rendered = format_assessment_attempt_cli(work)
        starts = [rendered.find(marker) for marker in ("📤 Your Submitted", "📋 Questions & Choices") if marker in rendered]
        if starts:
            lines += ["", rendered[min(starts):]]
        submission = data.get("submission")
        if submission:
            state = re.sub(r"[^A-Z]", "", str(data.get("status") or "").upper())
            final = state in ("NEEDSGRADING", "COMPLETED", "SUBMITTED", "GRADED")
            heading = ("Your draft:" if state == "INPROGRESS" else
                       "Your submission:" if final else "Your saved work (submission unverified):")
            lines += ["", heading]
            if final and submission.get("submitted_at"):
                lines.append(f"Submitted: {_format_local_datetime(submission['submitted_at'])}")
            if submission.get("text"):
                lines.append(submission["text"])
            for file in submission.get("files", []):
                lines.append(f"File: {file['name']}")
    return "\n".join(lines)
