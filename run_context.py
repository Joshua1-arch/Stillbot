import hashlib
import json
import re
from enum import Enum
from typing import Dict, List, Optional, Any, Tuple
from pydantic import BaseModel, Field

class PageState(str, Enum):
    INITIALISING = "INITIALISING"
    LOADING = "LOADING"
    QUESTION_DISCOVERY = "QUESTION_DISCOVERY"
    ANSWER_GENERATION = "ANSWER_GENERATION"
    FILLING = "FILLING"
    VERIFYING = "VERIFYING"
    WAITING_FOR_PAGE_CHANGE = "WAITING_FOR_PAGE_CHANGE"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    RECOVERABLE_ERROR = "RECOVERABLE_ERROR"
    COMPLETED = "COMPLETED"
    DRY_RUN_COMPLETED = "DRY_RUN_COMPLETED"
    DISQUALIFIED = "DISQUALIFIED"
    BLOCKED = "BLOCKED"
    HUMAN_REVIEW_REQUIRED = "HUMAN_REVIEW_REQUIRED"
    TIMED_OUT = "TIMED_OUT"
    FAILED = "FAILED"

class AnswerStatus(str, Enum):
    UNANSWERED = "UNANSWERED"
    GENERATED = "GENERATED"
    FILLED = "FILLED"
    VERIFIED = "VERIFIED"
    INAPPLICABLE = "INAPPLICABLE"
    VALIDATION_FAILED = "VALIDATION_FAILED"

def compute_logical_question_id(frame_idx: int, container_key: str, role: str, label: str, matrix_row: str = "") -> str:
    """
    Computes a stable logical question identifier.
    Distinguishes separate questions even if they share identical wording by incorporating
    container identity, section context, and matrix row identity.
    """
    norm_role = (role or "textbox").strip().lower()
    norm_label = re.sub(r'\s+', ' ', (label or "").strip().lower())
    norm_cont = re.sub(r'\s+', ' ', (container_key or "").strip().lower())
    norm_row = re.sub(r'\s+', ' ', (matrix_row or "").strip().lower())
    raw = f"{frame_idx}|{norm_cont}|{norm_role}|{norm_label}|{norm_row}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

def compute_options_signature(options: List[str] = None) -> str:
    """Computes a hash signature of the question's available options to track revisions."""
    norm_opts = ",".join(sorted([re.sub(r'\s+', ' ', (o or "").strip().lower()) for o in (options or [])]))
    return hashlib.sha256(norm_opts.encode("utf-8")).hexdigest()[:12]

def compute_question_fingerprint(frame_idx: int, role: str, label: str, options: List[str] = None, container_key: str = "", matrix_row: str = "") -> str:
    """
    Full fingerprint combining logical question identity and current options signature.
    Changes whenever the question wording, options, or container context changes.
    """
    qid = compute_logical_question_id(frame_idx, container_key, role, label, matrix_row)
    opts_sig = compute_options_signature(options)
    return f"{qid}:{opts_sig}"

class NormalizedQuestion(BaseModel):
    logical_id: str = ""
    fingerprint: str = ""
    id: str = ""  # live DOM locator id (transient per scrape)
    real_id: str = ""
    name: str = ""
    role: str = "textbox"
    type: str = "text"
    label: str = ""
    options: List[str] = Field(default_factory=list)
    options_signature: str = ""
    option_map: Dict[str, str] = Field(default_factory=dict)
    required: bool = False
    is_matrix: bool = False
    matrix_row: str = ""
    container_key: str = ""
    status: AnswerStatus = AnswerStatus.UNANSWERED
    verified_value: Any = None
    verification_error: Optional[str] = None
    applied_instruction_source: Optional[str] = None
    revision: int = 1

    def model_post_init(self, __context: Any) -> None:
        if not self.logical_id:
            if self.fingerprint and ":" in self.fingerprint:
                self.logical_id = self.fingerprint.split(":")[0]
            elif self.fingerprint:
                self.logical_id = self.fingerprint
            else:
                self.logical_id = compute_logical_question_id(0, self.container_key, self.role, self.label, self.matrix_row)
        if not self.fingerprint:
            if not self.options_signature and self.options:
                self.options_signature = compute_options_signature(self.options)
            self.fingerprint = f"{self.logical_id}:{self.options_signature}"
        elif not self.options_signature and ":" in self.fingerprint:
            self.options_signature = self.fingerprint.split(":", 1)[1]

