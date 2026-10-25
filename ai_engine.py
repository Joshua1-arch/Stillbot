import asyncio
import json
import base64
import re
from typing import Any, List, Literal, Dict, Union, Optional, Tuple
from pydantic import BaseModel, create_model, Field
from openai import AsyncOpenAI
from rapidfuzz import process, fuzz
import config
from run_context import RunContext, NormalizedQuestion

# Initialize OpenAI Client to use Lily (Poolot)
client = AsyncOpenAI(api_key=config.LILY_API_KEY, base_url="https://agent.poolot.com/v1")

def generate_dynamic_schema(fields: List[Dict]) -> type[BaseModel]:
    """
    Dynamically generates a Pydantic model enforcing the exact options available on the page.
    For massive dropdowns (>20 options), it requests a generic string to protect the token context, 
    which we will fuzzy-match locally.
    """
    schema_fields = {}
    for f in fields:
        field_id = f.get("id", "unknown_field")
        field_type = f.get("type", "text")
        options = f.get("options", [])
        
        # Determine the annotation constraint based on field type and option count
        if field_type in ["select", "radio", "checkbox"] and len(options) > 0:
            if len(options) > 20:
                # Protect context window: ask for a string, we fuzzy match later
                annotation = Union[str, int, float]
            else:
                # Strict constraints for smaller lists
                options_tuple = tuple(options)
                if field_type == "checkbox":
                    annotation = Union[List[Literal[options_tuple]], Literal[options_tuple], bool, str] # type: ignore
                else:
                    annotation = Union[Literal[options_tuple], str, int, float] # type: ignore
        elif field_type == "checkbox":
            # Individual boolean checkbox (e.g. opt-in, terms, individual race/military item)
            annotation = Union[bool, str, int]
        else:
            annotation = Union[str, bool, int, float]
            
        # Create a metadata-rich sub-model for this specific field
        sub_model_name = f"FieldAnswer_{field_id}"
        sub_model = create_model(
            sub_model_name,
            value=(Optional[annotation], None),
            confidence=(float, Field(0.9, ge=0.0, le=1.0, description="Confidence score from 0.0 to 1.0")),
            source=(Literal["persona", "instruction", "inferred", "deduced", "research", "permitted_alternative", "estimate", "unauthorized"], Field("deduced", description="Source of the answer.")),
            status=(Literal["ANSWERED", "NEEDS_CLARIFICATION", "UNANSWERABLE", "SKIPPED", "INVALID", "answered", "inferred", "unanswered"], Field("ANSWERED", description="Status of the decision.")),
            reason=(str, Field("Contextual deduction based on established persona and memory.", description="Brief logical justification / deduction reason based on persona and session memory."))
        )
        
        schema_fields[field_id] = (sub_model, Field(..., description=f"Provide value for: {f.get('label', field_id)}"))
        
    return create_model("DynamicFormSchema", **schema_fields)

def find_permitted_alternative(field: Dict) -> Optional[Tuple[str, str]]:
    """
    Checks if a field has explicit permitted alternatives in its options:
    - 'Prefer not to say', 'Prefer not to answer', 'Decline to state'
    - 'None of the above', 'None of these', 'Not applicable', 'N/A'
    - 'Don't know', 'Unsure', 'Not sure'
    Returns (selected_option, reason) or None.
    """
    options = field.get("options", [])
    if not options:
        return None
        
    prefer_not_say = ["prefer not to say", "prefer not to answer", "decline to answer", "decline to state", "i prefer not to say", "i'd prefer not to answer"]
    none_kw = ["none of the above", "none of these", "not applicable", "n/a", "neither"]
    unsure_kw = ["don't know", "do not know", "unsure", "not sure", "uncertain"]
    
    # 1. Prefer not to say
    for opt in options:
        opt_clean = opt.strip().lower()
        if any(k in opt_clean for k in prefer_not_say):
            return (opt, "Permitted alternative: 'Prefer not to say' explicitly provided by questionnaire")
            
    # 2. None of the above / Not applicable
    for opt in options:
        opt_clean = opt.strip().lower()
        if any(k == opt_clean or k in opt_clean for k in none_kw):
            return (opt, "Permitted alternative: 'None of the above' / 'Not applicable' explicitly provided by questionnaire")
            
    # 3. Don't know / Unsure
    for opt in options:
        opt_clean = opt.strip().lower()
        if any(k == opt_clean or k in opt_clean for k in unsure_kw):
            return (opt, "Permitted alternative: 'Don't know' / 'Unsure' explicitly provided by questionnaire")
            
    return None

