"""Unit tests for the config-driven sfmc-follow CLI flag promotion."""

from autopilot.launch import _augment_argv


def write_config(tmp_path, **kv):
    import yaml

    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(kv))
    return str(path)


def test_promotes_sfmc_hostname(tmp_path):
    cfg = write_config(
        tmp_path, predictions_dir="p", sfmc_hostname="gliderfmc0.example.edu"
    )
    argv = ["--glider", "osu999", "--config", cfg]

    assert _augment_argv(argv) == [*argv, "--hostname", "gliderfmc0.example.edu"]


def test_config_equals_form(tmp_path):
    cfg = write_config(tmp_path, sfmc_hostname="gliderfmc0.example.edu")
    argv = [f"--config={cfg}"]

    assert _augment_argv(argv) == [*argv, "--hostname", "gliderfmc0.example.edu"]


def test_explicit_hostname_wins(tmp_path):
    cfg = write_config(tmp_path, sfmc_hostname="gliderfmc0.example.edu")
    argv = ["--config", cfg, "--hostname", "gliderfmc1.example.edu"]

    assert _augment_argv(argv) == argv


def test_explicit_hostname_equals_form_wins(tmp_path):
    cfg = write_config(tmp_path, sfmc_hostname="gliderfmc0.example.edu")
    argv = ["--config", cfg, "--hostname=gliderfmc1.example.edu"]

    assert _augment_argv(argv) == argv


def test_no_config_flag_is_a_no_op():
    argv = ["--glider", "osu999"]
    assert _augment_argv(argv) == argv


def test_config_without_mapped_keys_is_a_no_op(tmp_path):
    cfg = write_config(tmp_path, predictions_dir="p")
    argv = ["--config", cfg]
    assert _augment_argv(argv) == argv


def test_missing_config_file_is_a_no_op():
    argv = ["--config", "/does/not/exist.yaml"]
    assert _augment_argv(argv) == argv


def test_malformed_config_file_is_a_no_op(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("predictions_dir: [unclosed\n")
    argv = ["--config", str(path)]
    assert _augment_argv(argv) == argv


# ── notify_email / notify_from ──────────────────────────────────


def test_promotes_notify_email_list(tmp_path):
    cfg = write_config(tmp_path, notify_email=["a@example.edu", "b@example.edu"])
    argv = ["--config", cfg]

    assert _augment_argv(argv) == [
        *argv,
        "--notify-email",
        "a@example.edu",
        "--notify-email",
        "b@example.edu",
    ]


def test_promotes_notify_email_single_string(tmp_path):
    cfg = write_config(tmp_path, notify_email="a@example.edu")
    argv = ["--config", cfg]

    assert _augment_argv(argv) == [*argv, "--notify-email", "a@example.edu"]


def test_promotes_notify_from(tmp_path):
    cfg = write_config(tmp_path, notify_from="autopilot@example.edu")
    argv = ["--config", cfg]

    assert _augment_argv(argv) == [*argv, "--notify-from", "autopilot@example.edu"]


def test_explicit_notify_email_skips_config_entirely(tmp_path):
    cfg = write_config(tmp_path, notify_email=["a@example.edu", "b@example.edu"])
    argv = ["--config", cfg, "--notify-email", "manual@example.edu"]

    assert _augment_argv(argv) == argv


def test_explicit_notify_from_wins(tmp_path):
    cfg = write_config(tmp_path, notify_from="config@example.edu")
    argv = ["--config", cfg, "--notify-from", "manual@example.edu"]

    assert _augment_argv(argv) == argv


def test_all_keys_promoted_together(tmp_path):
    cfg = write_config(
        tmp_path,
        sfmc_hostname="gliderfmc0.example.edu",
        notify_from="autopilot@example.edu",
        notify_email=["a@example.edu", "b@example.edu"],
    )
    argv = ["--config", cfg]

    assert _augment_argv(argv) == [
        *argv,
        "--hostname",
        "gliderfmc0.example.edu",
        "--notify-from",
        "autopilot@example.edu",
        "--notify-email",
        "a@example.edu",
        "--notify-email",
        "b@example.edu",
    ]
