"""Tests for src/email_send.py.

Only the pure-function pieces are tested directly (markdown_to_html and the
input validation guards in send_email). The actual Resend POST is not exercised
in unit tests — that path is integration-tested by a real run.
"""

from __future__ import annotations

import pytest

from src.email_send import EmailSendError, markdown_to_html, send_email


class TestMarkdownToHtml:
    def test_h2_rendered(self):
        assert "<h2>Shops on sale</h2>" in markdown_to_html("## Shops on sale")

    def test_h3_rendered(self):
        assert "<h3>Aniqi</h3>" in markdown_to_html("### Aniqi")

    def test_h3_distinct_from_h2(self):
        """### must be parsed as h3, not h2 with a leading '# '."""
        html = markdown_to_html("### Aniqi")
        assert "<h2>" not in html

    def test_bullet_list_wrapped_in_ul(self):
        html = markdown_to_html("- one\n- two")
        assert "<ul>" in html
        assert "</ul>" in html
        assert "<li>one</li>" in html
        assert "<li>two</li>" in html

    def test_bold_inline(self):
        html = markdown_to_html("**Aritzia**: 30% off")
        assert "<strong>Aritzia</strong>" in html

    def test_link_inline(self):
        html = markdown_to_html("- [link](https://example.com/x)")
        assert '<a href="https://example.com/x">link</a>' in html

    def test_bold_inside_list_item(self):
        html = markdown_to_html("- **Cool Shirt** — $40")
        assert "<li><strong>Cool Shirt</strong>" in html

    def test_list_closes_when_followed_by_header(self):
        html = markdown_to_html("- one\n\n## Next")
        # The list must close before the next h2.
        assert html.index("</ul>") < html.index("<h2>Next</h2>")

    def test_paragraph_for_non_list_non_header(self):
        html = markdown_to_html("Aniqi, Hokuro, Onsen")
        assert "<p>Aniqi, Hokuro, Onsen</p>" in html

    def test_doctype_and_style(self):
        html = markdown_to_html("## anything")
        assert "<!DOCTYPE html>" in html
        assert "<style>" in html
        assert 'charset="utf-8"' in html

    def test_em_dash_preserved(self):
        """Em-dashes must survive — the whole point of skipping stdin piping."""
        html = markdown_to_html("- **Cool** — $40")
        assert "—" in html

    def test_non_link_brackets_dont_swallow_real_link(self):
        """FX dual format puts '[CAD $45]' inline. The link regex must not
        chain across that bracketed text into the real [link](url) at end."""
        html = markdown_to_html(
            "- **Toque** — $30 USD [CAD $45] — [link](https://shop.com/x)"
        )
        assert '<a href="https://shop.com/x">link</a>' in html
        assert "[CAD $45]" in html  # bracketed text stays as literal text


class TestSendEmailValidation:
    def test_missing_api_key_raises(self):
        with pytest.raises(EmailSendError, match="required"):
            send_email("", "a@b.com", "c@d.com", "subj", "body")

    def test_missing_from_addr_raises(self):
        with pytest.raises(EmailSendError, match="required"):
            send_email("key", "", "c@d.com", "subj", "body")

    def test_missing_to_addr_raises(self):
        with pytest.raises(EmailSendError, match="required"):
            send_email("key", "a@b.com", "", "subj", "body")

    def test_empty_body_raises(self):
        with pytest.raises(EmailSendError, match="empty"):
            send_email("key", "a@b.com", "c@d.com", "subj", "   \n  ")
