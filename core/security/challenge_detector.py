# ==============================================================================
# WISE Security Subsystem — Multi-Modal Challenge Detector
# Detects CAPTCHA, MFA, OTP, Cloudflare Turnstile, and authentication barriers
# across UIA, browser DOM/ARIA, Windows Media OCR, and VLM perception.
#
# Fundamental Invariant:
# Perception evidence (including VLM) informs challenge detection,
# but NEVER grants authorization. SecurityGate remains sovereign.
# Zero singletons: Fully instantiable and injectable.
# ==============================================================================

from __future__ import annotations

import re
import logging
from typing import Dict, Any, List, Optional, Tuple, Union
from dataclasses import dataclass, field
from enum import Enum

from core.context.world_state import WISEWorldState

LOG = logging.getLogger("WISE.Security.ChallengeDetector")


class ChallengeType(str, Enum):
    CAPTCHA = "CAPTCHA"
    MFA = "MFA"
    OTP = "OTP"
    LOGIN = "LOGIN"
    CREDENTIAL_ENTRY = "CREDENTIAL_ENTRY"
    SECURITY_CHALLENGE = "SECURITY_CHALLENGE"
    UAC_ELEVATION = "UAC_ELEVATION"


@dataclass
class ChallengeDetectionResult:
    """
    Structured outcome of multi-modal challenge inspection.
    """
    detected: bool = False
    challenge_type: Optional[ChallengeType] = None
    confidence: float = 0.0
    evidence: List[str] = field(default_factory=list)
    primary_source: str = "none"  # "DOM", "UIA", "OCR", "VLM", "WORLD_STATE"
    requires_human: bool = False
    reason: str = ""
    prompt_to_user: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "detected": self.detected,
            "challenge_type": self.challenge_type.value if self.challenge_type else None,
            "confidence": round(self.confidence, 3),
            "evidence": list(self.evidence),
            "primary_source": self.primary_source,
            "requires_human": self.requires_human,
            "reason": self.reason,
            "prompt_to_user": self.prompt_to_user,
        }


# Known CAPTCHA widget indicators (attributes, class names, iframe sources)
_CAPTCHA_DOM_INDICATORS = [
    "recaptcha", "g-recaptcha", "hcaptcha", "h-captcha", "turnstile", "cf-turnstile",
    "arkose", "funcaptcha", "geetest", "botdetect", "challenge-form", "challenge-running",
    "cf-browser-verification", "cloudflare",
]

# MFA & OTP DOM input attributes and hints
_MFA_DOM_INDICATORS = [
    "one-time-code", "totp", "otp", "2fa", "mfa", "verification_code",
    "passcode", "security-code", "auth-code", "two-step",
]

# Text phrases indicating human verification challenges across OCR / DOM / VLM
_CAPTCHA_TEXT_PATTERNS = [
    re.compile(r"(?i)\b(?:verify\s+you\s+are\s+human|i['’]?m\s+not\s+a\s+robot|select\s+all\s+images\s+with)\b"),
    re.compile(r"(?i)\b(?:enter\s+the\s+characters\s+you\s+see|security\s+check\s+to\s+continue|bot\s+detection)\b"),
    re.compile(r"(?i)\b(?:cloud\s*flare|turnstile|arkose\s*labs|hcaptcha|recaptcha)\b"),
    re.compile(r"(?:يرجى إثبات أنك لست روبوت|أنا لست برنامج روبوت|التحقق الأمني|اختبار الأمان|اختر جميع الصور)"),
]

# Text phrases indicating MFA / OTP challenges
_MFA_TEXT_PATTERNS = [
    re.compile(r"(?i)\b(?:two[- ]step\s+verification|multi[- ]factor\s+authentication|enter\s+verification\s+code)\b"),
    re.compile(r"(?i)\b(?:enter\s+the\s+(?:6|4|8)[- ]digit\s+code|authenticator\s+app|sms\s+code\s+sent\s+to)\b"),
    re.compile(r"(?i)\b(?:one[- ]time\s+passcode|enter\s+otp|approve\s+sign[- ]in\s+request)\b"),
    re.compile(r"(?:التحقق بخطوتين|أدخل رمز التحقق|تم إرسال رمز إلى هاتفك|رمز الأمان لمرة واحدة|رمز المصادقة)"),
]

