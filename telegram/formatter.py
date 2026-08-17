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


def format_daily_briefing(briefing_data: Dict[str, Any]) -> List[str]:
    """Format full composite daily briefing into Telegram HTML."""
    now_str = datetime.now().strftime("%a, %b %d • %I:%M %p")
    lines = [
        "<b>📋 Blackboard Daily Briefing</b>",
        f"<i>{escape_html(now_str)}</i>",
        "━━━━━━━━━━━━━━━━━━━━━━",
        "",
    ]

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

            lines.append(f"<b>{escape_html(cname)}</b>")
            if unread_ann:
                lines.append(f"  📢 <i>{len(unread_ann)} new announcement(s)</i>")
                for ann in unread_ann[:2]:
                    lines.append(f"    • {escape_html(ann.get('title'))}")
            if recent_grades:
                lines.append(f"  📊 Graded items: <code>{len(recent_grades)}</code>")
            lines.append("")

    return chunk_message("\n".join(lines))


def format_urgent_due_alert(item: Dict[str, Any]) -> str:
    """Format urgent due date push alert."""
    return (
        "⚠️ <b>URGENT DEADLINE ALERT</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📝 <b>Assignment:</b> {escape_html(item.get('title'))}\n"
        f"📚 <b>Course:</b> {escape_html(item.get('course'))}\n"
        f"⏰ <b>Due Date:</b> <code>{escape_html(item.get('due_date') or item.get('due'))}</code>\n"
    )


def format_grade_alert(grade_item: Dict[str, Any], course_name: str) -> str:
    """Format new grade posting push alert."""
    return (
        "🎉 <b>NEW GRADE POSTED</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
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
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📚 <b>Course:</b> {escape_html(course_name)}\n"
        f"📌 <b>Title:</b> {escape_html(ann.get('title'))}\n"
        f"🕒 <b>Date:</b> <i>{escape_html(ann.get('meta', 'Recent'))}</i>\n\n"
        f"<blockquote>{escape_html(body_snippet)}</blockquote>"
    )


def format_due_dates_list(items: List[Dict[str, Any]], window_filter: str = "7d") -> List[str]:
    """Format list of due dates for Telegram bot /due response."""
    lines = [
        f"📅 <b>Upcoming Deadlines ({escape_html(window_filter.upper())})</b>",
        "━━━━━━━━━━━━━━━━━━━━━━",
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
