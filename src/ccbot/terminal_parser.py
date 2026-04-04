"""Terminal output parser — detects terminal UI elements in pane text.

Parses captured tmux pane content to detect:
  - Interactive UIs (AskUserQuestion, ExitPlanMode, Permission Prompt,
    RestoreCheckpoint) via regex-based UIPattern matching with top/bottom
    delimiters.
  - Status line (spinner characters + working text) by scanning from bottom up.

All Claude Code text patterns live here. To support a new UI type or
a changed Claude Code version, edit UI_PATTERNS / STATUS_SPINNERS.

Key functions: is_interactive_ui(), extract_interactive_content(),
parse_status_line(), classify_input_surface(), strip_pane_chrome(),
extract_bash_output().
"""

import re
from dataclasses import dataclass


@dataclass
class InteractiveUIContent:
    """Content extracted from an interactive UI."""

    content: str  # The extracted display content
    name: str = ""  # Pattern name that matched (e.g. "AskUserQuestion")


@dataclass(frozen=True)
class InputSurface:
    """Best-effort classification of the currently visible terminal surface."""

    kind: str
    has_visible_prompt: bool = False
    has_interactive_ui: bool = False
    status_line: str | None = None
    prompt_name: str = ""
    allows_remote_actions: bool = False


@dataclass(frozen=True)
class PendingInputPreview:
    """Best-effort extraction of Codex pending input preview surface."""

    pending_steers: tuple[str, ...] = ()
    rejected_steers: tuple[str, ...] = ()
    queued_messages: tuple[str, ...] = ()
    edit_hint: str = ""

    @property
    def is_empty(self) -> bool:
        return not (
            self.pending_steers
            or self.rejected_steers
            or self.queued_messages
        )


@dataclass(frozen=True)
class UIPattern:
    """A text-marker pair that delimits an interactive UI region.

    Extraction scans lines top-down: the first line matching any `top` pattern
    marks the start, the first subsequent line matching any `bottom` pattern
    marks the end.  Both boundary lines are included in the extracted content.

    ``top`` and ``bottom`` are tuples of compiled regexes — any single match
    is sufficient.  This accommodates wording changes across Claude Code
    versions (e.g. a reworded confirmation prompt).
    """

    name: str  # Descriptive label (not used programmatically)
    top: tuple[re.Pattern[str], ...]
    bottom: tuple[re.Pattern[str], ...]
    min_gap: int = 2  # minimum lines between top and bottom (inclusive)


# ── UI pattern definitions (order matters — first match wins) ────────────

UI_PATTERNS: list[UIPattern] = [
    UIPattern(
        name="CodexTrustPrompt",
        top=(re.compile(r"^\s*>\s*You are in "),),
        bottom=(re.compile(r"^\s*Press enter to continue"),),
    ),
    UIPattern(
        name="CodexExecApproval",
        top=(re.compile(r"^\s*Would you like to run the following command\?"),),
        bottom=(re.compile(r"^\s*Press enter to confirm or esc to cancel"),),
    ),
    UIPattern(
        name="CodexPatchApproval",
        top=(re.compile(r"^\s*Would you like to make the following edits\?"),),
        bottom=(re.compile(r"^\s*Press enter to confirm or esc to cancel"),),
    ),
    UIPattern(
        name="CodexPermissionsPopup",
        top=(re.compile(r"^\s*Update Model Permissions"),),
        bottom=(re.compile(r"^\s*Press enter to confirm or esc to go back"),),
    ),
    UIPattern(
        name="CodexModelPicker",
        top=(re.compile(r"^\s*Select Model and Effort"),),
        bottom=(
            re.compile(
                r"^\s*Press enter to select reasoning effort, or esc to dismiss\.?"
            ),
        ),
    ),
    UIPattern(
        name="CodexReasoningPicker",
        top=(re.compile(r"^\s*Select Reasoning Level for "),),
        bottom=(re.compile(r"^\s*Press enter to confirm or esc to go back"),),
    ),
    UIPattern(
        name="ExitPlanMode",
        top=(
            re.compile(r"^\s*Would you like to proceed\?"),
            # v2.1.29+: longer prefix that may wrap across lines
            re.compile(r"^\s*Claude has written up a plan"),
        ),
        bottom=(
            re.compile(r"^\s*ctrl-g to edit in "),
            re.compile(r"^\s*Esc to (cancel|exit)"),
        ),
    ),
    UIPattern(
        name="AskUserQuestion",
        top=(re.compile(r"^\s*←\s+[☐✔☒]"),),  # Multi-tab: no bottom needed
        bottom=(),
        min_gap=1,
    ),
    UIPattern(
        name="AskUserQuestion",
        top=(re.compile(r"^\s*[☐✔☒]"),),  # Single-tab: bottom required
        bottom=(re.compile(r"^\s*Enter to select"),),
        min_gap=1,
    ),
    UIPattern(
        name="PermissionPrompt",
        top=(
            re.compile(r"^\s*Do you want to proceed\?"),
            re.compile(r"^\s*Do you want to make this edit"),
            re.compile(r"^\s*Do you want to create \S"),
            re.compile(r"^\s*Do you want to delete \S"),
        ),
        bottom=(re.compile(r"^\s*Esc to cancel"),),
    ),
    UIPattern(
        # Permission menu with numbered choices (no "Esc to cancel" line)
        name="PermissionPrompt",
        top=(re.compile(r"^\s*❯\s*1\.\s*Yes"),),
        bottom=(),
        min_gap=2,
    ),
    UIPattern(
        # Bash command approval
        name="BashApproval",
        top=(
            re.compile(r"^\s*Bash command\s*$"),
            re.compile(r"^\s*This command requires approval"),
        ),
        bottom=(re.compile(r"^\s*Esc to cancel"),),
    ),
    UIPattern(
        name="RestoreCheckpoint",
        top=(re.compile(r"^\s*Restore the code"),),
        bottom=(re.compile(r"^\s*Enter to continue"),),
    ),
    UIPattern(
        name="Settings",
        top=(
            re.compile(r"^\s*Settings:.*tab to cycle"),
            re.compile(r"^\s*Select model"),
        ),
        bottom=(
            re.compile(r"Esc to cancel"),
            re.compile(r"Esc to exit"),
            re.compile(r"Enter to confirm"),
            re.compile(r"^\s*Type to filter"),
        ),
    ),
]

