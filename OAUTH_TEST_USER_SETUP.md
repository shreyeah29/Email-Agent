# OAuth Test User Setup

## Problem
You're seeing: "Access blocked: Email-Agent has not completed the Google verification process"

This happens because your Gmail OAuth app is in **Testing mode** and your email address needs to be added as a test user.

## Solution: Add Test User

### Step 1: Go to Google Cloud Console
1. Visit: https://console.cloud.google.com/apis/credentials
2. Make sure you're in the correct project: **email-agent-479007**

### Step 2: Open OAuth Consent Screen
1. Click on **"OAuth consent screen"** in the left sidebar
   - Or go to: **APIs & Services > OAuth consent screen**

### Step 3: Add Test User
1. Scroll down to the **"Test users"** section
2. Click **"+ ADD USERS"** button
3. Enter your email address: **invoicing24601@gmail.com**
4. Click **"ADD"**
5. Click **"SAVE"** at the bottom of the page

### Step 4: Try Again
1. Go back to your dashboard: http://localhost:8501
2. Click **"Sync Inbox"** again
3. The OAuth flow should now work!

## Your OAuth Client Details
- **Client ID**: 729224522226-scuue33esjetj7b3qpkmjuekpmj036e5.apps.googleusercontent.com
- **Project ID**: email-agent-479007
- **Email to add**: invoicing24601@gmail.com

## Alternative: Publish the App
If you want to allow any Gmail account to use the app (not just test users):
1. Go to OAuth consent screen
2. Click **"PUBLISH APP"** button
3. Confirm the publishing

**Note**: Publishing requires Google verification if you're requesting sensitive scopes. For testing, adding test users is easier.

