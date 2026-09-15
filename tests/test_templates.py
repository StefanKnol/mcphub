"""Template invariants that are easy to break and hard to notice.

The styling rule is `input[type=text], input[type=password], ...`. A CSS
attribute selector does not match a *defaulted* type, so an `<input>` written
without one renders completely unstyled next to its neighbours — which is how
the sign-in form ended up with a plain username box above a styled password
box, and nothing failed.
"""

import re
from pathlib import Path

import pytest

TEMPLATES = Path(__file__).resolve().parents[1] / "src" / "mcphub" / "web" / "templates"
INPUT_TAG = re.compile(r"<input\b[^>]*>", re.IGNORECASE | re.DOTALL)
COMMENTS = re.compile(r"<!--.*?-->|/\*.*?\*/", re.DOTALL)
STYLED_TYPES = {"text", "password", "number", "search", "email", "url"}


def template_files():
    return sorted(TEMPLATES.glob("*.html"))


def markup(path: Path) -> str:
    """Template text with comments removed.

    A CSS comment explaining this very rule quotes `<input name="username">`
    as the example, which the scan would otherwise report as a real offender.
    """
    return COMMENTS.sub("", path.read_text())


@pytest.mark.parametrize("path", template_files(), ids=lambda p: p.name)
def test_every_input_declares_its_type(path):
    untyped = [tag for tag in INPUT_TAG.findall(markup(path)) if "type=" not in tag]
    assert not untyped, (
        f"{path.name}: input without an explicit type, which the stylesheet cannot match:\n"
        + "\n".join("  " + " ".join(t.split())[:110] for t in untyped)
    )


def test_stylesheet_covers_every_type_used():
    css = (TEMPLATES / "base.html").read_text()
    used = set()
    for path in template_files():
        for tag in INPUT_TAG.findall(markup(path)):
            match = re.search(r'type="([a-z]+)"', tag)
            if match:
                used.add(match.group(1))

    for kind in used & STYLED_TYPES:
        assert f"input[type={kind}]" in css, f"no styling rule for input[type={kind}]"


def test_untyped_inputs_would_still_be_styled():
    """Belt and braces: the rule also matches an input with no type at all."""
    assert "input:not([type])" in (TEMPLATES / "base.html").read_text()


def test_invalid_styling_waits_for_interaction():
    """`:invalid` alone paints every empty required field red on page load."""
    css = (TEMPLATES / "base.html").read_text()
    assert "input:user-invalid" in css
    assert not re.search(r"(?<!user-)input:invalid\b", css), "bare :invalid fires before typing"
