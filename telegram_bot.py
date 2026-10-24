import os
import sys
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', line_buffering=True)
import asyncio
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler
from dotenv import load_dotenv

import config
import re
from main import run_bot

load_dotenv()

# Define conversation states
LINK, EMAIL, INSTRUCTIONS, PROXY_CHOICE = range(4)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 **Welcome to Stillbot Autonomous Filler!**\n\n"
        "Send me a **Questionnaire Link** to begin setup, or use these commands:\n"
        "• `/checkads` — 🔍 Scan Facebook Ad Library for new Shareablee ads\n"
        "• `/screenshot` — 📸 Capture live browser screenshot\n"
        "• `/pool` — 📊 Check email pool count\n"
        "• `/addemails` — 📝 Add new emails in bulk\n\n"
        "👉 Please send me the **Questionnaire Link** you want to fill:",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="Markdown"
    )
    return LINK

from telegram import InlineKeyboardMarkup, InlineKeyboardButton
from email_pool import load_pool, add_emails_to_pool, mark_email_used, get_pool_summary

def build_email_picker_keyboard(page=0, selected_indices=None):
    if selected_indices is None:
        selected_indices = set()
    pool = load_pool()
    PAGE_SIZE = 5
    total = len(pool)
    start_idx = page * PAGE_SIZE
    page_items = pool[start_idx:start_idx + PAGE_SIZE]
    
    keyboard = []
    for offset, item in enumerate(page_items):
        abs_idx = start_idx + offset
        is_sel = abs_idx in selected_indices
        check = "✅ " if is_sel else "⬜ "
        used_tag = " (used)" if item.get("used_count", 0) > 0 else ""
        btn_text = f"{check}{item['email']}{used_tag}"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"toggle_{abs_idx}_{page}")])
        
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"page_{page-1}"))
    if start_idx + PAGE_SIZE < total:
        nav_row.append(InlineKeyboardButton("➡️ Next", callback_data=f"page_{page+1}"))
    if nav_row:
        keyboard.append(nav_row)
        
    keyboard.append([
        InlineKeyboardButton("🎲 Pick 5 Fresh", callback_data="pick_random_5"),
        InlineKeyboardButton("🎲 Pick 1 Fresh", callback_data="pick_random_1")
    ])
    sel_count = len(selected_indices)
    keyboard.append([
        InlineKeyboardButton(f"🚀 Done ({sel_count} Selected)", callback_data="done_email_selection"),
        InlineKeyboardButton("⏭️ Skip / Type Custom", callback_data="skip_email_selection")
    ])
    return InlineKeyboardMarkup(keyboard)

