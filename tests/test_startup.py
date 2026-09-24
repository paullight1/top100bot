from unipod_bot.main import parse_args, run


def test_parse_check_flag():
    assert parse_args(["--check"]).check is True
    assert parse_args([]).check is False


def test_check_startup_exits_without_connect(monkeypatch, capsys):
    monkeypatch.setenv("DEBUG", "true")
    monkeypatch.setenv("ADMIN_PHONES", "2348011111111")
    run(["--check"])
    out = capsys.readouterr().out
    assert "event=startup" in out
    assert "event=check_ok" in out
    assert "event=connecting" not in out
