"""Actual local upstream parsers plus explicit offline source-contract fixtures."""
import json
import wave
import pytest

@pytest.fixture
def workspace(monkeypatch,tmp_path):
    import core.tools_bridge as bridge
    monkeypatch.setattr(bridge,"WORKSPACE_DIR",tmp_path)
    return tmp_path

def test_stix_actual_valid_bundle_metadata(workspace):
    import stix2
    from core.intelligence.tools import execute_tool
    identity=stix2.Identity(name="Synthetic WISE QA",identity_class="organization")
    (workspace/"bundle.json").write_text(stix2.Bundle(identity).serialize(),encoding="utf-8")
    result=execute_tool("stix-taxii",path="bundle.json")
    assert result["success"] and result["total"]==1
    assert result["records"][0]["name"]=="Synthetic WISE QA"
    assert "not TAXII" in result["coverage"]

def test_ffprobe_actual_small_local_audio(workspace):
    from core.intelligence.inventory import profile
    from core.intelligence.tools import execute_tool
    if not profile("ffmpeg")["available"]: pytest.skip("ffprobe not installed")
    with wave.open(str(workspace/"synthetic.wav"),"wb") as stream:
        stream.setnchannels(1);stream.setsampwidth(2);stream.setframerate(16000)
        stream.writeframes(b"\x00\x00"*160)
    result=execute_tool("ffmpeg",path="synthetic.wav")
    assert result["success"] and json.loads(result["output"])["format"]["format_name"]=="wav"

def test_media_playlist_is_rejected_before_any_external_stream(workspace):
    from core.intelligence.inventory import profile
    from core.intelligence.tools import execute_tool
    if not profile("ffmpeg")["available"]: pytest.skip("ffprobe not installed")
    (workspace/"unsafe.m3u").write_text("http://127.0.0.1/private")
    with pytest.raises(ValueError,match="playlists"):
        execute_tool("ffmpeg",path="unsafe.m3u")

def test_actual_psutil_only_aggregate_resources():
    from core.intelligence.tools import execute_tool
    result=execute_tool("psutil")
    assert result["success"] and result["memory"]["total"]>0
    assert set(result)=={"cpu_percent","memory","coverage","success"}

@pytest.fixture
def feed(monkeypatch):
    import core.intelligence.sources as sources
    import core.web_research as research
    monkeypatch.setattr(research,"_public_url",lambda url:url)
    state={"payload":b""}
    class Response:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def raise_for_status(self):pass
        def iter_bytes(self):yield state["payload"]
    class Client(Response):
        def __init__(self,**kwargs):pass
        def stream(self,*args,**kwargs):return Response()
    monkeypatch.setattr(sources.httpx,"Client",Client)
    return state

def test_rss_contract_preserves_publication_and_provenance(feed):
    from core.intelligence.sources import execute_source
    feed["payload"]=b"<rss><channel><item><title>QA</title><link>https://example.com/qa</link><pubDate>fixture date</pubDate></item></channel></rss>"
    result=execute_source("rss-atom-feeds",action="query",query="https://example.com/feed")
    assert result["records"][0]["title"]=="QA"
    assert result["records"][0]["published_at"]=="fixture date"
    assert result["provenance"]["source_url"]=="https://example.com/feed"

@pytest.mark.parametrize("payload",[b"<html><body>not a feed</body></html>",
    b'<!DOCTYPE rss [<!ENTITY x SYSTEM "file:///private">]><rss><channel><item><title>&x;</title></item></channel></rss>'])
def test_nonfeed_or_external_xml_entities_fail_closed(feed,payload):
    from core.intelligence.sources import execute_source
    from defusedxml.common import DefusedXmlException
    feed["payload"]=payload
    with pytest.raises((ValueError,DefusedXmlException)):
        execute_source("rss-atom-feeds",action="query",query="https://example.com/feed")
