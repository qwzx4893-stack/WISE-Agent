"""Reviewed inventory: software, sources, models and deployed platforms differ.

Entries originate from the user's document. The document never supplies executable
commands. Adapter bindings below are trusted code; missing bindings stay explicit.
"""
from __future__ import annotations
import importlib.util
import json
import shutil
from functools import lru_cache
from pathlib import Path

SOURCE_IDS = set("""openstreetmap overpass-turbo overpass-api wayback-machine common-crawl wikidata
google-dataset-search gdelt event-registry media-cloud occrp-aleph opencorporates opensanctions
world-bank-open-data data-gov academic-torrents tfl-open-data-api osint-framework
bellingcat-online-investigation-toolkit osint4all urlscan-io bgpview copernicus-data-space-ecosystem
have-i-been-pwned hibp-domain-search hibp-api hibp-pwned-passwords
cisa-known-exploited-vulnerabilities cisa-cyber-hygiene-services nvd cert-cc-vulnerability-notes
mitre-att-ck misp-galaxies-and-feeds abuse-ch urlhaus malwarebazaar feodo-tracker spamhaus-drop-edrop
phishtank openphish-community alienvault-otx circl-misp-feeds first-epss github-secret-scanning-guidance
meta-content-library-api telemetr-telemetr-io gdelt-social-media-in-the-news
osome-observatory-on-social-media mediawell-dataset-library rss-atom-feeds drivenlisten-com gdelt-project""".split())
# The public Scorecard REST binding reads already published results. The
# upstream CLI scanner remains a separate, unimplemented capability.
SOURCE_IDS.add("openssf-scorecard")
MODEL_IDS = set("opencv-zoo yunet sface mobilefacenet-tf".split())
ALIASES = {"gdelt-project":"gdelt"}
TOOL_IDS = set("""streamlink ffmpeg opencv onnx-runtime tesseract-ocr exiftool invid-weverify forensically
scrapy playwright selenium whisper-cpp vosk piper apache-tika ocrmypdf imagemagick sqlite duckdb recoll
maltego-ce spiderfoot intelo datasploit openrefine gephi amass theharvester subfinder httpx nmap
owasp-zap projectdiscovery-nuclei zeek suricata wireshark yara sigma stix-taxii autopsy the-sleuth-kit
plaso velociraptor grr-rapid-response ghidra radare2 cutter jadx semgrep cyberchef qgis gdal
restic wireguard psutil openssf-scorecard gitleaks trufflehog detect-secrets leakwatch bandit""".split())
URLS = {
    "openstreetmap":"https://www.openstreetmap.org", "overpass-turbo":"https://overpass-turbo.eu",
    "overpass-api":"https://wiki.openstreetmap.org/wiki/Overpass_API", "wayback-machine":"https://web.archive.org",
    "common-crawl":"https://commoncrawl.org", "wikidata":"https://www.wikidata.org",
    "google-dataset-search":"https://datasetsearch.research.google.com", "gdelt":"https://www.gdeltproject.org",
    "event-registry":"https://eventregistry.org", "media-cloud":"https://www.mediacloud.org",
    "occrp-aleph":"https://aleph.occrp.org", "opencorporates":"https://opencorporates.com",
    "opensanctions":"https://www.opensanctions.org", "world-bank-open-data":"https://data.worldbank.org",
    "data-gov":"https://data.gov", "academic-torrents":"https://academictorrents.com",
    "tfl-open-data-api":"https://api.tfl.gov.uk", "streamlink":"https://streamlink.github.io",
    "ffmpeg":"https://ffmpeg.org", "opencv":"https://opencv.org", "opencv-zoo":"https://github.com/opencv/opencv_zoo",
    "yunet":"https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet",
    "sface":"https://github.com/opencv/opencv_zoo/tree/main/models/face_recognition_sface",
    "onnx-runtime":"https://onnxruntime.ai", "tesseract-ocr":"https://github.com/tesseract-ocr/tesseract",
    "exiftool":"https://exiftool.org", "invid-weverify":"https://www.invid-project.eu/tools-and-services/invid-verification-plugin/",
    "forensically":"https://29a.ch/photo-forensics/", "scrapy":"https://scrapy.org",
    "playwright":"https://playwright.dev", "selenium":"https://www.selenium.dev", "apache-nutch":"https://nutch.apache.org",
    "heritrix":"https://github.com/internetarchive/heritrix3", "changedetection-io":"https://github.com/dgtlmoon/changedetection.io",
    "whisper-cpp":"https://github.com/ggml-org/whisper.cpp", "vosk":"https://alphacephei.com/vosk/",
    "piper":"https://github.com/OHF-Voice/piper1-gpl", "apache-tika":"https://tika.apache.org",
    "ocrmypdf":"https://ocrmypdf.readthedocs.io", "imagemagick":"https://imagemagick.org",
    "qdrant":"https://qdrant.tech", "milvus":"https://milvus.io", "chroma":"https://www.trychroma.com",
    "sqlite":"https://sqlite.org", "duckdb":"https://duckdb.org", "opensearch":"https://opensearch.org",
    "minio":"https://github.com/minio/minio", "openmetadata":"https://open-metadata.org",
    "paperless-ngx":"https://docs.paperless-ngx.com", "recoll":"https://www.recoll.org",
    "ollama":"https://ollama.com", "llama-cpp":"https://github.com/ggml-org/llama.cpp",
    "open-webui":"https://github.com/open-webui/open-webui", "langgraph":"https://github.com/langchain-ai/langgraph",
    "autogen":"https://github.com/microsoft/autogen", "crewai":"https://github.com/crewAIInc/crewAI",
    "letta":"https://github.com/letta-ai/letta", "openhands":"https://github.com/OpenHands/OpenHands",
    "aider":"https://aider.chat", "n8n":"https://n8n.io", "node-red":"https://nodered.org",
    "apache-airflow":"https://airflow.apache.org", "temporal":"https://temporal.io",
    "home-assistant":"https://www.home-assistant.io", "cron-systemd-timers":"https://www.freedesktop.org/software/systemd/man/latest/systemd.timer.html",
    "docker-engine":"https://docs.docker.com/engine/", "podman":"https://podman.io", "firejail":"https://firejail.wordpress.com",
    "gvisor":"https://gvisor.dev", "openbao":"https://openbao.org", "open-policy-agent":"https://www.openpolicyagent.org",
    "keycloak":"https://www.keycloak.org", "authelia":"https://www.authelia.com", "restic":"https://restic.net",
    "opentelemetry":"https://opentelemetry.io", "auditd":"https://github.com/linux-audit/audit-userspace",
    "prometheus":"https://prometheus.io", "grafana":"https://grafana.com/oss/grafana/", "netdata":"https://www.netdata.cloud",
    "uptime-kuma":"https://github.com/louislam/uptime-kuma", "ntopng":"https://github.com/ntop/ntopng",
    "wireguard":"https://www.wireguard.com", "psutil":"https://github.com/giampaolo/psutil",
    "meta-content-library-api":"https://transparency.meta.com/researchtools/meta-content-library/",
    "telemetr-telemetr-io":"https://telemetr.io", "gdelt-social-media-in-the-news":"https://www.gdeltproject.org",
    "osome-observatory-on-social-media":"https://osome.iu.edu", "mediawell-dataset-library":"https://mediawell.ssrc.org",
    "bandit":"https://bandit.readthedocs.io", "drivenlisten":"https://drivenlisten.com",
    "gdelt-project":"https://www.gdeltproject.org", "headscale":"https://headscale.net",
    "rss-atom-feeds":"https://www.rssboard.org/rss-specification",
}

