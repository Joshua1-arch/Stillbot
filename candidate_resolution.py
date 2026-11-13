import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

@dataclass
class CandidateResolutionResult:
    resolved_value: Any
    is_resolved: bool
    has_multiple_candidates: bool
    candidates: List[Any] = field(default_factory=list)
    resolution_strategy: str = "direct"
    status: str = "ANSWERED"
    reason: str = ""
    requires_human_review: bool = False


def is_respondent_age_question(label: str, role: str = "", f_type: str = "") -> bool:
    """
    Strict discriminator determining whether a question is genuinely asking for the
    respondent's current numeric age or age category, avoiding false positives on
    open-ended advice, opinions, peer references, or historical events.
    """
    if not label:
        return False
        
    lbl = label.lower().strip()
    r = (role or "").lower().strip()
    ft = (f_type or "").lower().strip()
    
    # 1. Textareas are multi-line open-ended inputs; age is NEVER a textarea
    if r == "textarea" or ft == "textarea":
        return False
        
    # 2. Advice, opinion, and peer-directed questions containing 'your age'
    # e.g. "What would you tell someone your age who vapes or uses marijuana to encourage them to quit?"
    opinion_and_advice_cues = [
        "what would you tell", "what advice", "how would you", "why do you",
        "in your opinion", "tell someone", "encourage", "recommend", "share your thoughts",
        "message to", "what do you think", "describe", "explain", "someone your age",
        "people your age", "peers of your age", "others your age", "friends your age",
        "youth your age", "for your age"
    ]
    if any(cue in lbl for cue in opinion_and_advice_cues):
        return False
        
    # 3. Third-party entity exclusions
    third_parties = [
        "child", "children", "spouse", "partner", "direct report", "employee",
        "parent", "mother", "father", "son", "daughter", "grandchild", "brother", "sister"
    ]
    if any(tp in lbl for tp in third_parties):
        return False

    # 4. Historical or contextual non-respondent age references
    historical_or_contextual = [
        "at what age", "age did you", "age when you", "age at which",
        "drinking age", "legal age", "voting age", "retirement age",
        "golden age", "new age", "age of consent", "bronze age", "stone age"
    ]
    if any(h in lbl for h in historical_or_contextual):
        return False

    # 5. Exact and targeted patterns for respondent age
    lbl_clean = re.sub(r'[\?\:\.\*\(\)\-]+', ' ', lbl).strip()
    lbl_clean = re.sub(r'\s+', ' ', lbl_clean)
    
    if lbl_clean in ["age", "your age", "current age", "your current age", "age in years", "enter age", "select age"]:
        return True

    age_patterns = [
        r'\bwhat is your (current\s+)?age\b',
        r'\bhow old are you\b',
        r'\benter your (current\s+)?age\b',
        r'\bstate your (current\s+)?age\b',
        r'\bselect your (current\s+)?age\b',
        r'\bplease (enter|state|select|provide) your age\b',
        r'\bage (group|bracket|range|category)\b',
        r'^\s*age\s*$'
    ]
    for pat in age_patterns:
        if re.search(pat, lbl):
            return True

    return False


