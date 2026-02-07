"""
LinkedIn Native Scheduler via Browser Automation
Uses Playwright to control real LinkedIn session
"""

from playwright.async_api import async_playwright
import asyncio
import os
from datetime import datetime

class LinkedInPoster:
    def __init__(self):
        self.browser = None
        self.context = None
        self.page = None
        self.session_file = "linkedin_session.json"
        self.playwright = None
    
    async def init_browser(self):
        """Initialize browser with persistent session"""
        self.playwright = await async_playwright().start()
        
        # Launch browser (headless=False for first setup, True for production)
        self.browser = await self.playwright.chromium.launch(
            headless=True,  # Set to False if you need to see browser
            args=['--disable-blink-features=AutomationControlled']
        )
        
        # Create persistent context to save login session
        self.context = await self.browser.new_context(
            storage_state=self.session_file if os.path.exists(self.session_file) else None,
            viewport={'width': 1920, 'height': 1080},
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        )
        
        self.page = await self.context.new_page()
        print("Browser initialized")
    
    async def login(self, email, password):
        """Login to LinkedIn (only needed first time)"""
        await self.page.goto('https://www.linkedin.com/login')
        await self.page.wait_for_load_state('networkidle')
        
        # Fill login form
        await self.page.fill('input[name="session_key"]', email)
        await self.page.fill('input[name="session_password"]', password)
        await self.page.click('button[type="submit"]')
        
        # Wait for redirect to feed (or 2FA page)
        try:
            await self.page.wait_for_url('**/feed/**', timeout=30000)
        except:
            # Might be on 2FA or verification page
            current_url = self.page.url
            if 'checkpoint' in current_url or 'challenge' in current_url:
                print("2FA or verification required - waiting 60 seconds for manual completion")
                await asyncio.sleep(60)
                
                # Check if we made it to feed
                if 'feed' not in self.page.url:
                    raise Exception("Login incomplete - please check 2FA/verification")
        
        # Save session
        await self.context.storage_state(path=self.session_file)
        print("Logged in and session saved")
    
    async def check_session(self):
        """Check if session is still valid"""
        try:
            await self.page.goto('https://www.linkedin.com/feed/', timeout=15000)
            await self.page.wait_for_load_state('networkidle')
            
            # If redirected to login page, session expired
            if 'login' in self.page.url or 'authwall' in self.page.url:
                return False
            return True
        except Exception as e:
            print(f"Session check failed: {e}")
            return False
    
    async def post_carousel(self, caption, image_paths, scheduled_time=None):
        """
        Post carousel to LinkedIn
        
        Args:
            caption: Post caption text
            image_paths: List of local image file paths
            scheduled_time: datetime object (if None, posts immediately)
        
        Returns:
            dict: {'success': bool, 'post_url': str or None, 'error': str or None}
        """
        try:
            # Go to feed
            await self.page.goto('https://www.linkedin.com/feed/')
            await self.page.wait_for_load_state('networkidle')
            await asyncio.sleep(2)
            
            # Click "Start a post" button
            try:
                await self.page.click('button:has-text("Start a post")', timeout=5000)
            except:
                # Try alternative selector
                await self.page.click('button[aria-label*="Start a post"]', timeout=5000)
            
            await asyncio.sleep(3)
            
            # Wait for post modal
            await self.page.wait_for_selector('div[role="dialog"]', timeout=10000)
            
            # Upload images
            # First, click the image upload button
            try:
                await self.page.click('button[aria-label*="Add a photo"]', timeout=5000)
            except:
                # Try alternative - click any image upload trigger
                await self.page.click('button:has-text("Media")', timeout=5000)
            
            await asyncio.sleep(2)
            
            # Find file input and upload
            file_input = await self.page.wait_for_selector('input[type="file"]', timeout=10000)
            await file_input.set_input_files(image_paths)
            
            # Wait for images to upload and process
            await asyncio.sleep(8)
            
            # Add caption
            # Find the text editor (LinkedIn uses contenteditable div)
            caption_field = await self.page.wait_for_selector('div[contenteditable="true"]', timeout=10000)
            await caption_field.click()
            await asyncio.sleep(1)
            await caption_field.fill(caption)
            await asyncio.sleep(2)
            
            if scheduled_time:
                # Click schedule button
                try:
                    await self.page.click('button:has-text("Schedule")', timeout=5000)
                except:
                    # Try finding clock icon or schedule option
                    await self.page.click('button[aria-label*="Schedule"]', timeout=5000)
                
                await asyncio.sleep(3)
                
                # Set date and time
                # LinkedIn's scheduler UI - this may need adjustment based on their current UI
                try:
                    # Find date input
                    date_input = await self.page.wait_for_selector('input[type="date"]', timeout=5000)
                    await date_input.fill(scheduled_time.strftime('%Y-%m-%d'))
                    
                    # Find time input
                    time_input = await self.page.wait_for_selector('input[type="time"]', timeout=5000)
                    await time_input.fill(scheduled_time.strftime('%H:%M'))
                    
                    await asyncio.sleep(2)
                    
                    # Click "Schedule" button in modal
                    await self.page.click('button:has-text("Schedule")', timeout=5000)
                except Exception as e:
                    print(f"Scheduling UI error: {e}")
                    # If scheduling fails, try to post immediately instead
                    await self.page.click('button:has-text("Post")', timeout=5000)
            else:
                # Click "Post" button for immediate posting
                await self.page.click('button:has-text("Post")', timeout=5000)
            
            await asyncio.sleep(5)
            
            # Get post URL (if available)
            post_url = None
            if not scheduled_time:
                # After posting, LinkedIn may redirect to the post
                post_url = self.page.url if 'feed/update' in self.page.url else None
            
            return {
                'success': True,
                'post_url': post_url,
                'error': None
            }
            
        except Exception as e:
            error_msg = str(e)
            print(f"Failed to post carousel: {error_msg}")
            
            # Take screenshot for debugging
            try:
                await self.page.screenshot(path=f"/tmp/linkedin_error_{datetime.now().timestamp()}.png")
            except:
                pass
            
            return {
                'success': False,
                'post_url': None,
                'error': error_msg
            }
    
    async def close(self):
        """Close browser"""
        if self.context:
            await self.context.close()
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
        print("Browser closed")
