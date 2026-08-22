"""
Unit Tests for Course Outline Explorer & Selective Expansion:
- Tree building and item calculation per folder
- Rich metadata formatting (e.g., item count and type breakdowns)
- Default shallow outline view with collapsed folders
- Selective folder expansion by ID and fuzzy title
- Depth limiting (--depth N)
- Full tree expansion (--expand-all / --deep)
- Clean JSON filtering by folder
"""

import unittest
from scrapers.outline import (
    compute_folder_stats,
    filter_outline_by_folder,
    format_outline_tree,
    clean_outline_json,
)


class TestOutlineExplorer(unittest.TestCase):
    def setUp(self):
        self.sample_outline = [
            # Root items
            {
                "content_id": "_root_syl_1",
                "parent_id": None,
                "parent_path": [],
                "title": "Course Syllabus",
                "content_type": "syllabus",
                "depth": 0,
                "has_children": False,
                "is_downloadable": True,
                "download_url": "https://example.com/syllabus.pdf",
                "attachments": [],
                "external_url": None,
                "links": [{"text": "Syllabus", "url": "https://example.com/syllabus.pdf"}],
                "due_date": "",
                "description": "Fall 2026 course policy",
            },
            # Root Folder 1: Homework
            {
                "content_id": "_folder_hw_1",
                "parent_id": None,
                "parent_path": [],
                "title": "Homework Assignments",
                "content_type": "folder",
                "depth": 0,
                "has_children": True,
                "is_downloadable": False,
                "download_url": None,
                "attachments": [],
                "external_url": None,
                "links": [],
                "due_date": "",
                "description": "Weekly homework assignments",
            },
            # Children of Homework
            {
                "content_id": "_hw_item_1",
                "parent_id": "_folder_hw_1",
                "parent_path": ["Homework Assignments"],
                "title": "Homework 1 - SQL Queries",
                "content_type": "assignment",
                "depth": 1,
                "has_children": False,
                "is_downloadable": False,
                "download_url": None,
                "attachments": [],
                "external_url": None,
                "links": [],
                "due_date": "2026-09-15",
                "description": "Submit .sql file",
            },
            {
                "content_id": "_hw_item_2",
                "parent_id": "_folder_hw_1",
                "parent_path": ["Homework Assignments"],
                "title": "Homework 2 - Normalization",
                "content_type": "assignment",
                "depth": 1,
                "has_children": False,
                "is_downloadable": False,
                "download_url": None,
                "attachments": [],
                "external_url": None,
                "links": [],
                "due_date": "2026-09-22",
                "description": "",
            },
            {
                "content_id": "_hw_file_1",
                "parent_id": "_folder_hw_1",
                "parent_path": ["Homework Assignments"],
                "title": "Database Schema Diagram",
                "content_type": "file",
                "depth": 1,
                "has_children": False,
                "is_downloadable": True,
                "download_url": "https://example.com/schema.png",
                "attachments": [],
                "external_url": None,
                "links": [],
                "due_date": "",
                "description": "",
            },
            # Root Folder 2: Review Materials (Nested)
            {
                "content_id": "_folder_rev_1",
                "parent_id": None,
                "parent_path": [],
                "title": "Exam Review Materials",
                "content_type": "folder",
                "depth": 0,
                "has_children": True,
                "is_downloadable": False,
                "download_url": None,
                "attachments": [],
                "external_url": None,
                "links": [],
                "due_date": "",
                "description": "Study guides and past exams",
            },
            # Child Subfolder inside Review Materials
            {
                "content_id": "_subfolder_midterm_1",
                "parent_id": "_folder_rev_1",
                "parent_path": ["Exam Review Materials"],
                "title": "Midterm Exam Prep",
                "content_type": "folder",
                "depth": 1,
                "has_children": True,
                "is_downloadable": False,
                "download_url": None,
                "attachments": [],
                "external_url": None,
                "links": [],
                "due_date": "",
                "description": "",
            },
            # Child item inside Midterm Exam Prep
            {
                "content_id": "_doc_study_guide",
                "parent_id": "_subfolder_midterm_1",
                "parent_path": ["Exam Review Materials", "Midterm Exam Prep"],
                "title": "Midterm Study Guide",
                "content_type": "document",
                "depth": 2,
                "has_children": False,
                "is_downloadable": True,
                "download_url": "https://example.com/guide.pdf",
                "attachments": [],
                "external_url": None,
                "links": [],
                "due_date": "",
                "description": "",
            },
            # Root Folder 3: Empty Folder
            {
                "content_id": "_folder_empty_1",
                "parent_id": None,
                "parent_path": [],
                "title": "Archive / Extra",
                "content_type": "folder",
                "depth": 0,
                "has_children": False,
                "is_downloadable": False,
                "download_url": None,
                "attachments": [],
                "external_url": None,
                "links": [],
                "due_date": "",
                "description": "",
            },
        ]

    def test_compute_folder_stats(self):
        stats = compute_folder_stats(self.sample_outline)
        
        # Homework folder stats
        hw_stat = stats["_folder_hw_1"]
        self.assertEqual(hw_stat["total_descendants"], 3)
        self.assertEqual(hw_stat["type_counts"]["assignment"], 2)
        self.assertEqual(hw_stat["type_counts"]["file"], 1)
        self.assertIn("3 items: 2 assignments, 1 file", hw_stat["summary_str"])

        # Review Materials (nested) stats
        rev_stat = stats["_folder_rev_1"]
        self.assertEqual(rev_stat["total_descendants"], 2)  # 1 subfolder + 1 document
        self.assertEqual(rev_stat["subfolder_count"], 1)
        self.assertIn("1 document", rev_stat["summary_str"])

        # Empty folder stats
        empty_stat = stats["_folder_empty_1"]
        self.assertEqual(empty_stat["total_descendants"], 0)
        self.assertEqual(empty_stat["summary_str"], "(0 items)")

    def test_filter_outline_by_folder_exact_id(self):
        filtered = filter_outline_by_folder(self.sample_outline, "_folder_hw_1")
        self.assertEqual(len(filtered), 4)  # Folder itself + 3 children
        titles = [item["title"] for item in filtered]
        self.assertIn("Homework Assignments", titles)
        self.assertIn("Homework 1 - SQL Queries", titles)
        self.assertIn("Database Schema Diagram", titles)
        self.assertNotIn("Midterm Study Guide", titles)

    def test_filter_outline_by_folder_fuzzy_name(self):
        filtered = filter_outline_by_folder(self.sample_outline, "homework")
        self.assertEqual(len(filtered), 4)
        titles = [item["title"] for item in filtered]
        self.assertIn("Homework Assignments", titles)
        self.assertIn("Homework 1 - SQL Queries", titles)

    def test_filter_outline_by_subfolder(self):
        filtered = filter_outline_by_folder(self.sample_outline, "midterm")
        self.assertEqual(len(filtered), 2)  # Subfolder + 1 document
        titles = [item["title"] for item in filtered]
        self.assertIn("Midterm Exam Prep", titles)
        self.assertIn("Midterm Study Guide", titles)

    def test_default_shallow_outline_formatting(self):
        output = format_outline_tree(self.sample_outline, "IS 410 Database", "_105737_1")
        
        # Root syllabus should be rendered
        self.assertIn("Course Syllabus", output)
        
        # Root folders should be rendered with counts
        self.assertIn("Homework Assignments [folder]", output)
        self.assertIn("[ID: _folder_hw_1]", output)
        self.assertIn("3 items: 2 assignments, 1 file", output)
        
        # Root folder children should NOT be expanded in default shallow view
        self.assertNotIn("Homework 1 - SQL Queries", output)
        self.assertNotIn("Midterm Study Guide", output)
        
        # Tip footer should be present
        self.assertIn("--folder", output)
        self.assertIn("--expand-all", output)

    def test_expand_all_outline_formatting(self):
        output = format_outline_tree(self.sample_outline, "IS 410 Database", "_105737_1", expand_all=True)
        
        # Everything should be visible
        self.assertIn("Course Syllabus", output)
        self.assertIn("Homework Assignments", output)
        self.assertIn("Homework 1 - SQL Queries", output)
        self.assertIn("Homework 2 - Normalization", output)
        self.assertIn("Database Schema Diagram", output)
        self.assertIn("Midterm Study Guide", output)

    def test_target_folder_expansion_formatting(self):
        output = format_outline_tree(self.sample_outline, "IS 410 Database", "_105737_1", target_folder="homework")
        
        # Only homework folder and its contents should be rendered
        self.assertIn("Homework Assignments", output)
        self.assertIn("Homework 1 - SQL Queries", output)
        self.assertIn("Homework 2 - Normalization", output)
        self.assertNotIn("Midterm Study Guide", output)
        self.assertNotIn("Course Syllabus", output)

    def test_depth_limiting_formatting(self):
        # depth=1 on full outline should show root items and depth 1 items
        output = format_outline_tree(self.sample_outline, "IS 410 Database", "_105737_1", depth=1)
        self.assertIn("Homework Assignments", output)
        self.assertIn("Homework 1 - SQL Queries", output)
        self.assertIn("Midterm Exam Prep", output)
        # depth 2 item should NOT appear when depth=1
        self.assertNotIn("Midterm Study Guide", output)


if __name__ == "__main__":
    unittest.main()