REMOTE_ACTION_PROMPTS = frozenset(
    {
        "CodexExecApproval",
        "CodexPatchApproval",
        "CodexPermissionsPopup",
        "CodexModelPicker",
        "CodexReasoningPicker",
        "CodexTrustPrompt",
    }
)


# ── Post-processing ──────────────────────────────────────────────────────

_RE_LONG_DASH = re.compile(r"^─{5,}$")
_QUEUED_FOLLOW_UP_HEADER_RE = re.compile(r"^\s*Queued follow-up messages\s*$")
_PENDING_STEERS_HEADER_RE = re.compile(
    r"^\s*Messages to be submitted after next tool call\s*$"
)
_REJECTED_STEERS_HEADER_RE = re.compile(
    r"^\s*Messages to be submitted at end of turn\s*$"
)
_EDIT_LAST_QUEUED_RE = re.compile(r"edit last queued message", re.IGNORECASE)
_PENDING_ITEM_PREFIX_RE = re.compile(
    r"^\s*(?:[☐☑☒□◻◽▫▪▢▣↳→➜➤•◦]\s*)+",
)


def _shorten_separators(text: str) -> str:
    """Replace lines of 5+ ─ characters with exactly ─────."""
    return "\n".join(
        "─────" if _RE_LONG_DASH.match(line) else line for line in text.split("\n")
    )


# ── Core extraction ──────────────────────────────────────────────────────


def _try_extract(lines: list[str], pattern: UIPattern) -> InteractiveUIContent | None:
    """Try to extract content matching a single UI pattern.

    When ``pattern.bottom`` is empty, the region extends from the top marker
    to the last non-empty line (used for multi-tab AskUserQuestion where the
    bottom delimiter varies by tab).
    """
    top_idx: int | None = None
    bottom_idx: int | None = None

    for i, line in enumerate(lines):
        if top_idx is None:
            if any(p.search(line) for p in pattern.top):
                top_idx = i
        elif pattern.bottom and any(p.search(line) for p in pattern.bottom):
            bottom_idx = i
            break

    if top_idx is None:
        return None

    # No bottom patterns → use last non-empty line as boundary
    if not pattern.bottom:
        for i in range(len(lines) - 1, top_idx, -1):
            if lines[i].strip():
                bottom_idx = i
                break

    if bottom_idx is None or bottom_idx - top_idx < pattern.min_gap:
        return None

    content = "\n".join(lines[top_idx : bottom_idx + 1]).rstrip()
    return InteractiveUIContent(content=_shorten_separators(content), name=pattern.name)


# ── Public API ───────────────────────────────────────────────────────────


def extract_interactive_content(pane_text: str) -> InteractiveUIContent | None:
    """Extract content from an interactive UI in terminal output.

    Tries each UI pattern in declaration order; first match wins.
    Returns None if no recognizable interactive UI is found.
    """
    if not pane_text:
        return None

    lines = pane_text.strip().split("\n")
    for pattern in UI_PATTERNS:
        result = _try_extract(lines, pattern)
        if result:
            return result
    return None