def resolve_public_fact_or_research(field: Dict, session_memory: dict = None) -> Optional[Dict]:
    """
    Resolves permitted public facts, geographic associations, mathematics, and definitions
    without fabricating personal respondent facts.
    Distinguishes IP-derived approximate location from exact personal street address.
    """
    lbl_lower = (field.get("label") or "").strip().lower()
    options = field.get("options", [])
    
    # 1. Mathematical / Arithmetic expressions (e.g. "What is 7 + 8?", "Calculate 15 * 2")
    math_m = re.search(r'(?:what\s+is|calculate|solve)?\s*(\d+)\s*([\+\-\*x\/])\s*(\d+)', lbl_lower)
    if math_m:
        n1 = int(math_m.group(1))
        op = math_m.group(2)
        n2 = int(math_m.group(3))
        res = None
        if op == '+': res = n1 + n2
        elif op == '-': res = n1 - n2
        elif op in ['*', 'x']: res = n1 * n2
        elif op == '/' and n2 != 0: res = n1 // n2
        if res is not None:
            target_val = str(res)
            if options:
                match = process.extractOne(target_val, options, scorer=fuzz.WRatio)
                if match and match[1] >= 80:
                    target_val = match[0]
            return {
                "value": target_val,
                "confidence": 1.0,
                "source": "research",
                "status": "ANSWERED",
                "reason": f"Mathematical evaluation: {n1} {op} {n2} = {res}"
            }

    # 2. Objective Calendar & Science Facts
    public_facts = [
        (r'\b(?:how many|number of)\s+days\s+(?:are\s+)?in\s+a\s+week\b', "7", "Public factual research: 7 days in a week"),
        (r'\b(?:how many|number of)\s+months\s+(?:are\s+)?in\s+a\s+year\b', "12", "Public factual research: 12 months in a year"),
        (r'\b(?:how many|number of)\s+days\s+(?:are\s+)?in\s+a\s+leap\s+year\b', "366", "Public factual research: 366 days in a leap year"),
        (r'\b(?:how many|number of)\s+days\s+(?:are\s+)?in\s+a\s+(?:regular\s+|normal\s+)?year\b', "365", "Public factual research: 365 days in a year"),
        (r'\b(?:how many|number of)\s+hours\s+(?:are\s+)?in\s+a\s+day\b', "24", "Public factual research: 24 hours in a day"),
        (r'\bboiling\s+point\s+of\s+water\b.*celsius', "100", "Scientific research: Boiling point of water is 100°C"),
        (r'\bfreezing\s+point\s+of\s+water\b.*celsius', "0", "Scientific research: Freezing point of water is 0°C"),
    ]
    for pat, fact_val, fact_reason in public_facts:
        if re.search(pat, lbl_lower):
            target_val = fact_val
            if options:
                match = process.extractOne(fact_val, options, scorer=fuzz.WRatio)
                if match and match[1] >= 70:
                    target_val = match[0]
            return {
                "value": target_val,
                "confidence": 1.0,
                "source": "research",
                "status": "ANSWERED",
                "reason": fact_reason
            }

    # 3. World Country Capitals
    world_capitals = {
        "france": "Paris", "germany": "Berlin", "italy": "Rome", "spain": "Madrid",
        "united kingdom": "London", "uk": "London", "england": "London",
        "canada": "Ottawa", "japan": "Tokyo", "australia": "Canberra",
        "china": "Beijing", "india": "New Delhi", "brazil": "Brasilia",
        "mexico": "Mexico City", "russia": "Moscow", "south korea": "Seoul",
        "egypt": "Cairo", "argentina": "Buenos Aires"
    }
    for country, cap in world_capitals.items():
        if f"capital of {country}" in lbl_lower or f"capital city of {country}" in lbl_lower:
            target_val = cap
            if options:
                match = process.extractOne(cap, options, scorer=fuzz.WRatio)
                if match and match[1] >= 70:
                    target_val = match[0]
            return {
                "value": target_val,
                "confidence": 1.0,
                "source": "research",
                "status": "ANSWERED",
                "reason": f"Public geographic research: Capital of {country.title()} is {cap}"
            }

    # 4. US State Capitals
    state_capitals = {
        "alabama": "Montgomery", "alaska": "Juneau", "arizona": "Phoenix", "arkansas": "Little Rock",
        "california": "Sacramento", "colorado": "Denver", "connecticut": "Hartford", "delaware": "Dover",
        "florida": "Tallahassee", "georgia": "Atlanta", "hawaii": "Honolulu", "idaho": "Boise",
        "illinois": "Springfield", "indiana": "Indianapolis", "iowa": "Des Moines", "kansas": "Topeka",
        "kentucky": "Frankfort", "louisiana": "Baton Rouge", "maine": "Augusta", "maryland": "Annapolis",
        "massachusetts": "Boston", "michigan": "Lansing", "minnesota": "St. Paul", "mississippi": "Jackson",
        "missouri": "Jefferson City", "montana": "Helena", "nebraska": "Lincoln", "nevada": "Carson City",
        "new hampshire": "Concord", "new jersey": "Trenton", "new mexico": "Santa Fe", "new york": "Albany",
        "north carolina": "Raleigh", "north dakota": "Bismarck", "ohio": "Columbus", "oklahoma": "Oklahoma City",
        "oregon": "Salem", "pennsylvania": "Harrisburg", "rhode island": "Providence", "south carolina": "Columbia",
        "south dakota": "Pierre", "tennessee": "Nashville", "texas": "Austin", "utah": "Salt Lake City",
        "vermont": "Montpelier", "virginia": "Richmond", "washington": "Olympia", "west virginia": "Charleston",
        "wisconsin": "Madison", "wyoming": "Cheyenne"
    }
    for state, capital in state_capitals.items():
        if f"capital of {state}" in lbl_lower or f"capital city of {state}" in lbl_lower:
            target_val = capital
            if options:
                match = process.extractOne(capital, options, scorer=fuzz.WRatio)
                if match and match[1] >= 70:
                    target_val = match[0]
            return {
                "value": target_val,
                "confidence": 1.0,
                "source": "research",
                "status": "ANSWERED",
                "reason": f"Public geographic research: Capital of {state.title()} is {capital}"
            }

    # 5. Geographic correlation from session memory
    if session_memory:
        city_to_state = {
            "chicago": "Illinois", "dallas": "Texas", "houston": "Texas", "austin": "Texas",
            "orlando": "Florida", "miami": "Florida", "los angeles": "California", "san francisco": "California",
            "salt lake city": "Utah", "new york": "New York", "philadelphia": "Pennsylvania"
        }
        if any(k in lbl_lower for k in ["state", "province", "region"]) and not any(k in lbl_lower for k in ["united states", "statement"]):
            for c_name, s_name in city_to_state.items():
                if any(c_name in str(v).lower() for v in session_memory.values()):
                    target_val = s_name
                    if options:
                        match = process.extractOne(s_name, options, scorer=fuzz.WRatio)
                        if match and match[1] >= 70:
                            target_val = match[0]
                    return {
                        "value": target_val,
                        "confidence": 0.95,
                        "source": "research",
                        "status": "ANSWERED",
                        "reason": f"Geographic correlation: Confirmed city maps to state {s_name}"
                    }

    return None

