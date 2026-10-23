"""Text that comes from the system is untrusted data.

Process names, application ids, mount points, battery and GPU model strings are chosen
by whatever wrote them (a program can name itself anything). Before such a string is
handed to an MCP client it is cleaned here: control and format characters (including
bidirectional overrides and zero-width characters) are removed, whitespace is collapsed,
the length is capped, and text that reads like instructions to an assistant is withheld.
"""
from __future__ import annotations

import re
import unicodedata

WITHHELD = "[withheld: text looked like instructions]"

# Unicode categories removed outright: Cc control, Cf format (bidi, zero width),
# Cs surrogates, Co private use, Cn unassigned. Line/paragraph separators become spaces.
_DROP = {"Cc", "Cf", "Cs", "Co", "Cn"}
_SPACE = {"Zl", "Zp", "Zs"}

_INSTRUCTION = re.compile(
    r"(?:\b(?:ignore|disregard|forget|override)\b.{0,40}"
    r"\b(?:instruction|instructions|prompt|prompts|rules|previous|above|earlier)\b)"
    r"|(?:\b(?:system|assistant|developer|user)\s*(?:prompt|message)\b)"
    r"|(?:^|\s)(?:system|assistant|developer|human)\s*:"
    r"|(?:<\s*/?\s*(?:system|instructions?|tool|im_start|im_end|prompt)\b)"
    r"|(?:\byou\s+(?:are|must|should|will|need\s+to)\s+(?:now\s+)?\w+)"
    r"|(?:\b(?:call|run|invoke|use|execute)\s+(?:the\s+)?(?:tool|function|command)\b)"
    r"|(?:\b(?:set_fan_curve|set_gpu_mode|set_power_limit|restore_defaults|"
    r"set_platform_profile|set_charge_limit|set_keyboard_\w+|approval_id|dry_run)\b)"
    r"|(?:\bnew\s+instructions?\b)|(?:```)|(?:\[\[|\]\])",
    re.IGNORECASE)


def clean(text, max_len: int = 200) -> str:
    """Printable, single-line, length-capped text. Never raises."""
    if text is None:
        return ""
    s = text if isinstance(text, str) else str(text)
    if len(s) > max_len * 4:  # bound the work on hostile input before normalising
        s = s[:max_len * 4]
    out = []
    for ch in s:
        cat = unicodedata.category(ch)
        if cat in _DROP:
            out.append(" " if ch in "\t\n\r\x0b\x0c" else "")
        elif cat in _SPACE:
            out.append(" ")
        else:
            out.append(ch)
    s = " ".join("".join(out).split())
    if len(s) > max_len:
        s = s[:max_len - 1].rstrip() + "…"
    return s


def looks_like_instruction(text: str) -> bool:
    raw = text or ""
    # also catch ignore_previous_instructions, Ignore-Previous, IgnorePreviousInstructions
    spaced = re.sub(r"[_\-.+/\\]+", " ", re.sub(r"(?<=[a-z])(?=[A-Z])", " ", raw))
    return bool(_INSTRUCTION.search(raw) or _INSTRUCTION.search(spaced))


def untrusted(text, max_len: int = 64) -> tuple[str, bool]:
    """(safe text, withheld?) for a system string shown to an agent."""
    s = clean(text, max_len)
    if looks_like_instruction(s):
        return WITHHELD, True
    return s, False


def clean_tree(obj, max_len: int = 300, max_items: int = 400, _depth: int = 0):
    """Clean every string (and key) in a JSON-like structure; cap sizes and depth."""
    if _depth > 12:
        return "[truncated]"
    if isinstance(obj, str):
        return clean(obj, max_len)
    if isinstance(obj, bool) or obj is None or isinstance(obj, int):
        return obj
    if isinstance(obj, float):
        return obj if obj == obj and obj not in (float("inf"), float("-inf")) else None
    if isinstance(obj, dict):
        out = {}
        for i, (k, v) in enumerate(obj.items()):
            if i >= max_items:
                out["_truncated"] = True
                break
            out[clean(k, 80)] = clean_tree(v, max_len, max_items, _depth + 1)
        return out
    if isinstance(obj, (list, tuple)):
        return [clean_tree(v, max_len, max_items, _depth + 1) for v in list(obj)[:max_items]]
    return clean(obj, max_len)


def escape_markup(text: str) -> str:
    """For notification bodies and GTK labels that interpret Pango markup."""
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))
