import discord
from discord.ext import commands
import os
import json
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime, timedelta, timezone
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from google import genai
from linkedin_poster import LinkedInPoster
import asyncio

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
bot = commands.Bot(command_prefix="/", intents=intents)

print("Starting LinCon...")

# ---- GOOGLE CREDS LOAD ----
raw_creds = os.getenv("GOOGLE_CREDS")

if not raw_creds:
    print("GOOGLE_CREDS ENV VAR NOT FOUND")

try:
    creds_dict = json.loads(raw_creds)
    print("Google creds JSON loaded")
except Exception as e:
    print("FAILED TO LOAD GOOGLE CREDS:", e)
    raise e

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file"
]

try:
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    client = gspread.authorize(creds)
    print("Google Sheets authorized")
except Exception as e:
    print("FAILED TO AUTHORIZE GOOGLE SHEETS:", e)
    raise e

try:
    spreadsheet = client.open_by_key("15Wn6cP6Jom_-uIwLGLY_RlvwQZNn17-aS31Xbr5U0qo")
    brain_sheet = spreadsheet.sheet1  # LinCon_Brain
    print("LinCon_Brain sheet opened successfully")
    
    # Get or create LinCon_Content sheet
    try:
        content_sheet = spreadsheet.worksheet("LinCon_Content")
        print("LinCon_Content sheet found")
    except gspread.exceptions.WorksheetNotFound:
        content_sheet = spreadsheet.add_worksheet(
            title="LinCon_Content",
            rows="1000",
            cols="20"
        )
        # Set headers (expanded for Milestone 3)
        content_sheet.update(values=[[
            'Timestamp', 'Post Type', 'Content/Hook', 'Slide 2', 'Slide 3',
            'Slide 4', 'Slide 5', 'Slide 6', 'Slide 7', 'Status', 'Source Rows',
            'State', 'Design Intent', 'Required Assets', 'Asset Links', 
            'Visual Links', 'Scheduled Time', 'Posted Time', 'Posting Status', 'Error Log'
        ]], range_name='A1:T1')
        print("LinCon_Content sheet created")
        
except Exception as e:
    print("FAILED TO OPEN SHEET:", e)
    raise e

# ---- GEMINI SETUP ----
try:
    client_gemini = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    print("Gemini configured")
except Exception as e:
    print("FAILED TO CONFIGURE GEMINI:", e)
    raise e

# ---- GOOGLE DRIVE SETUP ----
try:
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload
    import io
    
    drive_service = build('drive', 'v3', credentials=creds)
    print("Google Drive configured")
except Exception as e:
    print("FAILED TO CONFIGURE GOOGLE DRIVE:", e)
    drive_service = None

# ---- LINKEDIN POSTER SETUP ----
linkedin_poster = None

# ---- SCHEDULER SETUP ----
scheduler = AsyncIOScheduler()

# Your Discord User ID (REPLACE THIS)
MY_USER_ID = "895300631680655420"

# State tracking
current_draft = None
pending_approval = None
pending_asset_request = None
pending_visual_confirmation = None
pending_post_confirmation = None


# ---- STATE DEFINITIONS ----
class PostState:
    IDEA_CAPTURED = "IDEA_CAPTURED"
    CONTENT_READY = "CONTENT_READY"
    ASSETS_REQUIRED = "ASSETS_REQUIRED"
    ASSETS_ATTACHED = "ASSETS_ATTACHED"
    VISUALS_READY = "VISUALS_READY"
    READY_TO_POST = "READY_TO_POST"
    SCHEDULED = "SCHEDULED"
    POSTED = "POSTED"
    FAILED = "FAILED"


async def send_daily_question():
    """Send daily question to user"""
    try:
        user = await bot.fetch_user(int(MY_USER_ID))
        
        daily_question = (
            "🔍 **Daily Check-in**\n\n"
            "What did you work on today?\n\n"
            "Share:\n"
            "• Wins\n"
            "• Failures\n"
            "• Ideas\n"
            "• Anything worth remembering"
        )
        
        await user.send(daily_question)
        print(f"Daily question sent to user {MY_USER_ID}")
    except Exception as e:
        print(f"Failed to send daily question: {e}")