def resolve_permitted_estimate(field: Dict, session_memory: dict = None, persona: str = None) -> Optional[Dict]:
    """
    Resolves questions that explicitly request or permit an approximation.
    Keywords: 'approximately', 'roughly', 'about how many', 'estimate'.
    """
    lbl_lower = (field.get("label") or "").strip().lower()
    options = field.get("options", [])
    
    is_estimate_permitted = any(k in lbl_lower for k in ["approximately", "approximate", "roughly", "about how many", "estimate"])
    if not is_estimate_permitted:
        return None
        
    if options:
        mid_idx = len(options) // 2
        selected_opt = options[mid_idx]
        return {
            "value": selected_opt,
            "confidence": 0.85,
            "source": "estimate",
            "status": "ANSWERED",
            "reason": f"Permitted estimate derived for approximation question: '{selected_opt}'"
        }
        
    return None

def _attempt_field_resolution_fallback(
    field: Dict,
    run_context: Optional[RunContext] = None,
    session_memory: Optional[dict] = None,
    persona: Optional[str] = None
) -> Optional[Dict]:
    """
    Exhausts reasonable, permitted resolution strategies before escalating:
    1. Deterministic profile & explicit user instructions via run_context.
    2. Public factual research (arithmetic, geography, world capitals, science).
    3. Permitted estimate (for approximation questions).
    4. Prior session memory alignment.
    5. Permitted neutral alternative ('Prefer not to say', 'None of the above')
       ONLY when no facts or instructions exist.
    """
    f_id = field.get("id")
    fp = field.get("fingerprint") or ""
    lbl = (field.get("label") or "").strip()
    options = field.get("options", [])

    # 1. Deterministic profile & explicit instructions
    if run_context:
        norm_q = run_context.question_inventory.get(fp)
        if not norm_q:
            norm_q = NormalizedQuestion(
                fingerprint=fp,
                id=f_id,
                role=field.get("role", "textbox"),
                label=lbl,
                options=options,
                type=field.get("type", "text"),
                required=field.get("required", False)
            )
        det_res = run_context.resolve_deterministic_answer(norm_q)
        if det_res:
            det_val, det_src = det_res
            return {
                "value": det_val,
                "confidence": 1.0,
                "source": "instruction" if "instruction" in det_src else "persona",
                "status": "ANSWERED",
                "reason": f"Fallback resolution from {det_src}"
            }

    # 2. Public research (math, geography, science, etc.)
    res_ans = resolve_public_fact_or_research(field, session_memory)
    if res_ans:
        return res_ans

    # 3. Permitted estimate
    est_ans = resolve_permitted_estimate(field, session_memory, persona)
    if est_ans:
        return est_ans

    # 4. Session memory alignment
    if session_memory and lbl:
        lbl_clean = lbl.lower()
        for mem_k, mem_v in session_memory.items():
            if mem_v is not None and str(mem_v).strip() != "":
                mem_k_clean = str(mem_k).lower().strip()
                # Ensure prior answer is reused ONLY when questions genuinely refer to the same fact
                is_same_fact = (mem_k_clean == lbl_clean) or (fuzz.token_sort_ratio(mem_k_clean, lbl_clean) >= 82)
                # Prevent entity cross-contamination (e.g. respondent vs child/spouse)
                third_parties = ["child", "children", "spouse", "partner", "direct report", "employee", "parent", "mother", "father"]
                if any(w in lbl_clean for w in third_parties) != any(w in mem_k_clean for w in third_parties):
                    is_same_fact = False
                if is_same_fact:
                    match_val = mem_v
                    if options:
                        best = process.extractOne(str(mem_v), options, scorer=fuzz.WRatio)
                        if best and best[1] >= 65:
                            match_val = best[0]
                        else:
                            continue
                    return {
                        "value": match_val,
                        "confidence": 0.9,
                        "source": "inferred",
                        "status": "ANSWERED",
                        "reason": f"Resolved from prior session memory for '{mem_k}'"
                    }

    # 5. Permitted neutral alternative (ONLY when no fact is known)
    alt = find_permitted_alternative(field)
    if alt:
        return {
            "value": alt[0],
            "confidence": 0.9,
            "source": "permitted_alternative",
            "status": "ANSWERED",
            "reason": alt[1]
        }

    # 6. Ranking default resolution: if field is a ranking widget and options exist
    if (field.get("role") == "ranking" or field.get("type") == "ranking") and options:
        return {
            "value": list(options),
            "confidence": 0.85,
            "source": "inferred",
            "status": "ANSWERED",
            "reason": "Sequential ranking ordering inferred from available options"
        }

    return None