async def add_emails_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Allows user to send /addemails email1@gmail.com, email2@gmail.com, ... or paste a bulk list
    """
    raw_args = " ".join(context.args) if context.args else ""
    if not raw_args:
        await update.message.reply_text(
            "📝 **Bulk Add Emails To Pool:**\n\n"
            "Use `/addemails` followed by your emails separated by commas or newlines:\n"
            "Example:\n"
            "`/addemails user1@gmail.com, user2@gmail.com, user3@gmail.com`\n\n"
            "Or simply reply to this with a text message containing your email list!",
            parse_mode="Markdown"
        )
        return
    emails = re.findall(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+', raw_args)
    if not emails:
        await update.message.reply_text("❌ No valid email addresses found in your message.")
        return
    added, total = add_emails_to_pool(emails)
    await update.message.reply_text(
        f"✅ **Email Pool Updated!**\n"
        f"• Added New: **{added}**\n"
        f"• Total Pool Size: **{total}**"
    )

async def pool_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total, fresh = get_pool_summary()
    await update.message.reply_text(
        f"📊 **Current Email Pool Status:**\n"
        f"• Total Saved: **{total}**\n"
        f"• Fresh (Unused): **{fresh}**\n"
        f"• Used in previous runs: **{total - fresh}**\n\n"
        f"💡 Add more anytime with `/addemails ...`"
    )

async def get_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not (text.startswith("http://") or text.startswith("https://") or text.startswith("www.")):
        await update.message.reply_text("❌ That doesn't look like a valid link. Please start with http:// or https://")
        return LINK
        
    if text.startswith("www."):
        text = "http://" + text
        
    # Automatically unwrap Facebook link shim / redirect wrappers
    if "l.facebook.com" in text:
        import urllib.parse
        parsed = urllib.parse.urlparse(text)
        params = urllib.parse.parse_qs(parsed.query)
        if "u" in params:
            text = urllib.parse.unquote(params["u"][0])
            print(f"🔗 Unwrapped direct survey link: {text}")
        
    context.user_data['url'] = text
    context.user_data['selected_pool_indices'] = set()
    context.user_data['pool_page'] = 0
    
    pool = load_pool()
    if pool:
        total, fresh = get_pool_summary()
        markup = build_email_picker_keyboard(page=0, selected_indices=set())
        await update.message.reply_text(
            f"✅ **Link saved!**\n\n"
            f"📬 **Email Pool Available** ({total} total, {fresh} fresh):\n"
            f"• Tap any email below to toggle selection.\n"
            f"• Use **[ ➡️ Next ]** to browse through pages.\n"
            f"• Tap **[ 🎲 Pick 5 Fresh ]** to instantly select 5 fresh ones.\n"
            f"• Or tap **[ ⏭️ Skip / Type Custom ]** to type your own in the chat!\n\n"
            f"Select your email(s):",
            reply_markup=markup,
            parse_mode="Markdown"
        )
    else:
        reply_markup = ReplyKeyboardMarkup([['skip']], resize_keyboard=True, one_time_keyboard=True)
        await update.message.reply_text(
            "✅ Link saved!\n\n"
            "Next, please send me the **Gmail / Email Address(es)** you want the bot to use.\n"
            "(You can send multiple comma-separated emails, e.g. user1@gmail.com, user2@gmail.com)\n\n"
            "💡 *Tip: You can save emails permanently into your pool using `/addemails ...`*\n\n"
            "If you want the bot to invent a random email or skip, tap **skip**.",
            reply_markup=reply_markup
        )
    return EMAIL

async def handle_email_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass
        
    data = query.data
    pool = load_pool()
    selected_indices = context.user_data.get('selected_pool_indices', set())
    current_page = context.user_data.get('pool_page', 0)

    try:
        if data.startswith("toggle_"):
            parts = data.split("_")
            idx = int(parts[1])
            page = int(parts[2])
            if idx in selected_indices:
                selected_indices.remove(idx)
            else:
                selected_indices.add(idx)
            context.user_data['selected_pool_indices'] = selected_indices
            context.user_data['pool_page'] = page
            markup = build_email_picker_keyboard(page=page, selected_indices=selected_indices)
            try:
                await query.edit_message_reply_markup(reply_markup=markup)
            except Exception:
                pass

        elif data.startswith("page_"):
            page = int(data.split("_")[1])
            context.user_data['pool_page'] = page
            markup = build_email_picker_keyboard(page=page, selected_indices=selected_indices)
            try:
                await query.edit_message_reply_markup(reply_markup=markup)
            except Exception:
                pass

        elif data == "pick_random_5" or data == "pick_random_1":
            count = 5 if data == "pick_random_5" else 1
            fresh_indices = [i for i, item in enumerate(pool) if item.get("used_count", 0) == 0]
            if not fresh_indices:
                fresh_indices = list(range(len(pool)))
            selected_indices = set(fresh_indices[:count])
            context.user_data['selected_pool_indices'] = selected_indices
            markup = build_email_picker_keyboard(page=current_page, selected_indices=selected_indices)
            try:
                await query.edit_message_reply_markup(reply_markup=markup)
            except Exception:
                pass

        elif data == "done_email_selection":
            chosen_emails = [pool[i]["email"] for i in sorted(selected_indices) if i < len(pool)]
            if not chosen_emails:
                context.user_data['email'] = None
                await query.edit_message_text("✅ No emails selected from pool. The bot will invent an email if needed.")
            else:
                context.user_data['email'] = ", ".join(chosen_emails)
                emails_display = "\n".join([f"• `{e}`" for e in chosen_emails])
                await query.edit_message_text(f"✅ **Selected {len(chosen_emails)} Email(s):**\n{emails_display}", parse_mode="Markdown")
            return await prompt_for_instructions(query.message, context)

        elif data == "skip_email_selection":
            await query.edit_message_text("⌨️ **Custom Email Mode:**\nPlease type your email address(es) into the chat below, or type `skip`:")
            return EMAIL
    except Exception as e:
        print(f"⚠️ Email callback handling error: {e}")
        
    return EMAIL

async def prompt_for_instructions(message, context):
    reply_markup = ReplyKeyboardMarkup([['skip'], ['🛑 Cancel / Reset']], resize_keyboard=True, one_time_keyboard=True)
    await message.reply_text(
        "Finally, please send me any **Specific Instructions or Overrides** for this run.\n"
        "(e.g., 'Targeting Utah residents' or custom answers)\n"
        "📸 **OR Upload a Flyer / Screenshot** with the rules!\n\n"
        "💡 *The bot will automatically read your rules and choose the perfect US Residential State IP!*\n\n"
        "If you have no instructions, tap **skip**.",
        reply_markup=reply_markup
    )
    return INSTRUCTIONS

async def get_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    
    if text.lower() == 'skip':
        context.user_data['email'] = None
        await update.message.reply_text("✅ Skipping email (the bot will invent one if needed).")
    else:
        context.user_data['email'] = text
        await update.message.reply_text("✅ Email(s) saved!")
        
    return await prompt_for_instructions(update.message, context)

async def get_instructions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from ai_engine import extract_instructions_from_image
    import os
    
    if update.message.photo:
        await update.message.reply_text("📥 Image received! Downloading and reading rules with Vision AI...")
        
        try:
            photo = update.message.photo[-1]
            file = await context.bot.get_file(photo.file_id, read_timeout=60, write_timeout=60, connect_timeout=60)
            
            os.makedirs("artifacts", exist_ok=True)
            image_path = f"artifacts/telegram_photo_{update.message.message_id}.jpg"
            await file.download_to_drive(image_path)
            
            extracted_text = await extract_instructions_from_image(image_path)
            caption = update.message.caption if update.message.caption else ""
            
            final_override = ""
            if extracted_text:
                final_override += f"[EXTRACTED IMAGE INSTRUCTIONS]\n{extracted_text}\n\n"
            if caption:
                final_override += f"[USER CAPTION INSTRUCTIONS]\n{caption}"
                
            if final_override:
                context.user_data['instructions'] = final_override
                await update.message.reply_text("✅ Rules & context extracted from flyer successfully!")
            else:
                context.user_data['instructions'] = None
                await update.message.reply_text("❌ Failed to extract text from image. Continuing with default rules.")
                
        except Exception as e:
            context.user_data['instructions'] = update.message.caption if update.message.caption else None
            await update.message.reply_text(f"⚠️ Failed to process image due to network timeout: {e}")
            
    else:
        text = update.message.text.strip()
        
        if text.lower() == 'skip':
            context.user_data['instructions'] = None
            await update.message.reply_text("✅ Skipping instructions. Proceeding with default autonomous brain.")
        else:
            context.user_data['instructions'] = text
            await update.message.reply_text("✅ Instructions saved!")
            
    # Prompt user to choose connection mode
    reply_markup = ReplyKeyboardMarkup(
        [
            ['🔄 Two-Stage Proxy (VoiceFive Survey)'],
            ['🌐 Residential Proxy (Geonix 1:1)'],
            ['⚡ Direct Network (No Proxy)'],
            ['🛑 Cancel / Reset']
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    await update.message.reply_text(
        "⚡ **Select Connection Mode:**\n\n"
        "• **🔄 Two-Stage Proxy (VoiceFive Survey)**: *Recommended for VoiceFive!* Starts on Direct Network to pass Verisoul gatekeeper, then dynamically activates US Residential Proxy for all survey questions & location binding.\n"
        "• **🌐 Residential Proxy (Geonix 1:1)**: Standard 1:1 residential proxy from start to finish.\n"
        "• **⚡ Direct Network (No Proxy)**: Runs directly on your local network (zero proxy).\n\n"
        "Choose an option below:",
        reply_markup=reply_markup
    )
    return PROXY_CHOICE

async def start_execution(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target_url = context.user_data.get('url', '')
    
    # CRITICAL GUARD: Prevent AI from trying to fill Facebook Ad Library search engine
    if "facebook.com/ads/library" in target_url:
        await update.message.reply_text(
            "❌ **Invalid Survey Link!**\n\n"
            "This is a Facebook Ad Library page, NOT a survey questionnaire.\n"
            "Please click the ad's CTA button (e.g. 'Take Survey', 'Learn More') and send the real destination survey link (e.g. Qualtrics, VoiceFive, Shareablee).\n\n"
            "Tap /start to begin fresh.",
            reply_markup=ReplyKeyboardMarkup([['/start']], resize_keyboard=True)
        )
        return ConversationHandler.END

    global CURRENT_RUN_TASK
    CURRENT_RUN_TASK = asyncio.current_task()

    choice = update.message.text.strip()
    is_two_stage = "Two-Stage" in choice or "VoiceFive" in choice
    is_direct = not is_two_stage and ("Direct Network" in choice or "No Proxy" in choice)
    use_proxy = not is_direct
    email_str = context.user_data.get('email', "")
    general_inst = context.user_data.get('instructions')
    
    # Combine General Instructions
    base_instructions = ""
    if general_inst:
        base_instructions += f"USER INSTRUCTIONS: {general_inst}\n"
        
    combined_context = f"{target_url} {base_instructions}"
    
    from proxy_router import get_fast_verified_proxy_for_run, detect_target_state
    detected_state = detect_target_state(combined_context)
    state_label = detected_state.title() if detected_state else "General US (100-Port Pool)"
    
    # Parse Emails
    emails = [e.strip() for e in email_str.split(',') if e.strip()] if email_str else [None]
    
    global ACTIVE_RUN_INFO
    from datetime import datetime
    ACTIVE_RUN_INFO = {
        "status": f"Running {len(emails)} run(s) on {target_url}",
        "url": target_url,
        "started_at": datetime.now().strftime("%H:%M:%S")
    }
    
    if is_two_stage:
        await update.message.reply_text(
            f"🔄 **Two-Stage Smart Proxy Active (VoiceFive Survey):**\n"
            f"• Phase 1: **Direct Local Network** (Bypasses Verisoul Anti-Bot Gatekeeper)\n"
            f"• Phase 2: **Dynamic US Residential Proxy** (All survey answers & submissions bind to {state_label})\n"
            f"• Routing Mode: **1:1 Dedicated US IP Per Email**\n"
            f"• Total Runs: **{len(emails)}**\n\n"
            f"🚀 Booting Autonomous Bot with two-stage dynamic proxy bridge..."
        )
    elif use_proxy:
        await update.message.reply_text(
            f"📍 **Residential Proxy Mode Active:**\n"
            f"• Target Region: **{state_label}**\n"
            f"• Routing Mode: **1:1 Dedicated Residential IP Per Email**\n"
            f"• Health Check: **Active Pre-Flight Latency & Speed Verification**\n"
            f"• Total Runs: **{len(emails)}**\n\n"
            f"🚀 Booting Autonomous Bot with dynamic IP-to-form binding..."
        )
    else:
        await update.message.reply_text(
            f"⚡ **Direct Network Mode Active:**\n"
            f"• Routing Mode: **Local Network (Zero Proxy)**\n"
            f"• Total Runs: **{len(emails)}**\n\n"
            f"🚀 Booting Autonomous Bot directly on your local connection..."
        )
    
    async def safe_run_bot(idx, e):
        if is_two_stage or use_proxy:
            # 1. Select fast verified proxy with pre-flight health check (<3.5s) & live IP info
            proxy_url, state_name, ip_details = await get_fast_verified_proxy_for_run(combined_context, run_index=idx - 1, target_url=target_url)
            ip_str = ip_details.get("ip", "Connecting...")
            city_str = ip_details.get("city", "Unknown")
            region_str = ip_details.get("region", state_name)
            zip_str = ip_details.get("postal", "Unknown")
            isp_str = ip_details.get("org", "Residential Broadband")
            
            # Create strict 1:1 location binding instruction (Zero Discrepancy)
            is_custom_academic = general_inst and any(k in general_inst.lower() for k in ["bowen", "nursing", "hospital", "synthetic", "academic"])
            if not is_custom_academic:
                country_code = ip_details.get("country", "US")
                country_name = "Australia" if country_code == "AU" or (detected_state and detected_state.lower() == "australia") else "United States"
                is_us = country_name == "United States"
                zip_label = "ZIP Code" if is_us else "Postal Code"
                location_binding = (
                    f"\n[MANDATORY VERIFIED RESIDENTIAL LOCATION - ZERO DISCREPANCY]\n"
                    f"Your session is connecting through verified {country_name} Residential IP: {ip_str}\n"
                    f"• Country: {country_name}\n"
                    f"• State / Region / Province: {region_str}\n"
                    f"• City: {city_str}\n"
                    f"• {zip_label}: {zip_str}\n"
                    f"• ISP: {isp_str}\n\n"
                    f"STRICT RULES FOR THIS RUN:\n"
                    f"1. For ANY question asking for Country, you MUST select or write: {country_name}\n"
                    f"2. For ANY question asking for State, Province, or Region, you MUST select or write: {region_str}\n"
                    f"3. For ANY question asking for City, you MUST write: {city_str}\n"
                    f"4. For ANY question asking for {zip_label}, you MUST write: {zip_str}\n"
                    f"5. Street Address: You MUST invent an authentic, realistic residential street address located inside {city_str}, {region_str} {zip_str}, {country_name}.\n"
                    f"6. DO NOT randomize or invent another City, State, or {zip_label}. The survey researcher verifies your submission against your IP geolocation! Any discrepancy will disqualify the survey.\n"
                )
            else:
                location_binding = ""
            run_tz = ip_details.get("timezone", "Australia/Sydney" if country_name == "Australia" else "America/New_York")
            instance_instructions = base_instructions + location_binding
            bot_proxy_param = f"two_stage:{proxy_url}" if is_two_stage else proxy_url
        else:
            bot_proxy_param = "direct"
            ip_str = "Local Direct Network"
            city_str = "Local"
            region_str = "Local"
            zip_str = "Local"
            isp_str = "Direct Local Connection"
            run_tz = None
            instance_instructions = base_instructions
            
        if e:
            instance_instructions += f"\nCRITICAL PRIORITY: You MUST use the email address: {e}\n"
        if len(emails) > 1:
            instance_instructions += f"\nBATCH ITERATION {idx}: You may vary your Name and Age to appear as a unique respondent, but you MUST strictly maintain the City, State, and ZIP matching the IP above.\n"
            
        async def send_status(msg: str):
            try:
                await update.message.reply_text(msg, parse_mode="Markdown")
            except Exception:
                pass

        try:
            res_data = await run_bot(
                target_url=target_url,
                instructions=instance_instructions,
                proxy=bot_proxy_param,
                run_index=idx,
                status_callback=send_status,
                user_email=e,
                timezone=run_tz
            )
            
            # Extract answers, exit reason, and screenshot
            reward_data = None
            final_url = target_url
            final_page = 1
            if isinstance(res_data, dict) and "summary" in res_data:
                answers = res_data["summary"]
                exit_reason = res_data.get("exit_reason", "")
                screenshot_file = res_data.get("screenshot_path", f"artifacts/final_survey_result_run_{idx}.png")
                reward_data = res_data.get("reward")
                final_url = res_data.get("final_url", target_url)
                final_page = res_data.get("final_page", 1)
            else:
                answers = res_data or {}
                exit_reason = ""
                screenshot_file = f"artifacts/final_survey_result_run_{idx}.png"
                
            has_answers = bool(answers and len(answers) > 0)
            is_truly_completed = False
            exit_lower = exit_reason.lower()
            if any(k in exit_lower for k in ["complete", "thank", "submit", "record", "finish", "done"]):
                is_truly_completed = True
            elif not exit_reason and has_answers:
                is_truly_completed = True
                
            if is_truly_completed:
                status_text = f"Success ({len(answers)} fields filled)"
            elif exit_reason:
                status_text = f"Incomplete ({exit_reason})"
            elif has_answers:
                status_text = f"Partial ({len(answers)} fields filled, not submitted)"
            else:
                status_text = "Incomplete (0 fields found/answered)"
                
            return {
                "email": e or f"Run {idx}",
                "status": status_text,
                "summary": answers,
                "screenshot": screenshot_file,
                "reward": reward_data,
                "ip": ip_str,
                "city": city_str,
                "region": region_str,
                "zip": zip_str,
                "isp": isp_str,
                "final_url": final_url,
                "final_page": final_page
            }
        except Exception as err:
            print(f"❌ Batch run {idx} failed: {err}")
            return {
                "email": e or f"Run {idx}",
                "status": f"Failed: {err}",
                "summary": None,
                "screenshot": f"artifacts/final_survey_result_run_{idx}.png",
                "ip": ip_str,
                "city": city_str,
                "region": region_str,
                "zip": zip_str,
                "isp": isp_str,
                "final_url": target_url,
                "final_page": "Error"
            }

    # Execute all emails sequentially to protect RAM (<400MB) and ensure pristine isolation
    results = []
    for idx, e in enumerate(emails, 1):
        res = await safe_run_bot(idx, e)
        if e and "Success" in res.get("status", ""):
            mark_email_used(e)
        results.append(res)
    
    # Compile Master Summary
    master_summary_text = (
        f"✅ **Batch Job Completed for {len(emails)} run(s)!**\n\n"
        f"📍 **Summary of 1:1 Verified Runs:**\n\n"
    )
    
    for idx, res in enumerate(results, 1):
        master_summary_text += f"==============================\n"
        master_summary_text += f"📧 Run {idx}: {res['email']}\n"
        master_summary_text += f"🌐 Assigned IP: `{res['ip']}`\n"
        master_summary_text += f"📍 Location: {res['city']}, {res['region']} (ZIP: {res['zip']})\n"
        master_summary_text += f"🏢 ISP: {res['isp']}\n"
        master_summary_text += f"📊 Status: {res['status']}\n"
        if res.get('final_url'):
            master_summary_text += f"🔗 Link: {res['final_url']}\n"
        if res.get('final_page'):
            master_summary_text += f"📍 Stage: Page {res['final_page']}\n"
        if res.get('reward'):
            rw = res['reward']
            master_summary_text += f"🎁 Reward: {rw.get('status')} ({rw.get('reward_url')})\n"
        if res['summary']:
            for key, value in res['summary'].items():
                master_summary_text += f"• {key}: {value}\n"
        master_summary_text += f"==============================\n\n"
        
    reply_markup = ReplyKeyboardMarkup([['/start']], resize_keyboard=True, one_time_keyboard=False)
    
    if len(master_summary_text) > 4000:
        with open("batch_summary.txt", "w", encoding="utf-8") as f:
            f.write(master_summary_text)
        await update.message.reply_text(f"✅ Batch Job Completed ({len(results)} runs)!\nThe summary is too long to send as a message, so I've attached it as a file:", reply_markup=reply_markup)
        with open("batch_summary.txt", "rb") as f:
            await update.message.reply_document(f)
    else:
        await update.message.reply_text(master_summary_text, reply_markup=reply_markup)
        
    # Send screenshot proofs to Telegram for each completed run
    for res in results:
        shot_path = res.get("screenshot") or "artifacts/final_survey_result.png"
        if not os.path.exists(shot_path):
            shot_path = "artifacts/final_survey_result.png"
            
        if os.path.exists(shot_path):
            try:
                with open(shot_path, "rb") as photo_file:
                    caption = (
                        f"📸 **Visual Proof for {res['email']}**\n"
                        f"• Status: {res['status']}\n"
                        f"• IP: `{res['ip']}` ({res['city']}, {res['region']})"
                    )
                    await update.message.reply_photo(photo=photo_file, caption=caption)
            except Exception as e:
                print(f"⚠️ Could not send screenshot proof: {e}")

        # Send reward claim screenshot proof if available
        if res.get("reward") and res["reward"].get("proof_path"):
            rw_proof = res["reward"]["proof_path"]
            if os.path.exists(rw_proof):
                try:
                    with open(rw_proof, "rb") as photo_file:
                        caption = (
                            f"🎁 **Reward Claim Proof for {res['email']}**\n"
                            f"• Status: {res['reward'].get('status')}\n"
                            f"• URL: {res['reward'].get('reward_url')}"
                        )
                        await update.message.reply_photo(photo=photo_file, caption=caption)
                except Exception as e:
                    print(f"⚠️ Could not send reward screenshot proof: {e}")
            
    return ConversationHandler.END

# ==============================================================================
# [NEW MODULAR EXTENSIONS - CAN BE COMMENTED OUT SAFELY WITHOUT TOUCHING VOICEFIVE]
# ==============================================================================
import glob
from ad_watcher import run_fb_watcher, load_discovered

ACTIVE_RUN_INFO = {"status": "Idle", "url": None, "started_at": None}

async def checkads_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Checks Facebook Ad Library for active Shareablee ads"""
    await update.message.reply_text("🔍 Scanning Facebook Ad Library for active Shareablee ads... Please wait ~15-20s.")
    try:
        new_ads, all_ads = await run_fb_watcher(headless=True)
        total_count = len(all_ads)
        
        # Pick the 5 newest ads
        display_ads = new_ads[:5] if new_ads else [all_ads[k] for k in list(all_ads.keys())[-5:]]
        
        msg = (
            f"📊 *Active Shareablee Ads* (Total in DB: {total_count})\n"
            f"Showing top 5 freshest campaigns from Facebook:\n\n"
        )
        keyboard = []
        for ad in display_ads:
            ad_id = ad.get("ad_id")
            date_str = ad.get("started_date", "Recently Active")
            ad_url = f"https://www.facebook.com/ads/library/?id={ad_id}"
            
            msg += f"• *ID:* `{ad_id}`\n  📅 *Started:* {date_str}\n  🔗 [Open Ad Page]({ad_url})\n\n"
            keyboard.append([InlineKeyboardButton(f"🌐 View Ad #{ad_id} ({date_str})", url=ad_url)])
            
        msg += "💡 *Tap an ad above to open it on Facebook, click the CTA ('Take Survey' / 'Learn More'), and paste the survey link here to start auto-filling!*"
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard), disable_web_page_preview=True)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error checking Facebook Ad Library: {e}")

