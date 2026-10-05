from core.optimization.context_compressor import ContextCompressor


def _all_text(messages):
    return "\n".join(message.get("content", "") for message in messages)


def test_preserves_old_tool_facts_and_consent_before_windowing():
    messages = [
        {"role": "system", "content": "Security policy: never delete files without consent."},
        {"role": "user", "content": "Prepare the release report."},
        {"role": "assistant", "content": "I will inspect the repository."},
        {"role": "tool", "content": "Observation: artifact=/workspace/release.md; tests=142 passed"},
        {"role": "user", "content": "I confirm publishing only the report, never the secret key."},
        {"role": "assistant", "content": "Acknowledged."},
        {"role": "tool", "content": "Observation: changelog reviewed and no blocker found"},
        {"role": "assistant", "content": "Final Answer: ready"},
    ]
    compressor = ContextCompressor(max_tokens=110, keep_recent_turns=2)
    result = compressor.compress_with_report(messages)
    text = _all_text(result.messages)

    assert "never delete files without consent" in text
    assert "Prepare the release report" in text
    assert "artifact=/workspace/release.md" in text
    assert "never the secret key" in text
    assert result.report.summarised > 0
    assert result.report.retained_facts >= 2


def test_observations_are_not_treated_as_noise_and_duplicate_tool_output_is_collapsed():
    messages = [
        {"role": "system", "content": "Keep evidence."},
        {"role": "user", "content": "Find the version."},
        {"role": "tool", "content": "Observation: version=9.4.1"},
        {"role": "tool", "content": "Observation: version=9.4.1"},
    ]
    result = ContextCompressor(max_tokens=500).compress_with_report(messages)
    text = _all_text(result.messages)

    assert "Observation: version=9.4.1" in text
    assert text.count("Observation: version=9.4.1") == 1
    assert result.report.deduplicated == 1


def test_default_path_uses_no_llm_summary_call():
    calls = []

    def summariser(_messages):
        calls.append(True)
        return "external summary"

    messages = [{"role": "system", "content": "policy"}, {"role": "user", "content": "goal"}]
    messages.extend({"role": "assistant", "content": f"old fact {index}"} for index in range(12))
    result = ContextCompressor(max_tokens=140, keep_recent_turns=2).compress_with_report(messages)

    assert not calls
    assert "old fact 0" in _all_text(result.messages)