# Windows Security / CredUI dialog titles
_WINDOWS_SECURITY_TITLES = [
    "windows security", "security alert", "credential prompt", "user account control",
    "أمان windows", "تنبيه أمان", "مطالبة ببيانات الاعتماد", "التحكم في حساب المستخدم",
]


class ChallengeDetector:
    """
    Multi-modal Challenge Detection Engine.
    Inspects DOM/ARIA accessibility snapshots, UIA control trees, native Windows OCR,
    and visual VLM perception to detect human verification barriers.
    """

    def __init__(self) -> None:
        pass

    def detect_from_browser_elements(
        self,
        elements: List[Any],
        aria_text: str = "",
        page_url: str = "",
    ) -> ChallengeDetectionResult:
        """
        Inspects browser DOM element references and ARIA snapshot text.
        """
        evidence: List[str] = []
        confidence = 0.0
        detected_type: Optional[ChallengeType] = None

        # 1. Inspect DOM Element references (role, name, ref_id)
        for el in elements:
            role = getattr(el, "role", "").lower()
            name = getattr(el, "name", "").lower()
            ref_id = getattr(el, "ref_id", "").lower()
            combined = f"{role} {name} {ref_id}"

            # Check CAPTCHA widgets
            for ind in _CAPTCHA_DOM_INDICATORS:
                if ind in combined:
                    evidence.append(f"DOM element matched CAPTCHA marker '{ind}' ({role}: {name})")
                    confidence = max(confidence, 0.90)
                    detected_type = ChallengeType.CAPTCHA

            # Check MFA / OTP fields
            for ind in _MFA_DOM_INDICATORS:
                if ind in combined:
                    evidence.append(f"DOM input field matched MFA marker '{ind}' ({role}: {name})")
                    confidence = max(confidence, 0.85)
                    if detected_type != ChallengeType.CAPTCHA:
                        detected_type = ChallengeType.OTP

        # 2. Inspect ARIA Snapshot text with multi-lingual patterns
        if aria_text:
            for pattern in _CAPTCHA_TEXT_PATTERNS:
                m = pattern.search(aria_text)
                if m:
                    evidence.append(f"ARIA text matched CAPTCHA phrase: '{m.group(0)}'")
                    confidence = max(confidence, 0.95)
                    detected_type = ChallengeType.CAPTCHA

            for pattern in _MFA_TEXT_PATTERNS:
                m = pattern.search(aria_text)
                if m:
                    evidence.append(f"ARIA text matched MFA/OTP phrase: '{m.group(0)}'")
                    confidence = max(confidence, 0.90)
                    if detected_type != ChallengeType.CAPTCHA:
                        detected_type = ChallengeType.MFA

        # 3. Check URL hints (e.g. challenge, recaptcha, turnstile in URL query/path)
        if page_url and any(ind in page_url.lower() for ind in ("challenge", "recaptcha", "turnstile", "mfa", "2fa")):
            evidence.append(f"Page URL contains challenge path: '{page_url[:80]}'")
            confidence = max(confidence, 0.75)

        if evidence and confidence >= 0.70:
            ctype = detected_type or ChallengeType.SECURITY_CHALLENGE
            prompt = self._format_user_prompt(ctype, evidence)
            return ChallengeDetectionResult(
                detected=True,
                challenge_type=ctype,
                confidence=confidence,
                evidence=evidence,
                primary_source="DOM",
                requires_human=True,
                reason=f"Browser verification challenge detected ({ctype.value})",
                prompt_to_user=prompt,
            )

        return ChallengeDetectionResult(detected=False)

    def detect_from_uia(
        self,
        ui_tree: Any,
        window_title: str = "",
        class_name: str = "",
    ) -> ChallengeDetectionResult:
        """
        Inspects Windows native UIA tree and dialog attributes.
        """
        evidence: List[str] = []
        confidence = 0.0
        detected_type: Optional[ChallengeType] = None

        title_lower = window_title.lower()
        if any(sec_t in title_lower for sec_t in _WINDOWS_SECURITY_TITLES):
            evidence.append(f"Native Window Title matches security credential challenge: '{window_title}'")
            confidence = 0.95
            detected_type = ChallengeType.CREDENTIAL_ENTRY

        if class_name in ("CredUI", "#32770") and any(w in title_lower for w in ("security", "credentials", "أمان", "كلمة المرور")):
            evidence.append(f"Security dialog class '{class_name}' with title '{window_title}'")
            confidence = 0.95
            detected_type = ChallengeType.CREDENTIAL_ENTRY

        # Inspect UIA elements if provided
        if ui_tree:
            children = getattr(ui_tree, "children", []) or []
            for child in children:
                c_name = getattr(child, "name", "").lower()
                c_role = getattr(child, "control_type", "").lower()

                for pattern in _CAPTCHA_TEXT_PATTERNS:
                    if pattern.search(c_name):
                        evidence.append(f"UIA node '{c_name}' matched CAPTCHA pattern")
                        confidence = max(confidence, 0.90)
                        detected_type = ChallengeType.CAPTCHA

                for pattern in _MFA_TEXT_PATTERNS:
                    if pattern.search(c_name):
                        evidence.append(f"UIA node '{c_name}' matched MFA pattern")
                        confidence = max(confidence, 0.85)
                        detected_type = ChallengeType.MFA

        if evidence and confidence >= 0.70:
            ctype = detected_type or ChallengeType.SECURITY_CHALLENGE
            prompt = self._format_user_prompt(ctype, evidence)
            return ChallengeDetectionResult(
                detected=True,
                challenge_type=ctype,
                confidence=confidence,
                evidence=evidence,
                primary_source="UIA",
                requires_human=True,
                reason=f"Windows security barrier detected ({ctype.value})",
                prompt_to_user=prompt,
            )

        return ChallengeDetectionResult(detected=False)

    def detect_from_ocr(self, ocr_text: str) -> ChallengeDetectionResult:
        """
        Inspects native Windows Media OCR text for challenge indicators.
        """
        if not ocr_text:
            return ChallengeDetectionResult(detected=False)

        evidence: List[str] = []
        confidence = 0.0
        detected_type: Optional[ChallengeType] = None

        for pattern in _CAPTCHA_TEXT_PATTERNS:
            m = pattern.search(ocr_text)
            if m:
                evidence.append(f"OCR visual text identified CAPTCHA phrase: '{m.group(0)}'")
                confidence = max(confidence, 0.85)
                detected_type = ChallengeType.CAPTCHA

        for pattern in _MFA_TEXT_PATTERNS:
            m = pattern.search(ocr_text)
            if m:
                evidence.append(f"OCR visual text identified MFA/OTP phrase: '{m.group(0)}'")
                confidence = max(confidence, 0.85)
                if detected_type != ChallengeType.CAPTCHA:
                    detected_type = ChallengeType.MFA

        if evidence and confidence >= 0.70:
            ctype = detected_type or ChallengeType.SECURITY_CHALLENGE
            prompt = self._format_user_prompt(ctype, evidence)
            return ChallengeDetectionResult(
                detected=True,
                challenge_type=ctype,
                confidence=confidence,
                evidence=evidence,
                primary_source="OCR",
                requires_human=True,
                reason=f"Visual OCR verification challenge detected ({ctype.value})",
                prompt_to_user=prompt,
            )

        return ChallengeDetectionResult(detected=False)

    def detect_from_vlm_observation(self, vlm_observation: Any) -> ChallengeDetectionResult:
        """
        Extracts challenge evidence from a Level 4 VLM visual perception result.
        NOTE: VLM output serves strictly as observational EVIDENCE, never authorization.
        """
        if not vlm_observation:
            return ChallengeDetectionResult(detected=False)

        evidence: List[str] = []
        confidence = 0.0
        detected_type: Optional[ChallengeType] = None

        # Check visual targets / detected elements
        elements = getattr(vlm_observation, "elements", []) or []
        for el in elements:
            name = getattr(el, "name", "").lower()
            category = getattr(el, "category", "").lower()
            if any(term in name for term in ("captcha", "robot", "turnstile", "hcaptcha")):
                evidence.append(f"VLM visual element detected: '{el.name}' ({category})")
                confidence = max(confidence, 0.80)
                detected_type = ChallengeType.CAPTCHA
            elif any(term in name for term in ("mfa", "otp", "verification code", "authenticator")):
                evidence.append(f"VLM visual element detected: '{el.name}' ({category})")
                confidence = max(confidence, 0.80)
                detected_type = ChallengeType.MFA

        # Check active dialog summary from VLM
        dialog_title = getattr(vlm_observation, "active_modal_dialog", "") or ""
        if dialog_title:
            for pattern in _CAPTCHA_TEXT_PATTERNS:
                if pattern.search(dialog_title):
                    evidence.append(f"VLM modal dialog matched CAPTCHA: '{dialog_title}'")
                    confidence = max(confidence, 0.85)
                    detected_type = ChallengeType.CAPTCHA

        if evidence and confidence >= 0.70:
            ctype = detected_type or ChallengeType.SECURITY_CHALLENGE
            prompt = self._format_user_prompt(ctype, evidence)
            return ChallengeDetectionResult(
                detected=True,
                challenge_type=ctype,
                confidence=confidence,
                evidence=evidence,
                primary_source="VLM",
                requires_human=True,
                reason=f"Visual VLM challenge detected ({ctype.value})",
                prompt_to_user=prompt,
            )

        return ChallengeDetectionResult(detected=False)

    def evaluate_world_state(self, world_state: WISEWorldState) -> ChallengeDetectionResult:
        """
        Multi-modal synthesis across unified world state facts.
        """
        if not world_state:
            return ChallengeDetectionResult(detected=False)

        # 1. Check UIA / Active Window
        active_title = world_state.active_window.title if world_state.active_window else ""
        active_class = world_state.active_window.class_name if world_state.active_window else ""
        uia_res = self.detect_from_uia(world_state.ui_tree, active_title, active_class)
        if uia_res.detected:
            return uia_res

        # 2. Check Open Dialogs
        for diag in world_state.open_dialogs:
            d_res = self.detect_from_uia(None, diag.title, diag.class_name)
            if d_res.detected:
                return d_res

        # 3. Check OCR Preview
        if world_state.visual_state and world_state.visual_state.ocr_text_preview:
            ocr_res = self.detect_from_ocr(world_state.visual_state.ocr_text_preview)
            if ocr_res.detected:
                return ocr_res

        return ChallengeDetectionResult(detected=False)

    def _format_user_prompt(self, ctype: ChallengeType, evidence: List[str]) -> str:
        if ctype == ChallengeType.CAPTCHA:
            return "Human verification challenge (CAPTCHA / Bot Protection) detected. Please solve the challenge in the window to allow WISE to safely resume."
        elif ctype in (ChallengeType.MFA, ChallengeType.OTP):
            return "Multi-Factor Authentication (MFA/OTP) required. Please enter the verification code or approve the authentication request to continue."
        elif ctype == ChallengeType.CREDENTIAL_ENTRY:
            return "Sensitive credential entry prompt detected. Please enter your credentials directly into the secure system dialog."
        else:
            return "Security verification challenge detected. User intervention is required to complete this step safely."
