"""Public export preserves capabilities without copying unresolved example secrets."""
import hashlib
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('wise_public_prepare', Path(__file__).parents[2] / 'tools/prepare_public_update.py')
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)


@pytest.mark.parametrize('source', [b'password: "SYNTHETIC_ONLY_VALUE",\r\n',
    b'COMFYUI_TOKEN=SYNTHETIC_ONLY_VALUE\n', b'curl -d "password=SYNTHETIC_ONLY_VALUE&name=example"\n'])
def test_only_reviewed_literal_is_replaced(source):
    name = 'Supergent--main/skills/example/SKILL.md'
    review = {name: {'sha256': hashlib.sha256(source).hexdigest(), 'lines': [1]}}
    result = publisher.redact_reviewed_examples(name, source, review)
    assert result == source.replace(b'SYNTHETIC_ONLY_VALUE', b'YOUR_OWN_SECRET')
    assert source.endswith(b'\n')


@pytest.mark.parametrize('change', ['hash', 'executable', 'line', 'ambiguous'])
def test_unreviewed_export_cannot_guess_a_rewrite(change):
    source = b'password="SYNTHETIC_ONLY_VALUE"\n'
    name = 'example.md'
    entry = {'sha256': hashlib.sha256(source).hexdigest(), 'lines': [1]}
    if change == 'hash':
        entry['sha256'] = '0' * 64
    elif change == 'executable':
        name = 'example.py'
    elif change == 'line':
        entry['lines'] = [2]
    else:
        source = b'password="SYNTHETIC_ONLY_VALUE" token="ANOTHER_SYNTHETIC_VALUE"\n'
        entry['sha256'] = hashlib.sha256(source).hexdigest()
    with pytest.raises(ValueError):
        publisher.redact_reviewed_examples(name, source, {name: entry})


def test_unlisted_source_is_not_rewritten():
    source = b'Ordinary source with no credentials\n'
    assert publisher.redact_reviewed_examples('example.md', source, {}) == source


def test_reviewed_multiline_assignment_preserves_line_structure():
    source = b'API_TOKEN:\r\n  "SYNTHETIC_ONLY_VALUE"\r\n'
    review = {'example.md': {'sha256': hashlib.sha256(source).hexdigest(), 'lines': [1], 'spans': [[1, 2]]}}
    assert publisher.redact_reviewed_examples('example.md', source, review) == source.replace(
        b'SYNTHETIC_ONLY_VALUE', b'YOUR_OWN_SECRET')


def test_reviewed_header_example_does_not_rewrite_urls_or_content_type():
    source = (b'curl https://docs.example/read\n\n\n'
              b'curl https://docs.example/write\nContent-Type: application/json\n'
              b'X-API-Key: SYNTHETIC_ONLY_VALUE\n')
    review = {'example.md': {'sha256': hashlib.sha256(source).hexdigest(), 'lines': [1], 'spans': [[1, 6]]}}
    assert publisher.redact_reviewed_examples('example.md', source, review) == source.replace(
        b'SYNTHETIC_ONLY_VALUE', b'YOUR_OWN_SECRET')


def test_reviewed_encoded_auth_also_removes_plaintext_explanatory_comment():
    source = b'BASIC_AUTH="SYNTHETIC_ONLY_VALUE" # synthetic-user:NOT_A_REAL_PASSWORD\r\n'
    review = {'example.md': {'sha256': hashlib.sha256(source).hexdigest(), 'lines': [1]}}
    result = publisher.redact_reviewed_examples('example.md', source, review)
    assert result == b'BASIC_AUTH="YOUR_OWN_SECRET" # Configure your own credentials.\r\n'


def test_market_seed_and_current_plugin_guide_are_published():
    assert publisher.collect.__globals__['allowed']('config/mcp_market_seed.json')
    assert 'docs/PLUGINS.md' in publisher.APP_DOCS
    assert 'docs/MCP_CONNECTION_MARKETPLACE.md' in publisher.APP_DOCS
    assert 'docs/PLUGINS_NANGO.md' not in publisher.APP_DOCS
    assert 'Supergent--main/core/integrations/service.py' not in publisher.REQUIRED
    assert 'Supergent--main/core/mcp/oauth.py' in publisher.REQUIRED
