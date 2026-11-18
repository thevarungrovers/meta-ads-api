"""The write client's own naming guard, below every tool: with a prefix
configured, a named create or a rename without it is refused before anything
is sent. AB / max is the agent, AB-cli- the CLI."""

from __future__ import annotations

import pytest

from metaads_reporting.write.client import MutationError
from metaads_reporting.write.plan import PlannedChange, execute
from tests.test_write_plan import AD_ID, CAMPAIGN_ID, client  # noqa: F401 - the fixture


CREATES = [
    ("campaigns", {"status": "PAUSED", "objective": "OUTCOME_SALES"}),
    ("adsets", {"status": "PAUSED"}),
    ("ads", {"status": "PAUSED"}),
    ("adcreatives", {}),
    ("customaudiences", {}),
    ("saved_audiences", {}),
    ("ad_studies", {}),
    ("adlabels", {}),
]


@pytest.mark.parametrize("edge, extra", CREATES)
def test_an_unprefixed_named_create_is_refused_before_anything_is_sent(client, edge, extra):
    client.required_name_prefix = "AB-max-"
    sent = len(client._sim.writes)
    with pytest.raises(MutationError, match="AB-max-"):
        client.create(entity_type="x", edge=edge, payload={"name": "spring", **extra}, apply=True)
    assert len(client._sim.writes) == sent


@pytest.mark.parametrize("edge, extra", CREATES)
def test_a_prefixed_create_goes_through(client, edge, extra):
    client.required_name_prefix = "AB-max-"
    client.create(entity_type="x", edge=edge, payload={"name": "AB-max-spring", **extra})


def test_copies_are_exempt(client):
    client.required_name_prefix = "AB-max-"
    client.create(entity_type="campaign", edge="copies", parent=CAMPAIGN_ID,
                  payload={"status_option": "PAUSED", "deep_copy": "false"})


def test_an_image_upload_is_not_named(client):
    client.required_name_prefix = "AB-max-"
    client.create(entity_type="adimage", edge="adimages", payload={"bytes": "x"})


def test_a_rename_without_the_prefix_is_refused_unless_it_is_a_restore(client):
    client.required_name_prefix = "AB-max-"
    with pytest.raises(MutationError, match="AB-max-"):
        client.update(AD_ID, entity_type="ad", payload={"name": "plain"}, apply=True)
    plan = PlannedChange(action="update", entity="ad", entity_type="ad", node_id=AD_ID, label="x",
                         payload={"name": "plain"}, allow_unprefixed_name=True)
    execute(client, plan, apply=True)
    assert client._sim.objects[AD_ID]["name"] == "plain"


def test_no_prefix_configured_requires_nothing(client):
    assert client.required_name_prefix == ""
    client.create(entity_type="campaign", edge="campaigns",
                  payload={"name": "spring", "status": "PAUSED"})
    client.update(AD_ID, entity_type="ad", payload={"name": "plain"})


def test_the_cli_prefix_is_required_the_same_way(client):
    client.required_name_prefix = "AB-cli-"
    with pytest.raises(MutationError, match="AB-cli-"):
        client.create(entity_type="audience", edge="customaudiences", payload={"name": "AB-max-x"})
    client.create(entity_type="audience", edge="customaudiences", payload={"name": "AB-cli-x"})