async def classify_memories():
    """Daily background job: classify unprocessed memories using Gemini"""
    try:
        print("Starting memory classification...")
        
        # Get all rows from brain sheet
        all_rows = brain_sheet.get_all_values()
        
        if len(all_rows) <= 1:  # Only headers or empty
            print("No rows to classify")
            return
        
        # Find rows where Memory Type is empty (column D, index 3)
        unprocessed = []
        for idx, row in enumerate(all_rows[1:], start=2):  # Skip header, start at row 2
            # Ensure row has enough columns
            while len(row) < 7:
                row.append('')
            
            # Check if Memory Type (column D) is empty or 'raw'
            if not row[3] or row[3] == 'raw':
                unprocessed.append({
                    'row_num': idx,
                    'timestamp': row[0],
                    'source': row[1],
                    'content': row[2],
                    'current_row': row
                })
        
        if not unprocessed:
            print("No unprocessed memories found")
            return
        
        print(f"Found {len(unprocessed)} unprocessed memories")
        
        # Process each memory with Gemini
        for memory in unprocessed:
            try:
                prompt = f"""Classify this user input into EXACTLY ONE category:

Categories:
- work_log: Specific work tasks, what they built, code they wrote, meetings attended
- insight: Learning, realization, understanding something new
- failure: Mistakes, bugs, things that didn't work, lessons from failure
- idea: Future plans, feature ideas, thoughts to explore
- misc: Everything else

Input: "{memory['content']}"

Rules:
1. Return ONLY the category name (work_log, insight, failure, idea, or misc)
2. Do NOT explain
3. Do NOT rewrite the content
4. Do NOT add emojis
5. Pick the MOST specific category that fits

Also determine:
- Does this input have enough context to understand later? (YES/NO)
- Context is missing if it references "this" "that" "the bug" without explaining what

Response format (STRICT):
CATEGORY: [category]
CONTEXT: [YES or NO]"""

                response = client_gemini.models.generate_content(
                    model='gemini-2.5-flash',
                    contents=prompt
                )
                result = response.text.strip()
                
                # Parse response
                lines = result.split('\n')
                category = None
                context = None
                
                for line in lines:
                    if line.startswith('CATEGORY:'):
                        category = line.split(':', 1)[1].strip().lower()
                    elif line.startswith('CONTEXT:'):
                        context = line.split(':', 1)[1].strip().upper()
                
                # Validate category
                valid_categories = ['work_log', 'insight', 'failure', 'idea', 'misc']
                if category not in valid_categories:
                    category = 'misc'
                
                # Validate context
                if context not in ['YES', 'NO']:
                    context = 'NO'
                
                # Update sheet (new API: values first, then range)
                row_num = memory['row_num']
                brain_sheet.update(values=[[category]], range_name=f'D{row_num}')
                brain_sheet.update(values=[[context]], range_name=f'E{row_num}')
                brain_sheet.update(values=[['NO']], range_name=f'F{row_num}')  # Not used for content yet
                
                print(f"Row {row_num} classified as: {category}, context: {context}")
                
            except Exception as e:
                print(f"Failed to classify row {memory['row_num']}: {e}")
                continue
        
        print("Memory classification complete")
        
    except Exception as e:
        print(f"Failed in classify_memories: {e}")


def generate_design_intent(slides_data):
    """Generate Design-Intent Output (DIO) for carousel"""
    dio = []
    
    for i, slide in enumerate(slides_data, 1):
        # Determine font size based on text length
        text_length = len(slide)
        if text_length < 30:
            font_size = "Very Large"
        elif text_length < 60:
            font_size = "Large"
        else:
            font_size = "Medium"
        
        dio.append(f"""SLIDE {i}
Text: "{slide}"
Font: ExtraBold / {font_size}
Alignment: Center
Background: Dark solid
""")
    
    return "\n".join(dio)


