from datetime import datetime
from pathlib import Path
from playwright.sync_api import sync_playwright

from core.config import load_courses
from core.output import OUTPUT_BASE
from core.session import _launch_context
from scrapers.activity import scrape_activity, save_activity
from scrapers.calendar import scrape_calendar, save_calendar
from scrapers.announcements import scrape_announcements, save_announcements
from scrapers.grades import scrape_grades, save_grades
import scrapers.discussions  # Importing to ensure it exists, but briefing doesn't use it directly

def run_briefing(headless: bool = True, cdp_url: str = None, write_markdown: bool = True):
    """
    Run all core scrapers (activity, calendar, announcements, grades) across all
    courses sequentially to build the daily briefing markdown document.
    """
    courses = load_courses()
    now = datetime.now()
    lines = [
        f"# Blackboard Daily Briefing",
        f"_Generated: {now.strftime('%Y-%m-%d %H:%M')}_",
        "",
        "---",
        "",
    ]
    per_course_data = {}

    with sync_playwright() as p:
        ctx, page = _launch_context(p, headless, cdp_url)

        # --- Activity Stream (global) ---
        print("\n📋 Scraping global activity stream...")
        activity = scrape_activity(page)
        if write_markdown:
            save_activity(activity)

        urgent = [
            a for a in activity
            if a.get("due_date")
            or "past due" in a.get("title", "").lower()
            or "overdue" in a.get("title", "").lower()
            or "past due" in a.get("message", "").lower()
            or "overdue" in a.get("message", "").lower()
        ]
        recent = [a for a in activity if not a.get("due_date")][:5]

        if urgent:
            lines.append("## ⚠️ Urgent / Due Soon")
            for a in urgent:
                lines.append(f"- **{a['title']}** ({a.get('course', '')}) — Due: {a.get('due_date', 'see item')}")
            lines.append("")

        if recent:
            lines.append("## 🔔 Recent Activity")
            for a in recent:
                lines.append(f"- {a['title']} — {a.get('course', '')} {('· ' + a['date']) if a.get('date') else ''}")
            lines.append("")

        lines.append("---")
        lines.append("")

        # --- Calendar (global) ---
        print("\n🗓️ Scraping calendar due dates...")
        calendar = scrape_calendar(page)
        if write_markdown:
            save_calendar(calendar)
        if calendar:
            lines.append("## 📅 Upcoming Due Dates")
            for item in calendar[:10]:
                title = item.get("title", "Untitled item")
                course = item.get("course", "")
                due = item.get("due", "TBD")
                lines.append(f"- **{title}** ({course}) — {due}")
            lines.append("")
            lines.append("---")
            lines.append("")

        # --- Per-course Data ---
        for course_id, course_name in courses.items():
            print(f"\n📚 Scanning {course_name} ({course_id})")
            per_course_data[course_id] = {
                "course_name": course_name,
                "announcements": [],
                "grades": [],
            }
            lines.append(f"## {course_name}")
            lines.append("")

            # >> Announcements
            announcements = scrape_announcements(course_id, page)
            if write_markdown:
                save_announcements(announcements, course_id)
            per_course_data[course_id]["announcements"] = announcements
            unread = [a for a in announcements if a.get("unread", False)]
            to_show = unread[:3] if unread else announcements[:2]
            
            if to_show:
                lines.append("### 📢 Announcements")
                for ann in to_show:
                    badge = " 🆕" if ann.get("unread") else ""
                    lines.append(f"**{ann['title']}{badge}** ({ann['meta']})")
                    if ann["body"]:
                        snippet = ann["body"][:200].replace("\n", " ")
                        if len(ann["body"]) > 200:
                            snippet += "..."
                        lines.append(f"> {snippet}")
                    lines.append("")

            # >> Grades
            grades = scrape_grades(course_id, page)
            if write_markdown:
                save_grades(grades, course_id)
            per_course_data[course_id]["grades"] = grades
            graded = [g for g in grades if g.get("grade") and g["grade"] not in ("Not graded", "-- %", "")]
            upcoming = [g for g in grades if g.get("status", "").lower() in ("unopened", "in progress") and g.get("dueDate")]
            
            if graded:
                lines.append("### 📊 Grades")
                lines.append("| Assignment | Due | Grade |")
                lines.append("|---|---|---|")
                for g in graded:
                    lines.append(f"| {g['name']} | {g.get('dueDate','')} | {g['grade']} |")
                lines.append("")
                
            if upcoming:
                lines.append("### 📅 Upcoming Assignments")
                for g in upcoming[:5]:
                    lines.append(f"- {g['name']} — due {g.get('dueDate', 'TBD')}")
                lines.append("")

            lines.append("---")
            lines.append("")

        # Close the central orchestrator connection
        ctx.close()

    # Finalize Briefing output
    filepath = OUTPUT_BASE / "briefing.md"
    if write_markdown:
        filepath.write_text("\n".join(lines))
        print(f"\n✅ Briefing written to: {filepath.relative_to(Path.cwd()) if filepath.is_absolute() else filepath}")

    return {
        "briefing_path": filepath,
        "activity": activity,
        "calendar": calendar,
        "courses": per_course_data,
    }