def is_interactive_ui(pane_text: str) -> bool:
    """Check if terminal currently shows an interactive UI."""
    return extract_interactive_content(pane_text) is not None


def classify_input_surface(pane_text: str) -> InputSurface:
    """Classify the visible terminal surface for input-driver decisions.

    The classification is intentionally conservative: only surfaces that can be
    positively identified are named. Everything else falls back to ``unknown``
    so unsupported controls can degrade safely to no-op.
    """
    if not pane_text:
        return InputSurface(kind="unknown")

    interactive = extract_interactive_content(pane_text)
    if interactive:
        return InputSurface(
            kind="blocked_prompt",
            has_visible_prompt=True,
            has_interactive_ui=True,
            prompt_name=interactive.name or "interactive_ui",
            allows_remote_actions=(interactive.name or "") in REMOTE_ACTION_PROMPTS,
        )

    status_line = parse_status_line(pane_text)
    if status_line is not None:
        return InputSurface(kind="busy", status_line=status_line)

    lines = [line.strip() for line in pane_text.splitlines()[-8:] if line.strip()]
    if any(line.startswith("■") for line in lines) and any(
        line.startswith(("›", "❯")) for line in lines
    ):
        return InputSurface(
            kind="blocked_prompt",
            has_visible_prompt=True,
            prompt_name="VisiblePromptError",
        )
    if any(line.startswith(("›", "❯")) for line in lines):
        return InputSurface(kind="input_ready", has_visible_prompt=True)

    return InputSurface(kind="unknown")


# ── Status line parsing ─────────────────────────────────────────────────

# Spinner characters Claude Code uses in its status line
STATUS_SPINNERS = frozenset(["·", "✻", "✽", "✶", "✳", "✢"])


def parse_status_line(pane_text: str) -> str | None:
    """Extract the Claude Code status line from terminal output.

    The status line (spinner + working text) appears immediately above
    the chrome separator (a full line of ``─`` characters).  We locate
    the separator first, then check the line just above it — this avoids
    false positives from ``·`` bullets in Claude's regular output.

    Returns the text after the spinner, or None if no status line found.
    """
    if not pane_text:
        return None

    lines = pane_text.split("\n")

    # Find the chrome separator: topmost ──── line in the last 10 lines
    chrome_idx: int | None = None
    search_start = max(0, len(lines) - 10)
    for i in range(search_start, len(lines)):
        stripped = lines[i].strip()
        if len(stripped) >= 20 and all(c == "─" for c in stripped):
            chrome_idx = i
            break

    if chrome_idx is None:
        return None  # No chrome visible — can't determine status

    # Check lines just above the separator (skip blanks, up to 4 lines)
    for i in range(chrome_idx - 1, max(chrome_idx - 5, -1), -1):
        line = lines[i].strip()
        if not line:
            continue
        if line[0] in STATUS_SPINNERS:
            return line[1:].strip()
        # First non-empty line above separator isn't a spinner → no status
        return None
    return None


# ── Pane chrome stripping & bash output extraction ─────────────────────


def strip_pane_chrome(lines: list[str]) -> list[str]:
    """Strip Claude Code's bottom chrome (prompt area + status bar).

    The bottom of the pane looks like::

        ────────────────────────  (separator)
        ❯                        (prompt)
        ────────────────────────  (separator)
          [Opus 4.6] Context: 34%
          ⏵⏵ bypass permissions…

    This function finds the topmost ``────`` separator in the last 10 lines
    and strips everything from there down.
    """
    search_start = max(0, len(lines) - 10)
    for i in range(search_start, len(lines)):
        stripped = lines[i].strip()
        if len(stripped) >= 20 and all(c == "─" for c in stripped):
            return lines[:i]
    return lines


