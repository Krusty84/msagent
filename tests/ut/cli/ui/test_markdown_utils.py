from __future__ import annotations

import pytest

from msagent.cli.ui.markdown import wrap_html_in_code_blocks


def test_wrap_html_in_code_blocks_preserves_markdown_when_content_has_no_html() -> None:
    content = "# Heading\n\nPlain **markdown** text."

    assert wrap_html_in_code_blocks(content) == content


def test_wrap_html_in_code_blocks_wraps_html_block_when_block_is_complete() -> None:
    content = "<section>\n<p>Details</p>\n</section>"

    assert wrap_html_in_code_blocks(content) == "```html\n<section>\n<p>Details</p>\n</section>\n```"


def test_wrap_html_in_code_blocks_wraps_only_html_line_when_block_contains_plain_text() -> None:
    content = "<details>\nplain explanation\n</details>"

    result = wrap_html_in_code_blocks(content)

    assert result == "```html\n<details>\n```\nplain explanation\n```html\n</details>\n```"


def test_wrap_html_in_code_blocks_wraps_inline_html_when_html_appears_in_paragraph() -> None:
    content = "Before <span>important</span> after"

    assert wrap_html_in_code_blocks(content) == "```html\nBefore <span>important</span> after\n```"


def test_wrap_html_in_code_blocks_preserves_fenced_code_when_code_contains_html() -> None:
    content = "```html\n<div>example</div>\n```"

    assert wrap_html_in_code_blocks(content) == content


def test_wrap_html_in_code_blocks_preserves_indented_code_when_code_contains_html() -> None:
    content = "    <div>example</div>"

    assert wrap_html_in_code_blocks(content) == content


def test_wrap_html_in_code_blocks_escapes_html_when_tag_is_incomplete() -> None:
    content = "<section"

    assert wrap_html_in_code_blocks(content) == "&lt;section"


@pytest.mark.parametrize("content", ["", "plain text\n", "line one\nline two"])
def test_wrap_html_in_code_blocks_preserves_line_structure_when_content_is_plain(
    content: str,
) -> None:
    assert wrap_html_in_code_blocks(content) == content