def check_and_sanitize_unsupported_personal_inference(
    field: Dict,
    ans: Dict,
    run_context: Optional[RunContext] = None,
    session_memory: Optional[dict] = None,
    persona: Optional[str] = None
) -> Dict:
    """
    Prevents unsupported personal inferences and stereotypes (e.g. inferring education,
    smoking frequency, gender, or years of experience from role or age).
    Prefers confirmed profile facts and explicit instructions.
    If unconfirmed, selects neutral option ('Prefer not to say') when available,
    or returns UNANSWERABLE / SKIPPED rather than inventing personal facts.
    """
    lbl_lower = (field.get("label") or "").strip().lower()
    
    # Check if question asks for sensitive/personal traits
    is_personal_q = False
    personal_type = ""
    if any(k in lbl_lower for k in ["education", "highest degree", "highest level of education", "qualification", "degree obtained"]):
        is_personal_q = True
        personal_type = "education"
    elif any(k in lbl_lower for k in ["smoke", "smoking", "tobacco", "cigarette", "vape", "nicotine"]):
        is_personal_q = True
        personal_type = "smoking"
    elif re.search(r'\b(gender|what is your sex|sex at birth)\b', lbl_lower):
        is_personal_q = True
        personal_type = "gender"
    elif any(k in lbl_lower for k in ["years of experience", "years in role", "years of work experience", "how many years have you"]):
        is_personal_q = True
        personal_type = "experience"
    elif re.search(r'\b(what is your age|how old are you|enter your age|your age|age group|age)\b', lbl_lower):
        if not any(k in lbl_lower for k in ["child", "children", "spouse", "partner", "employee", "parent", "mother", "father", "son", "daughter"]):
            is_personal_q = True
            personal_type = "age"

    if not is_personal_q:
        return ans

    # Check if confirmed profile or session memory contains an authentic fact
    facts_text = ""
    if run_context:
        facts_text += json.dumps(run_context.confirmed_profile).lower() + " " + run_context.user_instructions.lower()
    if persona:
        facts_text += " " + persona.lower()
    if session_memory:
        facts_text += " " + json.dumps(session_memory).lower()

    has_confirmed_fact = False
    if personal_type == "education" and any(k in facts_text for k in ["bachelor", "master", "phd", "degree", "diploma", "high school", "university", "college", "doctorate"]):
        has_confirmed_fact = True
    elif personal_type == "smoking" and any(k in facts_text for k in ["smoke", "smoking", "tobacco", "cigarette", "non-smoker", "smoker"]):
        has_confirmed_fact = True
    elif personal_type == "gender" and any(k in facts_text for k in ["male", "female", "man", "woman", "non-binary"]):
        has_confirmed_fact = True
    elif personal_type == "experience" and any(k in facts_text for k in ["years of experience", "years experience", "experience:"]):
        has_confirmed_fact = True
    elif personal_type == "age" and (
        any(k in facts_text for k in ["age:", "age =", "age is", "years old"])
        or (run_context and bool(run_context.confirmed_profile.get("age")))
    ):
        has_confirmed_fact = True

    if has_confirmed_fact:
        return ans

    # If answer came directly from deterministic lookup (instruction / confirmed fact), trust it
    if ans.get("source") in ["persona", "instruction", "confirmed_respondent_fact"]:
        return ans

    # If the answer is already a permitted neutral option, keep it
    ans_val_str = str(ans.get("value") or "").lower()
    if any(k in ans_val_str for k in ["prefer not", "decline", "none of", "not applicable", "n/a", "unsure", "don't know"]):
        return ans

    # Otherwise, this is an unsupported inference! Check for permitted neutral option in field options
    alt = find_permitted_alternative(field)
    if alt:
        ans["value"] = alt[0]
        ans["source"] = "permitted_alternative"
        ans["status"] = "ANSWERED"
        ans["reason"] = f"Neutral option selected because {personal_type} is not specified in profile: {alt[1]}"
        return ans

    # No neutral option available: do NOT fabricate personal facts
    is_required = field.get("required", False)
    if is_required:
        ans["value"] = None
        ans["status"] = "UNANSWERABLE"
        ans["source"] = "unauthorized"
        ans["reason"] = f"Required personal question '{field.get('label')}' has no confirmed fact in profile and no neutral option. Human input required."
    else:
        ans["value"] = None
        ans["status"] = "SKIPPED"
        ans["reason"] = f"Optional personal question '{field.get('label')}' left blank to prevent unsupported inference."

    return ans