def extract_pending_input_preview(pane_text: str) -> PendingInputPreview:
    """Extract queued follow-up messages from the visible terminal surface."""
    if not pane_text:
        return PendingInputPreview()

    lines = strip_pane_chrome(pane_text.splitlines())
    header_indices: list[int] = []
    for index, line in enumerate(lines):
        if (
            _QUEUED_FOLLOW_UP_HEADER_RE.match(line)
            or _PENDING_STEERS_HEADER_RE.match(line)
            or _REJECTED_STEERS_HEADER_RE.match(line)
        ):
            header_indices.append(index)
    if not header_indices:
        return PendingInputPreview()

    # Prefer the newest contiguous pending-input block near the bottom.
    header_index = header_indices[-1]
    for previous in reversed(header_indices[:-1]):
        bridge_ok = True
        for raw_line in lines[previous + 1 : header_index]:
            stripped = raw_line.strip()
            if not stripped:
                continue
            if _EDIT_LAST_QUEUED_RE.search(stripped):
                continue
            if _PENDING_ITEM_PREFIX_RE.match(stripped):
                continue
            bridge_ok = False
            break
        if not bridge_ok:
            break
        header_index = previous

    pending_steers: list[str] = []
    rejected_steers: list[str] = []
    queued_messages: list[str] = []
    edit_hint = ""
    header_line = lines[header_index].strip()
    if _PENDING_STEERS_HEADER_RE.match(header_line):
        current_section = "pending_steers"
    elif _REJECTED_STEERS_HEADER_RE.match(header_line):
        current_section = "rejected_steers"
    else:
        current_section = "queued_messages"
    for raw_line in lines[header_index + 1 :]:
        stripped = raw_line.strip()
        if not stripped:
            continue
        if _EDIT_LAST_QUEUED_RE.search(stripped):
            edit_hint = stripped
            continue
        if _PENDING_STEERS_HEADER_RE.match(stripped):
            current_section = "pending_steers"
            continue
        if _REJECTED_STEERS_HEADER_RE.match(stripped):
            current_section = "rejected_steers"
            continue
        if _QUEUED_FOLLOW_UP_HEADER_RE.match(stripped):
            current_section = "queued_messages"
            continue
        if not current_section:
            continue

        message = _PENDING_ITEM_PREFIX_RE.sub("", stripped).strip()
        if not message:
            continue
        if current_section == "pending_steers":
            pending_steers.append(message)
            continue
        if current_section == "rejected_steers":
            rejected_steers.append(message)
            continue
        queued_messages.append(message)

    return PendingInputPreview(
        pending_steers=tuple(pending_steers),
        rejected_steers=tuple(rejected_steers),
        queued_messages=tuple(queued_messages),
        edit_hint=edit_hint,
    )


def extract_bash_output(pane_text: str, command: str) -> str | None:
    """Extract ``!`` command output from a captured tmux pane.

    Searches from the bottom for the ``! <command>`` echo line, then
    returns that line and everything below it (including the ``⎿`` output).
    Returns *None* if the command echo wasn't found.
    """
    lines = strip_pane_chrome(pane_text.splitlines())

    # Find the last "! <command>" echo line (search from bottom).
    # Match on the first 10 chars of the command in case the line is truncated.
    cmd_idx: int | None = None
    match_prefix = command[:10]
    for i in range(len(lines) - 1, -1, -1):
        stripped = lines[i].strip()
        if stripped.startswith(f"! {match_prefix}") or stripped.startswith(
            f"!{match_prefix}"
        ):
            cmd_idx = i
            break

    if cmd_idx is None:
        return None

    # Include the command echo line and everything after it
    raw_output = lines[cmd_idx:]

    # Strip trailing empty lines
    while raw_output and not raw_output[-1].strip():
        raw_output.pop()

    if not raw_output:
        return None

    return "\n".join(raw_output).strip()


# ── Usage modal parsing ──────────────────────────────────────────────────────────


@dataclass
class UsageInfo:
    """Parsed output from Claude Code's /usage modal."""

    raw_text: str  # Full captured pane text
    parsed_lines: list[str]  # Cleaned content lines from the modal


def parse_usage_output(pane_text: str) -> UsageInfo | None:
    """Extract usage information from Claude Code's /usage settings tab.

    The /usage modal shows a Settings overlay with a "Usage" tab containing
    progress bars and reset times.  This parser looks for the Settings header
    line, then collects all content until "Esc to cancel".

    Returns UsageInfo with cleaned lines, or None if not detected.
    """
    if not pane_text:
        return None

    lines = pane_text.strip().split("\n")

    # Find the Settings header that indicates we're in the usage modal
    start_idx: int | None = None
    end_idx: int | None = None

    for i, line in enumerate(lines):
        stripped = line.strip()
        if start_idx is None:
            # The usage tab header line
            if "Settings:" in stripped and "Usage" in stripped:
                start_idx = i + 1  # skip the header itself
        else:
            if stripped.startswith("Esc to"):
                end_idx = i
                break

    if start_idx is None:
        return None
    if end_idx is None:
        end_idx = len(lines)

    # Collect content lines, stripping progress bar characters and whitespace
    cleaned: list[str] = []
    for line in lines[start_idx:end_idx]:
        # Strip the line but preserve meaningful content
        stripped = line.strip()
        if not stripped:
            continue
        # Remove progress bar block characters but keep the rest
        # Progress bars are like: █████▋   38% used
        # Strip leading block chars, keep the percentage
        stripped = re.sub(r"^[\u2580-\u259f\s]+", "", stripped).strip()
        if stripped:
            cleaned.append(stripped)

    if cleaned:
        return UsageInfo(raw_text=pane_text, parsed_lines=cleaned)

    return None