async def analyze_asset_needs(slides_content, memories_text):
    """Use Gemini to intelligently determine if real photos are needed"""
    try:
        full_content = "\n".join(slides_content)
        
        prompt = f"""Analyze if this LinkedIn carousel needs a REAL photo taken by the user.

Carousel slides:
{full_content}

Original work:
{memories_text}

✅ Canva only (NO photo):
- Abstract concepts
- Technical concepts
- General lessons
- Frameworks
- Processes

❌ NEEDS real photo:
- Specific workspace/desk
- Physical products
- Actual dashboard
- Code editor showing bug
- Whiteboard session
- Meeting space
- Tools in use

Be conservative. If abstract, say NO.

Response:
NEEDS_PHOTO: [YES or NO]
REASON: [one sentence]
PHOTO_DESCRIPTION: [if YES: detailed instructions with angle, what to show, lighting]

Good example:
"Take photo of desk at 45-degree angle showing: laptop with terminal error visible, notebook with debugging notes, coffee mug. Window lighting from left. Error message must be readable."

Bad example:
"Take photo of work"

Analyze:"""

        response = client_gemini.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt
        )
        result = response.text.strip()
        
        needs_photo = False
        reason = ""
        photo_description = ""
        
        for line in result.split('\n'):
            line = line.strip()
            if line.startswith('NEEDS_PHOTO:'):
                needs_photo = 'YES' in line.upper()
            elif line.startswith('REASON:'):
                reason = line.split(':', 1)[1].strip()
            elif line.startswith('PHOTO_DESCRIPTION:'):
                photo_description = line.split(':', 1)[1].strip()
        
        return {
            'needs_photo': needs_photo,
            'reason': reason,
            'photo_description': photo_description
        }
        
    except Exception as e:
        print(f"Asset analysis failed: {e}")
        return {
            'needs_photo': False,
            'reason': 'Analysis failed, using Canva only',
            'photo_description': ''
        }


async def upload_to_drive(file_data, filename, mimetype):
    """Upload file to Google Drive"""
    if not drive_service:
        return None
    
    try:
        file_metadata = {'name': filename}
        media = MediaIoBaseUpload(
            io.BytesIO(file_data),
            mimetype=mimetype,
            resumable=True
        )
        
        file = drive_service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id'
        ).execute()
        
        return file.get('id')
    except Exception as e:
        print(f"Drive upload failed: {e}")
        return None


async def download_from_drive(file_id, local_path):
    """Download file from Google Drive"""
    if not drive_service:
        return False
    
    try:
        request = drive_service.files().get_media(fileId=file_id)
        fh = io.FileIO(local_path, 'wb')
        downloader = MediaIoBaseDownload(fh, request)
        
        done = False
        while not done:
            status, done = downloader.next_chunk()
        
        fh.close()
        return True
    except Exception as e:
        print(f"Drive download failed: {e}")
        return False


def get_content_row_by_timestamp(timestamp):
    """Get row number by timestamp"""
    all_rows = content_sheet.get_all_values()
    for idx, row in enumerate(all_rows[1:], start=2):
        if row[0] == timestamp:
            return idx
    return None


def update_content_state(row_num, state, **kwargs):
    """Update content state"""
    try:
        content_sheet.update(values=[[state]], range_name=f'L{row_num}')
        
        if 'design_intent' in kwargs:
            content_sheet.update(values=[[kwargs['design_intent']]], range_name=f'M{row_num}')
        if 'required_assets' in kwargs:
            content_sheet.update(values=[[kwargs['required_assets']]], range_name=f'N{row_num}')
        if 'asset_links' in kwargs:
            content_sheet.update(values=[[kwargs['asset_links']]], range_name=f'O{row_num}')
        if 'visual_links' in kwargs:
            content_sheet.update(values=[[kwargs['visual_links']]], range_name=f'P{row_num}')
        if 'scheduled_time' in kwargs:
            content_sheet.update(values=[[kwargs['scheduled_time']]], range_name=f'Q{row_num}')
        if 'posted_time' in kwargs:
            content_sheet.update(values=[[kwargs['posted_time']]], range_name=f'R{row_num}')
        if 'posting_status' in kwargs:
            content_sheet.update(values=[[kwargs['posting_status']]], range_name=f'S{row_num}')
        if 'error_log' in kwargs:
            content_sheet.update(values=[[kwargs['error_log']]], range_name=f'T{row_num}')
        
        print(f"Updated row {row_num} to: {state}")
    except Exception as e:
        print(f"State update failed: {e}")


