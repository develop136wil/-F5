
from playwright.sync_api import sync_playwright
import time
import os

def check_ui():
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            
            print("Navigating to http://localhost:1420...")
            page.goto("http://localhost:1420")
            
            # Wait for app to load (look for Dashboard or specific text)
            # We expect "Dashboard" or "Welfare" text from Layout
            try:
                page.wait_for_selector("text=Dashboard", timeout=10000)
                print("SUCCESS: Dashboard loaded.")
            except:
                print("WARNING: Dashboard text not found. Extracting page content...")
                
            # Screenshot
            page.screenshot(path="frontend_screenshot.png")
            print("Screenshot saved to frontend_screenshot.png")
            
            # Print content for analysis
            content = page.content()
            if "Welfare Master" in content or "Dashboard" in content:
                print("Content verification passed.")
            else:
                print("Content verification failed. Dumping first 500 chars:")
                print(content[:500])

            browser.close()
            
        except Exception as e:
            print(f"Playwright Error: {e}")

if __name__ == "__main__":
    check_ui()
