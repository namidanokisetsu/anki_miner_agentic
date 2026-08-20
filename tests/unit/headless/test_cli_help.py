from anki_miner.headless.cli import main


def test_help_is_compact_and_needs_no_config(capsys):
    assert main(["help"]) == 0
    output = capsys.readouterr().out

    assert output.count("\n") <= 4
    assert "settings" in output
    assert "workflow" in output
    assert "commands" in output


def test_settings_help_names_policy_owners(capsys):
    assert main(["help", "settings"]) == 0
    output = capsys.readouterr().out

    assert "knowledge_sources" in output
    assert "GUI profile" in output
    assert "runtime_overrides" not in output
    assert output.count("\n") <= 6