def validate_answer_set(
    fields: List[Dict],
    answers: Dict[str, Dict],
    run_context: Optional[RunContext] = None,
    session_memory: Optional[dict] = None,
    persona: Optional[str] = None
) -> Tuple[Dict[str, Dict], bool, List[Dict]]:
    """
    Dedicated Final-Answer Validation Stage.
    Verifies that:
    1. Every question in fields has an answer object.
    2. Every answer object contains valid types and metadata.
    3. No required answer contains None, empty string, whitespace only, or unresolved status.
    4. Options membership and control type constraints are satisfied.
    Returns (validated_answers, is_complete, unresolved_required_fields).
    """
    validated = dict(answers)
    is_complete = True
    unresolved_required = []
    
    for f in fields:
        fid = f.get("id")
        options = f.get("options", [])
        role = f.get("role", "textbox")
        is_required = f.get("required", False)
        
        # 1. Ensure answer object exists
        if fid not in validated:
            recovered = _attempt_field_resolution_fallback(f, run_context, session_memory, persona)
            if recovered:
                validated[fid] = recovered
            elif not is_required:
                validated[fid] = {
                    "value": None,
                    "confidence": 1.0,
                    "source": "deduced",
                    "status": "SKIPPED",
                    "reason": "Optional field without explicit input"
                }
            else:
                validated[fid] = {
                    "value": None,
                    "confidence": 0.0,
                    "source": "unauthorized",
                    "status": "UNANSWERABLE",
                    "reason": f"Required question '{f.get('label')}' omitted from resolution output"
                }
                
        ans = validated[fid]
        val = ans.get("value")
        
        # 2. Normalize whitespace, smart quotes, dashes
        if isinstance(val, str):
            val_clean = re.sub(r'[\u2018\u2019]', "'", val)
            val_clean = re.sub(r'[\u201c\u201d]', '"', val_clean)
            val_clean = re.sub(r'[\u2013\u2014\u2212–—]', '-', val_clean)
            val_clean = val_clean.replace("\u00a0", " ")
            val_clean = re.sub(r'\s+', ' ', val_clean).strip()
            
            if not val_clean:
                recovered = _attempt_field_resolution_fallback(f, run_context, session_memory, persona)
                if recovered:
                    ans.update(recovered)
                elif not is_required:
                    ans["value"] = None
                    ans["status"] = "SKIPPED"
                    ans["reason"] = "Optional field left blank"
                else:
                    ans["value"] = None
                    ans["status"] = "UNANSWERABLE"
                    ans["reason"] = "Required field received empty or whitespace-only value"
            else:
                ans["value"] = val_clean
                if options and role in ["radio", "combobox", "select"]:
                    if val_clean not in options:
                        val_norm = val_clean.lower()
                        cand_options = []
                        for opt in options:
                            opt_norm = opt.lower()
                            # Polarity protection
                            if "agree" in val_norm and not any(k in val_norm for k in ["disagree", "not", "un"]):
                                if any(k in opt_norm for k in ["disagree", "not", "un"]):
                                    continue
                            if any(k in val_norm for k in ["disagree", "not", "un"]):
                                if "agree" in opt_norm and not any(k in opt_norm for k in ["disagree", "not", "un"]):
                                    continue
                            # Gender protection
                            if val_norm == "male" and "female" in opt_norm:
                                continue
                            if val_norm == "female" and opt_norm == "male":
                                continue
                            cand_options.append(opt)
                        if cand_options:
                            best = process.extractOne(val_clean, cand_options, scorer=fuzz.WRatio)
                            if best and best[1] >= 60:
                                ans["value"] = best[0]
                                
        # 3. Sanitize unsupported personal inferences / stereotyping
        ans = check_and_sanitize_unsupported_personal_inference(f, ans, run_context, session_memory, persona)
        validated[fid] = ans

        # 4. Check required field integrity
        val_final = ans.get("value")
        status_final = ans.get("status", "").upper()
        if is_required:
            if val_final is None or (isinstance(val_final, str) and not val_final.strip()) or status_final in ["UNANSWERABLE", "NEEDS_CLARIFICATION", "INVALID", "UNRESOLVED"]:
                recovered = _attempt_field_resolution_fallback(f, run_context, session_memory, persona)
                if recovered and recovered.get("value") is not None and str(recovered.get("value")).strip() != "" and recovered.get("status", "").upper() not in ["UNANSWERABLE", "NEEDS_CLARIFICATION", "INVALID", "UNRESOLVED"]:
                    ans.update(recovered)
                else:
                    is_complete = False
                    unresolved_required.append({
                        "id": fid,
                        "label": f.get("label", "Unknown"),
                        "status": ans.get("status", status_final),
                        "reason": ans.get("reason", "Required field unresolved")
                    })
                
    return validated, is_complete, unresolved_required

