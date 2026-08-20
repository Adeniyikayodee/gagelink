"""That an agent can find this, install it, and run it.

A package an agent cannot discover is a package that does not exist to one, so the
registry manifest, the package metadata, and the documents that describe the server all
have to agree with each other and with what actually ships. They are checked here because
nothing else checks them until a publish fails or, worse, quietly succeeds while pointing
at the wrong thing.
"""

import json
from pathlib import Path

import gagelink

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = json.loads((ROOT / "server.json").read_text())
README = (ROOT / "README.md").read_text()
LLMS = (ROOT / "llms.txt").read_text()
PYPROJECT = (ROOT / "pyproject.toml").read_text()

SERVER_NAME = "io.github.Adeniyikayodee/gagelink"


def test_the_registry_manifest_names_the_package_that_is_published():
    """A manifest pointing at a package name that is not the one on PyPI installs
    nothing."""
    package = MANIFEST["packages"][0]
    assert package["registryType"] == "pypi"
    assert package["identifier"] == "gagelink"
    assert 'name = "gagelink"' in PYPROJECT


def test_the_manifest_version_matches_the_packaged_one():
    """The registry serves the manifest's version, so a stale one advertises a release
    that may not exist."""
    assert MANIFEST["version"] == gagelink.__version__
    assert MANIFEST["packages"][0]["version"] == gagelink.__version__


def test_the_server_name_agrees_across_every_place_it_appears():
    """The registry validates ownership by matching the name in the manifest against the
    one in the README, so a mismatch fails the publish."""
    assert MANIFEST["name"] == SERVER_NAME
    assert f"mcp-name: {SERVER_NAME}" in README
    assert f"mcp-name: {SERVER_NAME}" in LLMS


def test_the_manifest_declares_a_transport_a_client_can_speak():
    assert MANIFEST["packages"][0]["transport"]["type"] == "stdio"
    assert MANIFEST["packages"][0]["runtimeHint"] == "uvx"


def test_the_only_environment_variable_is_optional():
    """Anything required at startup is a reason an agent never gets past the handshake.
    The key raises an allowance; it does not gate the server."""
    variables = MANIFEST["packages"][0]["environmentVariables"]
    assert [v["name"] for v in variables] == ["GAGELINK_API_KEY"]
    assert variables[0]["isRequired"] is False
    assert variables[0]["isSecret"] is True


def test_the_server_starts_without_any_credential():
    """Checked rather than assumed, since the manifest promises it."""
    from gagelink.server import Server

    assert Server(api_key=None).list_tools()


def test_the_advertised_run_command_matches_the_console_script():
    """uvx runs a console script by name, and the name has to be the one the package
    actually installs or the copy-paste configuration fails."""
    assert 'gagelink-mcp = "gagelink.server:main"' in PYPROJECT
    assert '"--from", "gagelink", "gagelink-mcp"' in README
    assert "uvx --from gagelink gagelink-mcp" in LLMS


def test_every_tool_is_named_in_the_summary_an_agent_reads_first():
    """llms.txt is what an agent reads to decide whether this covers its question, so a
    tool missing from it is a capability the agent will not know exists."""
    from gagelink.server import TOOLS

    for tool in TOOLS:
        assert tool["name"] in LLMS, tool["name"]


def test_the_readme_leads_with_what_it_answers():
    """An agent, or a person choosing tools for one, decides in the first screen."""
    head = README[:2200]
    assert "Questions it answers" in head
    assert "mcpServers" in head
    assert "What it refuses" in head
