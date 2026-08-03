"""Card extraction unit tests using fake DOM elements."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from extraction import extract_card_project, parse_posted_time, PLATFORM_CAPABILITIES


class FakeEl:
    def __init__(self, text="", href=None, css_class="", tag="div", children=None, attrs=None):
        self.text = text
        self.tag_name = tag
        self._href = href
        self._class = css_class
        self._children = children or []
        self._attrs = attrs or {}
        self.id = id(self)

    def get_attribute(self, name):
        if name == "href":
            return self._href
        if name == "class":
            return self._class
        if name == "outerHTML":
            return f"<{self.tag_name}>{self.text}</{self.tag_name}>"
        return self._attrs.get(name)

    def find_elements(self, by, sel):
        # simplified: return children matching crude rules
        out = []
        for c in self._children:
            if by in ("css selector", "CSS_SELECTOR") or by == "css selector":
                if "h3" in sel and c.tag_name == "h3":
                    out.append(c)
                elif "a[href*='/brief/']" in sel and c._href and "/brief/" in c._href:
                    out.append(c)
                elif "StatusTag" in sel and "StatusTag" in (c._class or ""):
                    out.append(c)
            if by in ("xpath", "XPATH") or by == "xpath":
                if "not(child::*)" in sel and not c._children:
                    out.append(c)
        return out

    def find_element(self, by, sel):
        els = self.find_elements(by, sel)
        if not els:
            raise Exception("not found")
        return els[0]


class TestCardExtraction(unittest.TestCase):
    def _card(self):
        title = FakeEl("Senior Risk Analyst", tag="h3")
        link = FakeEl("Senior Risk Analyst", href="https://talent.fintalent.io/brief/ft123abc", tag="a")
        status = FakeEl("Open", css_class="StatusTag")
        loc = FakeEl("Remote · UTC+1", tag="span")
        budget = FakeEl("$120/hour", tag="span")
        duration = FakeEl("3 months", tag="span")
        posted = FakeEl("2 hours ago", tag="span")
        return FakeEl(children=[title, link, status, loc, budget, duration, posted])

    def test_title_and_identity(self):
        # Patch extract helpers by building a more cooperative card mock
        card = MagicMock()
        title_el = MagicMock()
        title_el.text = "Senior Risk Analyst"
        link_el = MagicMock()
        link_el.get_attribute.return_value = "https://talent.fintalent.io/brief/ft123abc"
        link_el.text = "Senior Risk Analyst"

        def find_elements(by, sel):
            if sel in ("h3", "h4", "h2", "h5") or sel == "h3":
                return [title_el]
            if "StatusTag" in sel or "status" in sel:
                return []
            if "not(child::*)" in str(sel):
                return [
                    MagicMock(text="Remote UTC+1"),
                    MagicMock(text="$120/hour"),
                    MagicMock(text="3 months"),
                    MagicMock(text="2 hours ago"),
                ]
            return []

        def find_element(by, sel):
            if "brief" in sel or "project" in sel or "link" in sel:
                return link_el
            raise Exception("no")

        card.find_elements.side_effect = find_elements
        card.find_element.side_effect = find_element
        card.get_attribute.return_value = ""

        result = extract_card_project(card)
        self.assertTrue(result["ok"])
        self.assertEqual(result["fields"]["title"], "Senior Risk Analyst")
        self.assertEqual(result["fields"]["project_id"], "ft123abc")
        self.assertIn("/brief/ft123abc", result["fields"]["source_url"])

    def test_no_invented_recently(self):
        card = MagicMock()
        title_el = MagicMock(text="Title Only")
        link_el = MagicMock()
        link_el.get_attribute.return_value = "https://talent.fintalent.io/brief/only1"
        link_el.text = "Title Only"

        def find_elements(by, sel):
            if sel == "h3":
                return [title_el]
            if "not(child::*)" in str(sel):
                return []
            return []

        card.find_elements.side_effect = find_elements
        card.find_element.side_effect = lambda by, sel: link_el if "brief" in sel else (_ for _ in ()).throw(Exception())
        card.get_attribute.return_value = ""
        result = extract_card_project(card)
        self.assertTrue(result["ok"])
        self.assertIsNone(result["fields"].get("time_posted_text"))
        self.assertNotEqual(result["fields"].get("time_posted_text"), "Recently")

    def test_no_invented_new_project_status(self):
        card = MagicMock()
        title_el = MagicMock(text="Title Only")
        link_el = MagicMock()
        link_el.get_attribute.return_value = "https://talent.fintalent.io/brief/only2"
        link_el.text = "x"

        def find_elements(by, sel):
            if sel == "h3":
                return [title_el]
            return []

        card.find_elements.side_effect = find_elements
        card.find_element.side_effect = lambda by, sel: link_el if "brief" in sel else (_ for _ in ()).throw(Exception())
        card.get_attribute.return_value = ""
        result = extract_card_project(card)
        self.assertTrue(result["ok"])
        self.assertIsNone(result["fields"].get("status"))
        self.assertNotEqual(result["fields"].get("status"), "New Project")

    def test_category_not_exposed(self):
        self.assertFalse(PLATFORM_CAPABILITIES["fintalent"]["category_exposed"])
        card = MagicMock()
        title_el = MagicMock(text="Cat Test")
        link_el = MagicMock()
        link_el.get_attribute.return_value = "https://talent.fintalent.io/brief/cat1"
        card.find_elements.side_effect = lambda by, sel: [title_el] if sel == "h3" else []
        card.find_element.side_effect = lambda by, sel: link_el if "brief" in sel else (_ for _ in ()).throw(Exception())
        card.get_attribute.return_value = ""
        result = extract_card_project(card)
        self.assertEqual(result["fields"]["platform_category_extraction_status"], "NOT_EXPOSED")
        self.assertEqual(result["card_extraction_status"], "COMPLETE")

    def test_rejects_missing_identity(self):
        card = MagicMock()
        title_el = MagicMock(text="No Link Title")
        card.find_elements.side_effect = lambda by, sel: [title_el] if sel == "h3" else []
        card.find_element.side_effect = Exception("none")
        card.get_attribute.return_value = ""
        result = extract_card_project(card)
        self.assertFalse(result["ok"])
        self.assertEqual(result["card_extraction_status"], "FAILED")

    def test_parse_posted_time(self):
        now = datetime_from = __import__("datetime").datetime(2026, 8, 4, 12, 0, tzinfo=__import__("datetime").timezone.utc)
        p = parse_posted_time("2 hours ago", now=now)
        self.assertEqual(p["time_posted_text"], "2 hours ago")
        self.assertTrue(p["source_posted_at_is_estimated"])
        self.assertIsNotNone(p["source_posted_at"])
        none_p = parse_posted_time(None)
        self.assertIsNone(none_p["time_posted_text"])
        recent = parse_posted_time("Recently")
        self.assertIsNone(recent["time_posted_text"])


if __name__ == "__main__":
    unittest.main()
