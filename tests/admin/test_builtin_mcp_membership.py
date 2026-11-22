"""Unit tests for admin/builtin_mcp_membership.py — pure ordering logic."""


def test_builtin_mcp_name_is_stable_and_kind_scoped():
    from admin.builtin_mcp_membership import builtin_mcp_name

    assert builtin_mcp_name("business") == "__builtin__:business"
    assert builtin_mcp_name("business") == builtin_mcp_name("business")
    assert builtin_mcp_name("orchard") != builtin_mcp_name("business")


def test_ordered_membership_follows_pool_order_then_http():
    from admin.builtin_mcp_membership import ordered_membership

    # Factory pool order: b, a, c (deliberately not alphabetical)
    pool = ["get_b", "get_a", "get_c"]
    rows = [
        {"python_name": "get_a", "tool_id": "id-a", "kind": "python"},
        {"python_name": "get_b", "tool_id": "id-b", "kind": "python"},
        {"python_name": "get_c", "tool_id": "id-c", "kind": "python"},
        # an HTTP tool not in the factory pool
        {"python_name": "crm_http", "tool_id": "id-h", "kind": "http"},
    ]
    members = ordered_membership(pool, rows)
    # pool order first (b,a,c), then http appended
    assert [m["python_name"] for m in members] == ["get_b", "get_a", "get_c", "crm_http"]
    assert [m["position"] for m in members] == [0, 1, 2, 3]
    assert members[3]["kind"] == "http"


def test_ordered_membership_skips_rows_without_name_or_id_and_dedups_pool():
    from admin.builtin_mcp_membership import ordered_membership

    pool = ["get_a", "get_a"]  # defensive: dup in pool
    rows = [
        {"python_name": "get_a", "tool_id": "id-a", "kind": "python"},
        {"python_name": "", "tool_id": "id-x", "kind": "python"},      # no name
        {"python_name": "get_z", "tool_id": None, "kind": "python"},   # no id
    ]
    members = ordered_membership(pool, rows)
    assert [m["python_name"] for m in members] == ["get_a"]
    assert members[0]["position"] == 0


def test_ordered_membership_duplicate_pool_name_uses_first_index():
    from admin.builtin_mcp_membership import ordered_membership
    pool = ["get_b", "get_a", "get_b"]  # get_b appears at index 0 and 2
    rows = [
        {"python_name": "get_a", "tool_id": "id-a", "kind": "python"},
        {"python_name": "get_b", "tool_id": "id-b", "kind": "python"},
    ]
    members = ordered_membership(pool, rows)
    # get_b's FIRST index (0) wins, so it sorts before get_a (index 1)
    assert [m["python_name"] for m in members] == ["get_b", "get_a"]


def test_ordered_membership_multiple_http_sorted_deterministically():
    from admin.builtin_mcp_membership import ordered_membership

    pool = ["get_a"]
    rows = [
        {"python_name": "get_a", "tool_id": "id-a", "kind": "python"},
        {"python_name": "zzz_http", "tool_id": "id-z", "kind": "http"},
        {"python_name": "aaa_http", "tool_id": "id-2", "kind": "http"},
    ]
    members = ordered_membership(pool, rows)
    # python pool first, http appended in python_name-sorted order
    assert [m["python_name"] for m in members] == ["get_a", "aaa_http", "zzz_http"]
    assert [m["position"] for m in members] == [0, 1, 2]