async def screenshot_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends the latest live screenshot snapshot of the active survey"""
    screenshots = sorted(glob.glob("artifacts/*.png"), key=os.path.getmtime, reverse=True)
    if screenshots:
        latest = screenshots[0]
        try:
            with open(latest, "rb") as photo:
                caption = (
                    f"📸 *Live Viewport Snapshot*\n"
                    f"• Status: {ACTIVE_RUN_INFO.get('status', 'Running')}\n"
                    f"• File: `{os.path.basename(latest)}`"
                )
                await update.message.reply_photo(photo=photo, caption=caption, parse_mode="Markdown")
                return
        except Exception as e:
            await update.message.reply_text(f"⚠️ Could not load snapshot: {e}")
            return
    await update.message.reply_text("ℹ️ No survey screenshot found yet. Launch a survey with `/start` first.")

async def handle_ad_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Triggered when user clicks 'Auto-Fill Ad #...' from /checkads"""
    query = update.callback_query
    await query.answer()
    data = query.data
    if data.startswith("fill_ad_"):
        ad_id = data.replace("fill_ad_", "")
        all_ads = load_discovered()
        ad_entry = all_ads.get(ad_id, {})
        ad_url = ad_entry.get("url", f"https://www.facebook.com/ads/library/?id={ad_id}")
        context.user_data['url'] = ad_url
        context.user_data['selected_pool_indices'] = set()
        context.user_data['pool_page'] = 0
        pool = load_pool()
        total, fresh = get_pool_summary()
        markup = build_email_picker_keyboard(page=0, selected_indices=set())
        await query.message.reply_text(
            f"🎯 *Selected Shareablee Ad:* `{ad_id}`\n🔗 `{ad_url}`\n\n"
            f"📬 *Email Pool Available* ({total} total, {fresh} fresh):\n"
            f"Select your email(s) below to continue setup:",
            reply_markup=markup,
            parse_mode="Markdown"
        )
        return EMAIL