def determine_field_expectation(field: Dict[str, Any]) -> str:
    """
    Determines the expected input structure and semantics for a given form field:
    - 'ranking': Ordered list of options/items
    - 'choice_multi': Multiple selections permitted (checkboxes, multi-select)
    - 'numeric_single': Exactly one numeric scalar (age, count, zip, phone, year)
    - 'choice_single': Exactly one choice (radio, single-select dropdown)
    - 'text_free': Multi-line or freeform text (textarea, comments, essay)
    - 'text_single': Single-line scalar text
    """
    role = (field.get("role") or "").lower().strip()
    f_type = (field.get("type") or "").lower().strip()
    label = (field.get("label") or "").lower().strip()

    if role == "ranking" or f_type == "ranking":
        return "ranking"

    # Multi-choice detection
    if role == "checkbox":
        return "choice_multi"
    if field.get("aria_multiselectable") in [True, "true"]:
        return "choice_multi"
    if any(phrase in label for phrase in ["select all that apply", "select all", "choose all", "check all", "which of these (select all)"]):
        return "choice_multi"

    # Freeform vs single-line text check (prioritize open-ended advice, textarea, essay)
    if role in ["textarea"] or f_type in ["textarea"]:
        return "text_free"
    if any(k in label for k in ["describe", "explain", "feedback", "comments", "additional details", "elaborate", "what would you tell", "what advice", "how would you", "why do you", "encourage"]):
        return "text_free"

    # Numeric single-value detection (Age, phone, postal code, quantity)
    if is_respondent_age_question(label, role=role, f_type=f_type):
        return "numeric_single"

    if f_type in ["number", "numeric"]:
        return "numeric_single"

    if re.search(r'\b(birth year|year of birth|born in|year born)\b', label):
        return "numeric_single"

    if any(k in label for k in ["zip code", "postal code", "postcode", "zip"]):
        return "numeric_single"

    if any(k in label for k in ["phone number", "mobile number", "telephone", "cell phone"]):
        return "numeric_single"

    # Single-choice detection
    if role in ["radio", "combobox", "select"]:
        return "choice_single"

    # Freeform vs single-line text
    if role in ["textarea"] or f_type in ["textarea"]:
        return "text_free"
    if any(k in label for k in ["describe", "explain", "feedback", "comments", "additional details", "elaborate"]):
        return "text_free"

    return "text_single"


def detect_candidates(raw_value: Any, expected_type: str = "text") -> Tuple[bool, List[Any]]:
    """
    Identifies whether an answer source contains multiple candidate values.
    Distinguishes lists, delimited tokens, numeric ranges, and concatenated values.
    """
    if raw_value is None:
        return False, []

    if isinstance(raw_value, (list, tuple, set)):
        items = [x for x in raw_value if x is not None]
        return len(items) > 1, list(items)

    if isinstance(raw_value, (int, float)):
        return False, [raw_value]

    raw_str = str(raw_value).strip()
    if not raw_str:
        return False, []

    # Strip trailing punctuation often left by cheat sheet notes (e.g. '22, 23, 24, 25, 26, 27, 28.')
    cleaned = re.sub(r'[\.\;\:]+$', '', raw_str).strip()

    # Numeric range check (e.g., '22-28', '22 - 28', '22 to 28')
    range_match = re.match(r'^\s*(\d{1,3})\s*(?:-|–|—|to)\s*(\d{1,3})\s*$', cleaned, re.IGNORECASE)
    if range_match:
        start_n = int(range_match.group(1))
        end_n = int(range_match.group(2))
        if start_n < end_n and (end_n - start_n) <= 30:
            candidates = [str(n) for n in range(start_n, end_n + 1)]
            return True, candidates

    # Comma or semicolon delimited values
    if "," in cleaned or ";" in cleaned:
        delims = re.split(r'[,;]\s*', cleaned)
        tokens = [t.strip() for t in delims if t.strip()]
        if len(tokens) > 1:
            # Avoid splitting free-text sentences containing commas unless numeric/short tokens
            if expected_type == "numeric_single" or all(len(t.split()) <= 4 for t in tokens):
                return True, tokens

    # 'or' delimited candidate values (e.g., 'Male or Female', 'Christian or Muslim')
    if re.search(r'\s+\bor\b\s+', cleaned, re.IGNORECASE):
        or_tokens = re.split(r'\s+\bor\b\s+', cleaned, flags=re.IGNORECASE)
        tokens = [t.strip() for t in or_tokens if t.strip()]
        if len(tokens) > 1 and all(len(t.split()) <= 4 for t in tokens):
            return True, tokens

    # Concatenated numbers check for numeric fields (e.g. '22232425262728')
    if expected_type == "numeric_single" and cleaned.isdigit() and len(cleaned) >= 6:
        # Check if it consists of 2-digit consecutive or ascending numbers
        chunks = [cleaned[i:i+2] for i in range(0, len(cleaned), 2)]
        if len(chunks) >= 3 and all(c.isdigit() and 10 <= int(c) <= 99 for c in chunks):
            return True, chunks

    return False, [cleaned]