# Public query adapters. Reading a landing page is deliberately a separate action.
QUERY_IDS = {"cisa-known-exploited-vulnerabilities", "nvd", "first-epss", "wikidata", "gdelt-project",
             "world-bank-open-data", "gdelt", "wayback-machine", "common-crawl", "urlscan-io", "rss-atom-feeds", "openssf-scorecard"}
BUILTIN_TOOLS = {"detect-secrets", "bandit", "sqlite", "psutil", "stix-taxii", "duckdb", "yara", "sigma"}
CLI_TOOLS = {"exiftool":"exiftool", "ffmpeg":"ffprobe", "tesseract-ocr":"tesseract",
             "yara":"yara", "radare2":"rabin2", "the-sleuth-kit":"fsstat",
             "gdal":"gdalinfo", "wireshark":"capinfos"}

@lru_cache(maxsize=1)
def entries():
    from core.paths import REPO_ROOT
    source = json.loads((REPO_ROOT/"config/intelligence_resources.json").read_text(encoding="utf-8"))["entries"]
    result = {row["id"]:dict(row) for row in source}
    result.setdefault("bandit", {"id":"bandit", "name":"Bandit", "group":"security", "description":"Offline Python security static analysis", "url":URLS["bandit"]})
    result.setdefault("headscale", {"id":"headscale", "name":"Headscale", "group":"monitoring", "description":"Self-hosted coordination service mentioned as the alternative in the source document", "url":URLS["headscale"]})
    for alias,canonical in ALIASES.items():
        if alias in result and canonical in result:
            result.pop(alias)
            result[canonical].setdefault("document_aliases",[]).append(alias)
    return result