# ==============================================================================

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global CURRENT_RUN_TASK
    if CURRENT_RUN_TASK and not CURRENT_RUN_TASK.done():
        try:
            CURRENT_RUN_TASK.cancel()
        except Exception:
            pass
        CURRENT_RUN_TASK = None
        reply_markup = ReplyKeyboardMarkup([['/start']], resize_keyboard=True, one_time_keyboard=False)
        await update.message.reply_text("🛑 **SURVEY STOPPED!** The active browser session was terminated immediately.", reply_markup=reply_markup)
        return ConversationHandler.END

    reply_markup = ReplyKeyboardMarkup([['/start']], resize_keyboard=True, one_time_keyboard=False)
    await update.message.reply_text("🛑 Setup cancelled. Tap /start to try again.", reply_markup=reply_markup)
    return ConversationHandler.END

def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token or token == "YOUR_TELEGRAM_TOKEN_HERE":
        print("❌ TELEGRAM_BOT_TOKEN not found in .env file. Please add it!")
        return

    async def post_init(app):
        loop = asyncio.get_running_loop()
        def _silent_win_error(l, ctx):
            exc = ctx.get('exception')
            if isinstance(exc, (ConnectionResetError, ConnectionAbortedError)):
                return
            l.default_exception_handler(ctx)
        loop.set_exception_handler(_silent_win_error)

    application = (
        Application.builder()
        .token(token)
        .post_init(post_init)
        .read_timeout(60)
        .write_timeout(60)
        .connect_timeout(60)
        .pool_timeout(60)
        .build()
    )

    from telegram.ext import CallbackQueryHandler

    application.add_handler(CommandHandler("addemails", add_emails_command))
    application.add_handler(CommandHandler("pool", pool_status_command))

    # --- New Modular Commands (Safe to comment out if needed) ---
    application.add_handler(CommandHandler("checkads", checkads_command))
    application.add_handler(CommandHandler("screenshot", screenshot_command))
    application.add_handler(CommandHandler("stop", cancel))
    # -------------------------------------------------------------

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CallbackQueryHandler(handle_ad_selection, pattern=r"^fill_ad_")
        ],
        states={
            LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_link)],
            EMAIL: [
                CallbackQueryHandler(handle_email_callback),
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_email)
            ],
            INSTRUCTIONS: [MessageHandler((filters.TEXT | filters.PHOTO) & ~filters.COMMAND, get_instructions)],
            PROXY_CHOICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, start_execution)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("stop", cancel),
            MessageHandler(filters.Regex(r"^🛑 Cancel / Reset$"), cancel)
        ],
        allow_reentry=True
    )

    application.add_handler(conv_handler)

    print("🤖 Conversational Telegram Bot is running... Waiting for /start")
    application.run_polling()

if __name__ == "__main__":
    main()
