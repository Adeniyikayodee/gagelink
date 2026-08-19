"""Checks on what actually ships, as opposed to what the source says."""

from pathlib import Path

import gagelink


def test_the_package_is_marked_as_typed():
    """The classifier claims inline types; without this marker no checker reads them."""
    assert (Path(gagelink.__file__).parent / "py.typed").exists()


def test_the_version_is_stated_once():
    """pyproject reads the version from here, so the two cannot drift apart."""
    pyproject = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text()
    assert 'dynamic = ["version"]' in pyproject
    assert 'path = "src/gagelink/__init__.py"' in pyproject
    assert gagelink.__version__.count(".") == 2


def test_quantity_guard_is_a_declared_dependency():
    """The boundary checks are imported rather than reimplemented, and a wheel that does
    not say so installs into an environment where they are absent."""
    pyproject = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text()
    assert "quantity-guard" in pyproject
