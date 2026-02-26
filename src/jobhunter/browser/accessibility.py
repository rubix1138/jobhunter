"""Accessibility tree helpers for stable element detection.

Uses page.accessibility.snapshot() (the AX tree) to find elements by their
ARIA role and label rather than CSS class names, which change constantly with
LinkedIn's SDUI redesigns.
"""

import re
from typing import Optional

from patchright.async_api import Locator, Page

from ..utils.logging import get_logger

logger = get_logger(__name__)

# ── Field extraction helpers ───────────────────────────────────────────────────

_FILLER_ROLES = {"textbox", "combobox", "listbox", "checkbox", "radio", "spinbutton"}
_GROUP_ROLES = {"radiogroup", "group"}
_NAV_NAMES = {"next", "save", "back", "submit", "cancel", "previous", "continue"}


def _walk_fields(node: dict, out: list[str], pending_label: str = "") -> None:
    """Recursively collect fillable field descriptions from an AX tree node.

    Radio groups are emitted as a single "radiogroup" line with option names,
    rather than individual radio items — giving the LLM planner the full
    question context ("radiogroup 'Are you 18+?' options: Yes, No").

    When children are processed in order, a preceding text/heading node is
    passed as ``pending_label`` to the next sibling so that unnamed radiogroups
    can use the text node label (common Workday pattern).
    """
    role = (node.get("role") or "").lower()
    name = (node.get("name") or "").strip()

    if role in _GROUP_ROLES:
        children = node.get("children") or []
        child_roles = {(c.get("role") or "").lower() for c in children}
        if child_roles & {"radio", "checkbox"}:
            # Use the node's own name, then a pending sibling label, then a text child
            label = name
            if not label:
                label = pending_label
            if not label:
                # Look for a text/heading child inside this node
                for c in children:
                    c_role = (c.get("role") or "").lower()
                    c_name = (c.get("name") or "").strip()
                    if c_role in ("text", "statictext", "heading", "label") and c_name:
                        if c_name.lower() not in _NAV_NAMES:
                            label = c_name
                            break
            if label:
                options = [
                    (c.get("name") or "").strip()
                    for c in children
                    if (c.get("role") or "").lower() in {"radio", "checkbox"}
                    and (c.get("name") or "").strip()
                ]
                req = " (required)" if node.get("required") or any(
                    c.get("required") for c in children
                ) else ""
                opts_str = ", ".join(options[:6]) if options else ""
                out.append(f"radiogroup '{label}'{req} options: {opts_str}")
                return  # Don't recurse into children — already captured
        # Non-radio group — process children, passing sibling text labels forward
        next_label = ""
        for child in children:
            _walk_fields(child, out, pending_label=next_label)
            c_role = (child.get("role") or "").lower()
            c_name = (child.get("name") or "").strip()
            if c_role in ("text", "statictext", "heading", "label") and c_name:
                if c_name.lower() not in _NAV_NAMES:
                    next_label = c_name  # Pass this label to the next sibling
                else:
                    next_label = ""
            else:
                next_label = ""
        return
    elif role in _FILLER_ROLES and name and name.lower() not in _NAV_NAMES:
        req = " (required)" if node.get("required") else ""
        out.append(f"{role} '{name}'{req}")

    # For non-group, non-filler nodes: recurse with sibling-label passing
    children = node.get("children") or []
    next_label = ""
    for child in children:
        _walk_fields(child, out, pending_label=next_label)
        c_role = (child.get("role") or "").lower()
        c_name = (child.get("name") or "").strip()
        if c_role in ("text", "statictext", "heading", "label") and c_name:
            if c_name.lower() not in _NAV_NAMES:
                next_label = c_name
            else:
                next_label = ""
        else:
            next_label = ""


def format_interactive_fields(tree: dict) -> str:
    """Extract fillable fields from AX tree as a compact string for LLM prompts.

    Walks the AX tree and returns a human-readable list of fillable fields.
    Radio groups are represented as "radiogroup 'Question' options: Yes, No"
    so the LLM planner can emit {"label": "Question", "field_type": "radiogroup", "value": "Yes"}.
    Excludes navigation buttons (Next, Save, Back, Submit, Cancel).
    Caps at 40 fields to keep prompt size bounded.

    Args:
        tree: AX tree dict as returned by page.accessibility.snapshot().

    Returns:
        Newline-separated field descriptions, or "" if no fillable fields found.
    """
    fields: list[str] = []
    _walk_fields(tree, fields)
    return "\n".join(fields[:40])


async def get_ax_tree(page: Page) -> Optional[dict]:
    """Return the accessibility tree snapshot for the page.

    Wraps page.accessibility.snapshot() in a single place so that swapping
    to a CDP-based implementation is a one-line change.

    Returns:
        AX tree dict, or None on failure.
    """
    try:
        return await page.accessibility.snapshot()
    except Exception as exc:
        logger.debug(f"get_ax_tree failed: {exc}")
        return None


