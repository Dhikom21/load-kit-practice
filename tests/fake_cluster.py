"""The simulated node (fake cluster) moved into the kit: loadkit/plugins/runners/fake.py.

This file re-exports it so existing test imports (`from tests.fake_cluster import FakeCluster`) keep working.
"""
from loadkit.plugins.runners.fake import FakeCluster  # noqa: F401
