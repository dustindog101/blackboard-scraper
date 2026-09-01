import html
from datetime import datetime
from typing import Any, Dict, List, Optional

TELEGRAM_MAX_MESSAGE_LENGTH = 3900  # Safe boundary below 4096


def escape_html(text: Optional[str]) -> str:
    """Safely escape text for Telegram HTML parse mode."""
    if not text:
        return ""
    return html.escape(str(text), quote=False)


def chunk_message(text: str, max_length: int = TELEGRAM_MAX_MESSAGE_LENGTH) -> List[str]:
    """Split long messages along line breaks to avoid exceeding Telegram limit."""
    if len(text) <= max_length:
        return [text]

    chunks = []
    lines = text.split("\n")
    current = ""
    for line in lines:
        if len(current) + len(line) + 1 > max_length:
            if current:
                chunks.append(current.strip())
            current = line + "\n"
        else:
            current += line + "\n"
    if current.strip():
        chunks.append(current.strip())
    return chunks


def format_main_menu(user_name: str = "Student", total_courses: int = 0) -> str:
    """Format the rich main dashboard greeting."""
    now_str = datetime.now().strftime("%A, %b %d • %I:%M %p")
    return (
        f"🎓 <b>UMBC Blackboard Assistant</b>\n"
        f"<i>{escape_html(now_str)}</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👋 Welcome back, <b>{escape_html(user_name)}</b>!\n\n"
        f"📚 <b>Configured Courses:</b> <code>{total_courses}</code>\n"
        f"⚡ <b>Engine:</b> Async Worker Pool (Active)\n\n"
        f"<i>Select an action below or type a command:</i>"
    )


def format_help_telegram() -> str:
    """Format the interactive help manual for Telegram."""
    return (
        "📖 <b>Blackboard Bot Command Manual</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "⚡ <b>Core Commands:</b>\n"
        "• /briefing — Full daily briefing across all courses\n"
        "• /due <code>[7d|14d]</code> — Upcoming deadlines & due dates\n"
        "• /outline — Course outlines, syllabi, and files\n"
        "• /assignments — Assignment prompts, rubrics, and points\n"
        "• /grades — Recent grades across courses\n"
        "• /announcements — Course announcements & updates\n"
        "• /find <code>&lt;query&gt;</code> — Search content across all courses\n"
        "• /courses — List all enrolled courses and IDs\n"
        "• /check — Verify Blackboard session health\n"
        "• /status — View bot daemon memory & engine metrics\n"
        "• /watch <code>[mins]</code> — Background monitoring daemon\n"
        "• /menu — Return to main interactive dashboard\n"
    )


def format_bot_status_telegram(metrics: Dict[str, Any]) -> str:
    """Format daemon health and memory metrics for Telegram."""
    uptime_sec = int(metrics.get("uptime_sec", 0))
    hours = uptime_sec // 3600
    mins = (uptime_sec % 3600) // 60
    secs = uptime_sec % 60
    uptime_str = f"{hours}h {mins}m {secs}s" if hours else f"{mins}m {secs}s"

    sess_icon = "✅ ACTIVE" if metrics.get("session_valid") else "❌ EXPIRED"
    return (
        "📊 <b>Bot Daemon Health & Metrics</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🤖 <b>Status:</b> <code>RUNNING</code> (PID: <code>{metrics.get('pid')}</code>)\n"
        f"⏱️ <b>Uptime:</b> <code>{uptime_str}</code>\n"
        f"🧠 <b>Memory (RSS):</b> <code>{metrics.get('memory_mb', 0)} MB</code>\n"
        f"🔐 <b>Blackboard Session:</b> <code>{sess_icon}</code>\n"
        f"📚 <b>Courses Monitored:</b> <code>{metrics.get('total_courses', 0)}</code>\n"
        f"🔔 <b>Watch Interval:</b> Every <code>{metrics.get('watch_mins', 30)} min</code>\n"
        f"⚡ <b>Scraper Engine:</b> <code>Adaptive Async Pool</code>\n"
    )



