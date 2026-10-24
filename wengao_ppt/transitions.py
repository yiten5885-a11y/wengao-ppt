"""wengao_ppt.transitions: PresentationML slide transitions module.

Provides unified slide transitions across entire decks adhering to
PresentationML schema and cross-software compatibility (PowerPoint & LibreOffice).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from pptx import Presentation
from pptx.oxml import parse_xml
from pptx.oxml.ns import nsdecls, qn

P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
TAG_TRANSITION = f"{{{P_NS}}}transition"
TAG_CSLD = f"{{{P_NS}}}cSld"
TAG_CLR_MAP_OVR = f"{{{P_NS}}}clrMapOvr"
TAG_TIMING = f"{{{P_NS}}}timing"
TAG_EXT_LST = f"{{{P_NS}}}extLst"

SUPPORTED_FAMILIES = {"fade", "wipe", "push", "cut"}
DEFAULT_FAMILY = "fade"
DEFAULT_DURATION_MS = 500


def _speed_from_duration(duration_ms: int) -> str:
    """Map duration in milliseconds to PresentationML spd attribute."""
    if duration_ms <= 300:
        return "fast"
    if duration_ms <= 750:
        return "med"
    return "slow"


def _build_transition_xml(
    family: str,
    speed: str,
    is_section: bool = False,
) -> str:
    """Generate ECMA-376 compliant XML for slide transition."""
    family = family.lower()
    if family == "fade":
        if is_section:
            child = '<p:fade thruBlk="1"/>'
        else:
            child = "<p:fade/>"
    elif family == "wipe":
        direction = "d" if is_section else "r"
        child = f'<p:wipe dir="{direction}"/>'
    elif family == "push":
        direction = "u" if is_section else "l"
        child = f'<p:push dir="{direction}"/>'
    elif family == "cut":
        child = "<p:cut/>"
    else:
        child = "<p:fade/>"

    return f'<p:transition {nsdecls("p")} spd="{speed}" advClick="1">{child}</p:transition>'


def _insert_or_replace_transition(slide_element, transition_xml: str) -> None:
    """Insert or replace p:transition ensuring ECMA-376 child element order.

    Order in p:sld:
    cSld -> clrMapOvr? -> transition? -> timing? -> extLst?
    """
    new_trans_elem = parse_xml(transition_xml)

    # 1. Look for existing transition element
    existing_trans = None
    for child in slide_element:
        if child.tag == TAG_TRANSITION:
            existing_trans = child
            break

    if existing_trans is not None:
        # Replace existing in-place
        slide_element.replace(existing_trans, new_trans_elem)
        return

    # 2. Determine insertion index adhering to PresentationML sequence
    insert_idx = len(slide_element)
    for idx, child in enumerate(slide_element):
        if child.tag in (TAG_TIMING, TAG_EXT_LST):
            insert_idx = idx
            break

    slide_element.insert(insert_idx, new_trans_elem)


def _remove_transition(slide_element) -> bool:
    """Remove existing p:transition if present."""
    removed = False
    for child in list(slide_element):
        if child.tag == TAG_TRANSITION:
            slide_element.remove(child)
            removed = True
    return removed


def apply_transitions(
    pptx_in: Union[str, Path],
    pptx_out: Union[str, Path],
    policy: Optional[Dict[str, Any]] = None,
    roles: Optional[Dict[Union[int, str], str]] = None,
) -> Dict[str, Any]:
    """Apply unified slide transitions to a presentation.

    Args:
        pptx_in: Source PPTX path.
        pptx_out: Target PPTX path.
        policy: Transition policy dict with keys:
            - enabled: bool (default True)
            - family: str (default 'fade')
            - duration_ms: int (default 500)
        roles: Mapping of slide index (1-based or 0-based integer) to role name
            (e.g., 'cover', 'section', 'content', 'closing').

    Returns:
        TransitionReport dictionary.
    """
    in_path = Path(pptx_in)
    out_path = Path(pptx_out)

    if not in_path.is_file():
        raise FileNotFoundError(f"Input presentation not found: {in_path}")

    policy = policy or {}
    enabled = policy.get("enabled", True)
    family = policy.get("family", DEFAULT_FAMILY).lower()
    if family not in SUPPORTED_FAMILIES:
        family = DEFAULT_FAMILY
    duration_ms = int(policy.get("duration_ms", DEFAULT_DURATION_MS))
    speed = _speed_from_duration(duration_ms)

    # Normalize roles mapping to 1-based index keys
    norm_roles: Dict[int, str] = {}
    if roles:
        for k, v in roles.items():
            try:
                ik = int(k)
                norm_roles[ik] = str(v).lower()
            except (ValueError, TypeError):
                continue

    prs = Presentation(str(in_path))
    total_slides = len(prs.slides)
    transitions_applied = 0
    transitions_removed = 0
    details: List[Dict[str, Any]] = []

    for idx, slide in enumerate(prs.slides, start=1):
        role = norm_roles.get(idx, norm_roles.get(idx - 1, "content"))
        is_section = role in ("section", "chapter", "part")

        if not enabled:
            was_removed = _remove_transition(slide._element)
            if was_removed:
                transitions_removed += 1
            details.append({
                "slide_index": idx,
                "role": role,
                "transition": "none",
                "removed": was_removed,
            })
        else:
            trans_xml = _build_transition_xml(family, speed, is_section=is_section)
            _insert_or_replace_transition(slide._element, trans_xml)
            transitions_applied += 1
            details.append({
                "slide_index": idx,
                "role": role,
                "family": family,
                "speed": speed,
                "duration_ms": duration_ms,
                "is_section_variant": is_section,
            })

    out_path.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(out_path))

    return {
        "schema": "rs.transition_report.v1",
        "pptx_in": str(in_path),
        "pptx_out": str(out_path),
        "enabled": enabled,
        "family": family if enabled else "none",
        "duration_ms": duration_ms if enabled else 0,
        "total_slides": total_slides,
        "transitions_applied": transitions_applied,
        "transitions_removed": transitions_removed,
        "details": details,
    }


def check_transitions(
    pptx_path: Union[str, Path],
    require_enabled: bool = True,
    expected_family: Optional[str] = None,
) -> Dict[str, Any]:
    """Inspect and verify transitions on each slide of a presentation.

    Args:
        pptx_path: Path to the presentation to check.
        require_enabled: If True, all slides must have a valid transition.
        expected_family: If specified, transitions must match this family.

    Returns:
        CheckReport dict with status ('PASS' or 'FAIL') and detailed findings.
    """
    path = Path(pptx_path)
    if not path.is_file():
        return {
            "schema": "rs.check_report.v1",
            "module": "transitions",
            "status": "FAIL",
            "errors": [
                {
                    "code": "TRANSITION.INSPECTION_FAILED",
                    "detail": f"File not found: {path}",
                }
            ],
            "total_slides": 0,
            "slides_with_transition": 0,
            "slides_missing_transition": 0,
            "details": [],
        }

    prs = Presentation(str(path))
    total_slides = len(prs.slides)
    slides_with_transition = 0
    slides_missing_transition = 0
    errors: List[Dict[str, Any]] = []
    details: List[Dict[str, Any]] = []

    for idx, slide in enumerate(prs.slides, start=1):
        trans_elem = None
        for child in slide._element:
            if child.tag == TAG_TRANSITION:
                trans_elem = child
                break

        if trans_elem is not None:
            slides_with_transition += 1
            # Inspect child elements for transition type
            child_tags = [c.tag.split("}")[-1] for c in trans_elem]
            spd = trans_elem.get("spd", "med")
            adv_click = trans_elem.get("advClick", "1")
            found_family = child_tags[0] if child_tags else "unknown"

            # Validate child order in slide
            child_sequence = [c.tag.split("}")[-1] for c in slide._element]
            trans_pos = child_sequence.index("transition")
            invalid_order = False
            for prev_idx in range(trans_pos):
                if child_sequence[prev_idx] in ("timing", "extLst"):
                    invalid_order = True
            for post_idx in range(trans_pos + 1, len(child_sequence)):
                if child_sequence[post_idx] in ("cSld", "clrMapOvr"):
                    invalid_order = True

            if invalid_order:
                errors.append({
                    "code": "TRANSITION.XML_STRUCTURE_INVALID",
                    "slide_index": idx,
                    "detail": f"Invalid element sequence around transition: {child_sequence}",
                })

            if expected_family and found_family != expected_family.lower():
                errors.append({
                    "code": "TRANSITION.INVALID_FAMILY",
                    "slide_index": idx,
                    "detail": f"Expected family '{expected_family}' but found '{found_family}'",
                })

            details.append({
                "slide_index": idx,
                "has_transition": True,
                "family": found_family,
                "spd": spd,
                "adv_click": adv_click,
            })
        else:
            slides_missing_transition += 1
            if require_enabled:
                errors.append({
                    "code": "TRANSITION.MISSING_TRANSITION",
                    "slide_index": idx,
                    "detail": f"Slide {idx} is missing required p:transition element",
                })
            details.append({
                "slide_index": idx,
                "has_transition": False,
            })

    status = "PASS" if not errors else "FAIL"

    return {
        "schema": "rs.check_report.v1",
        "module": "transitions",
        "status": status,
        "errors": errors,
        "total_slides": total_slides,
        "slides_with_transition": slides_with_transition,
        "slides_missing_transition": slides_missing_transition,
        "details": details,
    }
