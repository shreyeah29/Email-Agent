# Gmail OAuth Setup Guide

## Issue: "Access blocked" Error

If you see: **"Access blocked: Email-Agent has not completed the Google verification process"**

This means your email needs to be added as a **Test User** in Google Cloud Console.

## Solution: Add Test User

1. **Go to OAuth Consent Screen:**
   - Visit: https://console.cloud.google.com/apis/credentials/consent
   - Select your project: **Email-Agent**

2. **Add Test User:**
   - Scroll down to **"Test users"** section
   - Click **"+ ADD USERS"**
   - Enter your email: `shreyareddys29@gmail.com`
   - Click **"SAVE"**

3. **Retry OAuth:**
   ```bash
   python3 get_gmail_token.py
   ```

## Quick Links

- **OAuth Consent Screen:** https://console.cloud.google.com/apis/credentials/consent
- **Credentials:** https://console.cloud.google.com/apis/credentials
- **Project:** email-agent-478915

## After Adding Test User

Once you've added yourself as a test user:
1. Run `python3 get_gmail_token.py`
2. Browser will open for OAuth
3. Sign in with `shreyareddys29@gmail.com`
4. Grant permissions
5. Token will be saved to `token.json`

Then the ingestion service will work!