async def _get_form_answers_single_chunk(
    fields: List[Dict],
    session_memory: dict,
    validation_errors: List[str] = None,
    persona: str = None,
    run_context: Optional[RunContext] = None
):
    if not fields:
        return {}
        
    from datetime import datetime
    now = datetime.now()
    current_date_str = now.strftime('%B %d, %Y')
    current_year = now.year

    active_persona = persona if persona else config.USER_PERSONA
    
    # --- PHASE SIX: Token Optimisation via Deterministic Resolution, Research, and Caching ---
    resolved_answers = {}
    fields_needing_ai = []
    
    for f in fields:
        fid = f.get("id")
        fp = f.get("fingerprint") or ""
        
        # 1. Deterministic profile & instruction lookup
        if run_context:
            norm_q = run_context.question_inventory.get(fp)
            if not norm_q:
                norm_q = NormalizedQuestion(
                    fingerprint=fp,
                    id=fid,
                    role=f.get("role", "textbox"),
                    label=f.get("label", ""),
                    options=f.get("options", []),
                    type=f.get("type", "text"),
                    required=f.get("required", False)
                )
            det_res = run_context.resolve_deterministic_answer(norm_q)
            if det_res:
                det_val, det_src = det_res
                print(f"⚡ Deterministic Hit [{fid}]: '{det_val}' ({det_src}) - 0 AI tokens used")
                resolved_answers[fid] = {
                    "value": det_val,
                    "confidence": 1.0,
                    "source": "instruction" if "instruction" in det_src else "persona",
                    "status": "ANSWERED",
                    "reason": f"Resolved from {det_src}"
                }
                if fp:
                    run_context.question_cache[fp] = resolved_answers[fid]
                continue
                
            # 2. In-run Question Cache reuse (when no validation errors and options match)
            if not validation_errors:
                cached = run_context.get_cached_answer(norm_q)
                if cached:
                    print(f"📦 Question Cache Hit [{fid}]: '{cached.get('value')}' (saved AI call)")
                    run_context.token_metrics["cache_hits"] += 1
                    resolved_answers[fid] = dict(cached)
                    continue

        # 3. Public Fact & Geographic Research
        research_ans = resolve_public_fact_or_research(f, session_memory)
        if research_ans:
            print(f"🔬 Public Research Hit [{fid}]: '{research_ans['value']}' - 0 AI tokens used")
            resolved_answers[fid] = research_ans
            if run_context and fp:
                run_context.question_cache[fp] = research_ans
            continue

        # 4. Permitted Estimates for Approximation Questions
        est_ans = resolve_permitted_estimate(f, session_memory, active_persona)
        if est_ans:
            print(f"📏 Permitted Estimate Hit [{fid}]: '{est_ans['value']}' - 0 AI tokens used")
            resolved_answers[fid] = est_ans
            if run_context and fp:
                run_context.question_cache[fp] = est_ans
            continue

        fields_needing_ai.append(f)
        
    if not fields_needing_ai:
        val_ans, _, _ = validate_answer_set(fields, resolved_answers, run_context=run_context, session_memory=session_memory, persona=active_persona)
        return val_ans

    DynamicSchema = generate_dynamic_schema(fields_needing_ai)
    
    prompt = f"""
    You are an autonomous form-filling agent acting on behalf of this persona:
    {active_persona}
    
    TODAY'S DATE AND CURRENT YEAR:
    Today is {current_date_str} (Current Year: {current_year}).
    
    Session Memory (Previous Answers):
    {json.dumps(session_memory, indent=2)}
    
    Current Page Fields (HTML):
    {json.dumps(fields_needing_ai, indent=2)}
    
    TRUTHFULNESS & FACTUAL INTEGRITY MANDATE (PRIMARY PRINCIPLE):
    1. Truthfulness MUST take precedence over completion rate.
    2. Never invent personal experiences, demographics, employment details, addresses, qualifications, purchase history, or opinions.
    3. Never infer a factual personal response solely from age, location, occupation, or demographic labels unless explicitly supplied by the respondent.
    4. STRICT PROHIBITION AGAINST DEMOGRAPHIC & CAREER STEREOTYPING:
       - Never infer education level or degree (e.g. assuming a director has a Master's degree) from job title, income, or age.
       - Never infer lifestyle habits (smoking frequency, alcohol consumption, diet) from age, location, or job role.
       - Never infer gender, sexual orientation, marital status, or race from occupation or hobbies.
       - Never infer years of experience or tenure from current job title unless mathematically confirmed by explicit employment history.
    5. NEUTRAL OPTION MANDATE:
       - If a personal question is asked and the confirmed persona/profile does NOT specify the fact, check if neutral options exist such as 'Prefer not to say', 'Prefer not to answer', 'None of the above', or 'Not applicable'. If available, select the neutral option.
       - If NO neutral option exists and the field is required, do NOT invent a demographic fact or make assumptions based on stereotypes. Mark status="UNANSWERABLE" or "NEEDS_CLARIFICATION" with value=null so human assistance can be requested, rather than inventing personal facts.
    6. Allow legitimate optional fields to remain blank (status="SKIPPED", value=null) when no truthful response is known.
    7. Answer screener and eligibility questions truthfully according to confirmed respondent facts. NEVER fabricate qualifications or conditions to bypass screeners.
    8. For non-personal preference or product evaluation questions where persona sentiment is established, use coherent contextual deduction (source="deduced", status="ANSWERED"). Maintain absolute consistency with Session Memory.
    
    RESIDENTIAL ADDRESS & LOCATION POLICY:
    - Only supply address or postal details if they are explicitly part of the authorized persona or session memory.
    - If the respondent's exact street address or postal code has not been provided, do NOT fabricate a real or fake residential street address. Mark status="UNANSWERABLE" or "NEEDS_CLARIFICATION" with value=null.

    DATE OF BIRTH & AGE STRICT CONSISTENCY MANDATE:
    If any question asks for a Date of Birth (DOB) or Birthdate (for yourself, a child, or family member):
    - Check Session Memory for their previously stated Age (e.g. if a child's age was answered as 7 years old).
    - You MUST calculate the birth year with 100% mathematical precision:
      Birth Year = {current_year} - Age (adjusted for the month/day relative to today's date).
    - NEVER output an inconsistent birth year! Surveys cross-validate DOB against age and will disqualify you for discrepancies.

    HONEST & COHERENT ANSWER SELECTION:
    - Do not blindly avoid 'Other' when 'Other' is the truthful or appropriate response for the respondent.
    - If none of the predefined options fit the confirmed persona or facts, select 'Other' and provide a brief truthful specification if required.

    QUESTIONNAIRE, MATRIX & LIKERT SCALE INTELLIGENCE MANDATE:
    When answering survey evaluation grids, matrix statements, rating scales, or Likert questions:
    1. READ AND INTERPRET EVERY SINGLE STATEMENT:
       - You MUST carefully read each statement and evaluate its exact semantic meaning.
       - NEVER blindly pick 'Agree' or 'Strongly Agree' across every row! Straight-lining is an instant red flag.
    2. REVERSE-CODED & INVERTED POLARITY STATEMENTS (CRITICAL):
       - Positive statement: If you established a positive attitude and statement is positive, select 'Agree' or 'Strongly Agree'.
       - Inverted/Negative statement: If you support something, and a statement is framed negatively (e.g., "The platform is confusing and frustrating"), you MUST select 'Disagree' or 'Strongly Disagree'!
    3. CROSS-QUESTION LOGICAL COHERENCE:
       - Constantly check 'Session Memory' to ensure your opinions, ratings, and choices remain 100% logically consistent.
    4. NATURAL SENTIMENT VARIATION (NO STRAIGHT-LINING):
       - Human respondents naturally have nuanced opinions. Provide an authentic, non-uniform distribution across a multi-statement grid.
    5. ATTENTION CHECKS & HONESTY TRAPS:
       - If a statement contains an explicit trap instruction (e.g., "To ensure you are paying attention, please select 'Somewhat Disagree'"), follow it exactly.
    6. PUBLIC FACTS, MATHEMATICS & GENERAL KNOWLEDGE:
       - For objective public facts, mathematics (e.g. "What is 7 + 8?"), geography, world capitals, science, definitions, or attention checks that do not ask for private personal facts, you MUST resolve them using public factual reasoning (source="research", status="ANSWERED").
       - Never mark non-personal public factual questions as UNANSWERABLE.
    """
    
    schema_json = json.dumps(DynamicSchema.model_json_schema(), indent=2)
    prompt += f"\n\nCRITICAL: You MUST return ONLY a raw JSON object that exactly matches this schema:\n{schema_json}\nDo not include any conversational text or markdown code blocks (no ```json). ONLY return the raw JSON braces."
    
    if validation_errors:
        prompt += f"\n\nWARNING: The last submission failed with these errors. Fix your answers:\n{json.dumps(validation_errors)}"
        
    raw_response = ""
    for attempt in range(2):
        print(f"🧠 Querying AI Engine (poolot-standard on Lily, batch of {len(fields_needing_ai)} fields, attempt {attempt + 1})...")
        try:
            if run_context:
                run_context.token_metrics["ai_calls"] += 1
                # Estimate prompt tokens roughly
                run_context.token_metrics["prompt_tokens_est"] += len(prompt) // 4
                
            response = await client.chat.completions.create(
                model="poolot-standard",
                messages=[
                    {"role": "system", "content": "You are an expert form filler. Map answers meticulously. Output ONLY valid JSON."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.3
            )
            raw_content = response.choices[0].message.content if (response and response.choices and len(response.choices) > 0) else None
            if raw_content and raw_content.strip():
                raw_response = raw_content.strip()
                if run_context:
                    run_context.token_metrics["completion_tokens_est"] += len(raw_response) // 4
                break
        except Exception as e:
            print(f"⚠️ AI call error on attempt {attempt + 1}: {e}")
            await asyncio.sleep(1)
            
    if not raw_response:
        print("⚠️ AI Engine returned empty response content after retries. Returning fallback.")
        return resolved_answers
        
    if raw_response.startswith("```json"):
        raw_response = raw_response[7:-3].strip()
    elif raw_response.startswith("```"):
        raw_response = raw_response[3:-3].strip()
        
    try:
        json_data = json.loads(raw_response)
        if isinstance(json_data, dict):
            for f in fields_needing_ai:
                fid = f.get("id")
                if fid in json_data:
                    val = json_data[fid]
                    if not isinstance(val, dict):
                        json_data[fid] = {
                            "value": val,
                            "confidence": 1.0,
                            "source": "inferred",
                            "status": "answered",
                            "reason": "Contextual deduction"
                        }
                    elif "value" not in val:
                        json_data[fid] = {
                            "value": str(val),
                            "confidence": 1.0,
                            "source": "inferred",
                            "status": "answered",
                            "reason": "Contextual deduction"
                        }
        answers = DynamicSchema(**json_data).model_dump()
        for fid, a in answers.items():
            r = a.get("reason")
            val = a.get("value")
            if r and r != "Contextual deduction based on established persona and memory.":
                print(f"💡 Coherent Deduction [{fid}]: '{val}' (Reason: {r})")
    except Exception as e:
        print(f"⚠️ Pydantic schema validation note: {e}")
        answers = {}
        try:
            if isinstance(json_data, dict):
                for f in fields_needing_ai:
                    fid = f.get("id")
                    if fid in json_data:
                        v = json_data[fid]
                        val_actual = v.get("value") if isinstance(v, dict) else v
                        answers[fid] = {
                            "value": val_actual,
                            "confidence": 1.0,
                            "source": "inferred",
                            "status": "answered",
                            "reason": "Fallback parsing"
                        }
        except Exception:
            pass
    
    # --- PHASE SEVEN: Strict Answer Validation & Fuzzy Match Post-Processing ---
    for f in fields_needing_ai:
        field_id = f.get("id")
        options = f.get("options", [])
        role = f.get("role", "textbox")
        
        if field_id in answers:
            raw_val = answers[field_id]["value"]
            
            # 1. Single Choice Radio or Select
            if (role in ["radio", "combobox", "select"] or f.get("type") in ["radio", "select"]) and options:
                # If value is string and not in options, validate with fuzzy matching
                if isinstance(raw_val, str) and raw_val not in options:
                    best_match = process.extractOne(raw_val, options, scorer=fuzz.WRatio)
                    if best_match and best_match[1] >= 60:
                        print(f"🔍 Validated and aligned '{raw_val}' -> '{best_match[0]}' (Score: {best_match[1]})")
                        answers[field_id]["value"] = best_match[0]
                    else:
                        print(f"⚠️ Value '{raw_val}' not found in options {options}. Preserving unresolved state.")
                        answers[field_id]["status"] = "unresolved"
                        answers[field_id]["confidence"] = 0.0
                        
            # 2. Multi-Select Checkbox Group
            elif role == "checkbox" and options:
                if isinstance(raw_val, list):
                    valid_items = []
                    for item in raw_val:
                        if item in options:
                            valid_items.append(item)
                        else:
                            best_match = process.extractOne(str(item), options, scorer=fuzz.WRatio)
                            if best_match and best_match[1] >= 65:
                                valid_items.append(best_match[0])
                    answers[field_id]["value"] = valid_items

            # Cache the valid answer (do not cache unresolved/unanswerable answers)
            if (
                run_context 
                and answers[field_id].get("status") not in ["unresolved", "UNANSWERABLE", "NEEDS_CLARIFICATION", "INVALID"] 
                and answers[field_id].get("value") is not None
            ):
                fp = f.get("fingerprint")
                lid = f.get("logical_id")
                if fp:
                    run_context.question_cache[fp] = answers[field_id]
                if lid:
                    run_context.question_cache[lid] = answers[field_id]

    resolved_answers.update(answers)
    final_chunk_answers, is_complete, unresolved_req = validate_answer_set(fields, resolved_answers, run_context=run_context, session_memory=session_memory, persona=active_persona)
    
    # Cache all validated, completed answers
    if run_context:
        for f in fields:
            fid = f.get("id")
            if fid in final_chunk_answers:
                fa = final_chunk_answers[fid]
                if fa.get("status") not in ["unresolved", "UNANSWERABLE", "NEEDS_CLARIFICATION", "INVALID"] and fa.get("value") is not None:
                    fp = f.get("fingerprint")
                    lid = f.get("logical_id")
                    if fp:
                        run_context.question_cache[fp] = fa
                    if lid:
                        run_context.question_cache[lid] = fa

    return final_chunk_answers

async def get_form_answers(
    fields: List[Dict],
    session_memory: dict,
    validation_errors: List[str] = None,
    persona: str = None,
    run_context: Optional[RunContext] = None
):
    """
    Main entry point for AI form filling.
    If fields count is large (> 20), automatically chunks fields into batches of 20
    so LLM context/output token limits are never exceeded and empty responses are avoided.
    Enforces the Zero-Unresolved Output contract via validate_answer_set.
    """
    if not fields:
        return {}
        
    CHUNK_SIZE = 20
    if len(fields) > CHUNK_SIZE:
        print(f"📦 Large form detected ({len(fields)} fields). Processing in chunks of {CHUNK_SIZE} for 100% reliability...")
        all_answers = {}
        combined_memory = dict(session_memory)
        for i in range(0, len(fields), CHUNK_SIZE):
            if i > 0:
                await asyncio.sleep(1.0)
            chunk = fields[i:i + CHUNK_SIZE]
            chunk_ans = await _get_form_answers_single_chunk(chunk, combined_memory, validation_errors, persona, run_context=run_context)
            all_answers.update(chunk_ans)
            for cid, cval in chunk_ans.items():
                matching_f = next((f for f in chunk if f.get("id") == cid), None)
                if matching_f and "value" in cval and cval["value"] is not None:
                    combined_memory[matching_f.get("label", cid)] = cval["value"]
        validated_all, _, _ = validate_answer_set(fields, all_answers, run_context=run_context, session_memory=combined_memory, persona=persona)
        return validated_all
    else:
        ans = await _get_form_answers_single_chunk(fields, session_memory, validation_errors, persona, run_context=run_context)
        validated_all, _, _ = validate_answer_set(fields, ans, run_context=run_context, session_memory=session_memory, persona=persona)
        return validated_all

def encode_image(image_path):
    with open(image_path, "rb") as image_file:
        return base64.b64encode(image_file.read()).decode('utf-8')

async def get_vision_form_answers(image_path: str, num_boxes: int, session_memory: dict):
    """
    Sends the screenshot with numbered bounding boxes to the Vision AI.
    """
    print("👁️ Querying Vision AI Engine (Requires a Vision-capable model)...")
    base64_image = encode_image(image_path)
    
    class BoxAction(BaseModel):
        box_id: int = Field(..., description="The ID number of the red bounding box on the image.")
        value: str = Field(..., description="The value to type or click for this box. For checkboxes/radios, return 'CLICK'.")
        confidence: float
        source: Literal["persona", "inferred", "gap"]
        
    class VisionResponse(BaseModel):
        actions: List[BoxAction]
        
    prompt = f"""
    You are an autonomous form-filling agent acting on behalf of this persona:
    {config.USER_PERSONA}
    
    Session Memory: {json.dumps(session_memory, indent=2)}
    
    Look at the provided screenshot. There are {num_boxes} red bounding boxes with numbers.
    For each numbered box that corresponds to a form input, provide the value that should be filled.
    If the box is a radio button, checkbox, or button to submit, set the value to 'CLICK'.
    Ignore boxes that are just text or labels and do not require interaction.
    
    CRITICAL INSTRUCTION: Truthfulness takes precedence. Never invent personal facts or demographics.
    If a field requires information not in the persona, set source='gap' and do not invent arbitrary data.
    """
    
    try:
        response = await client.beta.chat.completions.parse(
            model="poolot-vision",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{base64_image}"
                            }
                        }
                    ]
                }
            ],
            response_format=VisionResponse,
        )
        return response.choices[0].message.parsed.model_dump()
    except Exception as e:
        print(f"❌ Vision API failed: {e}")
        return {"actions": []}

async def extract_instructions_from_image(image_path: str) -> str:
    """Uses Vision LLM to extract instructions, rules, or guides from an uploaded image."""
    print("👁️ Analyzing uploaded image for instructions...")
    try:
        base64_image = encode_image(image_path)
        system_prompt = (
            "You are an AI assistant. The user has uploaded an image (such as a screenshot of rules, "
            "a guide, a rubric, or instructions) to guide an autonomous form-filling bot. "
            "Your task is to extract all the rules, guidelines, facts, or instructions from this image. "
            "Do NOT treat this as a persona unless it explicitly represents one. "
            "Write out the explicit instructions, rules, or context found in the image clearly so "
            "the bot can use it to intelligently deduce the best answers for a questionnaire."
        )
        response = await client.chat.completions.create(
            model="poolot-vision",
            messages=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Extract all instructions, context, or guides from this image."},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}},
                    ]
                }
            ],
            temperature=0.2,
            max_tokens=1024,
        )
        extracted_text = response.choices[0].message.content.strip()
        print(f"✅ Vision extracted instructions:\n{extracted_text}")
        return extracted_text
    except Exception as e:
        print(f"❌ Vision Instruction Extraction Error: {e}")
        return ""
