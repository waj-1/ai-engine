"""Marks a test that needs this deployment's product plugin.

The generic engine runs with no plugin (`engine_plugins.PLUGINS == ()`), and
there a test that asserts a product route, seeder, env requirement or prompt
has nothing to assert about. Such a test is skipped there, with the reason
shown, instead of being deleted or failing:

    from tests.product import requires_product

    @requires_product
    def test_product_route_requires_its_env_var(): ...

Use it for a product assumption inside an otherwise generic test file. A file
that is ALL product tests should simply import the product code it tests; the
open-source export then leaves it out on its own.
"""
from __future__ import annotations

import pytest

import engine_plugins

HAS_PRODUCT = bool(engine_plugins.PLUGINS)

requires_product = pytest.mark.skipif(
    not HAS_PRODUCT, reason="needs this deployment's product plugin (engine_plugins.PLUGINS is empty)")
