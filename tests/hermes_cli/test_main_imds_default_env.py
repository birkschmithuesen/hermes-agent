"""hermes CLI processes disable AWS IMDS probing by default (#cron egress-deny-metadata noise).

``agent.auxiliary_client._build_bedrock_client`` falls back to
``agent.bedrock_adapter.has_aws_credentials()``, which — with the default
``instance_metadata=True`` and no AWS env var chain resolved — makes a real
boto3 IMDS probe to 169.254.169.254. Cron jobs that shell out to
``hermes ... kanban ...`` start a fresh CLI process per cycle, so every such
process without this default re-triggers the probe: wasted latency on a host
that never runs on EC2/ECS/Lambda, plus egress-deny-metadata alert noise.
``hermes_cli.main._disable_aws_imds_probe`` sets the IMDS-disabling env
defaults before any dispatch path can reach that call, mirroring the
test-only guard already in ``tests/conftest.py``.
"""
from __future__ import annotations

import os

from hermes_cli import main as main_mod


def test_disable_aws_imds_probe_sets_default_when_unset(monkeypatch):
    monkeypatch.delenv("AWS_EC2_METADATA_DISABLED", raising=False)
    monkeypatch.delenv("AWS_METADATA_SERVICE_TIMEOUT", raising=False)
    monkeypatch.delenv("AWS_METADATA_SERVICE_NUM_ATTEMPTS", raising=False)

    main_mod._disable_aws_imds_probe()

    assert os.environ["AWS_EC2_METADATA_DISABLED"] == "true"
    assert os.environ["AWS_METADATA_SERVICE_TIMEOUT"] == "1"
    assert os.environ["AWS_METADATA_SERVICE_NUM_ATTEMPTS"] == "1"


def test_disable_aws_imds_probe_never_overrides_a_caller_set_value(monkeypatch):
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "false")

    main_mod._disable_aws_imds_probe()

    assert os.environ["AWS_EC2_METADATA_DISABLED"] == "false"