def format_daily_briefing(briefing_data: Dict[str, Any]) -> List[str]:
    """Format full composite daily briefing into Telegram HTML."""
    now_str = datetime.now().strftime("%a, %b %d • %I:%M %p")
    lines = [
        "<b>📋 Blackboard Daily Briefing</b>",
        f"<i>{escape_html(now_str)}</i>",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
    ]

    # 0. In-Progress Unfinished Attempts (Highest Priority)
    open_attempts = briefing_data.get("in_progress_attempts", [])
    if open_attempts:
        lines.append(f"🚨 <b>UNSUBMITTED ATTEMPTS DETECTED ({len(open_attempts)}):</b>")
        for att in open_attempts:
            cname = att.get("course_name") or att.get("course_id")
            title = att.get("title")
            elapsed = att.get("elapsed_time_human", "Active")
            url = att.get("launcher_url", "")
            overdue = " ⚠️ <b>OVERDUE</b>" if att.get("is_overdue") else ""
            lines.append(f"• ⚠️ <b>{escape_html(title)}</b> ({escape_html(cname)}){overdue}")
            lines.append(f"  └ Started: {escape_html(elapsed)} | <a href=\"{url}\">Resume Test</a>")
        lines.append("")

    # 1. Urgent Items
    urgent = briefing_data.get("urgent", [])
    if urgent:
        lines.append("🚨 <b>URGENT & OVERDUE:</b>")
        for u in urgent:
            lines.append(f"• <b>{escape_html(u.get('title'))}</b>")
            lines.append(f"  └ <i>{escape_html(u.get('course'))}</i> • Due: <b>{escape_html(u.get('due_date', 'Today'))}</b>")
        lines.append("")

    # 2. Upcoming Due Dates
    calendar = briefing_data.get("calendar", [])
    if calendar:
        lines.append("📅 <b>UPCOMING ASSIGNMENTS:</b>")
        for item in calendar[:8]:
            lines.append(f"• <b>{escape_html(item.get('title'))}</b>")
            lines.append(f"  └ {escape_html(item.get('course'))} — <code>{escape_html(item.get('due', 'TBD'))}</code>")
        lines.append("")

    # 3. Course Specific Highlights
    courses = briefing_data.get("courses", {})
    any_course_updates = False
    if courses:
        lines.append("📚 <b>COURSE UPDATES:</b>")
        for cid, cdata in courses.items():
            if not isinstance(cdata, dict):
                continue
            cname = cdata.get("course_name", cid)
            announcements = cdata.get("announcements", [])
            unread_ann = [a for a in announcements if a.get("unread")]
            grades = cdata.get("grades", [])
            recent_grades = [g for g in grades if g.get("grade") and g["grade"] not in ("Not graded", "-- %", "")]

            if unread_ann or recent_grades:
                any_course_updates = True
                lines.append(f"\n▶ <b>{escape_html(cname)}</b>")
                if unread_ann:
                    lines.append(f"  📢 <i>{len(unread_ann)} new announcement(s)</i>")
                    for ann in unread_ann[:2]:
                        lines.append(f"    • {escape_html(ann.get('title'))}")
                if recent_grades:
                    lines.append(f"  📊 Graded items: <code>{len(recent_grades)}</code>")

    if not any_course_updates and not urgent and not calendar:
        lines.append("<i>(No urgent deadlines, unread announcements, or new grades across courses)</i>")

    return chunk_message("\n".join(lines))


def format_outline_telegram(outline_data: Dict[str, Any]) -> List[str]:
    """Format course outlines and syllabi for Telegram."""
    lines = [
        "📚 <b>Course Outlines & Syllabi</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
    ]

    type_icons = {
        "syllabus": "📜 [SYLLABUS]",
        "folder": "📁",
        "learning_module": "📦",
        "document": "📄",
        "assignment": "📝",
        "test": "🧪",
        "quiz": "🧪",
        "discussion": "💬",
        "link": "🔗",
        "file": "📎",
        "item": "📌",
    }

    if not outline_data:
        lines.append("<i>(All courses are currently closed or have no content items)</i>")
        return ["\n".join(lines)]

    for cid, items in outline_data.items():
        if not items or not isinstance(items, list):
            continue
        lines.append(f"\n🎓 <b>{escape_html(cid)}</b>")
        for item in items[:12]:
            depth = item.get("depth", 0)
            indent = "  " * depth
            icon = type_icons.get(item.get("content_type", "item"), "📌")
            title = escape_html(item.get("title", "Untitled"))
            due = f" — <i>(Due: {escape_html(item['due_date'])})</i>" if item.get("due_date") else ""
            lines.append(f"{indent}{icon} {title}{due}")
            for link in item.get("links", []):
                if link.get("url") and not link["url"].endswith("#"):
                    lines.append(f"{indent}   └ 🔗 <a href='{escape_html(link['url'])}'>{escape_html(link.get('text', 'Link'))}</a>")

    return chunk_message("\n".join(lines))


