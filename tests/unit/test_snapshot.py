import urllib.parse

from securecam.snapshot import SnapshotCapturer


def _url(config, user, password):
    return SnapshotCapturer(config, lambda: (user, password))._stream_url()


def test_credentials_go_in_the_userinfo_not_the_query(config):
    parts = urllib.parse.urlsplit(_url(config, "ticket", "abc.def-ghi_jkl"))
    assert urllib.parse.unquote(parts.username) == "ticket"
    assert urllib.parse.unquote(parts.password) == "abc.def-ghi_jkl"
    assert parts.query == ""
    assert parts.path == f"/{config.mediamtx.path_name}"


def test_the_scheme_host_and_port_survive(config):
    parts = urllib.parse.urlsplit(_url(config, "ticket", "token"))
    original = urllib.parse.urlsplit(config.mediamtx.rtsp_url)
    assert parts.scheme == "rtsp"
    assert parts.hostname == original.hostname
    assert parts.port == original.port


def test_reserved_characters_are_percent_encoded(config):
    parts = urllib.parse.urlsplit(_url(config, "tick@et", "pa:ss/word?x"))
    assert urllib.parse.unquote(parts.username) == "tick@et"
    assert urllib.parse.unquote(parts.password) == "pa:ss/word?x"
    assert parts.path == f"/{config.mediamtx.path_name}"
    assert parts.query == ""