async def init_linkedin_poster():
    """Initialize LinkedIn poster"""
    global linkedin_poster
    
    try:
        linkedin_poster = LinkedInPoster()
        await linkedin_poster.init_browser()
        
        is_valid = await linkedin_poster.check_session()
        
        if not is_valid:
            user = await bot.fetch_user(int(MY_USER_ID))
            await user.send(
                "⚠️ **LinkedIn Login Required**\n\n"
                "Use `/linkedin login` when ready."
            )
        else:
            print("LinkedIn session valid")
            
    except Exception as e:
        print(f"LinkedIn init failed: {e}")
        linkedin_poster = None


async def refresh_linkedin_session():
    """Check LinkedIn session"""
    global linkedin_poster
    
    if linkedin_poster and not await linkedin_poster.check_session():
        user = await bot.fetch_user(int(MY_USER_ID))
        await user.send(
            "⚠️ **LinkedIn session expired**\n\n"
            "Use `/linkedin login` to re-authenticate"
        )


# ---- DISCORD EVENTS ----
@bot.event
async def on_ready():
    print(f"LinCon online as {bot.user}")
    
    await init_linkedin_poster()
    
    if not scheduler.running:
        scheduler.add_job(
            send_daily_question,
            CronTrigger(hour=20, minute=0),
            id='daily_question',
            replace_existing=True
        )
        
        scheduler.add_job(
            classify_memories,
            CronTrigger(hour=23, minute=0),
            id='classify_memories',
            replace_existing=True
        )
        
        scheduler.add_job(
            refresh_linkedin_session,
            CronTrigger(hour=6, minute=0),
            id='linkedin_check',
            replace_existing=True
        )
        
        scheduler.start()
        print("Scheduler started")