def format_grades_telegram(grades_data: Dict[str, Any]) -> List[str]:
    """Format gradebook records for Telegram."""
    lines = [
        "🎓 <b>Recent Course Grades</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
    ]
    total_grades = 0
    for cid, gr_list in grades_data.items():
        if not isinstance(gr_list, list) or not gr_list:
            continue
        graded = [g for g in gr_list if g.get("grade") and g["grade"] not in ("Not graded", "-- %", "")]
        if graded:
            total_grades += len(graded)
            lines.append(f"\n📚 <b>{escape_html(cid)}</b>")
            for g in graded:
                due_str = f" (Due: {escape_html(g['dueDate'])})" if g.get("dueDate") else ""
                lines.append(f"• <b>{escape_html(g['name'])}</b>: <code>{escape_html(g.get('grade'))}</code>{due_str}")

    if total_grades == 0:
        lines.append("<i>No recent grades posted across courses.</i>")

    return chunk_message("\n".join(lines))


def format_announcements_telegram(ann_data: Dict[str, Any]) -> List[str]:
    """Format course announcements for Telegram."""
    lines = [
        "📢 <b>Course Announcements</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
    ]
    total_ann = 0
    for cid, items in ann_data.items():
        if not isinstance(items, list) or not items:
            continue
        total_ann += len(items)
        lines.append(f"\n📚 <b>{escape_html(cid)}</b>")
        for a in items[:4]:
            unread = "🆕 " if a.get("unread") else ""
            lines.append(f"• {unread}<b>{escape_html(a.get('title'))}</b> <i>({escape_html(a.get('meta',''))})</i>")
            if a.get("body"):
                snip = escape_html(a['body'][:120].replace("\n", " ").strip())
                lines.append(f"  <blockquote>{snip}...</blockquote>")

    if total_ann == 0:
        lines.append("<i>No announcements found across configured courses.</i>")

    return chunk_message("\n".join(lines))


def format_search_results_telegram(query: str, matches: List[Dict[str, Any]]) -> List[str]:
    """Format search query results for Telegram."""
    lines = [
        f"🔎 <b>Search Results for:</b> <code>{escape_html(query)}</code>",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
    ]
    if not matches:
        lines.append("<i>No matching items, syllabus docs, or assignments found.</i>")
    else:
        for m in matches[:15]:
            due_str = f" <i>(Due: {escape_html(m['due_date'])})</i>" if m.get("due_date") else ""
            lines.append(f"• [<b>{escape_html(m.get('course_name','Course'))}</b>] {escape_html(m.get('title'))} [<code>{escape_html(m.get('content_type','item'))}</code>]{due_str}")

    return chunk_message("\n".join(lines))


def format_urgent_due_alert(item: Dict[str, Any]) -> str:
    """Format urgent due date push alert."""
    return (
        "⚠️ <b>URGENT DEADLINE ALERT</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📝 <b>Assignment:</b> {escape_html(item.get('title'))}\n"
        f"📚 <b>Course:</b> {escape_html(item.get('course'))}\n"
        f"⏰ <b>Due Date:</b> <code>{escape_html(item.get('due_date') or item.get('due'))}</code>\n"
    )


def format_grade_alert(grade_item: Dict[str, Any], course_name: str) -> str:
    """Format new grade posting push alert."""
    return (
        "🎉 <b>NEW GRADE POSTED</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📚 <b>Course:</b> {escape_html(course_name)}\n"
        f"📝 <b>Item:</b> {escape_html(grade_item.get('name'))}\n"
        f"🎯 <b>Score:</b> <code>{escape_html(grade_item.get('grade'))}</code>\n"
        f"ℹ️ <b>Status:</b> {escape_html(grade_item.get('status', 'Graded'))}\n"
    )


def format_announcement_alert(ann: Dict[str, Any], course_name: str) -> str:
    """Format new course announcement push alert."""
    body_snippet = (ann.get("body") or "")[:250].strip()
    if len(ann.get("body", "")) > 250:
        body_snippet += "..."

    return (
        "📢 <b>NEW ANNOUNCEMENT</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📚 <b>Course:</b> {escape_html(course_name)}\n"
        f"📌 <b>Title:</b> {escape_html(ann.get('title'))}\n"
        f"🕒 <b>Date:</b> <i>{escape_html(ann.get('meta', 'Recent'))}</i>\n\n"
        f"<blockquote>{escape_html(body_snippet)}</blockquote>"
    )


def format_due_dates_list(items: List[Dict[str, Any]], window_filter: str = "7d") -> List[str]:
    """Format list of due dates for Telegram bot /due response."""
    lines = [
        f"📅 <b>Upcoming Deadlines ({escape_html(window_filter.upper())})</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
    ]
    if not items:
        lines.append("<i>No upcoming assignments found in this window.</i>")
    else:
        for it in items:
            c = it.get("course", "Course")
            t = it.get("title", "Assignment")
            d = it.get("due_date", "TBD")
            lines.append(f"• <b>{escape_html(t)}</b>")
            lines.append(f"  └ <i>{escape_html(c)}</i> • <code>{escape_html(d)}</code>")

    return chunk_message("\n".join(lines))
