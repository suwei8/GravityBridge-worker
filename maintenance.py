import os
import json
import requests
import subprocess
import argparse
import time

# Secrets from Environment
AGENTS_JSON_URL = os.getenv("AGENTS_JSON_URL")
SSH_USER = os.getenv("SSH_USERNAME", "sw")
SSH_PASS = os.getenv("SSH_PASSWORD")
GH_TOKEN = os.getenv("GITHUB_TOKEN")
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send_telegram(message):
    if not TG_TOKEN or not TG_CHAT_ID:
        print(f"⚠️ Telegram config missing, skipping msg: {message}")
        return
    
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {"chat_id": TG_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"❌ Failed to send Telegram: {e}")

def get_agents():
    print(f"Fetching agents from {AGENTS_JSON_URL}...")
    try:
        resp = requests.get(AGENTS_JSON_URL)
        resp.raise_for_status()
        data = resp.json()
        return data.get("agents", {})
    except Exception as e:
        msg = f"❌ **GravityBridge Alert**\nFailed to fetch `agents.json`: {e}"
        print(msg)
        send_telegram(msg)
        return {}

def check_deploy(agents):
    latest_ver = get_latest_version()
    print(f"Latest Version: {latest_ver}")
    
    missing_config = []
    
    for name, info in agents.items():
        # Handle both old string format and new object format
        if isinstance(info, str):
            print(f"⚠️ Skipping {name}: Missing ssh_host (Legacy Format)")
            missing_config.append(name)
            continue
        
        ssh_host = info.get("ssh_host")
        if not ssh_host:
            print(f"⚠️ Skipping {name}: Missing ssh_host field")
            missing_config.append(name)
            continue
            
        print(f"checking {name} ({ssh_host})...")
        
        # Check running version
        ret = run_ssh(ssh_host, "pgrep -f gravity-agent")
        
        if ret.returncode == 0:
            print(f"✅ {name}: Service Running")
        else:
            print(f"❌ {name}: Service NOT Running")

    if missing_config:
        msg = f"⚠️ **Configuration Missing**\nThe following agents lack `ssh_host` config:\n`{', '.join(missing_config)}`\nPlease update `agents.json`."
        send_telegram(msg)

def restart_services(agents):
    for name, info in agents.items():
        if isinstance(info, str): continue
        ssh_host = info.get("ssh_host")
        if not ssh_host: continue

        print(f"🔄 Restarting {name}...")
        cmd = "pkill -9 -f gravity-agent; nohup ~/gravity-agent/gravity-agent > ~/gravity-agent/agent.log 2>&1 &"
        ret = run_ssh(ssh_host, cmd)
        
        if ret.returncode == 0:
            print(f"✅ {name}: Restart Triggered")
        else:
            print(f"❌ {name}: Restart Failed: {ret.stderr}")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--action", choices=["check", "restart"], required=True)
    args = parser.parse_args()
    
    agents = get_agents()
    
    if args.action == "check":
        check_deploy(agents)
    elif args.action == "restart":
        restart_services(agents)

if __name__ == "__main__":
    main()