class InstructionPrecedence:
    """
    Authoritative instruction resolver enforcing strict hierarchy:
    1. System safety & platform constraints (truthful answering, valid types)
    2. Confirmed respondent facts & explicit profile overrides
    3. Applicable survey-specific instructions (e.g. user prompt for current run)
    4. User-defined global writing & answer preferences
    5. Default autonomous agent behavior
    """
    @staticmethod
    def resolve_fact(key: str, confirmed_profile: dict, user_instructions: str, global_prefs: dict = None) -> Tuple[Optional[Any], str]:
        key_lower = key.lower().strip()
        
        key_variants = [key_lower]
        if "_" in key_lower:
            key_variants.append(key_lower.replace("_", " "))
        elif " " in key_lower:
            key_variants.append(key_lower.replace(" ", "_"))

        # 1. Current run explicit user instruction
        if user_instructions:
            for k_var in key_variants:
                patterns = [
                    (r'\b(?:my\s+|target\s+|desired\s+|current\s+|preferred\s+)?' + re.escape(k_var) + r'\s*(?:is|=|:)\s*([A-Za-z0-9_\s,-]+?)(?:\.|$|,|\n)', 1),
                    (r'\b(?:set|use)\s+(?:target\s+|desired\s+|current\s+|preferred\s+)?' + re.escape(k_var) + r'\s*(?:to|=)?\s*([A-Za-z0-9_\s,-]+?)(?:\.|$|,|\n)', 1)
                ]
                for pat, group in patterns:
                    m = re.search(pat, user_instructions, re.IGNORECASE)
                    if m:
                        val = m.group(group).strip()
                        return val, "survey_specific_instruction"

        # 2. Confirmed respondent facts
        if confirmed_profile:
            for k_var in key_variants:
                for p_k, p_v in confirmed_profile.items():
                    p_k_clean = p_k.lower().strip()
                    if p_k_clean == k_var:
                        if p_v is not None and str(p_v).strip() != "":
                            return p_v, "confirmed_respondent_fact"
            for k_var in key_variants:
                for p_k, p_v in confirmed_profile.items():
                    p_k_clean = p_k.lower().strip()
                    if re.search(r'\b' + re.escape(k_var) + r'\b', p_k_clean.replace('_', ' ')):
                        if p_v is not None and str(p_v).strip() != "":
                            return p_v, "confirmed_respondent_fact"

        # 3. Global preferences
        if global_prefs and key_lower in global_prefs:
            return global_prefs[key_lower], "global_user_preference"

        return None, "default_agent_behavior"