def search_ax_tree(
    node: dict,
    *,
    role: Optional[str] = None,
    label_pattern: Optional[re.Pattern] = None,
    label_contains: Optional[str] = None,
) -> list[dict]:
    """Recursively walk an AX tree and return matching nodes.

    Filters are combined with AND — a node must satisfy every supplied filter
    to be included in the result.

    Args:
        node: AX tree node dict (as returned by page.accessibility.snapshot()).
        role: Required ARIA role (case-insensitive).  None = accept any role.
        label_pattern: compiled re.Pattern matched against the node's ``name``
            field (case-insensitive by default via the pattern flags).
        label_contains: Plain substring match on the node's ``name`` field
            (case-insensitive).  Cheaper than a regex when no pattern is needed.

    Returns:
        List of matching node dicts (may be empty).  Pure function — no I/O.
    """
    if not node or not isinstance(node, dict):
        return []

    results: list[dict] = []

    # Evaluate current node
    node_role = (node.get("role") or "").lower()
    node_name = node.get("name") or ""

    role_ok = role is None or node_role == role.lower()
    pattern_ok = label_pattern is None or bool(label_pattern.search(node_name))
    contains_ok = label_contains is None or label_contains.lower() in node_name.lower()

    if role_ok and pattern_ok and contains_ok and node_name:
        results.append(node)

    # Recurse into children
    for child in node.get("children") or []:
        results.extend(
            search_ax_tree(
                child,
                role=role,
                label_pattern=label_pattern,
                label_contains=label_contains,
            )
        )

    return results


async def find_by_aria_label(
    page: Page,
    label_pattern: re.Pattern,
    *,
    roles: tuple[str, ...] = ("button", "link"),
    job_id: Optional[str] = None,
    timeout_ms: int = 3_000,
) -> Optional[Locator]:
    """Find a visible element by its ARIA label using the accessibility tree.

    Snapshots the AX tree, finds a matching node, then maps it back to a real
    Playwright Locator via get_by_role → get_by_label fallback.

    Args:
        page: Active Playwright page.
        label_pattern: Compiled re.Pattern to match against element ARIA labels.
        roles: Tuple of ARIA roles to search.  Checked in order.
        job_id: When provided, prefer nodes whose ``name`` contains job_id —
            guards against accidentally matching a sidebar card for another job.
        timeout_ms: Visibility check timeout per candidate.

    Returns:
        A visible Playwright Locator, or None if nothing matched.
    """
    tree = await get_ax_tree(page)
    if tree is None:
        logger.debug("find_by_aria_label: AX tree unavailable")
        return None

    # Collect all matching nodes across all requested roles
    candidates: list[dict] = []
    for role in roles:
        candidates.extend(
            search_ax_tree(tree, role=role, label_pattern=label_pattern)
        )

    if not candidates:
        logger.debug(f"find_by_aria_label: no AX matches for pattern={label_pattern.pattern!r}")
        return None

    # If job_id supplied, prefer nodes whose name contains the job ID.
    # This prevents matching a sidebar card's "Easy Apply to <other job>" button.
    if job_id:
        preferred = [c for c in candidates if job_id in (c.get("name") or "")]
        if preferred:
            logger.debug(
                f"find_by_aria_label: {len(preferred)} job_id-filtered candidates "
                f"(job_id={job_id!r})"
            )
            candidates = preferred
        else:
            logger.debug(
                f"find_by_aria_label: no candidates matched job_id={job_id!r} — "
                "using all matches (no sidebar guard possible)"
            )

    # Map each AX node back to a real Playwright Locator and return the first visible one.
    for node in candidates:
        name = node.get("name") or ""
        node_role = (node.get("role") or "").lower()

        # Primary: get_by_role with exact name match (most precise)
        try:
            locator = page.get_by_role(node_role, name=name).first  # type: ignore[arg-type]
            if await locator.is_visible(timeout=timeout_ms):
                logger.debug(
                    f"find_by_aria_label: found via get_by_role(role={node_role!r}, "
                    f"name={name[:60]!r})"
                )
                return locator
        except Exception:
            pass

        # Fallback: get_by_label (works for inputs/buttons with aria-label attr)
        try:
            locator = page.get_by_label(name).first
            if await locator.is_visible(timeout=timeout_ms):
                logger.debug(
                    f"find_by_aria_label: found via get_by_label(name={name[:60]!r})"
                )
                return locator
        except Exception:
            pass

    logger.debug(
        f"find_by_aria_label: {len(candidates)} AX candidates found but none visible"
    )
    return None
