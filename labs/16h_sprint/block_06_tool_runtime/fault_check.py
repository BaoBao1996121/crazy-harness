from faulty_policy import authorize_after_hook


def test_hook_patch_is_revalidated_before_execution():
    assert authorize_after_hook("app.py", "../secret.txt") is False