@bot.event
async def on_message(message):
    global current_draft, pending_approval, pending_asset_request
    global pending_visual_confirmation, pending_post_confirmation
    
    if message.author == bot.user:
        return

    if isinstance(message.channel, discord.DMChannel):
        content_lower = message.content.lower().strip()
        
        if message.content.startswith('/'):
            await bot.process_commands(message)
            return
        
        # Handle CONFIRM/RESCHEDULE/CANCEL
        if pending_post_confirmation and content_lower in ['confirm', 'reschedule', 'cancel']:
            if content_lower == 'confirm':
                row_num = pending_post_confirmation['row_num']
                scheduled_time_str = pending_post_confirmation['scheduled_time']
                content_item = pending_post_confirmation['content']
                
                await message.channel.send("🔄 **Scheduling to LinkedIn...**")
                
                visual_links = content_item.get('visual_links', '').split(',')
                visual_links = [link.strip() for link in visual_links if link.strip()]
                
                if not visual_links:
                    await message.channel.send("❌ No visuals found")
                    pending_post_confirmation = None
                    return
                
                image_paths = []
                for link in visual_links:
                    try:
                        file_id = link.split('/d/')[1].split('/')[0]
                        local_path = f"/tmp/{file_id}.png"
                        
                        if await download_from_drive(file_id, local_path):
                            image_paths.append(local_path)
                    except Exception as e:
                        print(f"File download failed: {e}")
                
                if not image_paths:
                    await message.channel.send("❌ Download failed")
                    pending_post_confirmation = None
                    return
                
                scheduled_time = datetime.fromisoformat(scheduled_time_str)
                result = await linkedin_poster.post_carousel(
                    caption=content_item['content'],
                    image_paths=image_paths,
                    scheduled_time=scheduled_time
                )
                
                if result['success']:
                    update_content_state(
                        row_num,
                        PostState.SCHEDULED,
                        scheduled_time=scheduled_time_str,
                        posting_status="SUCCESS"
                    )
                    
                    await message.channel.send(
                        f"✅ **Scheduled**\n\n"
                        f"Time: {scheduled_time.strftime('%Y-%m-%d %H:%M UTC')}\n"
                        f"Check LinkedIn drafts."
                    )
                else:
                    update_content_state(
                        row_num,
                        PostState.FAILED,
                        posting_status="FAILED",
                        error_log=result['error']
                    )
                    
                    await message.channel.send(
                        f"❌ **Failed**\n\n"
                        f"Error: {result['error']}\n"
                        f"Try `/linkedin login`"
                    )
                
                pending_post_confirmation = None
                
            elif content_lower == 'cancel':
                row_num = pending_post_confirmation['row_num']
                update_content_state(row_num, PostState.FAILED, error_log="Cancelled")
                await message.channel.send("❌ **Cancelled**")
                pending_post_confirmation = None
            
            return
        
        # Handle DONE
        if pending_visual_confirmation and content_lower == 'done':
            if message.attachments:
                asset_links = []
                
                for attachment in message.attachments:
                    file_data = await attachment.read()
                    file_id = await upload_to_drive(
                        file_data,
                        attachment.filename,
                        attachment.content_type or 'image/png'
                    )
                    if file_id:
                        asset_links.append(f"https://drive.google.com/file/d/{file_id}/view")
                
                row_num = pending_visual_confirmation['row_num']
                update_content_state(
                    row_num,
                    PostState.VISUALS_READY,
                    visual_links=', '.join(asset_links)
                )
                
                await message.channel.send(
                    "✅ **Visuals stored**\n\n"
                    "Use `/post preview` to review."
                )
                
                pending_visual_confirmation = None
            else:
                await message.channel.send("⚠️ **No images found**")
            
            return
        
        # Handle SKIP or asset upload
        if pending_asset_request:
            if content_lower == 'skip':
                row_num = pending_asset_request['row_num']
                update_content_state(row_num, PostState.ASSETS_ATTACHED, required_assets="SKIPPED")
                
                await message.channel.send("✅ **Proceeding without assets**")
                
                await create_visuals(message.channel, pending_asset_request)
                pending_asset_request = None
                
            elif message.attachments:
                asset_links = []
                
                for attachment in message.attachments:
                    file_data = await attachment.read()
                    file_id = await upload_to_drive(
                        file_data,
                        attachment.filename,
                        attachment.content_type or 'image/png'
                    )
                    if file_id:
                        asset_links.append(f"https://drive.google.com/file/d/{file_id}/view")
                
                row_num = pending_asset_request['row_num']
                update_content_state(
                    row_num,
                    PostState.ASSETS_ATTACHED,
                    asset_links=', '.join(asset_links)
                )
                
                await message.channel.send(f"✅ **{len(asset_links)} file(s) saved**")
                
                await create_visuals(message.channel, pending_asset_request)
                pending_asset_request = None
            
            return
        
        # Handle approve/revise/reject
        if pending_approval and content_lower in ['approve', 'revise', 'reject']:
            if content_lower == 'approve':
                for row_num in pending_approval['source_rows']:
                    brain_sheet.update(values=[['YES']], range_name=f'F{row_num}')
                
                content_row = [
                    datetime.now(timezone.utc).isoformat(),
                    pending_approval['type'],
                    pending_approval['content'],
                    pending_approval.get('slide_2', ''),
                    pending_approval.get('slide_3', ''),
                    pending_approval.get('slide_4', ''),
                    pending_approval.get('slide_5', ''),
                    pending_approval.get('slide_6', ''),
                    pending_approval.get('slide_7', ''),
                    'APPROVED',
                    ','.join(map(str, pending_approval['source_rows'])),
                    PostState.CONTENT_READY,
                    '', '', '', '', '', '', '', ''
                ]
                content_sheet.append_row(content_row)
                
                await message.channel.send("✅ **Approved**\n\nState: CONTENT_READY")
                
                if pending_approval['type'] == 'carousel':
                    timestamp = content_row[0]
                    await asyncio.sleep(2)
                    
                    row_num = get_content_row_by_timestamp(timestamp)
                    
                    if row_num:
                        slides = [
                            pending_approval['content'],
                            pending_approval.get('slide_2', ''),
                            pending_approval.get('slide_3', ''),
                            pending_approval.get('slide_4', ''),
                            pending_approval.get('slide_5', ''),
                            pending_approval.get('slide_6', ''),
                            pending_approval.get('slide_7', '')
                        ]
                        slides = [s for s in slides if s]
                        
                        dio = generate_design_intent(slides)
                        update_content_state(row_num, PostState.CONTENT_READY, design_intent=dio)
                        
                        memories_list = []
                        for mem_row in pending_approval['source_rows']:
                            row_data = brain_sheet.row_values(mem_row)
                            if len(row_data) > 2:
                                memories_list.append(row_data[2])
                        
                        memories_text = "\n".join(memories_list)
                        
                        asset_analysis = await analyze_asset_needs(slides, memories_text)
                        
                        if asset_analysis['needs_photo']:
                            update_content_state(
                                row_num,
                                PostState.ASSETS_REQUIRED,
                                required_assets=asset_analysis['reason']
                            )
                            
                            await message.channel.send(
                                f"📸 **Real Photo Needed**\n\n"
                                f"**Why:** {asset_analysis['reason']}\n\n"
                                f"**What to photograph:**\n"
                                f"{asset_analysis['photo_description']}\n\n"
                                f"Upload photo or reply SKIP."
                            )
                            
                            pending_asset_request = {
                                'row_num': row_num,
                                'slides': slides,
                                'dio': dio
                            }
                        else:
                            await create_visuals(message.channel, {
                                'row_num': row_num,
                                'slides': slides,
                                'dio': dio
                            })
                
                pending_approval = None
                current_draft = None
                
            elif content_lower == 'reject':
                await message.channel.send("❌ **Rejected**")
                pending_approval = None
                current_draft = None
                
            elif content_lower == 'revise':
                await message.channel.send(
                    "✏️ **Revision mode**\n\n"
                    "Use `/draft text` or `/draft carousel`"
                )
                pending_approval = None
            
            return
        
        # Store as memory
        print("DM received:", message.content)

        try:
            brain_sheet.append_row([
                datetime.now(timezone.utc).isoformat(),
                "Discord DM",
                message.content,
                "", "", "NO", ""
            ])
            print("Row added")
            
            await message.channel.send("✅ Saved")
            
        except Exception as e:
            print(f"FAILED: {e}")
            await message.channel.send("⚠️ Failed")

    await bot.process_commands(message)