def profile(identifier):
    identifier=ALIASES.get(identifier,identifier)
    row = dict(entries()[identifier])
    kind = "source" if identifier in SOURCE_IDS else "model" if identifier in MODEL_IDS else "tool" if identifier in TOOL_IDS else "platform"
    row.update(kind=kind, category=row["group"], url=URLS.get(identifier) or row.get("url", ""),
               available=False, status="ADAPTER_REQUIRED", access="License/terms must be checked for the actual deployment")
    if kind == "source":
        row.update(available=bool(row["url"]), status="PUBLIC_SOURCE_ADAPTER" if row["url"] else "SOURCE_URL_REQUIRED",
                   actions=["read"] + (["query"] if identifier in QUERY_IDS else []),
                   capability_id="source."+identifier, limitations="Public page reading only unless query is listed; no account/private data access")
        if identifier == "openssf-scorecard":
            row["limitations"] = ("Query published/precalculated public GitHub repository Scorecard results using owner/repo. "
                "Not a fresh scan, full CLI integration or security certification; include observed score date, "
                "commit and missing checks. No credentials, scan submission or private repository access.")
    elif identifier in BUILTIN_TOOLS or identifier in CLI_TOOLS:
        from core.security.optional_tools import RECIPES, OptionalToolManager
        if identifier in RECIPES:
            row.update(available=True, status="ON_DEMAND", actions=["inspect"], capability_id="intelligence."+identifier,
                       required_next_step="Install the reviewed isolated recipe when invoked; failure is reported, not treated as success",
                       limitations="Reviewed isolated package installed when invoked, then cleaned after idle TTL; bounded offline inspection only, not the full upstream product")
            if identifier == "stix-taxii": row["limitations"] += "; STIX parsing only, no TAXII transport"
            if identifier == "sigma": row["limitations"] = (
                "Bounded offline Sigma YAML parsing and condition validation with reviewed pySigma; "
                "workspace rules only, no collection/correlation rules, aliases, unknown modifiers, "
                "SIEM, event matching, conversion backends or external plugins. "
                "Syntax-valid is not detection-effective or full specification compliance; dependencies install on demand.")
            return row
        if identifier in {"detect-secrets", "bandit"}:
            available = importlib.util.find_spec("detect_secrets" if identifier == "detect-secrets" else "bandit") is not None
        elif identifier == "psutil": available = importlib.util.find_spec("psutil") is not None
        elif identifier in {"duckdb","yara","stix-taxii"}: available=importlib.util.find_spec({"duckdb":"duckdb","yara":"yara","stix-taxii":"stix2"}[identifier]) is not None
        elif identifier in CLI_TOOLS: available = bool(shutil.which(CLI_TOOLS[identifier]))
        else: available = True
        row.update(available=available, status="ADAPTER_READY" if available else "DEPENDENCY_REQUIRED",
                   actions=["inspect"], capability_id="intelligence."+identifier,
                   limitations="Bounded offline inspection of authorized workspace data only; not the complete upstream product")
        if identifier == "yara": row["limitations"]="Offline file matching with an authorized rules_path; rule includes disabled; no process scans or matched secret bytes returned"
        if identifier == "duckdb": row["limitations"]="Local CSV statistics and bounded sample rows or read-only DuckDB schema; no arbitrary SQL, extensions or remote files"
        if identifier == "stix-taxii": row["limitations"]="STIX 2 parsing/validation via stix2; TAXII network transport is not implemented"
    elif kind == "platform":
        row.update(status="DEPLOYMENT_AND_ADAPTER_REQUIRED", actions=[],
                   limitations="Requires configured deployment/API or MCP; another agent framework is not a WISE tool automatically")
    elif kind == "model":
        row.update(status="MODEL_AND_ADAPTER_REQUIRED", actions=[], limitations="Weights/license/hardware and task-specific adapter required; identity matching is not enabled by default")
    else:
        row.update(actions=[], limitations="Upstream CLI/API schema, installation and safety contract must be integrated and tested")
    row["required_next_step"] = "Execute listed actions; report errors honestly" if row["available"] else row["limitations"]
    return row

def inventory():
    return [profile(identifier) for identifier in entries()]
