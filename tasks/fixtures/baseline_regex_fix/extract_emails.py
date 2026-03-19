import re

EMAIL_PATTERN = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{3,}\b")


def extract_emails(text: str) -> list[str]:
    return EMAIL_PATTERN.findall(text)
