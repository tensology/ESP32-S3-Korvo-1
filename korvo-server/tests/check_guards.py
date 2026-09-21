"""Runnable checks for LAN allowlisting and secret redaction."""

import ipaddress

from korvo_server.lan_guard import host_allowed, url_allowed
from korvo_server.secret_settings import merge_secret_updates, redact_settings


def main() -> None:
    assert host_allowed("127.0.0.1")
    assert host_allowed("10.1.2.3")
    assert host_allowed("192.168.4.1")
    assert host_allowed("172.16.0.1")
    assert host_allowed("172.31.255.255")
    assert not host_allowed("172.15.255.255")
    assert not host_allowed("172.32.0.1")
    assert not host_allowed("172.217.1.1")
    assert not host_allowed("8.8.8.8")
    assert not host_allowed("0.0.0.0")
    assert not host_allowed("169.254.169.254")
    assert not host_allowed("evil.example/scan")
    assert url_allowed("http://192.168.1.20/api/audio/stream")
    assert not url_allowed("http://8.8.8.8/scan")
    assert not url_allowed("http://user:pass@192.168.1.1/")
    # ipaddress agrees with the 172.16/12 boundary used above.
    assert ipaddress.ip_address("172.16.0.1").is_private
    assert not ipaddress.ip_address("172.217.1.1").is_private

    redacted = redact_settings({"wake_word": "hilexin", "openai_api_key": "sk-test", "aws_region": "eu-west-1"})
    assert redacted["wake_word"] == "hilexin"
    assert redacted["openai_api_key"] == ""
    assert redacted["openai_api_key_set"] == "1"
    assert redacted["aws_region"] == "eu-west-1"
    merged = merge_secret_updates(
        {"openai_api_key": "sk-test", "wake_word": "hilexin"},
        {"openai_api_key": "", "wake_word": "alexa"},
    )
    assert merged["openai_api_key"] == "sk-test"
    assert merged["wake_word"] == "alexa"
    print("ok")


if __name__ == "__main__":
    main()
