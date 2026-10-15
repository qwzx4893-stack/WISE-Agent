"""Offline contracts for rendering desktop telemetry in the WISE settings UI."""

from __future__ import annotations

from html.parser import HTMLParser
import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "ui" / "wise_web" / "app.js"


class _FirstSmallText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._inside = False
        self._finished = False
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "small" and not self._finished:
            self._inside = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "small" and self._inside:
            self._inside = False
            self._finished = True

    def handle_data(self, data: str) -> None:
        if self._inside:
            self.parts.append(data)


def _render_health_settings(titles: list[str | None]) -> list[str]:
    node = shutil.which("node")
    assert node is not None, "Node.js is required to verify the real WISE JavaScript renderer"
    script = r"""
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync(process.argv[1], "utf8");
const end = source.indexOf("\nfunction modelProvidersTemplate(");
if (end < 0) throw new Error("settingsTemplate boundary not found");
const context = {
  document: {documentElement: {lang: "en"}, addEventListener() {}},
  console,
};
vm.createContext(context);
vm.runInContext(source.slice(0, end), context, {filename: process.argv[1]});
const outputs = [];
for (const value of JSON.parse(process.argv[2])) {
  context.inputTitle = value;
  outputs.push(vm.runInContext(
    'state.telemetry = {active_window: inputTitle}; settingsTemplate("health")',
    context,
  ));
}
process.stdout.write(JSON.stringify(outputs));
"""
    completed = subprocess.run(
        [node, "-e", script, str(APP_JS), json.dumps(titles)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Meeting notes", "Meeting notes"),
        ("اجتماع الفريق", "اجتماع الفريق"),
        ('<meeting> & "notes"', '<meeting> & "notes"'),
        (None, "Desktop"),
    ],
)
def test_health_settings_renders_foreground_title_as_literal_text(title, expected):
    rendered = _render_health_settings([title])[0]
    parser = _FirstSmallText()
    parser.feed(rendered)
    assert "".join(parser.parts) == expected


def test_health_settings_encodes_special_characters_in_html_source():
    rendered = _render_health_settings(['<meeting> & "notes"'])[0]
    assert '<small>&lt;meeting&gt; &amp; &quot;notes&quot;</small>' in rendered
    assert '<small><meeting> & "notes"</small>' not in rendered
