from tasks.fixtures.baseline_regex_fix.extract_emails import extract_emails


def test_extract_emails() -> None:
    text = "Reach us at ops@example.com, qa-team@sample.io, or admin@service.dev."
    assert extract_emails(text) == [
        "ops@example.com",
        "qa-team@sample.io",
        "admin@service.dev",
    ]