def resolve_candidates(
    raw_value: Any,
    field: Dict[str, Any],
    session_memory: Optional[dict] = None,
    user_instructions: str = "",
    confirmed_profile: Optional[dict] = None,
    require_confirmed: bool = False
) -> CandidateResolutionResult:
    """
    Reusable candidate resolution layer.
    Processes candidate values according to the question and field type:
    - Preserves candidates for multi-select and ranking fields.
    - Resolves candidates to exactly one valid scalar answer for single-value fields.
    - Uses established precedence: confirmed session memory -> user instruction -> defensible selection rule.
    - Escalates to HUMAN_REVIEW_REQUIRED if no defensible candidate can be resolved.
    """
    expectation = determine_field_expectation(field)
    has_multiple, candidates = detect_candidates(raw_value, expected_type=expectation)
    options = field.get("options") or []
    lbl = (field.get("label") or "").strip()
    lbl_lower = lbl.lower()

    # 1. Ranking expectation: Preserve ordered sequence
    if expectation == "ranking":
        if has_multiple:
            return CandidateResolutionResult(
                resolved_value=candidates,
                is_resolved=True,
                has_multiple_candidates=True,
                candidates=candidates,
                resolution_strategy="ranking_preserved",
                status="ANSWERED",
                reason="Ranking order preserved from candidates"
            )
        else:
            val_list = candidates if isinstance(raw_value, list) else ([raw_value] if raw_value is not None else [])
            return CandidateResolutionResult(
                resolved_value=val_list,
                is_resolved=bool(val_list),
                has_multiple_candidates=False,
                candidates=val_list,
                resolution_strategy="ranking_direct",
                status="ANSWERED" if val_list else "UNANSWERABLE",
                reason="Direct ranking assignment"
            )

    # 2. Multi-select expectation: Preserve all candidate selections
    if expectation == "choice_multi":
        if has_multiple:
            return CandidateResolutionResult(
                resolved_value=candidates,
                is_resolved=True,
                has_multiple_candidates=True,
                candidates=candidates,
                resolution_strategy="multi_select_preserved",
                status="ANSWERED",
                reason="Multi-select choices preserved"
            )
        else:
            val = candidates[0] if candidates else raw_value
            resolved_list = [val] if val is not None and str(val).strip() != "" else []
            return CandidateResolutionResult(
                resolved_value=resolved_list,
                is_resolved=bool(resolved_list),
                has_multiple_candidates=False,
                candidates=resolved_list,
                resolution_strategy="multi_select_single",
                status="ANSWERED" if resolved_list else "SKIPPED",
                reason="Single selection for multi-choice field"
            )

    # 3. Single-value expectation: MUST resolve to exactly one scalar value
    # Check if we already have a single non-candidate value
    if not has_multiple:
        if not candidates:
            return CandidateResolutionResult(
                resolved_value=None,
                is_resolved=False,
                has_multiple_candidates=False,
                candidates=[],
                resolution_strategy="unresolvable",
                status="HUMAN_REVIEW_REQUIRED" if field.get("required") else "SKIPPED",
                requires_human_review=bool(field.get("required")),
                reason=f"No answer provided for required single-value field '{lbl}'"
            )

        single_val = candidates[0]

        # Verification for numeric single fields (e.g. age)
        if expectation == "numeric_single":
            val_str = str(single_val).strip()
            # Reject concatenated multi-values disguised as single string
            if val_str.isdigit() and len(val_str) >= 6:
                return CandidateResolutionResult(
                    resolved_value=None,
                    is_resolved=False,
                    has_multiple_candidates=True,
                    candidates=[],
                    resolution_strategy="unresolvable",
                    status="HUMAN_REVIEW_REQUIRED",
                    requires_human_review=True,
                    reason=f"Concatenated numeric value '{val_str}' rejected for single-value field '{lbl}'"
                )

            # Age validation: must be reasonable human age (1 to 120)
            if is_respondent_age_question(lbl_lower):
                num_match = re.search(r'\b\d{1,3}\b', val_str)
                if num_match:
                    age_int = int(num_match.group(0))
                    if 1 <= age_int <= 120:
                        return CandidateResolutionResult(
                            resolved_value=str(age_int),
                            is_resolved=True,
                            has_multiple_candidates=False,
                            candidates=[str(age_int)],
                            resolution_strategy="direct_numeric",
                            status="ANSWERED",
                            reason=f"Valid single age '{age_int}' confirmed"
                        )
                # Unresolvable / invalid age
                return CandidateResolutionResult(
                    resolved_value=None,
                    is_resolved=False,
                    has_multiple_candidates=False,
                    candidates=[single_val],
                    resolution_strategy="unresolvable",
                    status="HUMAN_REVIEW_REQUIRED",
                    requires_human_review=True,
                    reason=f"Invalid age value '{single_val}' cannot be resolved safely"
                )

        return CandidateResolutionResult(
            resolved_value=single_val,
            is_resolved=True,
            has_multiple_candidates=False,
            candidates=[single_val],
            resolution_strategy="direct_scalar",
            status="ANSWERED",
            reason="Direct single scalar value"
        )

    # 4. Multiple candidates detected for single-value field: Must resolve to exactly ONE
    clean_candidates = [str(c).strip().rstrip('.') for c in candidates if str(c).strip()]

    # Priority A: Check prior session memory for already confirmed fact
    if session_memory:
        # Check direct label match
        for m_k, m_v in session_memory.items():
            if m_v is not None and str(m_v).strip():
                if m_k.lower() == lbl_lower or (re.search(r'\bage\b', m_k.lower()) and re.search(r'\bage\b', lbl_lower)):
                    m_str = str(m_v).strip()
                    for cand in clean_candidates:
                        if cand.lower() == m_str.lower():
                            return CandidateResolutionResult(
                                resolved_value=cand,
                                is_resolved=True,
                                has_multiple_candidates=True,
                                candidates=clean_candidates,
                                resolution_strategy="session_memory_consistency",
                                status="ANSWERED",
                                reason=f"Resolved candidate '{cand}' matching confirmed session memory for '{m_k}'"
                            )

    # Priority B: Explicit user instruction / selection rule for current run
    if user_instructions:
        # Check if instructions explicitly name one candidate or rule
        for cand in clean_candidates:
            # Word-boundary check for candidate in instructions
            if re.search(r'\b' + re.escape(cand.lower()) + r'\b', user_instructions.lower()):
                if session_memory is not None:
                    session_memory[lbl] = cand
                return CandidateResolutionResult(
                    resolved_value=cand,
                    is_resolved=True,
                    has_multiple_candidates=True,
                    candidates=clean_candidates,
                    resolution_strategy="user_instruction",
                    status="ANSWERED",
                    reason=f"Candidate '{cand}' resolved via explicit user instruction"
                )

        # Check for selection rules (e.g., 'youngest', 'oldest', 'first', 'lowest', 'highest')
        if any(w in user_instructions.lower() for w in ["first", "lowest", "youngest", "min"]):
            chosen = clean_candidates[0]
            if session_memory is not None:
                session_memory[lbl] = chosen
            return CandidateResolutionResult(
                resolved_value=chosen,
                is_resolved=True,
                has_multiple_candidates=True,
                candidates=clean_candidates,
                resolution_strategy="instruction_rule_min",
                status="ANSWERED",
                reason=f"Candidate '{chosen}' resolved via min/first rule"
            )
        if any(w in user_instructions.lower() for w in ["last", "highest", "oldest", "max"]):
            chosen = clean_candidates[-1]
            if session_memory is not None:
                session_memory[lbl] = chosen
            return CandidateResolutionResult(
                resolved_value=chosen,
                is_resolved=True,
                has_multiple_candidates=True,
                candidates=clean_candidates,
                resolution_strategy="instruction_rule_max",
                status="ANSWERED",
                reason=f"Candidate '{chosen}' resolved via max/last rule"
            )

    # Priority C: Options matching (if radio or dropdown with options)
    if options:
        # 1. Exact match with an option
        for opt in options:
            opt_clean = opt.strip().lower()
            for cand in clean_candidates:
                if cand.lower() == opt_clean:
                    if session_memory is not None:
                        session_memory[lbl] = opt
                    return CandidateResolutionResult(
                        resolved_value=opt,
                        is_resolved=True,
                        has_multiple_candidates=True,
                        candidates=clean_candidates,
                        resolution_strategy="option_exact_match",
                        status="ANSWERED",
                        reason=f"Candidate '{cand}' matched option '{opt}'"
                    )

        # 2. Age range option matching (e.g. option '21-25' matches candidate '24')
        if expectation == "numeric_single" and is_respondent_age_question(lbl_lower):
            valid_numeric_cands = [int(c) for c in clean_candidates if c.isdigit() and 1 <= int(c) <= 120]
            if valid_numeric_cands:
                for opt in options:
                    m = re.search(r'(\d{1,3})\s*(?:-|–|—|to)\s*(\d{1,3})', opt)
                    if m:
                        low_n, high_n = int(m.group(1)), int(m.group(2))
                        for v_cand in valid_numeric_cands:
                            if low_n <= v_cand <= high_n:
                                if session_memory is not None:
                                    session_memory[lbl] = opt
                                return CandidateResolutionResult(
                                    resolved_value=opt,
                                    is_resolved=True,
                                    has_multiple_candidates=True,
                                    candidates=clean_candidates,
                                    resolution_strategy="option_range_match",
                                    status="ANSWERED",
                                    reason=f"Candidate age '{v_cand}' matched range option '{opt}'"
                                )

    # Priority D: Established Selection Rule for Persona Candidate Set
    if expectation == "numeric_single" and is_respondent_age_question(lbl_lower):
        valid_numeric_cands = [int(c) for c in clean_candidates if c.isdigit() and 1 <= int(c) <= 120]
        if valid_numeric_cands:
            # Deterministic, defensible candidate selection (median / middle of candidate set)
            # Avoids hardcoding any specific number while ensuring exact reproducibility
            mid_idx = (len(valid_numeric_cands) - 1) // 2
            chosen_age = str(valid_numeric_cands[mid_idx])
            if session_memory is not None:
                session_memory[lbl] = chosen_age
                session_memory["Your age"] = chosen_age
                session_memory["age"] = chosen_age
            return CandidateResolutionResult(
                resolved_value=chosen_age,
                is_resolved=True,
                has_multiple_candidates=True,
                candidates=clean_candidates,
                resolution_strategy="defensible_median_candidate",
                status="ANSWERED",
                reason=f"Resolved candidate age '{chosen_age}' using deterministic median selection from persona candidate set"
            )

    # General single-choice / single-text candidate resolution
    if clean_candidates:
        # Non-numeric single-value field (e.g. Gender 'Male or Female')
        chosen = clean_candidates[0]
        if session_memory is not None:
            session_memory[lbl] = chosen
        return CandidateResolutionResult(
            resolved_value=chosen,
            is_resolved=True,
            has_multiple_candidates=True,
            candidates=clean_candidates,
            resolution_strategy="defensible_primary_candidate",
            status="ANSWERED",
            reason=f"Resolved primary candidate '{chosen}' from candidate set"
        )

    # If no defensible candidate could be determined, pause for human review
    return CandidateResolutionResult(
        resolved_value=None,
        is_resolved=False,
        has_multiple_candidates=True,
        candidates=clean_candidates,
        resolution_strategy="unresolvable",
        status="HUMAN_REVIEW_REQUIRED",
        requires_human_review=True,
        reason=f"No defensible candidate could be resolved for single-value field '{lbl}'. Pausing for human review."
    )