async def create_visuals(channel, context):
    """Send Canva instructions"""
    global pending_visual_confirmation
    
    row_num = context['row_num']
    slides = context['slides']
    dio = context['dio']
    
    update_content_state(row_num, PostState.ASSETS_ATTACHED)
    
    await channel.send(
        f"🎨 **Canva Instructions**\n\n"
        f"```\n{dio}\n```\n\n"
        f"1. Open Canva carousel template\n"
        f"2. Apply design intent\n"
        f"3. Export as PNG\n"
        f"4. Upload here\n"
        f"5. Reply DONE"
    )
    
    pending_visual_confirmation = {
        'row_num': row_num,
        'slides': slides
    }


# ---- COMMANDS ----

@bot.command(name='draft')
async def draft(ctx, post_type: str = None):
    """Generate draft"""
    global current_draft, pending_approval
    
    if not isinstance(ctx.channel, discord.DMChannel):
        return
    
    if post_type not in ['text', 'carousel']:
        await ctx.send("Usage: `/draft text` or `/draft carousel`")
        return
    
    await ctx.send(f"🔄 Generating {post_type}...")
    
    try:
        all_rows = brain_sheet.get_all_values()
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        
        eligible_memories = []
        for idx, row in enumerate(all_rows[1:], start=2):
            if len(row) < 7:
                continue
            
            memory_type = row[3].lower() if len(row) > 3 else ''
            used = row[5].upper() if len(row) > 5 else 'NO'
            
            if memory_type in ['insight', 'failure', 'idea'] and used == 'NO':
                try:
                    timestamp = datetime.fromisoformat(row[0])
                    if timestamp >= cutoff:
                        eligible_memories.append({
                            'row_num': idx,
                            'type': memory_type,
                            'content': row[2]
                        })
                except:
                    continue
        
        if not eligible_memories:
            await ctx.send("❌ No unused content from last 7 days")
            return
        
        memories_text = "\n\n".join([
            f"[{m['type'].upper()}] {m['content']}"
            for m in eligible_memories
        ])
        
        if post_type == 'text':
            prompt = f"""Generate ONE LinkedIn text post:

{memories_text}

RULES:
1. NO emojis
2. NO "—" symbols
3. NO guru tone
4. NO motivation clichés
5. Reference specific work
6. 6-10 lines max
7. No CTA
8. Human voice
9. No buzzwords
10. Must be unique to you

Write:"""

            response = client_gemini.models.generate_content(
                model='gemini-2.5-flash',
                contents=prompt
            )
            draft_content = response.text.strip()
            
            current_draft = {
                'type': 'text',
                'content': draft_content,
                'source_rows': [m['row_num'] for m in eligible_memories]
            }
            pending_approval = current_draft
            
            await ctx.send(
                f"📄 **DRAFT**\n\n"
                f"───\n{draft_content}\n───\n\n"
                f"Reply: `approve` / `revise` / `reject`"
            )
        
        elif post_type == 'carousel':
            prompt = f"""Generate carousel (7 slides):

{memories_text}

RULES:
1. One sentence per slide
2. Based on ONE real problem
3. Logical progression
4. NO quotes
5. NO repeated ideas
6. Specific to your experience

Format:
SLIDE 1: [Hook]
SLIDE 2: [What tried]
SLIDE 3: [Why failed]
SLIDE 4: [What learned]
SLIDE 5: [What worked]
SLIDE 6: [Result]
SLIDE 7: [Insight]

Write:"""

            response = client_gemini.models.generate_content(
                model='gemini-2.5-flash',
                contents=prompt
            )
            result = response.text.strip()
            
            slides = {}
            for line in result.split('\n'):
                if line.startswith('SLIDE'):
                    parts = line.split(':', 1)
                    if len(parts) == 2:
                        slide_num = parts[0].strip().replace('SLIDE ', '')
                        slides[f'slide_{slide_num}'] = parts[1].strip()
            
            current_draft = {
                'type': 'carousel',
                'content': slides.get('slide_1', ''),
                'slide_2': slides.get('slide_2', ''),
                'slide_3': slides.get('slide_3', ''),
                'slide_4': slides.get('slide_4', ''),
                'slide_5': slides.get('slide_5', ''),
                'slide_6': slides.get('slide_6', ''),
                'slide_7': slides.get('slide_7', ''),
                'source_rows': [m['row_num'] for m in eligible_memories]
            }
            pending_approval = current_draft
            
            preview = "\n".join([
                f"**Slide {i}:** {slides.get(f'slide_{i}', 'N/A')}"
                for i in range(1, 8)
            ])
            
            await ctx.send(
                f"🎨 **CAROUSEL**\n\n"
                f"───\n{preview}\n───\n\n"
                f"Reply: `approve` / `revise` / `reject`"
            )
    
    except Exception as e:
        print(f"Draft failed: {e}")
        await ctx.send(f"⚠️ Failed: {e}")


