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
    """An agent, or a person choosing tools for one, decides in the first screen.

    What has to be there is the two things that decide it: the questions this answers, and
    that it refuses a comparison rather than guessing at one. The copy-paste configuration
    is checked for separately and is allowed to sit further down, since somebody who has
    decided will scroll and somebody who has not will not.

    The budget is a screen rather than a byte count, and it moved from 2200 when a third
    install path was added. Raise it for an install path or a heading, not to make room for
    prose: the reason the number is here at all is that the section above it grows and the
    reasons to use this do not move up on their own.
    """
    head = README[:2400]
    assert "What can it answer?" in head
    assert "refuses" in head


def test_the_readme_carries_a_configuration_that_can_be_pasted():
    """The no-install path. Without it the first step is working out what to install."""
    assert "mcpServers" in README


# The publish path -------------------------------------------------------------------------


RELEASE = (ROOT / ".github" / "workflows" / "release.yml").read_text()
PAGES = (ROOT / ".github" / "workflows" / "pages.yml").read_text()


def test_the_release_publishes_to_the_registry_and_not_only_to_pypi():
    """The two drifted when the registry publish was a manual step: 0.6.0 reached PyPI
    while the registry went on serving 0.5.0, so anything resolving through the registry
    installed a release without the 2026 protocol, the datum conversion, or the UK search.
    A publish that is part of the release cannot be the step somebody forgets."""
    assert "mcp-publisher" in RELEASE
    assert "login github-oidc" in RELEASE
    assert "registry:" in RELEASE
    assert "needs: publish" in RELEASE


def test_the_release_reads_back_what_the_registry_is_serving():
    """A publish that returns success and lists the previous version is the exact failure
    the job exists to prevent, and it is invisible unless something asks."""
    assert "registry.modelcontextprotocol.io/v0/servers?search=gagelink" in RELEASE
    assert "isLatest" in RELEASE


def test_the_release_refuses_a_tag_that_disagrees_with_the_manifest():
    """server.json carries the version the registry will serve, so a tag built from a
    stale manifest advertises a release that may not exist."""
    assert "server.json" in RELEASE


def test_the_summary_an_agent_reads_first_is_served_from_a_root():
    """llms.txt is looked for at https://<domain>/llms.txt. One reachable only at a blob
    URL inside the repository is a file written for agents that no agent fetches."""
    assert "llms.txt" in PAGES
    assert "server.json" in PAGES
    assert MANIFEST["websiteUrl"].startswith("https://adeniyikayodee.github.io/gagelink")


#: What the registry will accept, from the schema server.json names in its own $schema
#: field. Held here as numbers rather than fetched, because the suite answers offline and a
#: check that needs the network is a check that gets skipped. The v0.7.0 release published
#: to PyPI and then failed at the registry on a description of 193 characters, which is the
#: kind of thing worth finding before a version number is spent.
REGISTRY_LIMITS = {"description": 100, "name": 200, "title": 100, "version": 255}


def test_the_manifest_fits_inside_what_the_registry_accepts():
    """A field over length is a 422 after PyPI has already taken the release, and a PyPI
    filename is permanent, so the registry is the half that cannot then be retried under
    the same version."""
    for field, limit in REGISTRY_LIMITS.items():
        value = MANIFEST.get(field)
        if value is not None:
            assert len(value) <= limit, f"{field} is {len(value)} characters, limit {limit}"


def test_the_manifest_description_names_every_network_that_answers():
    """A search for a UK or French station matches on this text, and a description naming
    only the US services is one those searches do not reach."""
    described = MANIFEST["description"]
    for service in ("USGS", "NOAA", "Hub'Eau", "Environment Agency", "SWOT"):
        assert service in described, service
    # Both constraints at once is the whole difficulty: a hundred characters that name
    # five services leave room for little else, so this is checked rather than eyeballed.
    assert len(described) <= REGISTRY_LIMITS["description"]


# The surface besides the tools ------------------------------------------------------------


def test_every_prompt_is_named_where_an_agent_will_read_it():
    """A prompt states an order of operations a model gets wrong when it assembles one
    itself, which makes it worth finding before installation rather than after."""
    from gagelink.catalogue import PROMPTS

    for declared in PROMPTS:
        assert declared["name"] in LLMS, declared["name"]
        assert declared["name"] in README, declared["name"]


def test_every_resource_is_named_where_an_agent_will_read_it():
    from gagelink.catalogue import RESOURCES

    for declared in RESOURCES:
        assert declared["uri"] in LLMS, declared["uri"]
        assert declared["uri"] in README, declared["uri"]


def test_the_bundle_is_offered_as_an_install_path():
    """The one path that asks for nothing first. Somebody without a Python environment
    prepared has no other way in that does not start with preparing one."""
    assert ".mcpb" in README
    assert ".mcpb" in LLMS
    assert "scripts/build_bundle.py" in RELEASE


def test_the_bundle_manifest_is_generated_from_the_package_it_describes():
    """A tool list maintained by hand beside the code goes stale in the direction of
    promising tools that are not there, which a client shows before installation."""
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    from build_bundle import manifest

    from gagelink.catalogue import PROMPTS
    from gagelink.server import TOOLS

    built = manifest()
    assert built["version"] == gagelink.__version__
    assert [t["name"] for t in built["tools"]] == [t["name"] for t in TOOLS]
    assert [p["name"] for p in built["prompts"]] == [p["name"] for p in PROMPTS]


def test_the_bundle_asks_for_no_credential_to_start():
    """The manifest promises the server runs without a key, the same as every other
    install path. A required field here would be a wall the other paths do not have."""
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    from build_bundle import manifest

    configured = manifest()["user_config"]
    assert list(configured) == ["api_key"]
    assert configured["api_key"]["required"] is False
    assert configured["api_key"]["sensitive"] is True