class RunContext(BaseModel):
    run_id: str
    survey_url: str = ""
    platform: str = "generic"
    account_id: Optional[str] = None
    confirmed_profile: Dict[str, Any] = Field(default_factory=dict)
    user_instructions: str = ""
    global_preferences: Dict[str, Any] = Field(default_factory=dict)
    
    # State tracking
    page_state: PageState = PageState.INITIALISING
    page_num: int = 1
    consecutive_unchanged_passes: int = 0
    max_unchanged_passes: int = 4
    last_page_signature: str = ""
    same_page_pass_count: int = 0
    max_same_page_passes: int = 3

    def check_progress_watchdog(self, url: str, dom_fingerprint: str) -> Tuple[bool, int]:
        """
        Progress Watchdog:
        Tracks URL and DOM content signature. If the same page remains unchanged
        after max_same_page_passes (default 3), halts autonomous retries and
        prevents endless loops and incorrect page_num increments.
        Returns (is_stalled, same_page_pass_count).
        """
        sig_raw = f"{url}|{dom_fingerprint}"
        current_sig = hashlib.sha256(sig_raw.encode("utf-8")).hexdigest()[:16]
        if current_sig == self.last_page_signature:
            self.same_page_pass_count += 1
        else:
            self.last_page_signature = current_sig
            self.same_page_pass_count = 1
        
        is_stalled = (self.same_page_pass_count >= self.max_same_page_passes)
        return is_stalled, self.same_page_pass_count

    def reset_watchdog(self):
        """Resets the progress watchdog after a real page transition."""
        self.last_page_signature = ""
        self.same_page_pass_count = 0
    
    # Inventory: keyed by stable logical_id
    question_inventory: Dict[str, NormalizedQuestion] = Field(default_factory=dict)
    
    # Cache: logical_id -> { "value": ..., "confidence": ..., "source": ..., "options_sig": ..., "facts_hash": ... }
    question_cache: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    
    # Verified answers across entire session: label/logical_id -> value
    verified_answers: Dict[str, Any] = Field(default_factory=dict)
    
    # Metrics
    token_metrics: Dict[str, int] = Field(default_factory=lambda: {
        "ai_calls": 0,
        "prompt_tokens_est": 0,
        "completion_tokens_est": 0,
        "cache_hits": 0,
        "cache_invalidations": 0,
        "deterministic_hits": 0,
        "validation_failures": 0
    })

    def transition_state(self, new_state: PageState, reason: str = ""):
        old_state = self.page_state
        self.page_state = new_state
        print(f"🚦 [State Transition] {old_state.value} ➡️ {new_state.value} {f'({reason})' if reason else ''}")

    def register_discovered_questions(self, raw_fields: List[Dict[str, Any]], frame_idx: int = 0) -> List[NormalizedQuestion]:
        """
        Takes raw fields from scraper, normalizes them, and registers them into question_inventory.
        Distinguishes separate questions even with identical text via container_key/matrix_row.
        Detects if a question's options materially changed and triggers cache invalidation.
        Returns list of newly discovered or materially changed questions requiring processing.
        """
        unresolved_or_changed = []
        for f in raw_fields:
            role = f.get("role", "textbox")
            label = f.get("label", "Question")
            opts = f.get("options", [])
            cont_key = f.get("container_key", "")
            matrix_row = f.get("matrix_row", "")
            
            # Derive logical ID and options signature
            lid = f.get("logical_id") or compute_logical_question_id(frame_idx, cont_key, role, label, matrix_row)
            opts_sig = compute_options_signature(opts)
            fp = f.get("fingerprint") or f"{lid}:{opts_sig}"
            
            if lid in self.question_inventory:
                existing = self.question_inventory[lid]
                # Update transient locator information
                existing.id = f.get("id", existing.id)
                existing.option_map = f.get("option_map", existing.option_map)
                existing.required = f.get("required", existing.required)
                
                # Check if available options or wording changed (e.g. conditional branch update)
                if existing.options_signature != opts_sig:
                    print(f"🔄 Options materially changed for [{existing.label[:40]}]. Invalidating stale cache.")
                    old_fp = existing.fingerprint
                    if old_fp in self.question_inventory:
                        del self.question_inventory[old_fp]
                    existing.options = opts
                    existing.options_signature = opts_sig
                    existing.fingerprint = fp
                    self.question_inventory[fp] = existing
                    existing.revision += 1
                    existing.status = AnswerStatus.UNANSWERED
                    existing.verified_value = None
                    self.invalidate_cache(lid)
                    unresolved_or_changed.append(existing)
            else:
                norm_q = NormalizedQuestion(
                    logical_id=lid,
                    fingerprint=fp,
                    id=f.get("id", ""),
                    real_id=f.get("real_id", ""),
                    name=f.get("name", ""),
                    role=role,
                    type=f.get("type", "text"),
                    label=label,
                    options=opts,
                    options_signature=opts_sig,
                    option_map=f.get("option_map", {}),
                    required=f.get("required", False),
                    is_matrix=f.get("is_matrix", False),
                    matrix_row=matrix_row,
                    container_key=cont_key,
                    status=AnswerStatus.UNANSWERED
                )
                self.question_inventory[lid] = norm_q
                self.question_inventory[fp] = norm_q
                unresolved_or_changed.append(norm_q)
                
        return unresolved_or_changed

    def get_cached_answer(self, q: NormalizedQuestion) -> Optional[Dict[str, Any]]:
        """
        Retrieves a cached answer only if strict validity criteria are met:
        1. Logical question ID matches.
        2. Available options match or contain the cached answer.
        3. Answer satisfies current question type.
        """
        if q.logical_id not in self.question_cache:
            return None
            
        cached = self.question_cache[q.logical_id]
        val = cached.get("value")
        
        # If question has options (radio, select, checkbox), ensure cached value is valid within current options
        if q.options and q.role in ["radio", "combobox", "select"]:
            val_clean = str(val).strip()
            if val_clean not in q.options:
                print(f"⚠️ Cache entry '{val_clean}' no longer in options {q.options} for [{q.label[:30]}]. Invalidating.")
                self.invalidate_cache(q.logical_id)
                return None
        elif q.options and q.role == "checkbox":
            if isinstance(val, list):
                if any(str(v).strip() not in q.options for v in val):
                    self.invalidate_cache(q.logical_id)
                    return None
                    
        return cached

    def invalidate_cache(self, logical_id: str):
        if logical_id in self.question_cache:
            del self.question_cache[logical_id]
            self.token_metrics["cache_invalidations"] += 1

    def clear_cache(self):
        """Ensures complete run isolation by clearing all cached answers."""
        self.question_cache.clear()

    def get_unanswered_questions(self) -> List[NormalizedQuestion]:
        """Returns all questions that are not verified or inapplicable."""
        seen = set()
        unanswered = []
        for q in self.question_inventory.values():
            if id(q) not in seen:
                seen.add(id(q))
                if q.status not in [AnswerStatus.VERIFIED, AnswerStatus.INAPPLICABLE]:
                    unanswered.append(q)
        return unanswered

    def mark_verified(self, key: str, value: Any):
        if key in self.question_inventory:
            q = self.question_inventory[key]
            q.status = AnswerStatus.VERIFIED
            q.verified_value = value
            q.verification_error = None
            self.verified_answers[q.label] = value
            self.verified_answers[q.logical_id] = value
            self.verified_answers[q.fingerprint] = value

    def mark_validation_failed(self, key: str, error: str):
        if key in self.question_inventory:
            q = self.question_inventory[key]
            q.status = AnswerStatus.VALIDATION_FAILED
            q.verification_error = error

    def mark_inapplicable(self, key: str):
        if key in self.question_inventory:
            self.question_inventory[key].status = AnswerStatus.INAPPLICABLE

    def validate_completion(self) -> Tuple[bool, List[NormalizedQuestion]]:
        """
        Validates that every required question in question_inventory has reached
        VERIFIED status. Returns (is_complete, unverified_required_questions).
        """
        seen = set()
        unverified_required = []
        for q in self.question_inventory.values():
            if id(q) not in seen:
                seen.add(id(q))
                if q.required and q.status != AnswerStatus.VERIFIED:
                    unverified_required.append(q)
        return len(unverified_required) == 0, unverified_required

    def resolve_deterministic_answer(self, q: NormalizedQuestion) -> Optional[Tuple[Any, str]]:
        """
        Attempts to resolve an answer deterministically from verified profile/instructions
        before calling the AI. Uses word boundaries to avoid false substring matches.
        """
        lbl_lower = q.label.lower()
        
        # 1. Exact Email matching
        if any(k in lbl_lower for k in ["email address", "e-mail", "email"]):
            val, src = InstructionPrecedence.resolve_fact("email", self.confirmed_profile, self.user_instructions, self.global_preferences)
            if val:
                self.token_metrics["deterministic_hits"] += 1
                return val, src
                
        # 2. First Name
        if any(k in lbl_lower for k in ["first name", "given name", "first_name"]):
            val, src = InstructionPrecedence.resolve_fact("first_name", self.confirmed_profile, self.user_instructions, self.global_preferences)
            if not val:
                val, src = InstructionPrecedence.resolve_fact("name", self.confirmed_profile, self.user_instructions, self.global_preferences)
                if val and " " in str(val):
                    val = str(val).split()[0]
            if val:
                self.token_metrics["deterministic_hits"] += 1
                return val, src

        # 3. Last Name
        if any(k in lbl_lower for k in ["last name", "surname", "family name", "last_name"]):
            val, src = InstructionPrecedence.resolve_fact("last_name", self.confirmed_profile, self.user_instructions, self.global_preferences)
            if not val:
                val, src = InstructionPrecedence.resolve_fact("name", self.confirmed_profile, self.user_instructions, self.global_preferences)
                if val and " " in str(val):
                    val = str(val).split()[-1]
            if val:
                self.token_metrics["deterministic_hits"] += 1
                return val, src

        # 4. Full Name
        if any(k in lbl_lower for k in ["full name", "fullname", "your name"]):
            val, src = InstructionPrecedence.resolve_fact("full_name", self.confirmed_profile, self.user_instructions, self.global_preferences)
            if not val:
                val, src = InstructionPrecedence.resolve_fact("name", self.confirmed_profile, self.user_instructions, self.global_preferences)
            if val:
                self.token_metrics["deterministic_hits"] += 1
                return val, src

        # 5. Postal / Zip code
        if any(k in lbl_lower for k in ["zip code", "postal code", "postcode", "zip"]):
            val, src = InstructionPrecedence.resolve_fact("zip_code", self.confirmed_profile, self.user_instructions, self.global_preferences)
            if not val:
                val, src = InstructionPrecedence.resolve_fact("postal_code", self.confirmed_profile, self.user_instructions, self.global_preferences)
            if val:
                self.token_metrics["deterministic_hits"] += 1
                return val, src

        # 6. Gender (with strict word-boundary matching to prevent "male" matching "female")
        if "gender" in lbl_lower or "sex" in lbl_lower:
            val, src = InstructionPrecedence.resolve_fact("gender", self.confirmed_profile, self.user_instructions, self.global_preferences)
            if val and q.options:
                val_str = str(val).lower().strip()
                # 1. Exact match
                for opt in q.options:
                    if val_str == opt.lower().strip():
                        self.token_metrics["deterministic_hits"] += 1
                        return opt, src
                # 2. Word boundary match
                for opt in q.options:
                    if re.search(r'\b' + re.escape(val_str) + r'\b', opt.lower()):
                        self.token_metrics["deterministic_hits"] += 1
                        return opt, src
            elif val:
                self.token_metrics["deterministic_hits"] += 1
                return val, src

        # 7. Age (strictly for respondent)
        if re.search(r'\b(what is your age|how old are you|enter your age|your age|age)\b', lbl_lower):
            if not any(k in lbl_lower for k in ["child", "children", "spouse", "partner", "direct report", "employee", "parent", "mother", "father", "son", "daughter"]):
                val, src = InstructionPrecedence.resolve_fact("age", self.confirmed_profile, self.user_instructions, self.global_preferences)
                if val:
                    self.token_metrics["deterministic_hits"] += 1
                    return val, src

        # 8. Desired Role / Job Title
        if any(k in lbl_lower for k in ["desired role", "job title", "target role", "role"]):
            search_keys = ["target_role", "desired_role", "job_title", "role"] if "target" in lbl_lower else ["desired_role", "job_title", "target_role", "role"]
            val, src = None, None
            for rk in search_keys:
                val, src = InstructionPrecedence.resolve_fact(rk, self.confirmed_profile, self.user_instructions, self.global_preferences)
                if val:
                    break
            if val and q.options:
                val_clean = str(val).lower().strip()
                for opt in q.options:
                    if val_clean == opt.lower().strip() or val_clean in opt.lower().strip():
                        self.token_metrics["deterministic_hits"] += 1
                        return opt, src
            elif val:
                self.token_metrics["deterministic_hits"] += 1
                return val, src

        # 9. Relocation willingness
        if any(k in lbl_lower for k in ["relocate", "relocation"]):
            val, src = InstructionPrecedence.resolve_fact("will you relocate", self.confirmed_profile, self.user_instructions, self.global_preferences)
            if not val:
                val, src = InstructionPrecedence.resolve_fact("relocate", self.confirmed_profile, self.user_instructions, self.global_preferences)
            if val and q.options:
                val_str = str(val).lower().strip()
                for opt in q.options:
                    if val_str in opt.lower().strip() or opt.lower().strip() in val_str:
                        self.token_metrics["deterministic_hits"] += 1
                        return opt, src
            elif val:
                self.token_metrics["deterministic_hits"] += 1
                return val, src

        # 10. Country of residence
        if any(k in lbl_lower for k in ["country of residence", "what country", "your country", "country"]):
            if not any(k in lbl_lower for k in ["cross country", "country music"]):
                val, src = InstructionPrecedence.resolve_fact("country", self.confirmed_profile, self.user_instructions, self.global_preferences)
                if val and q.options:
                    val_str = str(val).lower().strip()
                    for opt in q.options:
                        if val_str == opt.lower().strip() or val_str in opt.lower().strip():
                            self.token_metrics["deterministic_hits"] += 1
                            return opt, src
                elif val:
                    self.token_metrics["deterministic_hits"] += 1
                    return val, src

        # 11. Highest level of education (strictly for respondent)
        if any(k in lbl_lower for k in ["education level", "highest level of education", "highest degree", "degree obtained"]):
            if not any(k in lbl_lower for k in ["child", "children", "spouse", "partner", "parent"]):
                val, src = InstructionPrecedence.resolve_fact("education", self.confirmed_profile, self.user_instructions, self.global_preferences)
                if not val:
                    val, src = InstructionPrecedence.resolve_fact("degree", self.confirmed_profile, self.user_instructions, self.global_preferences)
                if val and q.options:
                    val_str = str(val).lower().strip()
                    for opt in q.options:
                        if val_str == opt.lower().strip() or val_str in opt.lower().strip():
                            self.token_metrics["deterministic_hits"] += 1
                            return opt, src
                elif val:
                    self.token_metrics["deterministic_hits"] += 1
                    return val, src

        return None