@bot.command(name='status')
async def status(ctx):
    """Show status"""
    if not isinstance(ctx.channel, discord.DMChannel):
        return
    
    try:
        all_rows = brain_sheet.get_all_values()
        stats = {
            'total': len(all_rows) - 1,
            'unclassified': 0,
            'work_log': 0,
            'insight': 0,
            'failure': 0,
            'idea': 0,
            'used': 0,
            'unused': 0
        }
        
        for row in all_rows[1:]:
            if len(row) < 7:
                continue
            
            memory_type = row[3].lower() if row[3] else ''
            used = row[5].upper() if len(row) > 5 else 'NO'
            
            if not memory_type or memory_type == 'raw':
                stats['unclassified'] += 1
            else:
                stats[memory_type] = stats.get(memory_type, 0) + 1
            
            if used == 'YES':
                stats['used'] += 1
            
            if memory_type in ['insight', 'failure', 'idea'] and used == 'NO':
                stats['unused'] += 1
        
        content_rows = content_sheet.get_all_values()
        state_counts = {}
        
        for row in content_rows[1:]:
            if len(row) > 11:
                state = row[11]
                state_counts[state] = state_counts.get(state, 0) + 1
        
        state_info = "\n".join([
            f"• {state}: {count}" for state, count in state_counts.items()
        ]) if state_counts else "• None"
        
        linkedin_status = "✅ Connected" if linkedin_poster else "❌ Not initialized"
        
        await ctx.send(
            f"📊 **Status**\n\n"
            f"**Memories:** {stats['total']}\n"
            f"• Insights: {stats['insight']}\n"
            f"• Failures: {stats['failure']}\n"
            f"• Ideas: {stats['idea']}\n"
            f"• Unused: {stats['unused']}\n\n"
            f"**States:**\n{state_info}\n\n"
            f"**LinkedIn:** {linkedin_status}"
        )
        
    except Exception as e:
        await ctx.send(f"⚠️ Failed: {e}")


@bot.command(name='classify')
async def manual_classify(ctx):
    """Classify memories"""
    if not isinstance(ctx.channel, discord.DMChannel):
        return
    
    await ctx.send("🔄 Classifying...")
    await classify_memories()
    await ctx.send("✅ Done")


@bot.command(name='post')
async def post_command(ctx, action: str = None):
    """Post management"""
    global pending_post_confirmation
    
    if not isinstance(ctx.channel, discord.DMChannel):
        return
    
    if action not in ['preview', 'schedule']:
        await ctx.send("Usage: `/post preview` or `/post schedule`")
        return
    
    try:
        all_rows = content_sheet.get_all_values()
        ready_content = []
        
        for idx, row in enumerate(all_rows[1:], start=2):
            if len(row) > 11 and row[11] == PostState.VISUALS_READY:
                ready_content.append({
                    'row_num': idx,
                    'type': row[1],
                    'content': row[2],
                    'visual_links': row[15] if len(row) > 15 else ''
                })
        
        if not ready_content:
            await ctx.send("❌ No content ready")
            return
        
        item = ready_content[0]
        
        if action == 'preview':
            await ctx.send(
                f"📋 **Preview**\n\n"
                f"**Type:** {item['type']}\n"
                f"**Caption:**\n{item['content']}\n\n"
                f"**Visuals:** {item['visual_links'] or 'None'}\n\n"
                f"Use `/post schedule`"
            )
        
        elif action == 'schedule':
            if not linkedin_poster:
                await ctx.send("❌ LinkedIn not initialized\n\nUse `/linkedin login`")
                return
            
            if not item['visual_links']:
                await ctx.send("❌ No visuals")
                return
            
            scheduled_time = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
                hour=14, minute=0, second=0, microsecond=0
            ).isoformat()
            
            pending_post_confirmation = {
                'row_num': item['row_num'],
                'scheduled_time': scheduled_time,
                'content': item
            }
            
            visual_count = len([v for v in item['visual_links'].split(',') if v.strip()])
            
            await ctx.send(
                f"📅 **Final Approval**\n\n"
                f"**Type:** {item['type']}\n"
                f"**Time:** {datetime.fromisoformat(scheduled_time).strftime('%Y-%m-%d %H:%M UTC')}\n"
                f"**Slides:** {visual_count}\n\n"
                f"Reply:\n"
                f"• `CONFIRM`\n"
                f"• `CANCEL`"
            )
    
    except Exception as e:
        await ctx.send(f"⚠️ Failed: {e}")


@bot.command(name='linkedin')
async def linkedin_command(ctx, action: str = None):
    """LinkedIn management"""
    global linkedin_poster
    
    if not isinstance(ctx.channel, discord.DMChannel):
        return
    
    if action == 'login':
        await ctx.send(
            "🔐 **LinkedIn Login**\n\n"
            "Reply: `email@example.com password`\n"
            "(Message deleted after login)"
        )
        
        def check(m):
            return m.author == ctx.author and isinstance(m.channel, discord.DMChannel)
        
        try:
            creds_msg = await bot.wait_for('message', check=check, timeout=300)
            
            parts = creds_msg.content.strip().split(' ', 1)
            if len(parts) != 2:
                await ctx.send("❌ Format: `email password`")
                return
            
            email, password = parts
            await creds_msg.delete()
            
            await ctx.send("🔄 Logging in...")
            
            if not linkedin_poster:
                linkedin_poster = LinkedInPoster()
                await linkedin_poster.init_browser()
            
            await linkedin_poster.login(email, password)
            await ctx.send("✅ **Logged in**")
            
        except asyncio.TimeoutError:
            await ctx.send("⏱️ Timeout")
        except Exception as e:
            await ctx.send(f"❌ Failed: {e}")
    
    elif action == 'status':
        if not linkedin_poster:
            await ctx.send("❌ Not initialized")
            return
        
        is_valid = await linkedin_poster.check_session()
        
        if is_valid:
            await ctx.send("✅ **Session valid**")
        else:
            await ctx.send("❌ **Session expired**\n\nUse `/linkedin login`")
    
    else:
        await ctx.send(
            "Usage:\n"
            "• `/linkedin login`\n"
            "• `/linkedin status`"
        )


bot.run(os.getenv("DISCORD_TOKEN"))
